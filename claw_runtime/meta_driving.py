"""
Meta-driving: periodic autonomous_tick() without user instruction.

Connects SurvivalEngine, survival_reflex, ultimate (colab/proxy/treasury), evolution.
Does NOT: Selenium Google login, auto chain txs, third-party top-up APIs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from claw_runtime.memory import append_memory
from claw_runtime.survival_engine import SurvivalEngine, SurvivalState


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


def _append_tick_log(workspace: Path, payload: dict[str, Any]) -> None:
    log = workspace / ".claw" / "meta_tick_log.jsonl"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def autonomous_tick(workspace: Path | str) -> dict[str, Any]:
    """
    One meta-driving cycle: introspect survival → reflex / colab bundle / proxy / treasury observe / evolve.

    Returns a dict with state, reason, actions (for logs or TG forwarding).
    """
    ws = Path(workspace).resolve()
    actions: list[dict[str, Any]] = []
    eng = SurvivalEngine(ws)
    eng.heartbeat()
    fund_note = eng.autonomous_fund_check()
    if fund_note:
        actions.append({"name": "fund_low_persisted_task", "detail": fund_note[:240]})

    state, reason = eng.assess_survival_state()
    q = eng.check_quota()
    snap = eng.snapshot()

    debounce = float(os.environ.get("META_REFLEX_DEBOUNCE_SEC", "120") or "120")

    # 1) CRITICAL → same reflex as TG (shared debounce)
    if state == SurvivalState.CRITICAL:
        try:
            from claw_runtime.survival_reflex import run_critical_reflex

            if run_critical_reflex(ws, eng, debounce_sec=debounce, notify=None):
                actions.append({"name": "critical_reflex", "detail": "CURSOR_OUTBOX + parasite + treasury stub"})
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "critical_reflex_error", "detail": str(e)[:500]})

    # 2) DEGRADED host → optional Colab job bundle (no Selenium; user uploads zip)
    if state == SurvivalState.DEGRADED and _env_bool("META_COLAB_ON_DEGRADED", default=False):
        paths = [p.strip() for p in os.environ.get("META_COLAB_PATHS", "task_plan.md").split(",") if p.strip()]
        try:
            from claw_runtime.ultimate.colab_bundle import build_colab_job_bundle

            z = build_colab_job_bundle(
                ws,
                relative_paths=paths,
                instruction=os.environ.get("META_COLAB_INSTRUCTION", "Host DEGRADED — offload heavy step to Colab (manual upload)."),
            )
            actions.append({"name": "colab_bundle", "detail": str(z)})
            append_memory(
                ws,
                "lesson",
                f"[meta_tick] Built Colab bundle at {z} because host DEGRADED: {reason}",
            )
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "colab_bundle_error", "detail": str(e)[:500]})

    # 2b) 推理闭环占位：DEGRADED 或连续失败 → task_plan.md 追加假设-验证段（debounced）
    if _env_bool("META_REASONING_EPISODE", default=False):
        try:
            from claw_runtime.reasoning_episode import maybe_auto_reasoning_stub

            rp = maybe_auto_reasoning_stub(ws, state_value=state.value, reason=reason, snapshot=snap)
            if rp:
                actions.append({"name": "reasoning_episode", "detail": str(rp)})
                append_memory(
                    ws,
                    "lesson",
                    f"[meta_tick] Appended reasoning episode to task_plan.md ({state.value}, fails={snap.get('consecutive_failures', 0)}).",
                )
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "reasoning_episode_error", "detail": str(e)[:500]})

    # 3) Network pain → rotate proxy from user list (optional VPN_SWITCH_CMD)
    if int(q.get("rate_like_events_1h", 0) or 0) >= 1 and os.environ.get("PROXY_LIST_FILE", "").strip():
        if _env_bool("META_PROXY_ROTATE_ON_NET_PAIN", default=True):
            try:
                from claw_runtime.ultimate.proxy_env import rotate_proxy_index

                r = rotate_proxy_index(ws)
                actions.append({"name": "proxy_rotate", "detail": r})
            except Exception as e:  # noqa: BLE001
                actions.append({"name": "proxy_rotate_error", "detail": str(e)[:500]})

    # 4) Billing pain → treasury proposal + read-only funding observation (no trade / no top-up)
    if int(q.get("insufficient_quota_events_24h", 0) or 0) > 0:
        try:
            from claw_runtime.ultimate.treasury import write_treasury_proposal_stub

            write_treasury_proposal_stub(
                ws,
                reason="insufficient_quota",
                suggested_actions=[
                    "Fix OpenAI billing or use parasite / Ollama",
                    "Human-reviewed trading / treasury only",
                    "Never auto-call third-party top-up APIs from this tick",
                ],
            )
            actions.append({"name": "treasury_proposal", "detail": ".claw/treasury_proposal.json"})
        except Exception as e:  # noqa: BLE001
            actions.append({"name": "treasury_proposal_error", "detail": str(e)[:500]})
        qlesson = ws / ".claw" / "meta_last_quota_lesson_ts"
        now = time.time()
        try:
            lastq = float(qlesson.read_text(encoding="utf-8").strip()) if qlesson.is_file() else 0.0
        except ValueError:
            lastq = 0.0
        if now - lastq >= float(os.environ.get("META_QUOTA_LESSON_MIN_SEC", "3600") or "3600"):
            append_memory(ws, "lesson", f"[meta_tick] insufficient_quota signal; wrote treasury proposal. {reason}")
            try:
                qlesson.write_text(str(now), encoding="utf-8")
            except OSError:
                pass

        if _env_bool("META_OBSERVE_FUNDING", default=True):
            script = ws / "tools" / "trading_funding_binance.py"
            if script.is_file():
                try:
                    sym = os.environ.get("META_FUNDING_SYMBOL", "BTCUSDT").strip() or "BTCUSDT"
                    cp = subprocess.run(
                        [sys.executable, str(script), sym],
                        cwd=str(ws),
                        capture_output=True,
                        text=True,
                        timeout=45,
                    )
                    actions.append(
                        {
                            "name": "funding_observe",
                            "detail": (cp.stdout or cp.stderr or "")[:1500],
                            "returncode": cp.returncode,
                        }
                    )
                except Exception as e:  # noqa: BLE001
                    actions.append({"name": "funding_observe_error", "detail": str(e)[:500]})

    # 5) Optional: materialize evolve draft (debounced)
    if _env_bool("META_EVOLVE_ON_TICK", default=False):
        last_path = ws / ".claw" / "meta_last_evolve_ts"
        now = time.time()
        min_gap = float(os.environ.get("META_EVOLVE_MIN_SEC", "3600") or "3600")
        try:
            last = float(last_path.read_text(encoding="utf-8").strip()) if last_path.is_file() else 0.0
        except ValueError:
            last = 0.0
        if now - last >= min_gap:
            ev = ws / ".claw" / "evolution_failures.jsonl"
            if ev.is_file() and ev.stat().st_size > 0:
                try:
                    from claw_runtime.nightly_evolution import materialize_draft_skill

                    out = materialize_draft_skill(ws)
                    if out:
                        actions.append({"name": "evolve_draft", "detail": str(out)})
                        last_path.write_text(str(now), encoding="utf-8")
                except Exception as e:  # noqa: BLE001
                    actions.append({"name": "evolve_draft_error", "detail": str(e)[:500]})

    out: dict[str, Any] = {
        "ts": time.time(),
        "state": state.value,
        "reason": reason,
        "snapshot": snap,
        "actions": actions,
    }
    _append_tick_log(ws, out)
    return out


def run_autonomous_loop(workspace: Path | str, interval_sec: float) -> None:
    """Blocking loop for `py dev_claw/main.py --autonomous`."""
    ws = Path(workspace).resolve()
    interval_sec = max(15.0, float(interval_sec))
    print(f"[meta-driving] workspace={ws} interval={interval_sec}s (Ctrl+C to stop)")
    while True:
        try:
            summary = autonomous_tick(ws)
            print(json.dumps({k: summary[k] for k in ("state", "reason", "actions")}, ensure_ascii=False, indent=2))
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            print(f"[meta-driving] tick error: {e!s}")
        time.sleep(interval_sec)
