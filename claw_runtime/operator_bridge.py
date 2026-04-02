"""Local operator inbox/outbox for live bot integration tests."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

_locks: dict[str, threading.Lock] = {}


def _lock_for(workspace: Path) -> threading.Lock:
    key = str(workspace.resolve())
    if key not in _locks:
        _locks[key] = threading.Lock()
    return _locks[key]


def operator_inbox_path(workspace: Path | str) -> Path:
    return Path(workspace).resolve() / ".claw" / "operator_inbox.jsonl"


def operator_outbox_path(workspace: Path | str) -> Path:
    return Path(workspace).resolve() / ".claw" / "operator_outbox.jsonl"


def clear_operator_mailbox(workspace: Path | str) -> None:
    ws = Path(workspace).resolve()
    for path in (operator_inbox_path(ws), operator_outbox_path(ws)):
        with _lock_for(ws):
            try:
                path.unlink()
            except OSError:
                pass


def enqueue_operator_message(
    workspace: Path | str,
    text: str,
    *,
    chat_id: int = 0,
    source: str = "local",
    request_id: str | None = None,
) -> str:
    ws = Path(workspace).resolve()
    request_id = request_id or f"req-{time.time_ns()}"
    path = operator_inbox_path(ws)
    with _lock_for(ws):
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": request_id,
            "ts": time.time(),
            "chat_id": int(chat_id),
            "source": source,
            "text": (text or "")[:8000],
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return request_id


def append_operator_reply(
    workspace: Path | str,
    *,
    request_id: str,
    chat_id: int,
    text: str,
    kind: str = "reply",
    meta: dict[str, Any] | None = None,
) -> None:
    ws = Path(workspace).resolve()
    path = operator_outbox_path(ws)
    with _lock_for(ws):
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "id": request_id,
            "ts": time.time(),
            "chat_id": int(chat_id),
            "kind": kind,
            "text": (text or "")[:16000],
            "meta": meta or {},
        }
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def pop_operator_messages(workspace: Path | str, *, max_n: int = 10) -> list[dict[str, Any]]:
    ws = Path(workspace).resolve()
    path = operator_inbox_path(ws)
    with _lock_for(ws):
        if not path.is_file():
            return []
        try:
            lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        except OSError:
            return []
        if not lines:
            return []
        take = lines[:max_n]
        rest = lines[max_n:]
        out: list[dict[str, Any]] = []
        for line in take:
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                out.append(payload)
        try:
            path.write_text("\n".join(rest) + ("\n" if rest else ""), encoding="utf-8")
        except OSError:
            pass
        return out


def read_operator_outbox(workspace: Path | str) -> list[dict[str, Any]]:
    ws = Path(workspace).resolve()
    path = operator_outbox_path(ws)
    if not path.is_file():
        return []
    try:
        lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            out.append(payload)
    return out


def wait_for_operator_output(
    workspace: Path | str,
    request_id: str,
    *,
    min_messages: int = 1,
    timeout_sec: float = 30.0,
) -> list[dict[str, Any]]:
    ws = Path(workspace).resolve()
    deadline = time.time() + max(timeout_sec, 1.0)
    while time.time() < deadline:
        matches = [item for item in read_operator_outbox(ws) if str(item.get("id")) == request_id]
        if len(matches) >= min_messages:
            return matches
        time.sleep(0.5)
    return [item for item in read_operator_outbox(ws) if str(item.get("id")) == request_id]
