"""Typed append-only memory under workspace `.claw/memory/<category>/`."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

VALID = frozenset(
    {"decision", "lesson", "person", "commitment", "preference", "fact", "project"}
)


def append_memory(workspace: Path, category: str, text: str) -> str:
    cat = category.strip().lower()
    if cat not in VALID:
        return f"无效 category，允许: {', '.join(sorted(VALID))}"

    root = workspace / ".claw" / "memory" / cat
    root.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = root / f"{day}.md"
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
    line = f"\n## {ts}\n{text.strip()}\n"
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)
    return f"已追加到 {path.relative_to(workspace)}"
