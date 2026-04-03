"""
Dimension 3 - Serialize state on shutdown and restore with environment awareness.
"""

from __future__ import annotations

import atexit
import json
import os
import platform
import signal
import socket
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class EnvironmentProfile:
    kind: str
    platform: str
    hostname: str
    cwd: str
    github_actions: bool
    ci: bool
    writable_workspace: bool
    restrictions: list[str]


def detect_environment(workspace: Path | str) -> EnvironmentProfile:
    workspace = Path(workspace).resolve()
    github_actions = bool(os.environ.get("GITHUB_ACTIONS"))
    ci = github_actions or bool(os.environ.get("CI"))
    restrictions: list[str] = []
    if github_actions:
        restrictions.append("ephemeral_runner")
    if ci:
        restrictions.append("non_interactive")
    if os.name == "nt":
        kind = "local_windows"
    elif github_actions:
        kind = "github_actions"
    elif ci:
        kind = "ci_runner"
    else:
        kind = "local_posix"

    writable = True
    try:
        probe = workspace / ".claw" / ".env_probe"
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError:
        writable = False
        restrictions.append("read_only_workspace")

    return EnvironmentProfile(
        kind=kind,
        platform=platform.platform(),
        hostname=socket.gethostname(),
        cwd=str(Path.cwd()),
        github_actions=github_actions,
        ci=ci,
        writable_workspace=writable,
        restrictions=restrictions,
    )


def build_environment_system_append(profile: EnvironmentProfile) -> str:
    if profile.github_actions:
        return (
            "Nomad restore detected GitHub Actions. Keep silent, avoid interactive prompts, "
            "and focus on core serialization, tests, and artifact generation only."
        )
    if profile.ci:
        return (
            "Nomad restore detected a constrained CI runner. Avoid long-lived background loops "
            "and prefer deterministic validation plus compact summaries."
        )
    if profile.kind == "local_windows":
        return (
            "Nomad restore detected local Windows. Interactive tooling is available; keep "
            "human-facing explanations concise and preserve workspace artifacts."
        )
    return "Nomad restore detected a local environment. Prefer full repair loops with minimal surprise."


def write_nomad_snapshot(workspace: Path, extra: dict[str, Any] | None = None) -> Path:
    workspace = Path(workspace).resolve()
    from claw_runtime.survival_engine import SurvivalEngine

    eng = SurvivalEngine(workspace)
    snap = eng.snapshot()
    payload = {
        "ts": time.time(),
        "survival": snap,
        "environment": asdict(detect_environment(workspace)),
        "open_tasks_hint": os.environ.get("NOMAD_OPEN_TASKS", ""),
        "extra": extra or {},
    }
    out = workspace / ".claw" / "nomad_snapshot.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    return out


def restore_nomad_identity(workspace: Path | str) -> dict[str, Any]:
    workspace = Path(workspace).resolve()
    snapshot_path = workspace / ".claw" / "nomad_snapshot.json"
    previous: dict[str, Any] = {}
    if snapshot_path.is_file():
        try:
            loaded = json.loads(snapshot_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                previous = loaded
        except (OSError, json.JSONDecodeError):
            previous = {}

    current = detect_environment(workspace)
    system_append = build_environment_system_append(current)
    payload = {
        "restored_at": time.time(),
        "current_environment": asdict(current),
        "previous_environment": previous.get("environment", {}),
        "system_append": system_append,
    }
    out = workspace / ".claw" / "nomad_environment.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return payload


def load_nomad_system_append(workspace: Path | str) -> str | None:
    workspace = Path(workspace).resolve()
    env_path = workspace / ".claw" / "nomad_environment.json"
    if not env_path.is_file():
        if not (workspace / ".claw" / "nomad_snapshot.json").is_file():
            return None
        return restore_nomad_identity(workspace).get("system_append")  # type: ignore[return-value]
    try:
        data = json.loads(env_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return restore_nomad_identity(workspace).get("system_append")  # type: ignore[return-value]
    value = data.get("system_append")
    return str(value) if value else None


def _git_push_snapshot(workspace: Path) -> None:
    if os.environ.get("NOMAD_GIT_PUSH", "").strip().lower() not in {"1", "true", "yes"}:
        return
    delay = float(os.environ.get("NOMAD_SHUTDOWN_DELAY_SEC", "2") or "2")
    if delay > 0:
        time.sleep(min(delay, 10.0))
    try:
        add_args = ["git", "add", "-f", ".claw/nomad_snapshot.json", ".claw/nomad_environment.json"]
        if os.environ.get("NOMAD_GIT_PUSH_SESSION_MEMORY", "").strip().lower() in {"1", "true", "yes"}:
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


def emergency_cold_backup(workspace: Path, *, notify: Callable[[str], None] | None = None) -> Path | None:
    """
    Emergency cold backup: zip critical state files and push to remote.

    Called during CRITICAL survival state or before shutdown.
    Preserves: memory, task queue, survival state, session logs, skills.
    """
    import zipfile

    workspace = Path(workspace).resolve()
    ts = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    backup_dir = workspace / ".claw" / "cold_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    zip_path = backup_dir / f"emergency_backup_{ts}.zip"

    critical_patterns = [
        ".claw/survival_state.json",
        ".claw/runtime_control.json",
        ".claw/smart_task_registry.jsonl",
        ".claw/persisted_tasks.jsonl",
        ".claw/nomad_snapshot.json",
        ".claw/nomad_environment.json",
        ".claw/meta_tick_log.jsonl",
        ".claw/evolution_failures.jsonl",
        ".claw/quota_tracker.json",
        ".claw/fund_estimate.json",
    ]

    try:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            # Add critical state files
            for pattern in critical_patterns:
                p = workspace / pattern
                if p.is_file():
                    zf.write(p, pattern)

            # Add memory directory
            mem_dir = workspace / ".claw" / "memory"
            if mem_dir.is_dir():
                for f in mem_dir.rglob("*.md"):
                    rel = f.relative_to(workspace)
                    zf.write(f, str(rel))

            # Add latest session log
            sessions_dir = workspace / ".claw" / "sessions"
            if sessions_dir.is_dir():
                session_files = sorted(sessions_dir.glob("*.jsonl"))
                for sf in session_files[-3:]:  # last 3 days
                    rel = sf.relative_to(workspace)
                    zf.write(sf, str(rel))

            # Add task_plan.md
            tp = workspace / "task_plan.md"
            if tp.is_file():
                zf.write(tp, "task_plan.md")

        if notify:
            size_kb = zip_path.stat().st_size / 1024
            notify(f"[Nomad] 紧急冷备份已创建: {zip_path.name} ({size_kb:.1f} KB)")

        return zip_path

    except Exception as exc:
        if notify:
            notify(f"[Nomad] 冷备份失败: {exc!s}")
        return None


def restore_from_cold_backup(workspace: Path, backup_path: Path) -> dict[str, Any]:
    """
    Restore state from a cold backup zip file.

    Returns dict with restored file count and any errors.
    """
    import zipfile

    workspace = Path(workspace).resolve()
    restored = 0
    errors: list[str] = []

    try:
        with zipfile.ZipFile(backup_path, "r") as zf:
            for info in zf.infolist():
                target = workspace / info.filename
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        dst.write(src.read())
                    restored += 1
                except OSError as e:
                    errors.append(f"{info.filename}: {e!s}")
    except Exception as exc:
        errors.append(f"zip open failed: {exc!s}")

    return {
        "restored_files": restored,
        "errors": errors,
        "backup_path": str(backup_path),
    }


def register_nomad_handlers(workspace: Path, *, on_snapshot: Callable[[Path], None] | None = None) -> None:
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
        restore_nomad_identity(workspace)
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
