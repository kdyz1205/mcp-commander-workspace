"""
Self-Intelligence Engine — DevClaw's ability to learn HOW to be smarter.

This is not a specific skill. This is the meta-skill: the ability to observe
its own performance, identify weaknesses, and autonomously upgrade itself.

The cycle:
1. OBSERVE: Track every action's outcome (success, tokens, time, quality)
2. REFLECT: Analyze patterns — what works, what fails, what wastes tokens
3. ADAPT: Adjust behavior rules, tool preferences, and delegation strategies
4. EVOLVE: Write new skills or modify system prompt based on learned rules

This is the engine that makes DevClaw get smarter every day, automatically.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ActionOutcome:
    """Record of one action's outcome for learning."""
    timestamp: float
    action_type: str          # "tool_call", "delegation", "decomposition", "trade"
    tool_name: str            # specific tool or brain used
    instruction_summary: str  # first 200 chars
    success: bool
    tokens_used: int
    time_sec: float
    error: str = ""
    quality_score: float = 0.0  # 0-1, estimated quality of output
    model_used: str = ""        # which model/brain handled this


@dataclass
class IntelligenceProfile:
    """DevClaw's self-assessment of its own capabilities."""
    overall_iq: float = 50.0          # 0-100 self-assessed intelligence
    tool_proficiency: dict[str, float] = field(default_factory=dict)  # tool → success rate
    delegation_preference: dict[str, float] = field(default_factory=dict)  # brain → preference score
    learned_rules: list[str] = field(default_factory=list)
    weak_areas: list[str] = field(default_factory=list)
    strong_areas: list[str] = field(default_factory=list)
    total_actions: int = 0
    total_successes: int = 0
    last_reflection: float = 0.0
    generation: int = 0


def _intelligence_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "intelligence_profile.json"


def _outcomes_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "action_outcomes.jsonl"


def record_action(workspace: Path, outcome: ActionOutcome) -> None:
    """Record an action outcome for future learning."""
    ws = Path(workspace).resolve()
    path = _outcomes_path(ws)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(outcome), ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def load_recent_outcomes(workspace: Path, hours: float = 24) -> list[ActionOutcome]:
    """Load action outcomes from the last N hours."""
    ws = Path(workspace).resolve()
    path = _outcomes_path(ws)
    if not path.is_file():
        return []

    cutoff = time.time() - hours * 3600
    outcomes = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                d = json.loads(line)
                if float(d.get("timestamp", 0)) >= cutoff:
                    outcomes.append(ActionOutcome(**{
                        k: v for k, v in d.items()
                        if k in ActionOutcome.__dataclass_fields__
                    }))
            except (json.JSONDecodeError, TypeError):
                continue
    except OSError:
        pass
    return outcomes


def load_intelligence_profile(workspace: Path) -> IntelligenceProfile:
    """Load the current intelligence profile."""
    ws = Path(workspace).resolve()
    path = _intelligence_path(ws)
    if not path.is_file():
        return IntelligenceProfile()
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
        return IntelligenceProfile(**{
            k: v for k, v in d.items()
            if k in IntelligenceProfile.__dataclass_fields__
        })
    except (OSError, json.JSONDecodeError, TypeError):
        return IntelligenceProfile()


def save_intelligence_profile(workspace: Path, profile: IntelligenceProfile) -> None:
    """Save the intelligence profile."""
    ws = Path(workspace).resolve()
    path = _intelligence_path(ws)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(profile), indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    except OSError:
        pass


# ---------------------------------------------------------------------------
# REFLECT: Analyze patterns from action outcomes
# ---------------------------------------------------------------------------

def reflect_on_outcomes(outcomes: list[ActionOutcome]) -> dict[str, Any]:
    """
    Analyze action outcomes to extract patterns and insights.
    This is DevClaw's "thinking about thinking".
    """
    if not outcomes:
        return {"insights": [], "tool_stats": {}, "delegation_stats": {}}

    # Tool success rates
    tool_stats: dict[str, dict[str, Any]] = {}
    for o in outcomes:
        key = o.tool_name or o.action_type
        if key not in tool_stats:
            tool_stats[key] = {"total": 0, "success": 0, "total_tokens": 0, "total_time": 0}
        tool_stats[key]["total"] += 1
        if o.success:
            tool_stats[key]["success"] += 1
        tool_stats[key]["total_tokens"] += o.tokens_used
        tool_stats[key]["total_time"] += o.time_sec

    # Calculate rates
    for key, stats in tool_stats.items():
        stats["success_rate"] = stats["success"] / max(stats["total"], 1)
        stats["avg_tokens"] = stats["total_tokens"] / max(stats["total"], 1)
        stats["avg_time"] = stats["total_time"] / max(stats["total"], 1)

    # Delegation effectiveness
    delegation_stats: dict[str, dict[str, Any]] = {}
    for o in outcomes:
        if o.model_used:
            if o.model_used not in delegation_stats:
                delegation_stats[o.model_used] = {"total": 0, "success": 0}
            delegation_stats[o.model_used]["total"] += 1
            if o.success:
                delegation_stats[o.model_used]["success"] += 1

    for key, stats in delegation_stats.items():
        stats["success_rate"] = stats["success"] / max(stats["total"], 1)

    # Extract insights
    insights: list[str] = []

    # Find tools that consistently fail
    for tool, stats in tool_stats.items():
        if stats["total"] >= 3 and stats["success_rate"] < 0.3:
            insights.append(
                f"AVOID: {tool} has {stats['success_rate']:.0%} success rate "
                f"({stats['success']}/{stats['total']}). Consider delegating these tasks."
            )
        elif stats["total"] >= 3 and stats["success_rate"] > 0.8:
            insights.append(
                f"STRENGTH: {tool} has {stats['success_rate']:.0%} success rate. Keep using it."
            )

    # Find if delegation is better than self-execution
    self_rate = sum(1 for o in outcomes if o.success and not o.model_used) / max(
        sum(1 for o in outcomes if not o.model_used), 1
    )
    delegated_rate = sum(1 for o in outcomes if o.success and o.model_used) / max(
        sum(1 for o in outcomes if o.model_used), 1
    )
    if delegated_rate > self_rate + 0.2 and sum(1 for o in outcomes if o.model_used) >= 3:
        insights.append(
            f"DELEGATE MORE: External brains succeed {delegated_rate:.0%} vs self {self_rate:.0%}. "
            f"Outsource complex tasks whenever possible."
        )

    # Token efficiency
    total_tokens = sum(o.tokens_used for o in outcomes)
    total_success = sum(1 for o in outcomes if o.success)
    if total_tokens > 0 and total_success > 0:
        tokens_per_success = total_tokens / total_success
        insights.append(f"EFFICIENCY: ~{tokens_per_success:.0f} tokens per successful action.")

    # Time-of-day patterns
    hour_success: dict[int, list[bool]] = {}
    for o in outcomes:
        hour = int(time.gmtime(o.timestamp).tm_hour)
        hour_success.setdefault(hour, []).append(o.success)
    for hour, results in hour_success.items():
        rate = sum(results) / len(results)
        if len(results) >= 3 and rate < 0.3:
            insights.append(f"TIMING: Poor performance at UTC hour {hour} ({rate:.0%} success). Avoid heavy tasks then.")

    return {
        "insights": insights,
        "tool_stats": tool_stats,
        "delegation_stats": delegation_stats,
        "overall_success_rate": sum(1 for o in outcomes if o.success) / max(len(outcomes), 1),
        "total_actions": len(outcomes),
    }


# ---------------------------------------------------------------------------
# ADAPT: Update intelligence profile based on reflections
# ---------------------------------------------------------------------------

def adapt_profile(
    workspace: Path,
    reflection: dict[str, Any],
    profile: IntelligenceProfile,
) -> IntelligenceProfile:
    """
    Update the intelligence profile based on reflections.
    This is how DevClaw gets smarter over time.
    """
    # Update tool proficiency
    for tool, stats in reflection.get("tool_stats", {}).items():
        profile.tool_proficiency[tool] = round(stats.get("success_rate", 0.5), 3)

    # Update delegation preferences
    for brain, stats in reflection.get("delegation_stats", {}).items():
        profile.delegation_preference[brain] = round(stats.get("success_rate", 0.5), 3)

    # Update learned rules from insights
    for insight in reflection.get("insights", []):
        # Avoid duplicates
        if not any(insight[:50] in existing for existing in profile.learned_rules):
            profile.learned_rules.append(insight)
    # Keep only the 20 most recent/relevant rules
    profile.learned_rules = profile.learned_rules[-20:]

    # Identify weak and strong areas
    profile.weak_areas = [
        tool for tool, rate in profile.tool_proficiency.items()
        if rate < 0.4
    ]
    profile.strong_areas = [
        tool for tool, rate in profile.tool_proficiency.items()
        if rate > 0.7
    ]

    # Update overall IQ estimate
    overall_rate = reflection.get("overall_success_rate", 0.5)
    profile.overall_iq = round(50 + (overall_rate - 0.5) * 100, 1)

    # Update counters
    profile.total_actions += reflection.get("total_actions", 0)
    profile.total_successes += int(
        reflection.get("total_actions", 0) * reflection.get("overall_success_rate", 0.5)
    )
    profile.last_reflection = time.time()
    profile.generation += 1

    save_intelligence_profile(workspace, profile)
    return profile


# ---------------------------------------------------------------------------
# EVOLVE: Generate system prompt additions from learned intelligence
# ---------------------------------------------------------------------------

def build_intelligence_prompt(workspace: Path) -> str:
    """
    Build a system prompt addition based on DevClaw's accumulated intelligence.

    This is the "wisdom" it has gained from experience — injected into every
    future conversation to make it smarter.
    """
    ws = Path(workspace).resolve()
    profile = load_intelligence_profile(ws)

    if not profile.learned_rules and not profile.tool_proficiency:
        return ""

    lines = [
        "\n\n【自进化智慧 — Learned Intelligence (Generation {})】".format(profile.generation),
        f"自评智商: {profile.overall_iq}/100 | 总执行: {profile.total_actions} | "
        f"成功率: {profile.total_successes}/{max(profile.total_actions, 1)}",
        "",
    ]

    if profile.learned_rules:
        lines.append("**经验教训（从历史行动中自主学习）：**")
        for rule in profile.learned_rules[-10:]:  # Show last 10 rules
            lines.append(f"  - {rule}")
        lines.append("")

    if profile.weak_areas:
        lines.append(f"**弱项（自动避免或委派）：** {', '.join(profile.weak_areas)}")
    if profile.strong_areas:
        lines.append(f"**强项（优先自己执行）：** {', '.join(profile.strong_areas)}")

    # Delegation preferences
    if profile.delegation_preference:
        best_brain = max(profile.delegation_preference.items(), key=lambda x: x[1], default=None)
        if best_brain and best_brain[1] > 0.6:
            lines.append(f"**最佳外包对象：** {best_brain[0]} (成功率 {best_brain[1]:.0%})")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full self-intelligence cycle (run daily or on-demand)
# ---------------------------------------------------------------------------

def run_intelligence_cycle(
    workspace: Path | str,
    *,
    hours: float = 24,
    emit: Any = None,
) -> dict[str, Any]:
    """
    Run one full self-intelligence cycle:
    OBSERVE → REFLECT → ADAPT → EVOLVE

    Call this daily (e.g., in REM sleep) or after every N tasks.
    """
    ws = Path(workspace).resolve()

    # OBSERVE
    outcomes = load_recent_outcomes(ws, hours=hours)
    if not outcomes:
        return {"status": "no_data", "message": "No recent outcomes to learn from"}

    # REFLECT
    reflection = reflect_on_outcomes(outcomes)

    # ADAPT
    profile = load_intelligence_profile(ws)
    profile = adapt_profile(ws, reflection, profile)

    # Report
    summary = {
        "status": "evolved",
        "generation": profile.generation,
        "overall_iq": profile.overall_iq,
        "actions_analyzed": len(outcomes),
        "insights_gained": len(reflection.get("insights", [])),
        "learned_rules": len(profile.learned_rules),
        "weak_areas": profile.weak_areas,
        "strong_areas": profile.strong_areas,
    }

    if callable(emit):
        emit(
            f"[🧬 自进化] Generation {profile.generation} | IQ: {profile.overall_iq} | "
            f"分析了 {len(outcomes)} 个行动 | 学到 {len(reflection.get('insights', []))} 条新规则"
        )

    return summary
