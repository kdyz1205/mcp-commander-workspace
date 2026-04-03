"""gene_splicer — Auto-detect complex needs, merge existing skills into hybrid skills.

Template-based skill merging: reads two parent skills, composes a new SKILL.md + runner.py,
and registers the result in the workspace skill directory.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SkillInfo:
    """Metadata for a single registered skill."""

    name: str
    description: str
    path: str
    files: List[str] = field(default_factory=list)
    has_runner: bool = False

    def capability_keywords(self) -> List[str]:
        """Return lowercased keywords extracted from name + description."""
        text = f"{self.name} {self.description}"
        # Strip markdown / punctuation, split on whitespace
        tokens = re.findall(r"[a-zA-Z0-9_]+", text.lower())
        # Remove very short / stop-ish words
        return [t for t in tokens if len(t) > 2]


@dataclass
class SkillGap:
    """Description of a missing capability."""

    task_summary: str
    missing_keywords: List[str]
    best_matches: List[str]
    gap_description: str


# ---------------------------------------------------------------------------
# 1. scan_skill_registry
# ---------------------------------------------------------------------------

def scan_skill_registry(workspace: str) -> List[SkillInfo]:
    """Walk ``skills/`` and return metadata for every skill found.

    Parameters
    ----------
    workspace:
        Root of the project workspace (the directory that contains ``skills/``).

    Returns
    -------
    list[SkillInfo]
        One entry per skill directory that contains at least a ``SKILL.md``.
    """
    skills_dir = Path(workspace) / "skills"
    results: List[SkillInfo] = []

    if not skills_dir.is_dir():
        return results

    for entry in sorted(skills_dir.iterdir()):
        if not entry.is_dir():
            continue
        skill_md = entry / "SKILL.md"
        if not skill_md.exists():
            continue

        name = entry.name
        description = _extract_frontmatter_field(skill_md, "description") or ""
        files = [f.name for f in entry.iterdir() if f.is_file()]
        has_runner = (entry / "runner.py").exists()

        results.append(
            SkillInfo(
                name=name,
                description=description,
                path=str(entry),
                files=files,
                has_runner=has_runner,
            )
        )

    return results


# ---------------------------------------------------------------------------
# 2. identify_skill_gap
# ---------------------------------------------------------------------------

def identify_skill_gap(
    task_instruction: str,
    existing_skills: List[SkillInfo],
) -> Dict[str, Any]:
    """Analyse *task_instruction* against *existing_skills* and describe what is missing.

    Parameters
    ----------
    task_instruction:
        Free-text description of the task the user wants to accomplish.
    existing_skills:
        Output of :func:`scan_skill_registry`.

    Returns
    -------
    dict
        Serialisable :class:`SkillGap` with ``task_summary``, ``missing_keywords``,
        ``best_matches`` (top-2 skill names), and ``gap_description``.
    """
    task_tokens = set(re.findall(r"[a-zA-Z0-9_]+", task_instruction.lower()))
    task_tokens = {t for t in task_tokens if len(t) > 2}

    # Score each skill by keyword overlap with the task
    scored: List[Tuple[float, SkillInfo]] = []
    covered_tokens: set[str] = set()
    for skill in existing_skills:
        kw = set(skill.capability_keywords())
        overlap = task_tokens & kw
        score = len(overlap)
        if score > 0:
            scored.append((score, skill))
            covered_tokens |= overlap

    scored.sort(key=lambda x: x[0], reverse=True)
    best_matches = [s.name for _, s in scored[:2]]
    missing = sorted(task_tokens - covered_tokens)

    gap = SkillGap(
        task_summary=task_instruction[:200],
        missing_keywords=missing,
        best_matches=best_matches,
        gap_description=(
            f"No single skill covers the full task. "
            f"Best partial matches: {best_matches}. "
            f"Uncovered keywords: {missing[:10]}."
        ),
    )
    return asdict(gap)


# ---------------------------------------------------------------------------
# 3. splice_skills
# ---------------------------------------------------------------------------

def splice_skills(
    workspace: str,
    skill_a_name: str,
    skill_b_name: str,
    new_name: str,
    merge_instruction: str = "",
) -> str:
    """Merge two existing skills into a new hybrid skill on disk.

    Parameters
    ----------
    workspace:
        Project root containing ``skills/``.
    skill_a_name, skill_b_name:
        Directory names of the two parent skills.
    new_name:
        Directory name for the newly created skill.
    merge_instruction:
        Optional human guidance on *how* to combine the two parents.

    Returns
    -------
    str
        Absolute path to the new skill directory.

    Raises
    ------
    FileNotFoundError
        If either parent skill directory or its ``SKILL.md`` does not exist.
    FileExistsError
        If ``skills/<new_name>/`` already exists.
    """
    skills_dir = Path(workspace) / "skills"
    parent_a_dir = skills_dir / skill_a_name
    parent_b_dir = skills_dir / skill_b_name
    new_dir = skills_dir / new_name

    # Validate parents
    for pdir, pname in [(parent_a_dir, skill_a_name), (parent_b_dir, skill_b_name)]:
        if not (pdir / "SKILL.md").exists():
            raise FileNotFoundError(
                f"Parent skill '{pname}' not found at {pdir / 'SKILL.md'}"
            )

    if new_dir.exists():
        raise FileExistsError(f"Skill directory already exists: {new_dir}")

    # Read parent bodies
    body_a = (parent_a_dir / "SKILL.md").read_text(encoding="utf-8")
    body_b = (parent_b_dir / "SKILL.md").read_text(encoding="utf-8")
    desc_a = _extract_frontmatter_field(parent_a_dir / "SKILL.md", "description") or skill_a_name
    desc_b = _extract_frontmatter_field(parent_b_dir / "SKILL.md", "description") or skill_b_name

    # Strip frontmatter from bodies for inclusion
    content_a = _strip_frontmatter(body_a)
    content_b = _strip_frontmatter(body_b)

    # Build merged SKILL.md
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    rationale = merge_instruction or (
        f"Fuse `{skill_a_name}` and `{skill_b_name}` because the requested "
        f"workflow spans both skill lineages and the agent should preserve an "
        f"explicit reasoning trail before materializing a new capability."
    )

    merged_skill_md = textwrap.dedent(f"""\
        ---
        name: {new_name}
        description: "Synthesized from `{skill_a_name}` + `{skill_b_name}` - review before production use."
        metadata: {{"openclaw": {{"always": false}}}}
        ---

        ## Lineage

        - Parent A: **{skill_a_name}** - {desc_a}
        - Parent B: **{skill_b_name}** - {desc_b}
        - Created: {timestamp}

        ## Why This Merge Exists

        {rationale}

        ## Combined playbook (agent-facing)

        ### From {skill_a_name}

        {content_a.strip()}

        ### From {skill_b_name}

        {content_b.strip()}

        ## Runner

        Call `execute_terminal`: `py skills/{new_name}/runner.py --task "..."`.
        The runner returns structured lineage, rationale, and next-step hints.
    """)

    # Build merged runner.py
    merged_runner = textwrap.dedent(f'''\
        """Hybrid skill runner: {new_name}

        Auto-generated by gene_splicer from parents: {skill_a_name}, {skill_b_name}.
        Created: {timestamp}
        """
        from __future__ import annotations

        import argparse
        import json


        def run(task: str) -> str:
            """Execute the hybrid skill and return structured metadata."""
            payload = {{
                "skill": "{new_name}",
                "parents": ["{skill_a_name}", "{skill_b_name}"],
                "task_preview": (task[:200] or "(empty)"),
                "rationale": {json.dumps(rationale)},
                "next_steps": [
                    "Read the combined SKILL.md before taking action.",
                    "Prefer minimal experiments before promoting this skill to production.",
                    "Extend runner.py with domain-specific behavior only after tests are updated.",
                ],
            }}
            return json.dumps(payload, ensure_ascii=False)


        def main() -> int:
            parser = argparse.ArgumentParser(description="Runner for hybrid skill: {new_name}")
            parser.add_argument("--task", default="")
            args = parser.parse_args()
            print(run(args.task))
            return 0


        if __name__ == "__main__":
            raise SystemExit(main())
    ''')

    # Write to disk
    new_dir.mkdir(parents=True, exist_ok=False)
    (new_dir / "SKILL.md").write_text(merged_skill_md, encoding="utf-8")
    (new_dir / "runner.py").write_text(merged_runner, encoding="utf-8")

    return str(new_dir.resolve())


# ---------------------------------------------------------------------------
# 4. auto_splice_for_task
# ---------------------------------------------------------------------------

def auto_splice_for_task(
    workspace: str,
    task_instruction: str,
) -> Optional[str]:
    """End-to-end: analyse a task, pick two best parents, splice, return new skill name.

    Parameters
    ----------
    workspace:
        Project root.
    task_instruction:
        Free-text task description.

    Returns
    -------
    str or None
        The new skill's directory name, or ``None`` if fewer than two candidate
        parents could be identified.
    """
    existing = scan_skill_registry(workspace)
    if len(existing) < 2:
        return None

    gap = identify_skill_gap(task_instruction, existing)
    best: List[str] = gap.get("best_matches", [])

    if len(best) < 2:
        return None

    skill_a, skill_b = best[0], best[1]

    # Derive a new name from the two parents
    new_name = f"{_sanitize(skill_a)}_{_sanitize(skill_b)}_hybrid"

    # Avoid collisions by appending a short timestamp suffix
    skills_dir = Path(workspace) / "skills"
    if (skills_dir / new_name).exists():
        suffix = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        new_name = f"{new_name}_{suffix}"

    try:
        splice_skills(
            workspace=workspace,
            skill_a_name=skill_a,
            skill_b_name=skill_b,
            new_name=new_name,
            merge_instruction=(
                f"Auto-spliced for task: {task_instruction[:120]}. "
                f"Merges {skill_a} + {skill_b}."
            ),
        )
    except (FileNotFoundError, FileExistsError) as exc:
        # Log but don't crash — the caller gets None
        print(f"[gene_splicer] splice failed: {exc}")
        return None

    return new_name


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_frontmatter_field(path: Path, field: str) -> Optional[str]:
    """Extract a single YAML-ish frontmatter field from a SKILL.md file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None

    match = re.search(
        rf"^{field}\s*:\s*(.+)$",
        text,
        re.MULTILINE,
    )
    if match:
        value = match.group(1).strip().strip("\"'")
        return value
    return None


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (``---`` delimited) from the beginning of *text*."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            return text[end + 3:].strip()
    return text


def _sanitize(name: str) -> str:
    """Normalise a skill name into a safe directory-name fragment."""
    return re.sub(r"[^a-zA-Z0-9_]", "_", name).strip("_").lower()


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI entry-point for gene_splicer."""
    # Ensure UTF-8 output on Windows (avoids charmap codec errors)
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]

    parser = argparse.ArgumentParser(
        description="gene_splicer: merge existing skills into new hybrid skills",
    )
    sub = parser.add_subparsers(dest="command")

    # -- scan
    scan_p = sub.add_parser("scan", help="List all skills in the registry")
    scan_p.add_argument("--workspace", default=".", help="Project root")

    # -- gap
    gap_p = sub.add_parser("gap", help="Identify skill gap for a task")
    gap_p.add_argument("--workspace", default=".", help="Project root")
    gap_p.add_argument("--task", required=True, help="Task description")

    # -- splice
    splice_p = sub.add_parser("splice", help="Merge two skills into a new one")
    splice_p.add_argument("--workspace", default=".", help="Project root")
    splice_p.add_argument("skill_a", help="First parent skill name")
    splice_p.add_argument("skill_b", help="Second parent skill name")
    splice_p.add_argument("new_name", help="Name for the new hybrid skill")
    splice_p.add_argument("--instruction", default="", help="Merge guidance")

    # -- auto
    auto_p = sub.add_parser("auto", help="Auto-splice for a task")
    auto_p.add_argument("--workspace", default=".", help="Project root")
    auto_p.add_argument("--task", required=True, help="Task description")

    args = parser.parse_args()

    if args.command == "scan":
        skills = scan_skill_registry(args.workspace)
        print(json.dumps([asdict(s) for s in skills], indent=2, ensure_ascii=False))

    elif args.command == "gap":
        skills = scan_skill_registry(args.workspace)
        gap = identify_skill_gap(args.task, skills)
        print(json.dumps(gap, indent=2, ensure_ascii=False))

    elif args.command == "splice":
        new_path = splice_skills(
            args.workspace, args.skill_a, args.skill_b, args.new_name, args.instruction
        )
        print(json.dumps({"created": new_path}, indent=2))

    elif args.command == "auto":
        result = auto_splice_for_task(args.workspace, args.task)
        print(json.dumps({"new_skill": result}, indent=2))

    else:
        parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
