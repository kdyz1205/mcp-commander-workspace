"""
Budget and quota management for DevClaw agent framework.

Controls spending on API calls, evolution, and operations.
Config loaded from `.claw/budget_config.json`; spending tracked
in `.claw/budget_ledger.jsonl` (append-only JSONL).

Integrates with survival_engine.SurvivalState for evolution gating.
"""

from __future__ import annotations

import json
import os
import time
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Cost tables (USD per 1 M tokens) ────────────────────────────────────────

_MODEL_COSTS: dict[str, tuple[float, float]] = {
    # (input_per_1m, output_per_1m)
    "gpt-4o":        (2.50, 10.00),
    "gpt-4o-mini":   (0.15,  0.60),
    "claude-sonnet": (3.00, 15.00),
    "claude-opus":   (15.00, 75.00),
    "ollama":        (0.00,  0.00),
}

_DEFAULT_CONFIG: dict[str, Any] = {
    "monthly_usd_cap": 200,
    "daily_usd_cap": 8,
    "hard_reserve_usd": 40,
    "max_single_task_usd": 5,
    "max_single_evolution_usd": 3,
    "evolution_daily_cap_usd": 6,
    "allow_evolution_when_healthy": True,
    "allow_evolution_when_degraded": False,
    "allow_evolution_when_critical": False,
}


@dataclass
class BudgetStatus:
    daily_spent_usd: float
    daily_remaining_usd: float
    monthly_spent_usd: float
    monthly_remaining_usd: float
    hard_reserve_usd: float
    above_reserve: bool
    evolution_spent_today_usd: float
    evolution_remaining_today_usd: float


class BudgetManager:
    """Gate every paid action through budget checks before execution."""

    def __init__(self, claw_dir: str = ".claw") -> None:
        self._claw_dir = Path(claw_dir).resolve()
        self._config_path = self._claw_dir / "budget_config.json"
        self._ledger_path = self._claw_dir / "budget_ledger.jsonl"
        self._lock = threading.Lock()
        self._config = self._load_config()

    # ── Config ───────────────────────────────────────────────────────────────

    def _load_config(self) -> dict[str, Any]:
        self._claw_dir.mkdir(parents=True, exist_ok=True)
        if self._config_path.is_file():
            try:
                data = json.loads(self._config_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    merged = {**_DEFAULT_CONFIG, **data}
                    return merged
            except (OSError, json.JSONDecodeError, TypeError, ValueError):
                pass
        # write default config
        try:
            tmp = self._config_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(_DEFAULT_CONFIG, indent=2), encoding="utf-8")
            tmp.replace(self._config_path)
        except OSError:
            pass
        return dict(_DEFAULT_CONFIG)

    # ── Ledger I/O ───────────────────────────────────────────────────────────

    def _read_ledger(self) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        if not self._ledger_path.is_file():
            return entries
        try:
            with open(self._ledger_path, "r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict):
                            entries.append(entry)
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            pass
        return entries

    def _append_ledger(self, entry: dict[str, Any]) -> None:
        try:
            self._claw_dir.mkdir(parents=True, exist_ok=True)
            with open(self._ledger_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            pass

    # ── Time helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _today_utc() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    @staticmethod
    def _month_utc() -> str:
        return time.strftime("%Y-%m", time.gmtime())

    def _daily_entries(self, entries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if entries is None:
            entries = self._read_ledger()
        today = self._today_utc()
        out: list[dict[str, Any]] = []
        for e in entries:
            ts = e.get("timestamp")
            if ts is None:
                continue
            day = time.strftime("%Y-%m-%d", time.gmtime(float(ts)))
            if day == today:
                out.append(e)
        return out

    def _monthly_entries(self, entries: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
        if entries is None:
            entries = self._read_ledger()
        month = self._month_utc()
        out: list[dict[str, Any]] = []
        for e in entries:
            ts = e.get("timestamp")
            if ts is None:
                continue
            m = time.strftime("%Y-%m", time.gmtime(float(ts)))
            if m == month:
                out.append(e)
        return out

    @staticmethod
    def _sum_amount(entries: list[dict[str, Any]]) -> float:
        return sum(float(e.get("amount_usd", 0)) for e in entries)

    # ── Public API ───────────────────────────────────────────────────────────

    def status(self) -> BudgetStatus:
        with self._lock:
            entries = self._read_ledger()
            daily = self._daily_entries(entries)
            monthly = self._monthly_entries(entries)

            daily_spent = self._sum_amount(daily)
            monthly_spent = self._sum_amount(monthly)

            daily_cap = float(self._config["daily_usd_cap"])
            monthly_cap = float(self._config["monthly_usd_cap"])
            hard_reserve = float(self._config["hard_reserve_usd"])

            evo_daily = [e for e in daily if e.get("category") == "evolution"]
            evo_spent = self._sum_amount(evo_daily)
            evo_cap = float(self._config["evolution_daily_cap_usd"])

            return BudgetStatus(
                daily_spent_usd=round(daily_spent, 6),
                daily_remaining_usd=round(max(0.0, daily_cap - daily_spent), 6),
                monthly_spent_usd=round(monthly_spent, 6),
                monthly_remaining_usd=round(max(0.0, monthly_cap - monthly_spent), 6),
                hard_reserve_usd=hard_reserve,
                above_reserve=monthly_spent <= (monthly_cap - hard_reserve),
                evolution_spent_today_usd=round(evo_spent, 6),
                evolution_remaining_today_usd=round(max(0.0, evo_cap - evo_spent), 6),
            )

    def can_spend(self, amount_usd: float, category: str = "task") -> tuple[bool, str]:
        with self._lock:
            entries = self._read_ledger()
            daily = self._daily_entries(entries)
            monthly = self._monthly_entries(entries)

            daily_spent = self._sum_amount(daily)
            monthly_spent = self._sum_amount(monthly)

            daily_cap = float(self._config["daily_usd_cap"])
            monthly_cap = float(self._config["monthly_usd_cap"])
            hard_reserve = float(self._config["hard_reserve_usd"])

            # Monthly cap
            if monthly_spent + amount_usd > monthly_cap:
                return False, f"Monthly cap exceeded: spent ${monthly_spent:.2f} + ${amount_usd:.2f} > ${monthly_cap:.2f}"

            # Hard reserve
            if monthly_spent + amount_usd > monthly_cap - hard_reserve:
                return False, f"Would breach hard reserve: spent ${monthly_spent:.2f} + ${amount_usd:.2f} > cap ${monthly_cap:.2f} - reserve ${hard_reserve:.2f}"

            # Daily cap
            if daily_spent + amount_usd > daily_cap:
                return False, f"Daily cap exceeded: spent ${daily_spent:.2f} + ${amount_usd:.2f} > ${daily_cap:.2f}"

            # Single-item caps
            if category == "task":
                max_single = float(self._config["max_single_task_usd"])
                if amount_usd > max_single:
                    return False, f"Single task cost ${amount_usd:.2f} exceeds max ${max_single:.2f}"
            elif category == "evolution":
                max_single = float(self._config["max_single_evolution_usd"])
                if amount_usd > max_single:
                    return False, f"Single evolution cost ${amount_usd:.2f} exceeds max ${max_single:.2f}"
                evo_daily = [e for e in daily if e.get("category") == "evolution"]
                evo_spent = self._sum_amount(evo_daily)
                evo_cap = float(self._config["evolution_daily_cap_usd"])
                if evo_spent + amount_usd > evo_cap:
                    return False, f"Evolution daily cap exceeded: spent ${evo_spent:.2f} + ${amount_usd:.2f} > ${evo_cap:.2f}"

            return True, "ok"

    def record_spend(
        self,
        amount_usd: float,
        category: str,
        task_id: str = "",
        model: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        entry = {
            "timestamp": time.time(),
            "category": category,
            "task_id": task_id,
            "amount_usd": round(amount_usd, 8),
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
        }
        with self._lock:
            self._append_ledger(entry)

    def can_evolve(self, health_state: str = "HEALTHY") -> tuple[bool, str]:
        state_key = f"allow_evolution_when_{health_state.lower()}"
        allowed = self._config.get(state_key)
        if allowed is None:
            return False, f"Unknown health state: {health_state}"
        if not allowed:
            return False, f"Evolution not allowed when {health_state}"

        # Also check evolution daily cap
        with self._lock:
            entries = self._read_ledger()
            daily = self._daily_entries(entries)
            evo_daily = [e for e in daily if e.get("category") == "evolution"]
            evo_spent = self._sum_amount(evo_daily)
            evo_cap = float(self._config["evolution_daily_cap_usd"])
            if evo_spent >= evo_cap:
                return False, f"Evolution daily cap exhausted: spent ${evo_spent:.2f} >= ${evo_cap:.2f}"

        return True, "ok"

    def estimate_cost(self, tokens_in: int, tokens_out: int, model: str = "gpt-4o") -> float:
        costs = _MODEL_COSTS.get(model)
        if costs is None:
            # Fall back to gpt-4o pricing for unknown models
            costs = _MODEL_COSTS["gpt-4o"]
        input_cost = (tokens_in / 1_000_000) * costs[0]
        output_cost = (tokens_out / 1_000_000) * costs[1]
        return round(input_cost + output_cost, 8)

    def get_daily_summary(self) -> dict:
        with self._lock:
            entries = self._read_ledger()
            daily = self._daily_entries(entries)

        by_category: dict[str, float] = {}
        by_model: dict[str, float] = {}
        total_tokens_in = 0
        total_tokens_out = 0

        for e in daily:
            cat = e.get("category", "unknown")
            mod = e.get("model", "unknown")
            amt = float(e.get("amount_usd", 0))
            by_category[cat] = by_category.get(cat, 0.0) + amt
            by_model[mod] = by_model.get(mod, 0.0) + amt
            total_tokens_in += int(e.get("tokens_in", 0))
            total_tokens_out += int(e.get("tokens_out", 0))

        total = self._sum_amount(daily)
        return {
            "date": self._today_utc(),
            "total_usd": round(total, 6),
            "by_category": {k: round(v, 6) for k, v in by_category.items()},
            "by_model": {k: round(v, 6) for k, v in by_model.items()},
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "entry_count": len(daily),
        }

    def get_monthly_summary(self) -> dict:
        with self._lock:
            entries = self._read_ledger()
            monthly = self._monthly_entries(entries)

        by_category: dict[str, float] = {}
        by_model: dict[str, float] = {}
        by_day: dict[str, float] = {}
        total_tokens_in = 0
        total_tokens_out = 0

        for e in monthly:
            cat = e.get("category", "unknown")
            mod = e.get("model", "unknown")
            amt = float(e.get("amount_usd", 0))
            by_category[cat] = by_category.get(cat, 0.0) + amt
            by_model[mod] = by_model.get(mod, 0.0) + amt
            total_tokens_in += int(e.get("tokens_in", 0))
            total_tokens_out += int(e.get("tokens_out", 0))
            ts = e.get("timestamp")
            if ts is not None:
                day = time.strftime("%Y-%m-%d", time.gmtime(float(ts)))
                by_day[day] = by_day.get(day, 0.0) + amt

        total = self._sum_amount(monthly)
        return {
            "month": self._month_utc(),
            "total_usd": round(total, 6),
            "by_category": {k: round(v, 6) for k, v in by_category.items()},
            "by_model": {k: round(v, 6) for k, v in by_model.items()},
            "by_day": {k: round(v, 6) for k, v in sorted(by_day.items())},
            "total_tokens_in": total_tokens_in,
            "total_tokens_out": total_tokens_out,
            "entry_count": len(monthly),
        }

    def reset_daily(self) -> None:
        """Called at midnight to handle any daily reset bookkeeping.

        The ledger itself is append-only and never truncated. This method
        exists as a hook for downstream consumers that cache daily totals.
        """
        # Currently a no-op: daily filtering is done dynamically by timestamp.
        # Subclasses or future code may override to flush caches, emit
        # summary records, or rotate the ledger file.
        pass
