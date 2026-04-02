"""
Sleep-cycle digest: append failures to a log; optionally materialize a draft SKILL stub.

Policy-safe: does not execute arbitrary network fetches unless you run LLM yourself.
"""

from __future__ import annotations

import json
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


def materialize_draft_skill(workspace: Path, *, max_lines: int = 80) -> Path | None:
    """
    Create skills/auto_evolve_<unix>/SKILL.md from recent failure lines (template only).
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
    body = f"""---
name: {name}
description: Auto-generated survival digest from recent failures (review before trusting).
metadata: {{"openclaw":{{"always":false}}}}
---

## Origin

Materialized by `claw_runtime.nightly_evolution.materialize_draft_skill`. **Human review required.**

## Recent pain signals

{bullets}

## Next agent steps

1. Cluster duplicates; pick one root cause.
2. Propose a minimal fix or a new skill under `skills/<real_name>/`.
3. Run tests / safety_scan_file on any new script.
"""
    out = skill_dir / "SKILL.md"
    try:
        out.write_text(body, encoding="utf-8")
    except OSError:
        return None
    return out


def main() -> int:
    import argparse
    import os

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
