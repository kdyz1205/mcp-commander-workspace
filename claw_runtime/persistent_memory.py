"""
Persistent Conversation Memory — survives process restarts.

Stores conversation history per chat_id on disk as JSON.
Loaded at startup, saved after each exchange.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def _history_dir(workspace: Path) -> Path:
    d = Path(workspace).resolve() / ".claw" / "conversations"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _history_file(workspace: Path, chat_id: int) -> Path:
    return _history_dir(workspace) / f"chat_{chat_id}.json"


def save_history(
    workspace: str | Path,
    chat_id: int,
    history: list[dict[str, Any]],
    max_entries: int = 50,
) -> None:
    """Save conversation history for a chat_id, capped at max_entries."""
    ws = Path(workspace).resolve()
    path = _history_file(ws, chat_id)
    # Keep only latest entries
    trimmed = history[-max_entries:] if len(history) > max_entries else history
    try:
        path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def load_history(
    workspace: str | Path,
    chat_id: int,
) -> list[dict[str, Any]]:
    """Load conversation history for a chat_id from disk."""
    ws = Path(workspace).resolve()
    path = _history_file(ws, chat_id)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []
