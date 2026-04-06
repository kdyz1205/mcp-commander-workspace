"""
Dream State — idle-time self-audit and improvement.

During idle periods, DevClaw enters "dream state":
1. Scans error logs for recurring patterns
2. Checks for incomplete tasks in EVOLUTION_BACKLOG
3. Generates focused improvement prompts
4. Injects fix tasks into the task queue
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path


def _scan_error_logs(workspace: Path, max_entries: int = 50) -> list[dict]:
    """Read recent error entries from .claw logs."""
    errors = []
    log_files = [
        workspace / ".claw" / "heal_log.jsonl",
        workspace / ".claw" / "pain_log.jsonl",
        workspace / ".claw" / "evolution_failures.jsonl",
    ]
    for lf in log_files:
        if not lf.is_file():
            continue
        try:
            lines = lf.read_text(encoding="utf-8").strip().split("\n")
            for line in lines[-max_entries:]:
                if line.strip():
                    try:
                        errors.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        except Exception:
            pass
    return errors


def _get_incomplete_tasks(workspace: Path) -> list[str]:
    """Extract incomplete tasks from EVOLUTION_BACKLOG.md."""
    backlog = workspace / "EVOLUTION_BACKLOG.md"
    if not backlog.is_file():
        return []
    try:
        content = backlog.read_text(encoding="utf-8")
        tasks = []
        for line in content.split("\n"):
            line = line.strip()
            if line.startswith("- [ ]"):
                tasks.append(line[6:].strip())
        return tasks[:20]
    except Exception:
        return []


def inject_dream_tasks(workspace: Path) -> list[str]:
    """
    Analyze error logs and backlog, generate actionable fix tasks.
    Returns list of task descriptions that were injected.
    """
    ws = Path(workspace).resolve()
    injected = []

    # Scan for recurring errors
    errors = _scan_error_logs(ws)
    if not errors:
        return injected

    # Group errors by type
    error_types: dict[str, int] = {}
    for e in errors:
        kind = e.get("kind") or e.get("error_type") or "unknown"
        error_types[kind] = error_types.get(kind, 0) + 1

    # Generate fix tasks for recurring errors (appeared 3+ times)
    backlog = ws / "EVOLUTION_BACKLOG.md"
    if not backlog.is_file():
        return injected

    try:
        bl_text = backlog.read_text(encoding="utf-8")
    except Exception:
        return injected

    for kind, count in error_types.items():
        if count >= 3:
            task = f"[DREAM] 修复反复出现的错误: {kind} (出现{count}次)"
            if task not in bl_text:
                bl_text = bl_text.replace(
                    "## Active Tasks\n",
                    f"## Active Tasks\n- [ ] {task}\n",
                )
                injected.append(task)

    if injected:
        try:
            backlog.write_text(bl_text, encoding="utf-8")
        except Exception:
            pass

    return injected


def generate_dream_prompt(workspace: Path) -> str:
    """
    Generate a focused self-improvement prompt based on recent state.
    Used by the idle autotick loop to run targeted improvements.
    """
    ws = Path(workspace).resolve()

    # Get incomplete tasks
    tasks = _get_incomplete_tasks(ws)

    # Get recent errors
    errors = _scan_error_logs(ws, max_entries=10)

    prompt_parts = ["自检系统状态并尝试修复发现的问题。"]

    if errors:
        recent_errors = [
            e.get("detail", e.get("text", str(e)))[:100]
            for e in errors[-3:]
        ]
        prompt_parts.append(
            "最近的错误:\n" + "\n".join(f"- {e}" for e in recent_errors)
        )

    if tasks:
        prompt_parts.append(
            "待办任务 (选1-2个执行):\n" + "\n".join(f"- {t}" for t in tasks[:5])
        )

    if not errors and not tasks:
        prompt_parts.append("没有已知错误。检查代码质量，运行测试，确保所有技能正常工作。")

    return "\n\n".join(prompt_parts)
