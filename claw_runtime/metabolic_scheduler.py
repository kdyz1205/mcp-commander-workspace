"""
Energy-based compute optimisation — every action has a cost; the scheduler
maximises value per token spent.

Reads budget state from quota_tracker, survival_state, and system vitals.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class ComputeBudget:
    """Snapshot of available compute resources and spend."""

    total_tokens_available: int = 1_000_000
    tokens_used_today: int = 0
    api_cost_per_1k_tokens: float = 0.002
    memory_pressure_pct: float = 0.0
    cpu_pressure_pct: float = 0.0
    network_latency_ms: float = 0.0


# ---------------------------------------------------------------------------
# Task cost estimation
# ---------------------------------------------------------------------------

# Heuristic multipliers keyed on task_type
_COST_MULTIPLIERS: dict[str, float] = {
    "code_generation": 2.5,
    "code_review": 1.5,
    "refactor": 2.0,
    "test_writing": 1.8,
    "debugging": 2.2,
    "research": 3.0,
    "api_call": 0.5,
    "file_edit": 0.8,
    "chat": 1.0,
    "planning": 1.2,
    "documentation": 1.4,
    "analysis": 2.0,
}


def estimate_task_cost(instruction: str, task_type: str = "chat") -> dict[str, Any]:
    """Heuristic token estimation based on instruction length and type.

    Args:
        instruction: The task instruction text.
        task_type: Category of the task (see ``_COST_MULTIPLIERS``).

    Returns:
        Dict with ``estimated_tokens``, ``estimated_cost_usd``,
        ``estimated_time_sec``.
    """
    # Base estimate: ~1.3 tokens per character of instruction (prompt + response)
    base_tokens = max(100, int(len(instruction) * 1.3))
    multiplier = _COST_MULTIPLIERS.get(task_type, 1.0)

    # Response tokens are typically 3-5x the prompt for generation tasks
    if task_type in ("code_generation", "research", "test_writing"):
        response_factor = 4.0
    elif task_type in ("refactor", "debugging", "analysis"):
        response_factor = 3.0
    else:
        response_factor = 2.0

    estimated_tokens = int(base_tokens * multiplier * response_factor)
    estimated_cost = round(estimated_tokens / 1000 * 0.002, 6)
    # Rough time: ~50 tokens/sec generation speed
    estimated_time = round(estimated_tokens / 50, 1)

    return {
        "estimated_tokens": estimated_tokens,
        "estimated_cost_usd": estimated_cost,
        "estimated_time_sec": estimated_time,
    }


# ---------------------------------------------------------------------------
# Task value estimation
# ---------------------------------------------------------------------------

_URGENCY_KEYWORDS: dict[str, float] = {
    "critical": 30.0,
    "urgent": 25.0,
    "emergency": 30.0,
    "asap": 20.0,
    "immediately": 20.0,
    "紧急": 25.0,
    "危急": 30.0,
    "blocking": 20.0,
    "p0": 30.0,
    "hotfix": 25.0,
}

_REVENUE_KEYWORDS: dict[str, float] = {
    "trading": 20.0,
    "funding": 18.0,
    "revenue": 18.0,
    "profit": 15.0,
    "monetize": 15.0,
    "payment": 15.0,
    "billing": 12.0,
    "收益": 15.0,
    "交易": 18.0,
    "盈利": 15.0,
}

_SURVIVAL_KEYWORDS: dict[str, float] = {
    "self-heal": 22.0,
    "backup": 18.0,
    "recovery": 20.0,
    "restore": 18.0,
    "survival": 22.0,
    "heartbeat": 15.0,
    "health_check": 12.0,
    "failover": 20.0,
    "自愈": 22.0,
    "备份": 18.0,
    "存活": 22.0,
}

_ROUTINE_KEYWORDS: dict[str, float] = {
    "cleanup": -10.0,
    "lint": -8.0,
    "format": -8.0,
    "tidy": -5.0,
    "routine": -10.0,
    "chore": -8.0,
    "日常": -8.0,
    "清理": -8.0,
}


def estimate_task_value(instruction: str, workspace: Path | None = None) -> float:
    """Score a task's value from 0-100.

    Higher scores go to urgent, revenue-relevant, or survival-critical tasks.
    Routine maintenance scores lower.

    Args:
        instruction: The task instruction text.
        workspace: Optional workspace path (reserved for future context).

    Returns:
        Value score in range [0, 100].
    """
    text_lower = instruction.lower()
    score = 30.0  # baseline

    for keyword, bonus in _URGENCY_KEYWORDS.items():
        if keyword in text_lower:
            score += bonus

    for keyword, bonus in _REVENUE_KEYWORDS.items():
        if keyword in text_lower:
            score += bonus

    for keyword, bonus in _SURVIVAL_KEYWORDS.items():
        if keyword in text_lower:
            score += bonus

    for keyword, penalty in _ROUTINE_KEYWORDS.items():
        if keyword in text_lower:
            score += penalty  # penalty is negative

    # Bonus for longer, more detailed instructions (likely more important)
    if len(instruction) > 500:
        score += 5.0
    if len(instruction) > 1500:
        score += 5.0

    return max(0.0, min(100.0, round(score, 2)))


# ---------------------------------------------------------------------------
# Priority computation
# ---------------------------------------------------------------------------

def compute_priority(
    task_value: float,
    task_cost: dict[str, Any],
    budget: ComputeBudget,
) -> float:
    """Compute execution priority for a task.

    Formula::

        priority = (value - cost_penalty) * resource_multiplier

    - ``cost_penalty`` increases as the token budget depletes.
    - ``resource_multiplier`` decreases under memory/CPU pressure.

    Args:
        task_value: Value score from ``estimate_task_value`` (0-100).
        task_cost: Cost dict from ``estimate_task_cost``.
        budget: Current compute budget snapshot.

    Returns:
        Priority score (higher = do first).
    """
    estimated_tokens = task_cost.get("estimated_tokens", 0)

    # Budget depletion ratio: 0.0 (fresh) to 1.0 (exhausted)
    remaining = max(0, budget.total_tokens_available - budget.tokens_used_today)
    if budget.total_tokens_available > 0:
        depletion = 1.0 - (remaining / budget.total_tokens_available)
    else:
        depletion = 1.0

    # Cost penalty: scales with both estimated tokens and depletion
    # At 0% depletion, penalty is low; at 90%+ depletion, penalty is steep
    token_ratio = estimated_tokens / max(1, remaining) if remaining > 0 else 1.0
    cost_penalty = 20.0 * depletion + 30.0 * min(1.0, token_ratio)

    # Resource pressure multiplier (1.0 = healthy, approaches 0.3 under heavy load)
    mem_factor = max(0.3, 1.0 - (budget.memory_pressure_pct / 100.0) * 0.7)
    cpu_factor = max(0.3, 1.0 - (budget.cpu_pressure_pct / 100.0) * 0.7)
    resource_multiplier = (mem_factor + cpu_factor) / 2.0

    priority = (task_value - cost_penalty) * resource_multiplier
    return round(priority, 4)


# ---------------------------------------------------------------------------
# Budget retrieval
# ---------------------------------------------------------------------------

def get_current_budget(workspace: Path) -> ComputeBudget:
    """Read current budget state from workspace persisted files + system vitals.

    Aggregates data from:
    - ``.claw/quota_tracker.json``
    - ``.claw/survival_state.json``
    - System CPU/memory via psutil (if available) or shutil for disk.

    Args:
        workspace: Project workspace root.

    Returns:
        A populated ``ComputeBudget`` instance.
    """
    workspace = Path(workspace).resolve()
    budget = ComputeBudget()

    # --- Token budget from quota tracker ---
    qt_path = workspace / ".claw" / "quota_tracker.json"
    if qt_path.is_file():
        try:
            data = json.loads(qt_path.read_text(encoding="utf-8"))
            usage = data.get("usage", {})
            # Count total API calls today as a proxy for tokens used
            now = time.time()
            day_start = now - 86400
            calls_today = 0
            for _plat, stamps in usage.items():
                if isinstance(stamps, list):
                    calls_today += sum(1 for t in stamps if isinstance(t, (int, float)) and t >= day_start)
            # Rough estimate: ~1000 tokens per API call
            budget.tokens_used_today = calls_today * 1000
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    # --- Total budget from env or default ---
    try:
        budget.total_tokens_available = int(
            os.environ.get("DEVCLAW_TOKEN_BUDGET", "") or 1_000_000
        )
    except ValueError:
        budget.total_tokens_available = 1_000_000

    # --- Cost per 1k tokens from env ---
    try:
        budget.api_cost_per_1k_tokens = float(
            os.environ.get("DEVCLAW_COST_PER_1K", "") or 0.002
        )
    except ValueError:
        budget.api_cost_per_1k_tokens = 0.002

    # --- System vitals ---
    try:
        import psutil  # type: ignore[import-untyped]
        budget.cpu_pressure_pct = round(psutil.cpu_percent(interval=0.15), 1)
        budget.memory_pressure_pct = round(psutil.virtual_memory().percent, 1)
    except ImportError:
        pass

    # --- Network latency (rough probe) ---
    budget.network_latency_ms = _probe_network_latency()

    return budget


def _probe_network_latency() -> float:
    """Quick HTTP HEAD to a fast endpoint to measure network latency."""
    import urllib.request
    import urllib.error

    t0 = time.monotonic()
    try:
        req = urllib.request.Request("https://httpbin.org/status/200", method="HEAD")
        with urllib.request.urlopen(req, timeout=5):
            pass
        return round((time.monotonic() - t0) * 1000, 1)
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Execution decision
# ---------------------------------------------------------------------------

def should_execute(
    instruction: str,
    task_type: str,
    workspace: Path,
) -> dict[str, Any]:
    """Full metabolic analysis: decide whether a task should run now.

    Estimates cost, value, and priority, then compares against budget
    constraints.

    Args:
        instruction: The task instruction text.
        task_type: Category of the task.
        workspace: Project workspace root.

    Returns:
        Dict with ``execute`` (bool), ``priority`` (float),
        ``reason`` (str), ``budget_remaining`` (dict).
    """
    workspace = Path(workspace).resolve()

    cost = estimate_task_cost(instruction, task_type)
    value = estimate_task_value(instruction, workspace)
    budget = get_current_budget(workspace)
    priority = compute_priority(value, cost, budget)

    remaining_tokens = max(0, budget.total_tokens_available - budget.tokens_used_today)
    estimated_tokens = cost.get("estimated_tokens", 0)

    budget_remaining = {
        "tokens_remaining": remaining_tokens,
        "budget_pct": round(remaining_tokens / max(1, budget.total_tokens_available) * 100, 1),
        "memory_pressure_pct": budget.memory_pressure_pct,
        "cpu_pressure_pct": budget.cpu_pressure_pct,
    }

    # Decision logic
    if remaining_tokens < estimated_tokens * 0.5:
        return {
            "execute": False,
            "priority": priority,
            "reason": f"Insufficient token budget: need ~{estimated_tokens}, have {remaining_tokens}",
            "budget_remaining": budget_remaining,
        }

    if budget.memory_pressure_pct > 95.0:
        return {
            "execute": False,
            "priority": priority,
            "reason": f"Memory pressure critical: {budget.memory_pressure_pct}%",
            "budget_remaining": budget_remaining,
        }

    if priority < -10.0:
        return {
            "execute": False,
            "priority": priority,
            "reason": f"Priority too low ({priority:.2f}); budget depletion or low value",
            "budget_remaining": budget_remaining,
        }

    # Green light
    reason = f"Approved: value={value:.1f}, cost={estimated_tokens}tok, priority={priority:.2f}"
    return {
        "execute": True,
        "priority": priority,
        "reason": reason,
        "budget_remaining": budget_remaining,
    }


# ---------------------------------------------------------------------------
# Heartbeat interval optimisation
# ---------------------------------------------------------------------------

def get_optimal_heartbeat_interval(budget: ComputeBudget) -> float:
    """Determine heartbeat interval based on resource health.

    - Healthy budget  -> fast heartbeat (30s)
    - Tight budget    -> slow heartbeat (300s)
    - CRITICAL budget -> minimal heartbeat (600s)

    Args:
        budget: Current compute budget snapshot.

    Returns:
        Recommended heartbeat interval in seconds.
    """
    remaining = max(0, budget.total_tokens_available - budget.tokens_used_today)
    if budget.total_tokens_available > 0:
        budget_pct = remaining / budget.total_tokens_available
    else:
        budget_pct = 0.0

    # System pressure factor
    pressure = max(budget.memory_pressure_pct, budget.cpu_pressure_pct) / 100.0

    # CRITICAL: budget < 5% or extreme system pressure
    if budget_pct < 0.05 or pressure > 0.95:
        return 600.0

    # TIGHT: budget < 20% or high pressure
    if budget_pct < 0.20 or pressure > 0.80:
        return 300.0

    # MODERATE: budget < 50% or moderate pressure
    if budget_pct < 0.50 or pressure > 0.60:
        return 120.0

    # HEALTHY
    return 30.0
