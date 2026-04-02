"""
Sleep-cycle digest: append failures to a log; optionally materialize a draft SKILL stub.

Compares with task_plan.md reasoning; optional pytest gate to promote draft over a target skill (HEALTHY only).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def evolution_log_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "evolution_failures.jsonl"


def append_evolution_failure(workspace: Path, *, kind: str, detail: str) -> None:
    p = evolution_log_path(workspace)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": time.time(), "kind": kind, "detail": (detail or "")[:4000]},
            ensure_ascii=False,
        )
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def _read_tail_jsonl(path: Path, max_lines: int = 200) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out


def task_plan_reasoning_excerpt(workspace: Path, *, max_chars: int = 3500) -> str:
    """Tail of task_plan.md — aligns materialized skills with on-file meta-reasoning."""
    p = Path(workspace).resolve() / "task_plan.md"
    if not p.is_file():
        return "(no task_plan.md)"
    try:
        raw = p.read_text(encoding="utf-8")
    except OSError:
        return "(task_plan.md unreadable)"
    raw = raw.strip()
    if not raw:
        return "(task_plan.md empty)"
    # Prefer sections that look like reasoning chains
    lowered = raw.lower()
    idx = lowered.rfind("[现状]")
    if idx == -1:
        idx = lowered.rfind("## ")
    chunk = raw[idx:] if idx >= 0 else raw
    if len(chunk) > max_chars:
        chunk = "…\n" + chunk[-max_chars:]
    return chunk


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def maybe_promote_draft_skill(workspace: Path, draft_skill_dir: Path) -> Path | None:
    """
    If AUTO_EVOLVE_PYTEST_PROMOTE=1, AUTO_EVOLVE_PYTEST_CMD is set, and host is HEALTHY:
    run pytest shell command; on success copy draft SKILL.md over skills/<target>/SKILL.md.
    """
    if not _env_truthy("AUTO_EVOLVE_PYTEST_PROMOTE"):
        return None
    cmd = os.environ.get("AUTO_EVOLVE_PYTEST_CMD", "").strip()
    if not cmd:
        return None
    ws = Path(workspace).resolve()
    draft_skill_dir = Path(draft_skill_dir).resolve()
    src = draft_skill_dir / "SKILL.md"
    if not src.is_file():
        return None

    from claw_runtime.survival_engine import SurvivalEngine, SurvivalState

    eng = SurvivalEngine(ws)
    if eng.assess_survival_state()[0] != SurvivalState.HEALTHY:
        return None

    try:
        cp = subprocess.run(
            cmd,
            shell=True,
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("AUTO_EVOLVE_PYTEST_TIMEOUT_SEC", "600") or "600"),
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None
    if cp.returncode != 0:
        append_evolution_failure(
            ws,
            kind="pytest_promote_fail",
            detail=(cp.stdout or "")[:2000] + "\n" + (cp.stderr or "")[:2000],
        )
        return None

    target = os.environ.get("AUTO_EVOLVE_PROMOTE_TARGET", "trading_evolved").strip() or "trading_evolved"
    dest_dir = ws / "skills" / target
    dest = dest_dir / "SKILL.md"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        if dest.is_file():
            bak = dest_dir / f"SKILL.md.bak.{int(time.time())}"
            shutil.copy2(dest, bak)
        shutil.copy2(src, dest)
    except OSError:
        return None
    return dest


def materialize_draft_skill(workspace: Path, *, max_lines: int = 80) -> Path | None:
    """
    Create skills/auto_evolve_<unix>/SKILL.md from recent failure lines + task_plan excerpt.
    Optionally run pytest and promote into skills/<AUTO_EVOLVE_PROMOTE_TARGET>/.
    """
    workspace = Path(workspace).resolve()
    failures = _read_tail_jsonl(evolution_log_path(workspace), max_lines=max_lines)
    if not failures:
        return None
    name = f"auto_evolve_{int(time.time())}"
    skill_dir = workspace / "skills" / name
    try:
        skill_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    bullets = "\n".join(
        f"- ({f.get('kind', '?')}) {(f.get('detail') or '')[:200]}"
        for f in failures[-20:]
    )
    tpe = task_plan_reasoning_excerpt(workspace, max_chars=3200).replace("```", "\n~~~\n")
    body = (
        f"---\nname: {name}\n"
        "description: Auto-generated survival digest from recent failures + task_plan (review before trusting).\n"
        'metadata: {"openclaw":{"always":false}}\n'
        "---\n\n"
        "## Origin\n\n"
        "Materialized by `claw_runtime.nightly_evolution.materialize_draft_skill`. "
        "**Human review required** unless pytest promote succeeded.\n\n"
        "## task_plan.md reasoning (excerpt)\n\n"
        "```text\n"
        f"{tpe}\n"
        "```\n\n"
        "## Recent pain signals\n\n"
        f"{bullets}\n\n"
        "## Next agent steps\n\n"
        "1. Cluster duplicates; pick one root cause.\n"
        "2. Align with task_plan [现状]→[错误归因]→[学习调研]→[重构方案]→[实验验证].\n"
        "3. Propose a minimal fix or merge into `skills/trading/` (or promote target).\n"
        "4. Run tests / safety_scan_file on any new script.\n"
    )
    out = skill_dir / "SKILL.md"
    try:
        out.write_text(body, encoding="utf-8")
    except OSError:
        return None

    promoted = maybe_promote_draft_skill(workspace, skill_dir)
    if promoted is not None:
        try:
            append_evolution_failure(
                workspace,
                kind="skill_promoted",
                detail=f"Draft {name} promoted to {promoted} after pytest.",
            )
        except Exception:
            pass

    return out


def main() -> int:
    import argparse

    p = argparse.ArgumentParser(description="Nightly evolution: draft SKILL from failure log")
    p.add_argument(
        "--workspace",
        default=os.environ.get("DEVCLAW_WORKSPACE", "."),
        help="Workspace root",
    )
    args = p.parse_args()
    ws = Path(args.workspace).resolve()
    path = materialize_draft_skill(ws)
    if path:
        print(path)
        return 0
    print("No failures logged; nothing to materialize.", file=__import__("sys").stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
