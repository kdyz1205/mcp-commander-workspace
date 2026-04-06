"""
Step-by-step planning for DevClaw.

Given a task instruction, produces an ordered list of PlanSteps with tool
selections, dependency edges, timeouts, and cost estimates.

Two planning modes:
- plan_heuristic(): fast, keyword-driven, no LLM call
- plan_with_llm(): formats a structured prompt and parses the LLM's JSON plan
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class PlanStep:
    """A single step in an execution plan."""

    step_id: int
    action: str  # "terminal" | "browser" | "patch" | "test" | "screenshot" | "git" | "observe"
    description: str
    tool: str  # specific tool method, e.g. "terminal_tool.run"
    params: dict  # tool parameters
    depends_on: list[int]  # step_ids this depends on
    timeout_sec: float = 120
    retry_on_fail: bool = False
    max_retries: int = 2


@dataclass
class ExecutionPlan:
    """Ordered list of steps to accomplish a task."""

    task_id: str
    instruction: str
    steps: list[PlanStep]
    created_at: float
    estimated_duration_sec: float
    estimated_cost_usd: float
    requires_browser: bool
    requires_terminal: bool


# ---------------------------------------------------------------------------
# Keyword rule tables for heuristic planning
# ---------------------------------------------------------------------------

# Each rule: (compiled pattern, list-of-step-templates)
# Step templates: (action, description, tool, params_factory)

def _term(cmd: str, desc: str | None = None) -> dict:
    """Shorthand for a terminal step template."""
    return {
        "action": "terminal",
        "description": desc or f"Run: {cmd}",
        "tool": "terminal_tool.run",
        "params": {"command": cmd},
    }


def _browser(url: str = "", desc: str = "Open browser") -> dict:
    return {
        "action": "browser",
        "description": desc,
        "tool": "browser_tool.navigate",
        "params": {"url": url},
    }


def _screenshot(desc: str = "Take screenshot for verification") -> dict:
    return {
        "action": "screenshot",
        "description": desc,
        "tool": "browser_tool.screenshot",
        "params": {},
    }


def _patch(desc: str = "Apply code patch") -> dict:
    return {
        "action": "patch",
        "description": desc,
        "tool": "patch_tool.apply",
        "params": {},
    }


def _test(cmd: str = "pytest", desc: str = "Run tests") -> dict:
    return {
        "action": "test",
        "description": desc,
        "tool": "terminal_tool.run",
        "params": {"command": cmd},
    }


def _git(cmd: str = "git status", desc: str = "Git operation") -> dict:
    return {
        "action": "git",
        "description": desc,
        "tool": "terminal_tool.run",
        "params": {"command": cmd},
    }


def _observe(desc: str = "Read output / observe state") -> dict:
    return {
        "action": "observe",
        "description": desc,
        "tool": "terminal_tool.run",
        "params": {"command": "echo 'observing...'"},
    }


# Heuristic rule bank: (regex, step_templates)
_RULES: list[tuple[re.Pattern, list[dict]]] = [
    # --- Start / Run / Deploy ---
    (
        re.compile(r"启动|start|run\b|deploy|launch|serve", re.I),
        [
            _term("# start/deploy command", "Start or deploy the service"),
            _observe("Verify service is running"),
        ],
    ),
    # --- Browse / Open / Verify UI ---
    (
        re.compile(r"打开|open\b|browse|verify.?ui|check.?page|visit", re.I),
        [
            _browser("http://localhost:3000", "Open target URL in browser"),
            _screenshot("Screenshot page for visual verification"),
        ],
    ),
    # --- Fix / Patch / Change ---
    (
        re.compile(r"修复|fix\b|patch|change|update|edit|modify|改|修改", re.I),
        [
            _observe("Read the relevant code / logs"),
            _patch("Apply the fix"),
            _test("pytest -x", "Run tests to verify the fix"),
        ],
    ),
    # --- Test / Verify ---
    (
        re.compile(r"测试|test\b|verify|check|validate|assert|验证", re.I),
        [
            _test("pytest -x", "Run test suite"),
            _observe("Review test results"),
        ],
    ),
    # --- Git operations ---
    (
        re.compile(r"commit|push|pull|merge|branch|rebase|提交|推送|合并", re.I),
        [
            _git("git status", "Check repo status"),
            _git("git add -A && git commit -m 'auto'", "Stage and commit"),
        ],
    ),
    # --- Install / Setup ---
    (
        re.compile(r"install|setup|init|bootstrap|pip|npm|yarn|安装|初始化", re.I),
        [
            _term("# install command", "Install dependencies"),
            _observe("Verify installation succeeded"),
        ],
    ),
    # --- Build ---
    (
        re.compile(r"build|compile|bundle|构建|编译|打包", re.I),
        [
            _term("# build command", "Build the project"),
            _test("# smoke test", "Smoke-test the build artifact"),
        ],
    ),
    # --- Read / Explore / Understand ---
    (
        re.compile(r"read|explore|understand|analyze|look|check|inspect|查看|分析|阅读", re.I),
        [
            _observe("Read and analyze the target files"),
        ],
    ),
]


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


class Planner:
    """
    Produces an ExecutionPlan for a given instruction.

    - plan(): auto-selects heuristic or LLM-based planning
    - plan_heuristic(): fast keyword-driven planning
    - plan_with_llm(): LLM-generated structured plan
    - replan_after_failure(): adjusts a plan after a step fails
    """

    def __init__(self, workspace: str = ".") -> None:
        self.workspace = workspace

    # ---- Main entry point --------------------------------------------------

    def plan(self, instruction: str, context: dict | None = None) -> ExecutionPlan:
        """
        Plan execution steps for an instruction using heuristic rules.

        For LLM-backed planning, call plan_with_llm() directly with an llm_fn.
        """
        ctx = context or {}
        return self.plan_heuristic(instruction, ctx)

    # ---- Heuristic planning ------------------------------------------------

    def plan_heuristic(self, instruction: str, context: dict) -> ExecutionPlan:
        """
        Keyword-driven step generation. Matches instruction against rule bank
        and assembles an ordered plan.
        """
        task_id = context.get("task_id", uuid.uuid4().hex[:12])
        steps: list[PlanStep] = []
        matched_actions: set[str] = set()

        for pattern, templates in _RULES:
            if pattern.search(instruction):
                for tmpl in templates:
                    # Avoid duplicate actions from overlapping rules
                    key = (tmpl["action"], tmpl["tool"])
                    if key in matched_actions:
                        continue
                    matched_actions.add(key)

                    step_id = len(steps) + 1
                    depends = [step_id - 1] if step_id > 1 else []
                    steps.append(
                        PlanStep(
                            step_id=step_id,
                            action=tmpl["action"],
                            description=tmpl["description"],
                            tool=tmpl["tool"],
                            params=dict(tmpl["params"]),
                            depends_on=depends,
                            timeout_sec=_timeout_for(tmpl["action"]),
                            retry_on_fail=tmpl["action"] in ("test", "terminal"),
                        )
                    )

        # Fallback: if no rules matched, produce a single observe step
        if not steps:
            steps.append(
                PlanStep(
                    step_id=1,
                    action="observe",
                    description="Analyze the instruction and determine next steps",
                    tool="terminal_tool.run",
                    params={"command": f"echo 'Analyzing: {instruction[:80]}'"},
                    depends_on=[],
                )
            )

        requires_browser = any(s.action in ("browser", "screenshot") for s in steps)
        requires_terminal = any(
            s.action in ("terminal", "test", "git") for s in steps
        )

        return ExecutionPlan(
            task_id=task_id,
            instruction=instruction,
            steps=steps,
            created_at=time.time(),
            estimated_duration_sec=sum(s.timeout_sec for s in steps) * 0.4,
            estimated_cost_usd=_estimate_cost(steps),
            requires_browser=requires_browser,
            requires_terminal=requires_terminal,
        )

    # ---- LLM-backed planning -----------------------------------------------

    def plan_with_llm(
        self,
        instruction: str,
        context: dict,
        llm_fn: Callable[..., str],
    ) -> ExecutionPlan:
        """
        Ask an LLM to generate a structured JSON plan, then parse it.

        Args:
            instruction: The user's task
            context: Workspace / file context
            llm_fn: Callable that accepts a prompt string and returns the
                     LLM's text response (e.g. ``openai_chat(prompt)``).
        """
        task_id = context.get("task_id", uuid.uuid4().hex[:12])
        prompt = _build_llm_prompt(instruction, context)

        raw = llm_fn(prompt)

        # Extract JSON from the response (tolerant of markdown fences)
        json_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", raw, re.S)
        if json_match:
            payload = json_match.group(1)
        else:
            # Try bare JSON array
            arr_match = re.search(r"\[.*\]", raw, re.S)
            payload = arr_match.group(0) if arr_match else "[]"

        try:
            raw_steps: list[dict] = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            logger.warning("LLM plan JSON parse failed; falling back to heuristic")
            return self.plan_heuristic(instruction, context)

        steps = _parse_llm_steps(raw_steps)
        if not steps:
            logger.warning("LLM returned empty plan; falling back to heuristic")
            return self.plan_heuristic(instruction, context)

        requires_browser = any(s.action in ("browser", "screenshot") for s in steps)
        requires_terminal = any(
            s.action in ("terminal", "test", "git") for s in steps
        )

        return ExecutionPlan(
            task_id=task_id,
            instruction=instruction,
            steps=steps,
            created_at=time.time(),
            estimated_duration_sec=sum(s.timeout_sec for s in steps) * 0.4,
            estimated_cost_usd=_estimate_cost(steps),
            requires_browser=requires_browser,
            requires_terminal=requires_terminal,
        )

    # ---- Replanning after failure ------------------------------------------

    def replan_after_failure(
        self,
        plan: ExecutionPlan,
        failed_step: int,
        error: str,
    ) -> ExecutionPlan:
        """
        Produce a revised plan after a step fails.

        Strategy:
        - Keep all completed steps (step_id < failed_step)
        - Replace the failed step with a diagnostic observe step
        - Re-attempt the failed step with adjusted params
        - Keep remaining steps, shifting IDs
        """
        completed = [s for s in plan.steps if s.step_id < failed_step]
        failed = next((s for s in plan.steps if s.step_id == failed_step), None)
        remaining = [s for s in plan.steps if s.step_id > failed_step]

        new_steps = list(completed)
        next_id = len(new_steps) + 1

        # Diagnostic step
        new_steps.append(
            PlanStep(
                step_id=next_id,
                action="observe",
                description=f"Diagnose failure in step {failed_step}: {error[:200]}",
                tool="terminal_tool.run",
                params={"command": f"echo 'Error: {error[:120]}'"},
                depends_on=[next_id - 1] if next_id > 1 else [],
            )
        )
        next_id += 1

        # Retry the failed step (if it exists)
        if failed:
            retry = PlanStep(
                step_id=next_id,
                action=failed.action,
                description=f"Retry: {failed.description}",
                tool=failed.tool,
                params=dict(failed.params),
                depends_on=[next_id - 1],
                timeout_sec=failed.timeout_sec * 1.5,
                retry_on_fail=False,  # no infinite retries
                max_retries=0,
            )
            new_steps.append(retry)
            next_id += 1

        # Remaining steps with shifted IDs
        for s in remaining:
            new_steps.append(
                PlanStep(
                    step_id=next_id,
                    action=s.action,
                    description=s.description,
                    tool=s.tool,
                    params=dict(s.params),
                    depends_on=[next_id - 1] if next_id > 1 else [],
                    timeout_sec=s.timeout_sec,
                    retry_on_fail=s.retry_on_fail,
                    max_retries=s.max_retries,
                )
            )
            next_id += 1

        return ExecutionPlan(
            task_id=plan.task_id,
            instruction=plan.instruction,
            steps=new_steps,
            created_at=time.time(),
            estimated_duration_sec=sum(s.timeout_sec for s in new_steps) * 0.4,
            estimated_cost_usd=_estimate_cost(new_steps),
            requires_browser=any(
                s.action in ("browser", "screenshot") for s in new_steps
            ),
            requires_terminal=any(
                s.action in ("terminal", "test", "git") for s in new_steps
            ),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_ACTION_TIMEOUTS: dict[str, float] = {
    "terminal": 120,
    "browser": 60,
    "screenshot": 30,
    "patch": 60,
    "test": 180,
    "git": 60,
    "observe": 30,
}


def _timeout_for(action: str) -> float:
    return _ACTION_TIMEOUTS.get(action, 120)


def _estimate_cost(steps: list[PlanStep]) -> float:
    """Very rough cost estimate (USD) based on step count and types."""
    base = 0.001  # per step baseline
    cost = 0.0
    for s in steps:
        if s.action in ("browser", "screenshot"):
            cost += base * 3  # browser steps are more expensive
        elif s.action == "test":
            cost += base * 2
        else:
            cost += base
    return round(cost, 4)


def _build_llm_prompt(instruction: str, context: dict) -> str:
    """Build a structured prompt for LLM-based plan generation."""
    workspace = context.get("workspace", ".")
    files = context.get("files", [])
    files_str = "\n".join(f"  - {f}" for f in files[:20]) if files else "  (none provided)"

    return f"""\
You are DevClaw's task planner. Given a task, produce an ordered JSON array of
execution steps.

Each step object MUST have these fields:
- "step_id": int (sequential starting at 1)
- "action": one of "terminal" | "browser" | "patch" | "test" | "screenshot" | "git" | "observe"
- "description": brief human-readable description
- "tool": tool method name (e.g. "terminal_tool.run", "browser_tool.navigate",
  "patch_tool.apply", "browser_tool.screenshot")
- "params": dict of tool parameters
- "depends_on": list of step_ids this step depends on ([] if none)
- "timeout_sec": float, execution timeout (default 120)
- "retry_on_fail": bool (default false)

Context:
  Workspace: {workspace}
  Files:
{files_str}

Task: {instruction}

Respond ONLY with the JSON array inside a ```json code fence. No extra commentary.
"""


def _parse_llm_steps(raw: list[dict]) -> list[PlanStep]:
    """Parse a list of dicts (from LLM JSON) into PlanStep objects."""
    steps: list[PlanStep] = []
    for i, entry in enumerate(raw, start=1):
        try:
            steps.append(
                PlanStep(
                    step_id=entry.get("step_id", i),
                    action=str(entry.get("action", "observe")),
                    description=str(entry.get("description", "")),
                    tool=str(entry.get("tool", "terminal_tool.run")),
                    params=entry.get("params", {}),
                    depends_on=entry.get("depends_on", []),
                    timeout_sec=float(entry.get("timeout_sec", 120)),
                    retry_on_fail=bool(entry.get("retry_on_fail", False)),
                    max_retries=int(entry.get("max_retries", 2)),
                )
            )
        except (TypeError, ValueError) as exc:
            logger.warning("Skipping malformed LLM step %d: %s", i, exc)
    return steps
