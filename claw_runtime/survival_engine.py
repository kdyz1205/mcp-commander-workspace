"""
Survival engine — distilled from claude-tg-bot vital_signs + tracker/quota ideas.

- check_vitals(): host CPU / RAM / disk (psutil if installed, else disk-only).
- check_quota(): persisted OpenAI-style failures + optional soft budget counters.
- hunger_signals(): metaphorical "starvation" flags (billing, soft budget, no key, task streak).
- assess_survival_state(): HEALTHY | DEGRADED | CRITICAL for downstream hooks
  (parasite mode, CURSOR_OUTBOX, TG alerts — see survival_reflex / meta_driving).

State file: <workspace>/.claw/survival_state.json
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class SurvivalState(str, Enum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    CRITICAL = "CRITICAL"


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default


def read_fund_estimate_usd(workspace: Path) -> float | None:
    """
    Optional operating budget signal (not OpenAI billing — that uses insufficient_quota events).
    Set SURVIVAL_FUND_BALANCE_USD or write `.claw/fund_estimate.json` {\"usd\": 12.34}.
    """
    env = os.environ.get("SURVIVAL_FUND_BALANCE_USD", "").strip()
    if env:
        try:
            return float(env)
        except ValueError:
            pass
    p = Path(workspace).resolve() / ".claw" / "fund_estimate.json"
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "usd" in data:
            return float(data["usd"])
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return None


@dataclass
class SurvivalEngine:
    """Minimal meta-cognitive vitals + quota pain for mcp-commander / DevClaw."""

    workspace: Path = field(default_factory=lambda: Path.cwd().resolve())
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.workspace = Path(self.workspace).resolve()
        self._state_path = self.workspace / ".claw" / "survival_state.json"

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.is_file():
            return {"api_events": [], "soft_credits_used": 0, "soft_credits_day": ""}
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {"api_events": [], "soft_credits_used": 0, "soft_credits_day": ""}

    def _save_state(self, data: dict[str, Any]) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError:
            pass

    def record_api_error(self, code: str, message: str = "") -> None:
        """Call from DevClaw/TG on OpenAI errors (e.g. insufficient_quota, rate_limit)."""
        now = time.time()
        with self._lock:
            st = self._load_state()
            ev = st.setdefault("api_events", [])
            ev.append({"ts": now, "code": str(code), "msg": (message or "")[:500]})
            cutoff = now - 86400
            st["api_events"] = [e for e in ev if isinstance(e, dict) and float(e.get("ts", 0)) > cutoff]
            self._save_state(st)

    def record_soft_credit_use(self, units: int = 1) -> None:
        """Optional daily soft budget (SURVIVAL_SOFT_CREDIT_DAILY > 0)."""
        if units <= 0:
            return
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            st = self._load_state()
            if st.get("soft_credits_day") != day:
                st["soft_credits_used"] = 0
                st["soft_credits_day"] = day
            st["soft_credits_used"] = int(st.get("soft_credits_used", 0)) + units
            self._save_state(st)

    def heartbeat(self) -> None:
        with self._lock:
            st = self._load_state()
            st["last_heartbeat_ts"] = time.time()
            self._save_state(st)

    def check_vitals(self) -> dict[str, Any]:
        """CPU / memory / free disk under workspace root."""
        out: dict[str, Any] = {"cpu_percent": None, "mem_percent": None, "disk_free_gb": None}
        try:
            import shutil

            du = shutil.disk_usage(str(self.workspace))
            out["disk_free_gb"] = round(du.free / (1024**3), 2)
        except OSError:
            pass

        try:
            import psutil  # type: ignore[import-untyped]

            out["cpu_percent"] = round(psutil.cpu_percent(interval=0.15), 1)
            out["mem_percent"] = round(psutil.virtual_memory().percent, 1)
        except ImportError:
            pass
        return out

    def check_quota(self) -> dict[str, Any]:
        """Pain signals derived from recent API errors + optional soft credits."""
        now = time.time()
        st = self._load_state()
        events = [e for e in st.get("api_events", []) if isinstance(e, dict)]

        def recent(codes: set[str], window: float) -> int:
            t0 = now - window
            return sum(1 for e in events if e.get("code") in codes and float(e.get("ts", 0)) >= t0)

        insufficient = recent({"insufficient_quota"}, 86400)
        r429 = recent({"rate_limit", "429", "RateLimitError"}, 3600)

        daily = _env_int("SURVIVAL_SOFT_CREDIT_DAILY", 0)
        used = int(st.get("soft_credits_used", 0))
        day = time.strftime("%Y-%m-%d", time.gmtime())
        if st.get("soft_credits_day") != day:
            used = 0
        soft_exhausted = daily > 0 and used >= daily

        return {
            "openai_key_configured": bool(os.environ.get("OPENAI_API_KEY", "").strip()),
            "insufficient_quota_events_24h": insufficient,
            "rate_like_events_1h": r429,
            "soft_credits_daily_limit": daily,
            "soft_credits_used_today": used,
            "soft_budget_exhausted": soft_exhausted,
            "last_heartbeat_ts": st.get("last_heartbeat_ts"),
        }

    def hunger_signals(self) -> dict[str, Any]:
        """
        Physiological metaphor for downstream UX / logs: "starved" when paid API path is unusable
        or task pain is high. Does not move money; triggers are wired in survival_reflex / DevClaw.
        """
        q = self.check_quota()
        fails = self.consecutive_failures()
        thr = max(1, _env_int("SURVIVAL_HUNGER_FAIL_STREAK", 3))
        return {
            "billing_starved": q["insufficient_quota_events_24h"] > 0,
            "soft_budget_starved": bool(q["soft_budget_exhausted"]),
            "no_cloud_key": not q["openai_key_configured"],
            "task_pain_streak": fails,
            "task_pain_elevated": fails >= thr,
        }

    def assess_survival_state(self) -> tuple[SurvivalState, str]:
        """
        CRITICAL: host or API survival threatened — caller should stop paid work / enter parasite mode.
        DEGRADED: throttle non-essential tasks.
        """
        v = self.check_vitals()
        q = self.check_quota()

        mem_thr_crit = _env_float("SURVIVAL_MEM_CRITICAL_PCT", 94.0)
        mem_thr_deg = _env_float("SURVIVAL_MEM_DEGRADED_PCT", 88.0)
        disk_crit_gb = _env_float("SURVIVAL_DISK_CRITICAL_GB", 0.8)
        disk_deg_gb = _env_float("SURVIVAL_DISK_DEGRADED_GB", 2.0)
        r429_crit = _env_int("SURVIVAL_429_CRITICAL_COUNT", 5)
        r429_deg = _env_int("SURVIVAL_429_DEGRADED_COUNT", 2)

        reasons: list[str] = []

        mp = v.get("mem_percent")
        if mp is not None:
            if mp >= mem_thr_crit:
                reasons.append(f"memory {mp}% >= {mem_thr_crit}%")
            elif mp >= mem_thr_deg:
                reasons.append(f"memory {mp}% >= {mem_thr_deg}%")

        dg = v.get("disk_free_gb")
        if dg is not None:
            if dg < disk_crit_gb:
                reasons.append(f"disk_free {dg}GiB < {disk_crit_gb}")
            elif dg < disk_deg_gb:
                reasons.append(f"disk_free {dg}GiB < {disk_deg_gb}")

        if q["insufficient_quota_events_24h"] > 0:
            reasons.append("OpenAI insufficient_quota (billing)")

        if q["rate_like_events_1h"] >= r429_crit:
            reasons.append(f"rate-like errors >= {r429_crit}/1h")
        elif q["rate_like_events_1h"] >= r429_deg:
            reasons.append(f"rate-like errors >= {r429_deg}/1h")

        if q["soft_budget_exhausted"]:
            reasons.append("soft credit daily budget exhausted")

        if not q["openai_key_configured"]:
            reasons.append("OPENAI_API_KEY missing")

        is_critical = (
            q["insufficient_quota_events_24h"] > 0
            or q["soft_budget_exhausted"]
            or not q["openai_key_configured"]
            or (dg is not None and dg < disk_crit_gb)
            or (mp is not None and mp >= mem_thr_crit)
            or q["rate_like_events_1h"] >= r429_crit
        )
        if is_critical:
            return SurvivalState.CRITICAL, "; ".join(reasons) or "critical"

        is_degraded = (
            (dg is not None and dg < disk_deg_gb)
            or (mp is not None and mp >= mem_thr_deg)
            or q["rate_like_events_1h"] >= r429_deg
        )
        if is_degraded:
            return SurvivalState.DEGRADED, "; ".join(reasons) or "degraded"

        return SurvivalState.HEALTHY, "ok"

    def snapshot(self) -> dict[str, Any]:
        """For logs / TG / future router."""
        v = self.check_vitals()
        q = self.check_quota()
        state, reason = self.assess_survival_state()
        return {
            "state": state.value,
            "reason": reason,
            "vitals": v,
            "quota": q,
            "hunger": self.hunger_signals(),
            "consecutive_failures": self.consecutive_failures(),
            "parasite_active": self.parasite_active(),
        }

    # --- Parasite mode (local / free LLM path) ---------------------------------

    def parasite_mode_path(self) -> Path:
        return self.workspace / ".claw" / "parasite_mode.json"

    def parasite_active(self) -> bool:
        if os.environ.get("DEVCLAW_PARASITE_MODE", "").strip().lower() in {"1", "true", "yes"}:
            return True
        p = self.parasite_mode_path()
        if not p.is_file():
            return False
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return bool(data.get("active"))
        except (OSError, json.JSONDecodeError, TypeError):
            return False

    def set_parasite_mode(self, active: bool, reason: str = "") -> None:
        p = self.parasite_mode_path()
        with self._lock:
            try:
                p.parent.mkdir(parents=True, exist_ok=True)
                if not active:
                    if p.exists():
                        p.unlink()
                    return
                data = {"active": True, "reason": (reason or "")[:800], "ts": time.time()}
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                tmp.replace(p)
            except OSError:
                pass

    # --- Task outcomes → jailbreak escalation (aligned with self_repair loops) ---

    def consecutive_failures(self) -> int:
        """Persisted counter for jailbreak escalation (see `record_task_outcome`)."""
        with self._lock:
            st = self._load_state()
            return int(st.get("consecutive_failures", 0))

    def record_task_outcome(self, success: bool) -> None:
        """
        One DevClaw/TG **task run** outcome: success → reset counter to 0; failure → +1.
        Used by `dev_claw_run` `finally` so jailbreak / system prompt escalation tracks real runs.
        """
        with self._lock:
            st = self._load_state()
            if success:
                st["consecutive_failures"] = 0
            else:
                st["consecutive_failures"] = int(st.get("consecutive_failures", 0)) + 1
            self._save_state(st)

    def jailbreak_escalated(self) -> bool:
        thr = _env_int("DEVCLAW_JAILBREAK_FAIL_THRESHOLD", 5)
        return self.consecutive_failures() >= max(1, thr)

    # --- Critical reflex debounce (TG heartbeat) ------------------------------

    def try_acquire_critical_reflex_slot(self, debounce_sec: float) -> bool:
        """Return True if caller should run outbox+TG+parasite (not within debounce)."""
        now = time.time()
        with self._lock:
            st = self._load_state()
            last = float(st.get("last_critical_reflex_ts", 0) or 0)
            if now - last < debounce_sec:
                return False
            st["last_critical_reflex_ts"] = now
            self._save_state(st)
            return True

    def autonomous_fund_check(self) -> str | None:
        """
        If fund estimate is below SURVIVAL_FUND_LOW_USD (default 5), enqueue a persisted task for
        the TG worker / human loop. Does **not** move money or call top-up APIs.

        Returns the task text if enqueued, else None.
        """
        if os.environ.get("SURVIVAL_AUTONOMOUS_FUND_CHECK", "").strip().lower() in {"0", "false", "no"}:
            return None
        threshold = _env_float("SURVIVAL_FUND_LOW_USD", 5.0)
        debounce = _env_float("SURVIVAL_FUND_TASK_DEBOUNCE_SEC", 3600.0)
        usd = read_fund_estimate_usd(self.workspace)
        if usd is None:
            return None
        if usd >= threshold:
            return None
        now = time.time()
        with self._lock:
            st = self._load_state()
            last = float(st.get("last_fund_autonomy_ts", 0) or 0)
            if now - last < debounce:
                return None
            st["last_fund_autonomy_ts"] = now
            self._save_state(st)

        text = (
            "【生存维系 · 队列任务】运营资金估算低于阈值（见 .claw/fund_estimate.json 或 SURVIVAL_FUND_BALANCE_USD）。"
            "请 load_skill trading，在**人工批准**与合规边界内评估策略；仅允许只读/模拟/已授权操作。"
            "严禁自动 OpenAI 代充或无人托管链上出金。若存在套利或续费方案，写入 task_plan.md 推理链并等待老板确认。"
        )
        try:
            from claw_runtime.bot_task_queue import enqueue_persisted_task

            enqueue_persisted_task(
                self.workspace,
                "fund_low_autonomy",
                text,
                {"usd": usd, "threshold_usd": threshold},
            )
        except Exception:
            return None
        return text
