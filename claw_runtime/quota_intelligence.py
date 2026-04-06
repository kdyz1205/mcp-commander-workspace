"""
Quota Intelligence Layer for DevClaw.

Tracks quota/usage across ALL resource providers in a unified way.
Each provider (Ollama, Claude CLI, OpenAI API, Anthropic API, etc.) has
different quota models; this module abstracts them into a single tracking
system with cooldown logic, refresh-pattern learning, and fallback routing.

State is persisted to <claw_dir>/quota_state.json.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Environment knobs
# ---------------------------------------------------------------------------

DEVCLAW_QUOTA_RECHECK_SEC: int = int(
    os.environ.get("DEVCLAW_QUOTA_RECHECK_SEC", "300")
)

# ---------------------------------------------------------------------------
# Cooldown schedule  (consecutive_failures -> wait seconds)
# ---------------------------------------------------------------------------

_COOLDOWN_MAP: dict[int, float] = {
    1: 5,
    2: 30,
    3: 120,
    4: 120,
}
_COOLDOWN_MAX: float = 600  # 5+ failures


def _cooldown_for(consecutive_failures: int) -> float:
    """Return the cooldown duration in seconds for a given failure count."""
    if consecutive_failures <= 0:
        return 0
    if consecutive_failures >= 5:
        return _COOLDOWN_MAX
    return _COOLDOWN_MAP.get(consecutive_failures, _COOLDOWN_MAX)


# ---------------------------------------------------------------------------
# Default provider configurations
# ---------------------------------------------------------------------------

DEFAULT_PROVIDERS: dict[str, dict[str, Any]] = {
    "ollama": {
        "quota_type": "unlimited",
        "refresh_pattern": "none",
        "exhaustion_signals": ["connection refused", "ECONNREFUSED", "timeout"],
        "cooldown_strategy": "wait",
        "fallback_priority": ["claude_cli", "openai_api"],
    },
    "claude_cli": {
        "quota_type": "subscription",
        "refresh_pattern": "rolling_window",
        "exhaustion_signals": ["rate limit", "too many requests", "429", "busy"],
        "cooldown_strategy": "wait",
        "fallback_priority": ["ollama", "openai_api"],
    },
    "openai_api": {
        "quota_type": "token_budget",
        "refresh_pattern": "monthly_reset",
        "exhaustion_signals": ["insufficient_quota", "rate_limit_exceeded", "429"],
        "cooldown_strategy": "fallback",
        "fallback_priority": ["claude_cli", "ollama"],
    },
    "anthropic_api": {
        "quota_type": "token_budget",
        "refresh_pattern": "monthly_reset",
        "exhaustion_signals": ["rate_limit", "overloaded", "529", "429"],
        "cooldown_strategy": "fallback",
        "fallback_priority": ["claude_cli", "ollama"],
    },
}

# ---------------------------------------------------------------------------
# QuotaRecord dataclass
# ---------------------------------------------------------------------------


@dataclass
class QuotaRecord:
    resource_id: str
    quota_type: str  # "token_budget" | "message_window" | "request_limit" | "unlimited" | "subscription"
    remaining_estimate: float | None = None  # None = unknown
    max_capacity: float | None = None
    refresh_pattern: str = "none"  # "rolling_window" | "daily_reset" | "monthly_reset" | "on_demand" | "none"
    refresh_eta_sec: float | None = None  # seconds until next refresh, None if unknown
    last_observed_at: float = 0.0
    exhaustion_signals: list[str] = field(default_factory=list)
    cooldown_strategy: str = "wait"  # "wait" | "fallback" | "degrade" | "stop"
    fallback_priority: list[str] = field(default_factory=list)
    usage_today: float = 0.0  # tokens/requests/messages used today
    usage_this_month: float = 0.0
    last_failure_at: float | None = None
    consecutive_failures: int = 0

    # ---- internal bookkeeping (not part of the public spec but persisted) --
    _failure_timestamps: list[float] = field(default_factory=list)
    _recovery_timestamps: list[float] = field(default_factory=list)
    _learned_refresh_sec: float | None = None
    _usage_day_stamp: str = ""   # ISO date string for rolling daily usage
    _usage_month_stamp: str = ""  # "YYYY-MM" for rolling monthly usage

    # -- serialisation helpers ----------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "QuotaRecord":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known}
        return cls(**filtered)


# ---------------------------------------------------------------------------
# QuotaIntelligence
# ---------------------------------------------------------------------------


class QuotaIntelligence:
    """Unified quota tracker across all DevClaw resource providers."""

    def __init__(self, claw_dir: str = ".claw") -> None:
        self._claw_dir = Path(claw_dir)
        self._state_path = self._claw_dir / "quota_state.json"
        self._records: dict[str, QuotaRecord] = {}
        self._last_recheck: float = 0.0

        # Bootstrap: load persisted state, then ensure defaults are present.
        self.load()
        self._ensure_defaults()

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_provider(
        self,
        resource_id: str,
        quota_type: str,
        max_capacity: float | None = None,
        refresh_pattern: str = "none",
        exhaustion_signals: list[str] | None = None,
        fallback_priority: list[str] | None = None,
    ) -> None:
        """Register (or re-register) a provider with quota metadata."""
        now = time.time()
        existing = self._records.get(resource_id)
        if existing is not None:
            # Merge: update config fields but keep runtime counters.
            existing.quota_type = quota_type
            existing.max_capacity = max_capacity
            existing.refresh_pattern = refresh_pattern
            existing.exhaustion_signals = exhaustion_signals or []
            existing.fallback_priority = fallback_priority or []
            existing.last_observed_at = now
        else:
            self._records[resource_id] = QuotaRecord(
                resource_id=resource_id,
                quota_type=quota_type,
                max_capacity=max_capacity,
                refresh_pattern=refresh_pattern,
                exhaustion_signals=exhaustion_signals or [],
                fallback_priority=fallback_priority or [],
                last_observed_at=now,
            )
        self.save()

    # ------------------------------------------------------------------
    # Usage tracking
    # ------------------------------------------------------------------

    def record_usage(
        self, resource_id: str, amount: float = 1, success: bool = True
    ) -> None:
        """Record a successful (or unsuccessful) usage event."""
        rec = self._records.get(resource_id)
        if rec is None:
            return
        now = time.time()
        rec.last_observed_at = now
        self._roll_usage_windows(rec)
        rec.usage_today += amount
        rec.usage_this_month += amount
        if rec.max_capacity is not None and rec.remaining_estimate is not None:
            rec.remaining_estimate = max(0.0, rec.remaining_estimate - amount)

        if success:
            # A success after failures means recovery.
            if rec.consecutive_failures > 0:
                rec._recovery_timestamps.append(now)
                rec.consecutive_failures = 0
                rec.last_failure_at = None
        self.save()

    def record_failure(self, resource_id: str, error_msg: str) -> None:
        """Record a failure event and update cooldown state."""
        rec = self._records.get(resource_id)
        if rec is None:
            return
        now = time.time()
        rec.last_observed_at = now
        rec.last_failure_at = now
        rec.consecutive_failures += 1
        rec._failure_timestamps.append(now)
        # Trim old timestamps (keep last 50).
        rec._failure_timestamps = rec._failure_timestamps[-50:]

        if self.detect_exhaustion(resource_id, error_msg):
            # Mark remaining as zero when we detect quota exhaustion.
            rec.remaining_estimate = 0.0
        self.save()

    def record_refresh(self, resource_id: str) -> None:
        """Called when quota is observed to have refreshed."""
        rec = self._records.get(resource_id)
        if rec is None:
            return
        now = time.time()
        rec._recovery_timestamps.append(now)
        rec.consecutive_failures = 0
        rec.last_failure_at = None
        if rec.max_capacity is not None:
            rec.remaining_estimate = rec.max_capacity
        else:
            rec.remaining_estimate = None  # back to "unknown but available"
        rec.last_observed_at = now
        self.learn_refresh_pattern(resource_id)
        self.save()

    # ------------------------------------------------------------------
    # Query
    # ------------------------------------------------------------------

    def get_quota(self, resource_id: str) -> QuotaRecord | None:
        return self._records.get(resource_id)

    def get_all_quotas(self) -> dict[str, QuotaRecord]:
        return dict(self._records)

    def is_available(self, resource_id: str) -> bool:
        """True if the provider has remaining quota and is not cooling down."""
        rec = self._records.get(resource_id)
        if rec is None:
            return False
        if self.is_cooling_down(resource_id):
            return False
        if rec.remaining_estimate is not None and rec.remaining_estimate <= 0:
            return False
        # quota_type "unlimited" is always available unless cooling down.
        return True

    def is_cooling_down(self, resource_id: str) -> bool:
        """True if the provider is in a cooldown period after failures."""
        rec = self._records.get(resource_id)
        if rec is None:
            return False
        if rec.consecutive_failures <= 0 or rec.last_failure_at is None:
            return False
        cooldown_sec = _cooldown_for(rec.consecutive_failures)
        elapsed = time.time() - rec.last_failure_at
        return elapsed < cooldown_sec

    def get_best_available(self, candidates: list[str]) -> str | None:
        """Pick the best available provider from *candidates*.

        Ranking heuristic (descending priority):
        1. Available (not cooling down, has quota)
        2. Fewest consecutive failures
        3. Most remaining quota (if known)
        4. Least usage today (tie-break)
        """
        scored: list[tuple[float, str]] = []
        for rid in candidates:
            rec = self._records.get(rid)
            if rec is None:
                continue
            if not self.is_available(rid):
                continue
            # Lower score = better.
            remaining = rec.remaining_estimate if rec.remaining_estimate is not None else float("inf")
            score = (
                rec.consecutive_failures * 1_000_000
                - remaining
                + rec.usage_today * 0.001
            )
            scored.append((score, rid))
        if not scored:
            return None
        scored.sort(key=lambda t: t[0])
        return scored[0][1]

    def estimate_refresh_eta(self, resource_id: str) -> float | None:
        """Estimate seconds until the provider is available again.

        Returns None if unknown.
        """
        rec = self._records.get(resource_id)
        if rec is None:
            return None

        # If currently cooling down, return remaining cooldown time.
        if rec.consecutive_failures > 0 and rec.last_failure_at is not None:
            cooldown_sec = _cooldown_for(rec.consecutive_failures)
            elapsed = time.time() - rec.last_failure_at
            remaining = cooldown_sec - elapsed
            if remaining > 0:
                return remaining

        # If we have a learned refresh interval, use it.
        if rec._learned_refresh_sec is not None and rec.last_failure_at is not None:
            eta = rec._learned_refresh_sec - (time.time() - rec.last_failure_at)
            return max(0.0, eta)

        # If we have a static refresh_eta_sec set on the record.
        if rec.refresh_eta_sec is not None:
            return rec.refresh_eta_sec

        return None

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------

    def learn_refresh_pattern(self, resource_id: str) -> None:
        """Analyze failure/recovery timestamps to learn refresh cadence.

        If a provider fails at time T1 and succeeds at time T2, the refresh
        window is approximately T2 - T1.  We take the median of observed
        intervals for robustness.
        """
        rec = self._records.get(resource_id)
        if rec is None:
            return
        failures = rec._failure_timestamps
        recoveries = rec._recovery_timestamps
        if not failures or not recoveries:
            return

        intervals: list[float] = []
        for t_recover in recoveries:
            # Find the most recent failure before this recovery.
            preceding = [tf for tf in failures if tf < t_recover]
            if preceding:
                intervals.append(t_recover - preceding[-1])

        if not intervals:
            return
        intervals.sort()
        median = intervals[len(intervals) // 2]
        rec._learned_refresh_sec = median
        rec.refresh_eta_sec = median

    def detect_exhaustion(self, resource_id: str, error_msg: str) -> bool:
        """Return True if *error_msg* matches any known exhaustion signal."""
        rec = self._records.get(resource_id)
        if rec is None:
            return False
        error_lower = error_msg.lower()
        for signal in rec.exhaustion_signals:
            if signal.lower() in error_lower:
                return True
        return False

    # ------------------------------------------------------------------
    # Summary / reporting
    # ------------------------------------------------------------------

    def export_summary(self) -> dict[str, Any]:
        """Return a JSON-serialisable dict suitable for LLM context injection."""
        now = time.time()
        summary: dict[str, Any] = {
            "generated_at": now,
            "recheck_interval_sec": DEVCLAW_QUOTA_RECHECK_SEC,
            "providers": {},
        }
        for rid, rec in self._records.items():
            self._roll_usage_windows(rec)
            summary["providers"][rid] = {
                "quota_type": rec.quota_type,
                "available": self.is_available(rid),
                "cooling_down": self.is_cooling_down(rid),
                "remaining_estimate": rec.remaining_estimate,
                "max_capacity": rec.max_capacity,
                "usage_today": rec.usage_today,
                "usage_this_month": rec.usage_this_month,
                "consecutive_failures": rec.consecutive_failures,
                "cooldown_strategy": rec.cooldown_strategy,
                "fallback_priority": rec.fallback_priority,
                "refresh_pattern": rec.refresh_pattern,
                "learned_refresh_sec": rec._learned_refresh_sec,
                "refresh_eta_sec": self.estimate_refresh_eta(rid),
            }
        return summary

    def daily_report(self) -> dict[str, Any]:
        """Return a daily usage report across all providers."""
        now = time.time()
        report: dict[str, Any] = {
            "date": time.strftime("%Y-%m-%d", time.localtime(now)),
            "generated_at": now,
            "providers": {},
            "total_usage_today": 0.0,
            "total_failures_today": 0,
            "unavailable_providers": [],
        }
        for rid, rec in self._records.items():
            self._roll_usage_windows(rec)
            failures_today = sum(
                1
                for ts in rec._failure_timestamps
                if ts >= _start_of_day(now)
            )
            report["providers"][rid] = {
                "usage_today": rec.usage_today,
                "usage_this_month": rec.usage_this_month,
                "failures_today": failures_today,
                "consecutive_failures": rec.consecutive_failures,
                "available": self.is_available(rid),
            }
            report["total_usage_today"] += rec.usage_today
            report["total_failures_today"] += failures_today
            if not self.is_available(rid):
                report["unavailable_providers"].append(rid)
        return report

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persist all quota records to disk."""
        self._claw_dir.mkdir(parents=True, exist_ok=True)
        data = {rid: rec.to_dict() for rid, rec in self._records.items()}
        tmp_path = self._state_path.with_suffix(".tmp")
        try:
            tmp_path.write_text(json.dumps(data, indent=2, default=str))
            tmp_path.replace(self._state_path)
        except OSError:
            # Best-effort; don't crash the runtime if disk write fails.
            pass

    def load(self) -> None:
        """Load quota records from disk (if present)."""
        if not self._state_path.exists():
            return
        try:
            raw = json.loads(self._state_path.read_text())
            for rid, d in raw.items():
                self._records[rid] = QuotaRecord.from_dict(d)
        except (json.JSONDecodeError, OSError, TypeError):
            # Corrupted state: start fresh.
            self._records = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_defaults(self) -> None:
        """Pre-register default providers if they are not already tracked."""
        for rid, cfg in DEFAULT_PROVIDERS.items():
            if rid not in self._records:
                self._records[rid] = QuotaRecord(
                    resource_id=rid,
                    quota_type=cfg["quota_type"],
                    refresh_pattern=cfg["refresh_pattern"],
                    exhaustion_signals=list(cfg["exhaustion_signals"]),
                    cooldown_strategy=cfg["cooldown_strategy"],
                    fallback_priority=list(cfg["fallback_priority"]),
                    last_observed_at=time.time(),
                )

    @staticmethod
    def _roll_usage_windows(rec: QuotaRecord) -> None:
        """Reset daily/monthly counters when the calendar window rolls over."""
        now = time.time()
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        month = time.strftime("%Y-%m", time.localtime(now))

        if rec._usage_day_stamp != today:
            rec.usage_today = 0.0
            rec._usage_day_stamp = today
        if rec._usage_month_stamp != month:
            rec.usage_this_month = 0.0
            rec._usage_month_stamp = month


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _start_of_day(ts: float) -> float:
    """Return the epoch timestamp for the start of the day containing *ts*."""
    lt = time.localtime(ts)
    return time.mktime(time.strptime(time.strftime("%Y-%m-%d", lt), "%Y-%m-%d"))
