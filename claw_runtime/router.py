"""
Task intake and routing for DevClaw.

Front door: receives tasks from Telegram / CLI / operator and routes them
through the classification -> gating -> complexity -> planning pipeline.

Integrates with:
- task_complexity_detector.assess_complexity()
- task_decomposer.decompose_and_enqueue()
- consciousness_router.route_message()
- survival_engine.SurvivalEngine
- bot_task_queue.SmartTaskRegistry
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .consciousness_router import route_message as _route_message
from .survival_engine import SurvivalEngine, SurvivalState, read_fund_estimate_usd
from .task_complexity_detector import assess_complexity
from .task_decomposer import decompose_and_enqueue

if TYPE_CHECKING:
    from .planner import ExecutionPlan

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class TaskEnvelope:
    """Incoming task wrapper — everything the router needs to decide."""

    task_id: str
    instruction: str
    source: str  # "telegram" | "cli" | "operator" | "decomposer" | "evolution"
    context: dict  # workspace path, files, env vars, etc.
    priority: int  # 1=highest, 10=lowest
    created_at: float
    parent_task_id: str | None = None


@dataclass
class RouteDecision:
    """What the router decided to do with a task."""

    action: str  # "execute" | "decompose" | "reject" | "queue" | "defer"
    reason: str
    plan: "ExecutionPlan | None" = None
    sub_tasks: list[dict] | None = None


# ---------------------------------------------------------------------------
# Classification patterns
# ---------------------------------------------------------------------------

_ENGINEERING_PAT = re.compile(
    r"bug|fix|refactor|pytest|git|commit|build|deploy|docker|api|error|traceback|"
    r"code|patch|test|lint|compile|module|class|function|import|"
    r"代码|修复|重构|提交|报错|异常|测试|部署|接口|编译|模块",
    re.I,
)

_TRADING_PAT = re.compile(
    r"\b(btc|eth|sol|dex|perp|funding|mcap|volume|liquidity|chain|on-?chain|"
    r"whale|token|pair|swap|defi|price|market|order|position|pnl)\b|"
    r"行情|K线|交易|资金费|链上|聪明钱|市值|流动性|仓位",
    re.I,
)

_OPS_PAT = re.compile(
    r"disk|memory|cpu|quota|budget|survival|restart|backup|cron|health|"
    r"monitor|alert|log\b|status|uptime|"
    r"磁盘|内存|配额|预算|存活|重启|备份|健康|监控|日志",
    re.I,
)

_META_PAT = re.compile(
    r"evolve|improve yourself|self-test|nightly|meta|skill.?install|"
    r"consciousness|upgrade|mutation|self-heal|"
    r"进化|自测|自愈|元认知|技能安装|意识",
    re.I,
)

# Keywords that indicate survival-related tasks (allowed even in CRITICAL)
_SURVIVAL_PAT = re.compile(
    r"survival|heal|recover|disk|quota|budget|restart|backup|"
    r"存活|恢复|修复配额|磁盘|预算|重启|备份",
    re.I,
)

# Budget floor — reject tasks if estimated fund < this (USD)
_BUDGET_FLOOR_USD = 0.10


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_envelope(
    instruction: str,
    source: str = "cli",
    context: dict | None = None,
    priority: int = 5,
    parent_task_id: str | None = None,
) -> TaskEnvelope:
    """Convenience factory for creating a TaskEnvelope."""
    return TaskEnvelope(
        task_id=uuid.uuid4().hex[:12],
        instruction=instruction,
        source=source,
        context=context or {},
        priority=priority,
        created_at=time.time(),
        parent_task_id=parent_task_id,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


class Router:
    """
    Central intake router for DevClaw.

    1. Classify task category (engineering / trading / ops / meta)
    2. Check survival & budget gates
    3. Assess complexity
    4. Route: decompose grand tasks, plan atomic/moderate ones
    """

    def __init__(self, workspace: str = ".") -> None:
        self.workspace = Path(workspace).resolve()
        self._survival = SurvivalEngine(workspace=self.workspace)

    # ---- public API --------------------------------------------------------

    async def route(self, envelope: TaskEnvelope) -> RouteDecision:
        """
        Main entry point: classify, gate-check, assess complexity, and route.

        Returns a RouteDecision describing the chosen action.
        """
        instruction = envelope.instruction.strip()
        if not instruction:
            return RouteDecision(action="reject", reason="Empty instruction")

        # --- Gate checks (survival + budget) ---
        allowed, gate_reason = self.check_gates(envelope)
        if not allowed:
            return RouteDecision(action="reject", reason=gate_reason)

        # --- Classification (for logging / metrics; doesn't block routing) ---
        category = self.classify_task(instruction)
        logger.info(
            "Task %s classified as '%s' (source=%s, priority=%d)",
            envelope.task_id,
            category,
            envelope.source,
            envelope.priority,
        )

        # --- Complexity assessment ---
        assessment = assess_complexity(
            instruction, workspace=self.workspace
        )
        logger.info(
            "Task %s complexity: score=%d level=%s decompose=%s",
            envelope.task_id,
            assessment.score,
            assessment.level,
            assessment.should_decompose,
        )

        # --- Grand → decompose ---
        if assessment.should_decompose:
            was_decomposed, report, task_ids = decompose_and_enqueue(
                instruction,
                self.workspace,
                context=envelope.context.get("extra_context", ""),
            )
            if was_decomposed:
                sub_tasks = [
                    {"task_id": tid, "parent": envelope.task_id}
                    for tid in task_ids
                ]
                return RouteDecision(
                    action="decompose",
                    reason=(
                        f"Grand task (complexity {assessment.score}/10). "
                        f"Decomposed into {len(task_ids)} sub-tasks. {report}"
                    ),
                    sub_tasks=sub_tasks,
                )
            # Decomposition declined (e.g., not actually grand after deeper check)
            logger.info(
                "Task %s decomposition declined — falling through to planning",
                envelope.task_id,
            )

        # --- Atomic / Moderate → plan and execute ---
        from .planner import Planner  # local import to avoid circular

        planner = Planner(workspace=str(self.workspace))
        plan = planner.plan(instruction, context=envelope.context)

        return RouteDecision(
            action="execute",
            reason=(
                f"Routed for execution (complexity {assessment.score}/10, "
                f"category={category}, {len(plan.steps)} steps)"
            ),
            plan=plan,
        )

    def classify_task(self, instruction: str) -> str:
        """
        Classify an instruction into one of: engineering, trading, ops, meta.

        Uses keyword pattern matching. Falls back to the consciousness_router
        lane mapping when no strong signal is found.
        """
        text = instruction.strip()

        scores = {
            "engineering": len(_ENGINEERING_PAT.findall(text)),
            "trading": len(_TRADING_PAT.findall(text)),
            "ops": len(_OPS_PAT.findall(text)),
            "meta": len(_META_PAT.findall(text)),
        }

        best = max(scores, key=scores.get)  # type: ignore[arg-type]
        if scores[best] > 0:
            return best

        # Fallback: use existing consciousness_router for code vs trading
        cr = _route_message(text)
        if cr.lane == "code":
            return "engineering"
        if cr.lane == "trading":
            return "trading"
        return "ops"  # default bucket for unclassified tasks

    def check_gates(self, envelope: TaskEnvelope) -> tuple[bool, str]:
        """
        Pre-routing gate checks.

        Returns (allowed, reason).  If allowed is False the task should be
        rejected or deferred.
        """
        # --- Survival gate ---
        state, reason = self._survival.assess_survival_state()

        if state == SurvivalState.CRITICAL:
            is_survival_task = bool(_SURVIVAL_PAT.search(envelope.instruction))
            # Tasks from the survival/evolution subsystem are always allowed
            if envelope.source in ("evolution",) or is_survival_task:
                logger.warning(
                    "CRITICAL state but allowing survival-related task %s",
                    envelope.task_id,
                )
            else:
                return (
                    False,
                    f"System in CRITICAL state ({reason}). "
                    "Only survival-related tasks are accepted.",
                )

        # --- Budget gate ---
        fund = read_fund_estimate_usd(self.workspace)
        if fund is not None and fund < _BUDGET_FLOOR_USD:
            # Still allow survival tasks even when broke
            is_survival_task = bool(_SURVIVAL_PAT.search(envelope.instruction))
            if not is_survival_task:
                return (
                    False,
                    f"Budget too low (${fund:.2f} < ${_BUDGET_FLOOR_USD:.2f}). "
                    "Task rejected to preserve funds.",
                )

        return (True, "all gates passed")
