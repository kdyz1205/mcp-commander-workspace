"""
Dimension 3 — Serialize state on shutdown; optional git commit for GitHub Actions resume (user-owned repo).

Does not embed secrets in snapshot by default.
"""

from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable


def write_nomad_snapshot(workspace: Path, extra: dict[str, Any] | None = None) -> Path:
    workspace = Path(workspace).resolve()
    from claw_runtime.survival_engine import SurvivalEngine

    eng = SurvivalEngine(workspace)
    snap = eng.snapshot()
    payload = {
        "ts": time.time(),
        "survival": snap,
        "open_tasks_hint": os.environ.get("NOMAD_OPEN_TASKS", ""),
        "extra": extra or {},
    }
    out = workspace / ".claw" / "nomad_snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return out


def _git_push_snapshot(workspace: Path) -> None:
    if os.environ.get("NOMAD_GIT_PUSH", "").strip().lower() not in {"1", "true", "yes"}:
        return
    delay = float(os.environ.get("NOMAD_SHUTDOWN_DELAY_SEC", "2") or "2")
    if delay > 0:
        time.sleep(min(delay, 10.0))
    try:
        add_args = ["git", "add", "-f", ".claw/nomad_snapshot.json"]
        if os.environ.get("NOMAD_GIT_PUSH_SESSION_MEMORY", "").strip().lower() in {"1", "true", "yes"}:
            # .claw/ is usually gitignored; -f forces session/memory artifacts for Actions resume
            for rel in (".claw/sessions", ".claw/memory", ".claw/meta_tick_log.jsonl"):
                p = workspace / rel
                if p.exists():
                    add_args.extend(["-f", rel])
        subprocess.run(
            add_args,
            cwd=str(workspace),
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        subprocess.run(
            ["git", "commit", "-m", "chore(nomad): survival snapshot (auto)"],
            cwd=str(workspace),
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        subprocess.run(
            ["git", "push"],
            cwd=str(workspace),
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


def register_nomad_handlers(workspace: Path, *, on_snapshot: Callable[[Path], None] | None = None) -> None:
    """
    Register atexit + SIGTERM/SIGINT hooks. Windows: SIGBREAK if present.
    """
    workspace = Path(workspace).resolve()
    lock = threading.Lock()
    fired = False

    def _run() -> None:
        nonlocal fired
        with lock:
            if fired:
                return
            fired = True
        path = write_nomad_snapshot(workspace)
        if on_snapshot:
            try:
                on_snapshot(path)
            except Exception:
                pass
        _git_push_snapshot(workspace)

    if os.environ.get("NOMAD_ATEXIT_SNAPSHOT", "").strip().lower() in {"1", "true", "yes"}:
        atexit.register(_run)

    def _sig_handler(_signum: int, _frame: Any) -> None:
        _run()
        sys.exit(128 + (_signum if _signum > 0 else 0))

    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None), getattr(signal, "SIGBREAK", None)):
        if sig is None:
            continue
        try:
            signal.signal(sig, _sig_handler)
        except (OSError, ValueError):
            pass
