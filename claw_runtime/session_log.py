"""Append JSON lines for each tool invocation (audit / replay)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def log_tool(workspace: Path, iteration: int, name: str, args: dict[str, Any], result_preview: str) -> None:
    root = workspace / ".claw" / "sessions"
    root.mkdir(parents=True, exist_ok=True)
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = root / f"{day}.jsonl"
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "iteration": iteration,
        "tool": name,
        "args": args,
        "result_chars": len(result_preview),
        "result_preview": result_preview[:2000],
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
