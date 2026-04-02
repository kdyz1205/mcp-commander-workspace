"""
Persistent JSONL task queue for TG / DevClaw: survives process restarts.

Used when SurvivalEngine pushes autonomy tasks (e.g. low fund estimate) without an in-memory queue.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_locks: dict[str, threading.Lock] = {}


def _lock_for(ws: Path) -> threading.Lock:
    key = str(ws.resolve())
    if key not in _locks:
        _locks[key] = threading.Lock()
    return _locks[key]


def persisted_tasks_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "persisted_tasks.jsonl"


def enqueue_persisted_task(
    workspace: Path,
    kind: str,
    text: str,
    meta: dict[str, Any] | None = None,
) -> None:
    ws = Path(workspace).resolve()
    p = persisted_tasks_path(ws)
    with _lock_for(ws):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": time.time(),
                "kind": kind,
                "text": (text or "")[:8000],
                "meta": meta or {},
            }
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


def pop_persisted_tasks(workspace: Path, max_n: int = 5) -> list[dict[str, Any]]:
    """Atomically remove up to max_n tasks from the head of the queue."""
    ws = Path(workspace).resolve()
    p = persisted_tasks_path(ws)
    with _lock_for(ws):
        if not p.is_file():
            return []
        try:
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            return []
        if not lines:
            return []
        take = lines[:max_n]
        rest = lines[max_n:]
        out: list[dict[str, Any]] = []
        for ln in take:
            try:
                o = json.loads(ln)
                if isinstance(o, dict):
                    out.append(o)
            except json.JSONDecodeError:
                continue
        try:
            p.write_text("\n".join(rest) + ("\n" if rest else ""), encoding="utf-8")
        except OSError:
            pass
        return out


def persisted_queue_depth(workspace: Path) -> int:
    p = persisted_tasks_path(Path(workspace).resolve())
    if not p.is_file():
        return 0
    try:
        return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        return 0
