"""
Dimension 2 - Skill hybrid: merge two SKILL.md files into a reviewed skill package.
"""

from __future__ import annotations

import re
import textwrap
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
    rationale: str | None = None,
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
    tests_dir = skill_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)

    rationale_text = rationale or (
        f"Fuse `{a.name}` and `{b.name}` because the requested workflow spans both skill lineages "
        "and the agent should preserve an explicit reasoning trail before materializing a new capability."
    )

    md = f"""---
name: {name}
description: Synthesized from `{a.name}` + `{b.name}` - review before production use.
metadata: {{"openclaw": {{"always": false}}}}
---

## Lineage

- Parent A: **{a.name}** - {a.description}
- Parent B: **{b.name}** - {b.description}

## Why This Merge Exists

{rationale_text}

## Combined playbook (agent-facing)

### From {a.name}

{a.body[:6000]}

### From {b.name}

{b.body[:6000]}

## Runner

Call `execute_terminal`: `py skills\\\\{name}\\\\runner.py --task "..."`.
The runner returns structured lineage, rationale, and next-step hints.
"""

    runner = f'''from __future__ import annotations

import argparse
import json


def run(task: str) -> str:
    payload = {{
        "skill": "{name}",
        "parents": ["{a.name}", "{b.name}"],
        "task_preview": (task[:200] or "(empty)"),
        "rationale": {rationale_text!r},
        "next_steps": [
            "Read the combined SKILL.md before taking action.",
            "Prefer minimal experiments before promoting this skill to production.",
            "Extend runner.py with domain-specific behavior only after tests are updated.",
        ],
    }}
    return json.dumps(payload, ensure_ascii=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task", default="")
    args = parser.parse_args()
    print(run(args.task))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

    rationale_md = textwrap.dedent(
        f"""\
        # Synthesis Rationale

        - Output skill: `{name}`
        - Parent A: `{a.name}`
        - Parent B: `{b.name}`

        ## Logic

        {rationale_text}
        """
    )

    test_py = textwrap.dedent(
        f"""\
        from __future__ import annotations

        import json

        from skills.{name}.runner import run


        def test_{name}_runner_reports_lineage() -> None:
            payload = json.loads(run("sample task"))
            assert payload["skill"] == "{name}"
            assert payload["parents"] == ["{a.name}", "{b.name}"]
            assert payload["rationale"]
        """
    )

    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(md, encoding="utf-8")
    (skill_dir / "runner.py").write_text(runner, encoding="utf-8")
    (skill_dir / "RATIONALE.md").write_text(rationale_md, encoding="utf-8")
    (tests_dir / f"test_{name}.py").write_text(test_py, encoding="utf-8")
    msg = f"Wrote {skill_md}, runner.py, RATIONALE.md, and pytest scaffold"
    return skill_md, msg
