"""
Backlog Manager — DevClaw's prefrontal cortex.

Reads EVOLUTION_BACKLOG.md, understands priority sections,
and selects the next task based on current survival mode.

Intelligence: TTL drives which task type (RESEARCH vs PROFIT) is picked.
Low TTL → PROFIT first (earn to survive).
High TTL → RESEARCH first (evolve to thrive).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _parse_backlog(backlog_path: str) -> list[dict[str, str]]:
    """Parse backlog markdown into structured task list with priorities."""
    try:
        content = Path(backlog_path).read_text(encoding="utf-8")
    except OSError:
        return []

    tasks: list[dict[str, str]] = []
    current_priority = "NORMAL"

    for line in content.split("\n"):
        line = line.strip()

        # Detect priority sections
        if "HIGH_PRIORITY" in line:
            current_priority = "HIGH"
        elif "NORMAL_PRIORITY" in line:
            current_priority = "NORMAL"
        elif "LOW_PRIORITY" in line:
            current_priority = "LOW"

        # Parse task lines: - [TYPE] description
        m = re.match(r"^- \[(RESEARCH|PROFIT)\]\s+(.*)", line)
        if m:
            tasks.append({
                "type": m.group(1),
                "description": m.group(2).strip(),
                "priority": current_priority,
                "raw_line": line,
            })

    return tasks


def get_next_task(backlog_path: str, mode: str = "RESEARCH") -> str | None:
    """Get highest-priority task matching the mode (RESEARCH or PROFIT)."""
    tasks = _parse_backlog(backlog_path)
    priority_order = {"HIGH": 0, "NORMAL": 1, "LOW": 2}

    matching = [
        t for t in tasks
        if t["type"] == mode and "[DONE]" not in t.get("raw_line", "")
    ]

    if not matching:
        return None

    matching.sort(key=lambda t: priority_order.get(t["priority"], 9))
    return matching[0]["description"]


def get_all_tasks(backlog_path: str) -> list[dict[str, str]]:
    """Get all tasks with priorities, sorted by priority."""
    tasks = _parse_backlog(backlog_path)
    priority_order = {"HIGH": 0, "NORMAL": 1, "LOW": 2}
    tasks.sort(key=lambda t: priority_order.get(t["priority"], 9))
    return tasks


def get_strategic_task(backlog_path: str, ttl: float) -> str | None:
    """
    TTL-aware task selection — the core intelligence.

    TTL > 30: RESEARCH first (evolve)
    7 < TTL <= 30: alternate PROFIT and RESEARCH
    TTL <= 7: PROFIT only (survive)
    """
    if ttl <= 7:
        # SURVIVAL: profit only
        task = get_next_task(backlog_path, "PROFIT")
        if task:
            return task
        return get_next_task(backlog_path, "RESEARCH")  # fallback

    elif ttl <= 30:
        # BALANCE: prefer profit slightly
        profit = get_next_task(backlog_path, "PROFIT")
        research = get_next_task(backlog_path, "RESEARCH")
        return profit or research

    else:
        # RESEARCH: evolve
        research = get_next_task(backlog_path, "RESEARCH")
        if research:
            return research
        return get_next_task(backlog_path, "PROFIT")


def mark_task_done(backlog_path: str, task_description: str) -> bool:
    """Mark a task as [DONE] in the backlog file."""
    p = Path(backlog_path)
    try:
        content = p.read_text(encoding="utf-8")
    except OSError:
        return False

    # Find and replace the task line
    for task_type in ("RESEARCH", "PROFIT"):
        old = f"- [{task_type}] {task_description}"
        new = f"- [DONE] [{task_type}] {task_description}"
        if old in content:
            content = content.replace(old, new)
            p.write_text(content, encoding="utf-8")
            return True

    return False
