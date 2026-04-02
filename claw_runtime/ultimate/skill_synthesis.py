"""
Dimension 2 — Skill hybrid: merge two SKILL.md into a new draft skill + stub Python module.
"""

from __future__ import annotations

import re
from pathlib import Path

from claw_runtime.skill_registry import SkillRegistry


def _slug(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return s or "synth"


def synthesize_two_skills(
    workspace: Path,
    skill_a: str,
    skill_b: str,
    out_skill_name: str | None = None,
) -> tuple[Path, str]:
    workspace = Path(workspace).resolve()
    reg = SkillRegistry(workspace)
    a = reg.get(skill_a.strip())
    b = reg.get(skill_b.strip())
    if not a or not b:
        missing = []
        if not a:
            missing.append(skill_a)
        if not b:
            missing.append(skill_b)
        raise ValueError(f"Skills not found: {', '.join(missing)}")

    name = out_skill_name or f"synth_{_slug(skill_a)}_{_slug(skill_b)}"
    name = _slug(name)
    skill_dir = workspace / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)

    md = f"""---
name: {name}
description: Synthesized from `{a.name}` + `{b.name}` — review before production use.
metadata: {{"openclaw": {{"always": false}}}}
---

## Lineage

- Parent A: **{a.name}** — {a.description}
- Parent B: **{b.name}** — {b.description}

## Combined playbook (agent-facing)

### From {a.name}

{a.body[:6000]}

### From {b.name}

{b.body[:6000]}

## Stub runner

Call `execute_terminal`: `py skills\\\\{name}\\\\runner.py --task "..."` (extend this file).
"""

    py = f'''# Auto-synthesized stub — implement real logic after review.
from __future__ import annotations

import argparse


def run(task: str) -> str:
    return (
        "synth:{name}: combine concerns from {a.name} + {b.name}. "
        "Task preview: " + (task[:200] or "(empty)")
    )


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--task", default="")
    args = p.parse_args()
    print(run(args.task))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(md, encoding="utf-8")
    (skill_dir / "runner.py").write_text(py, encoding="utf-8")
    msg = f"Wrote {skill_md} and runner.py"
    return skill_md, msg
