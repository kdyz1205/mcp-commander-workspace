"""Sequential multi-phase DevClaw runs (planner → builder → auditor)."""

from __future__ import annotations

from pathlib import Path


def run_phased_pipeline(
    workspace: Path,
    instruction: str,
    *,
    phases: list[tuple[str, str, int]] | None = None,
) -> None:
    """Each phase is (title, system_append, max_iterations)."""
    if phases is None:
        phases = [
            (
                "规划",
                "阶段目标：只维护 task_plan.md、列出可验证子任务、用终端做只读探查。"
                "不要实现核心业务逻辑。结束用 5 行内总结。",
                8,
            ),
            (
                "实施",
                "阶段目标：按 task_plan.md 写代码、跑测试与修复；可改 tools/、temp_tools/。",
                22,
            ),
            (
                "审计",
                "阶段目标：安全与边界（密钥、命令注入、路径穿越）；"
                "必要时 append_typed_memory lesson；不改无关大段代码。",
                10,
            ),
        ]

    from dev_claw.main import dev_claw_run

    buf = instruction.strip()
    for title, append, iters in phases:
        dev_claw_run(
            f"[多Agent编排 — {title}]\n\n原始需求:\n{buf}",
            max_iterations=iters,
            system_append=append,
        )
