"""
Provider Router — DevClaw's "Which Brain to Use" decision layer.

Given a task's type, complexity, system health, and budget state, selects
the optimal provider (Ollama, Claude CLI, OpenAI API, Anthropic API) and
returns a fully populated ProviderDecision with fallback chain.

Routing priorities:
  1. Survival (CRITICAL/DEGRADED) overrides everything — free tier only.
  2. Budget guards prevent overspend — force free when daily < $1.
  3. Task requirements (tools, realtime) constrain provider choice.
  4. Complexity score selects quality tier.
  5. Evolution tasks gated by health + budget + time window.

Integrates lazily with:
  - resource_registry (provider availability)
  - budget_manager    (spend tracking)
  - survival_engine   (health state)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class RoutingContext:
    """Everything the router needs to pick a provider."""
    task_type: str                  # "chat" | "engineering" | "trading" | "ops" | "evolution" | "survival"
    complexity_score: int           # 1-10
    health_state: str               # "HEALTHY" | "DEGRADED" | "CRITICAL"
    budget_remaining_daily_usd: float
    is_evolution_window: bool
    requires_tools: bool            # does the task need file/terminal/browser access?
    requires_realtime: bool         # does the task need live data (prices, APIs)?
    message_length: int
    priority: int                   # 1 = highest


@dataclass
class ProviderDecision:
    """The router's output — which provider/model to use and how."""
    provider: str                   # "ollama" | "claude_cli" | "openai_api" | "anthropic_api" | "offline" | "reject"
    model: str                      # specific model name
    channel: str                    # "local" | "api" | "cli" | "none"
    cost_tier: str                  # "free" | "low" | "medium" | "high"
    timeout_sec: float
    allow_parallel: bool
    allow_upgrade: bool             # can escalate to more expensive model if this fails?
    fallback_chain: list[str]       # ordered list of fallback provider names
    reason: str                     # human-readable explanation


# ── Provider metadata ────────────────────────────────────────────────────────

_PROVIDER_META: dict[str, dict[str, Any]] = {
    "ollama": {
        "channel": "local",
        "cost_tier": "free",
        "default_model": "llama3",
        "timeout_sec": 60.0,
        "has_tools": False,
        "has_realtime": False,
        "quality": 3,               # 1-10 subjective quality score
        "cost_per_1k_tokens": 0.0,
    },
    "claude_cli": {
        "channel": "cli",
        "cost_tier": "free",
        "default_model": "claude-sonnet",
        "timeout_sec": 120.0,
        "has_tools": True,
        "has_realtime": True,        # can shell out for live data
        "quality": 9,
        "cost_per_1k_tokens": 0.0,   # user subscription, free to the agent
    },
    "openai_api": {
        "channel": "api",
        "cost_tier": "medium",
        "default_model": "gpt-4o",
        "timeout_sec": 90.0,
        "has_tools": False,
        "has_realtime": True,
        "quality": 8,
        "cost_per_1k_tokens": 0.01,  # rough average across in/out
    },
    "anthropic_api": {
        "channel": "api",
        "cost_tier": "high",
        "default_model": "claude-sonnet",
        "timeout_sec": 90.0,
        "has_tools": False,
        "has_realtime": True,
        "quality": 9,
        "cost_per_1k_tokens": 0.015,
    },
}

# Per-1M-token pricing used by estimate_cost (input, output).
_MODEL_COSTS: dict[str, tuple[float, float]] = {
    "gpt-4o":         (2.50, 10.00),
    "gpt-4o-mini":    (0.15,  0.60),
    "claude-sonnet":  (3.00, 15.00),
    "claude-opus":    (15.00, 75.00),
    "llama3":         (0.00,  0.00),
    "ollama":         (0.00,  0.00),
}

_FREE_PROVIDERS = {"ollama", "claude_cli"}
_PAID_PROVIDERS = {"openai_api", "anthropic_api"}
_ALL_PROVIDERS = _FREE_PROVIDERS | _PAID_PROVIDERS


# ── Lazy imports from sibling modules ────────────────────────────────────────

def _try_import_resource_registry():
    """Return ResourceRegistry class or None."""
    try:
        from claw_runtime.resource_registry import ResourceRegistry
        return ResourceRegistry
    except Exception:
        return None


def _try_import_budget_manager():
    """Return BudgetManager class or None."""
    try:
        from claw_runtime.budget_manager import BudgetManager
        return BudgetManager
    except Exception:
        return None


def _try_import_survival_engine():
    """Return SurvivalState enum or None."""
    try:
        from claw_runtime.survival_engine import SurvivalState
        return SurvivalState
    except Exception:
        return None


# ── Helper factories ─────────────────────────────────────────────────────────

def _decision(
    provider: str,
    reason: str,
    *,
    model: str | None = None,
    fallback_chain: list[str] | None = None,
    allow_upgrade: bool = True,
    allow_parallel: bool = True,
    timeout_override: float | None = None,
) -> ProviderDecision:
    """Build a ProviderDecision from a provider name + minimal overrides."""
    meta = _PROVIDER_META.get(provider, {})
    return ProviderDecision(
        provider=provider,
        model=model or meta.get("default_model", "unknown"),
        channel=meta.get("channel", "none"),
        cost_tier=meta.get("cost_tier", "free"),
        timeout_sec=timeout_override or meta.get("timeout_sec", 60.0),
        allow_parallel=allow_parallel,
        allow_upgrade=allow_upgrade,
        fallback_chain=fallback_chain or [],
        reason=reason,
    )


def _reject(reason: str) -> ProviderDecision:
    return ProviderDecision(
        provider="reject",
        model="none",
        channel="none",
        cost_tier="free",
        timeout_sec=0.0,
        allow_parallel=False,
        allow_upgrade=False,
        fallback_chain=[],
        reason=reason,
    )


def _offline(reason: str) -> ProviderDecision:
    return ProviderDecision(
        provider="offline",
        model="none",
        channel="none",
        cost_tier="free",
        timeout_sec=0.0,
        allow_parallel=False,
        allow_upgrade=False,
        fallback_chain=[],
        reason=reason,
    )


# ── Main router ──────────────────────────────────────────────────────────────

class ProviderRouter:
    """
    Stateless decision engine.  Call ``route(ctx)`` with a populated
    RoutingContext to get a ProviderDecision.

    The router never calls providers itself — it only *selects* them.
    """

    def __init__(self, workspace: str = ".") -> None:
        self.workspace = Path(workspace).resolve()
        self._registry_cls = _try_import_resource_registry()
        self._budget_cls = _try_import_budget_manager()

    # ── Public API ────────────────────────────────────────────────────────

    def route(self, context: RoutingContext) -> ProviderDecision:
        """Core routing logic.  Returns a ProviderDecision."""
        ctx = context

        # ── Rule 1: CRITICAL state — free only, reject evolution ──────
        if ctx.health_state == "CRITICAL":
            if ctx.task_type == "evolution":
                return _reject("Evolution rejected: system is CRITICAL")
            return self._route_free_only(ctx, "System CRITICAL — free providers only")

        # ── Rule 2: DEGRADED state — prefer free, allow low cost, no evolution
        if ctx.health_state == "DEGRADED":
            if ctx.task_type == "evolution":
                return _reject("Evolution rejected: system is DEGRADED")
            return self._route_degraded(ctx)

        # ── Rule 7: Budget < $1 daily remaining — force free tier ─────
        if ctx.budget_remaining_daily_usd < 1.0:
            if ctx.task_type == "evolution":
                return _reject("Evolution rejected: daily budget < $1")
            return self._route_free_only(ctx, f"Budget low (${ctx.budget_remaining_daily_usd:.2f} remaining) — free tier only")

        # ── Rule 6: Evolution gating ─────────────────────────────────
        if ctx.task_type == "evolution":
            return self._route_evolution(ctx)

        # ── Rule 8: Grand tasks (score >= 7) — high quality only ─────
        if ctx.complexity_score >= 7:
            return self._route_grand(ctx)

        # ── Task-type specific routing ────────────────────────────────
        if ctx.task_type == "chat":
            return self._route_chat(ctx)
        if ctx.task_type == "engineering":
            return self._route_engineering(ctx)
        if ctx.task_type == "trading":
            return self._route_trading(ctx)
        if ctx.task_type == "ops":
            return self._route_ops(ctx)
        if ctx.task_type == "survival":
            return self._route_survival(ctx)

        # Fallback for unknown task types
        return self._route_chat(ctx)

    def route_simple(self, text: str, health_state: str = "HEALTHY") -> ProviderDecision:
        """
        Convenience method — builds a minimal RoutingContext from raw text
        and routes it.  Useful for quick dispatch without full context.
        """
        length = len(text)
        # Simple heuristic: short messages are chat, longer are moderate
        if length < 200:
            task_type = "chat"
            complexity = 2
        elif length < 800:
            task_type = "chat"
            complexity = 4
        else:
            task_type = "engineering"
            complexity = 6

        # Check for tool / realtime keywords
        _tool_keywords = {"file", "terminal", "edit", "create", "delete", "run", "execute", "build", "test"}
        _rt_keywords = {"price", "market", "live", "fetch", "api", "current", "now", "latest"}
        words = set(text.lower().split())

        requires_tools = bool(words & _tool_keywords)
        requires_realtime = bool(words & _rt_keywords)

        if requires_tools:
            task_type = "engineering"
            complexity = max(complexity, 5)
        if requires_realtime:
            task_type = "trading"

        ctx = RoutingContext(
            task_type=task_type,
            complexity_score=complexity,
            health_state=health_state,
            budget_remaining_daily_usd=8.0,     # assume full budget
            is_evolution_window=False,
            requires_tools=requires_tools,
            requires_realtime=requires_realtime,
            message_length=length,
            priority=5,
        )
        return self.route(ctx)

    def get_fallback(self, failed_provider: str, context: RoutingContext) -> ProviderDecision | None:
        """
        After a provider fails, return the next best option or None.
        Respects the same health/budget constraints as ``route()``.
        """
        decision = self.route(context)
        chain = decision.fallback_chain

        # If the original decision IS the failed provider, try the chain
        if decision.provider == failed_provider:
            for fb in chain:
                if fb == failed_provider:
                    continue
                if not self._provider_allowed(fb, context):
                    continue
                return _decision(
                    fb,
                    f"Fallback from {failed_provider}: trying {fb}",
                    allow_upgrade=False,
                    fallback_chain=[p for p in chain if p != fb and p != failed_provider],
                )
            return None

        # Decision already picked something different
        if decision.provider != failed_provider:
            return decision

        return None

    def estimate_cost(self, provider: str, model: str, est_tokens: int) -> float:
        """
        Estimate cost in USD for a given provider/model/token count.
        Uses a 1:1 input/output ratio assumption when not specified.
        """
        meta = _PROVIDER_META.get(provider, {})
        if meta.get("cost_tier") == "free":
            return 0.0

        costs = _MODEL_COSTS.get(model)
        if costs:
            input_per_m, output_per_m = costs
            # Assume roughly 60% input, 40% output
            input_tokens = est_tokens * 0.6
            output_tokens = est_tokens * 0.4
            return (input_tokens / 1_000_000 * input_per_m) + (output_tokens / 1_000_000 * output_per_m)

        # Fallback: use per-1k cost from meta
        per_1k = meta.get("cost_per_1k_tokens", 0.01)
        return est_tokens / 1000 * per_1k

    def available_providers(self) -> list[str]:
        """
        Return list of provider names that appear to be available.
        Checks environment for API keys and common indicators.
        """
        available: list[str] = []

        # Ollama: check if OLLAMA_HOST is set or default localhost is assumed
        available.append("ollama")

        # Claude CLI: check if `claude` would be on PATH
        claude_path = os.environ.get("CLAUDE_CLI_PATH")
        if claude_path or os.path.exists("/usr/local/bin/claude") or os.path.exists(os.path.expanduser("~/.local/bin/claude")):
            available.append("claude_cli")
        else:
            # Assume available by default in the DevClaw environment
            available.append("claude_cli")

        # OpenAI API
        if os.environ.get("OPENAI_API_KEY"):
            available.append("openai_api")

        # Anthropic API
        if os.environ.get("ANTHROPIC_API_KEY"):
            available.append("anthropic_api")

        # Try resource_registry for richer availability info
        if self._registry_cls is not None:
            try:
                registry = self._registry_cls(str(self.workspace))
                if hasattr(registry, "available_providers"):
                    reg_providers = registry.available_providers()
                    for p in reg_providers:
                        if p not in available:
                            available.append(p)
            except Exception:
                pass

        return available

    # ── Private routing strategies ────────────────────────────────────

    def _route_free_only(self, ctx: RoutingContext, reason: str) -> ProviderDecision:
        """Only allow ollama or claude_cli."""
        if ctx.requires_tools:
            # Claude CLI is the only free provider with tool access
            return _decision(
                "claude_cli", f"{reason}; tools required → Claude CLI",
                fallback_chain=["ollama"],
                allow_upgrade=False,
            )
        if ctx.requires_realtime:
            return _decision(
                "claude_cli", f"{reason}; realtime required → Claude CLI",
                fallback_chain=["ollama"],
                allow_upgrade=False,
            )
        # Simple task — prefer ollama (faster, fully local)
        return _decision(
            "ollama", reason,
            fallback_chain=["claude_cli"],
            allow_upgrade=False,
        )

    def _route_degraded(self, ctx: RoutingContext) -> ProviderDecision:
        """DEGRADED: prefer free, allow low-cost API if capabilities demand it."""
        if ctx.requires_tools:
            return _decision(
                "claude_cli", "DEGRADED + tools required → Claude CLI (free)",
                fallback_chain=["ollama"],
                allow_upgrade=False,
            )
        if ctx.requires_realtime:
            return _decision(
                "claude_cli", "DEGRADED + realtime required → Claude CLI",
                fallback_chain=["openai_api"],
                allow_upgrade=False,
            )
        if ctx.complexity_score >= 7:
            # Grand task in degraded — use Claude CLI, allow low-cost fallback
            return _decision(
                "claude_cli", "DEGRADED + grand task → Claude CLI",
                fallback_chain=["openai_api"],
                allow_upgrade=False,
            )
        return _decision(
            "ollama", "DEGRADED — prefer free local provider",
            fallback_chain=["claude_cli"],
            allow_upgrade=False,
        )

    def _route_chat(self, ctx: RoutingContext) -> ProviderDecision:
        """Rule 3: Chat — Ollama first, Claude CLI fallback."""
        if ctx.complexity_score <= 4 and ctx.message_length < 500:
            return _decision(
                "ollama", "Simple chat — Ollama (fast, free, local)",
                fallback_chain=["claude_cli"],
            )
        # More complex or longer chat
        return _decision(
            "claude_cli", "Complex chat — Claude CLI (higher quality)",
            fallback_chain=["ollama", "openai_api"],
        )

    def _route_engineering(self, ctx: RoutingContext) -> ProviderDecision:
        """Rule 4: Engineering — Claude CLI (has tool access via -p flag)."""
        fallback = ["openai_api", "anthropic_api"] if ctx.budget_remaining_daily_usd >= 1.0 else []
        return _decision(
            "claude_cli", "Engineering task — Claude CLI (tool access via -p flag)",
            fallback_chain=fallback,
            timeout_override=180.0,     # engineering tasks may be long
        )

    def _route_trading(self, ctx: RoutingContext) -> ProviderDecision:
        """Rule 5: Trading — Claude CLI → OpenAI API fallback (needs realtime)."""
        return _decision(
            "claude_cli", "Trading task — Claude CLI (realtime via shell)",
            fallback_chain=["openai_api", "anthropic_api"],
            timeout_override=120.0,
        )

    def _route_ops(self, ctx: RoutingContext) -> ProviderDecision:
        """Ops tasks — Claude CLI preferred (tool access), ollama for simple checks."""
        if ctx.complexity_score <= 3:
            return _decision(
                "ollama", "Simple ops check — Ollama",
                fallback_chain=["claude_cli"],
            )
        return _decision(
            "claude_cli", "Ops task — Claude CLI (tool access needed)",
            fallback_chain=["ollama"],
        )

    def _route_survival(self, ctx: RoutingContext) -> ProviderDecision:
        """Survival tasks — always free, prefer local."""
        return _decision(
            "ollama", "Survival check — local Ollama (free, no external deps)",
            fallback_chain=["claude_cli"],
            allow_upgrade=False,
        )

    def _route_evolution(self, ctx: RoutingContext) -> ProviderDecision:
        """
        Rule 6: Evolution — only when HEALTHY + budget allows + evolution window.
        Already past CRITICAL/DEGRADED gates by the time we get here.
        """
        if not ctx.is_evolution_window:
            return _reject("Evolution rejected: outside evolution window")

        if ctx.budget_remaining_daily_usd < 3.0:
            return _reject(f"Evolution rejected: daily budget too low (${ctx.budget_remaining_daily_usd:.2f})")

        # Evolution needs high-quality reasoning — Claude CLI or paid API
        return _decision(
            "claude_cli", "Evolution task — Claude CLI (high quality, free)",
            model="claude-sonnet",
            fallback_chain=["anthropic_api", "openai_api"],
            allow_upgrade=True,
            timeout_override=300.0,     # evolution can be slow
        )

    def _route_grand(self, ctx: RoutingContext) -> ProviderDecision:
        """Rule 8: Grand tasks (score >= 7) — high-quality provider only."""
        if ctx.requires_tools:
            return _decision(
                "claude_cli", f"Grand task (score={ctx.complexity_score}) + tools → Claude CLI",
                fallback_chain=["anthropic_api", "openai_api"],
                timeout_override=240.0,
            )
        # No tools needed — pick by budget
        if ctx.budget_remaining_daily_usd >= 3.0:
            return _decision(
                "claude_cli", f"Grand task (score={ctx.complexity_score}) → Claude CLI",
                fallback_chain=["anthropic_api", "openai_api"],
                timeout_override=240.0,
            )
        return _decision(
            "claude_cli", f"Grand task (score={ctx.complexity_score}), budget constrained → Claude CLI (free)",
            fallback_chain=[],
            allow_upgrade=False,
            timeout_override=240.0,
        )

    def _provider_allowed(self, provider: str, ctx: RoutingContext) -> bool:
        """Check whether a provider is permissible given current constraints."""
        if ctx.health_state == "CRITICAL" and provider in _PAID_PROVIDERS:
            return False
        if ctx.health_state == "DEGRADED" and provider == "anthropic_api":
            # In degraded, allow only low-cost paid (openai gpt-4o-mini)
            return False
        if ctx.budget_remaining_daily_usd < 1.0 and provider in _PAID_PROVIDERS:
            return False
        return True
