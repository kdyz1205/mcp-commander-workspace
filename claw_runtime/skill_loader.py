"""Parse AgentSkills-style SKILL.md (YAML-like frontmatter + body). No PyYAML required."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SkillRecord:
    name: str
    description: str
    body: str
    path: str
    source_root: str
    metadata: dict | None = None


def _parse_simple_frontmatter(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z0-9_-]+)\s*:\s*(.*)$", line)
        if m:
            key, val = m.group(1).lower(), m.group(2).strip()
            if val.startswith('"') and val.endswith('"'):
                val = val[1:-1]
            elif val.startswith("'") and val.endswith("'"):
                val = val[1:-1]
            out[key] = val
    return out


def _try_parse_metadata(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def parse_skill_file(skill_md: Path, source_root: str) -> SkillRecord | None:
    text = skill_md.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---"):
        name = skill_md.parent.name
        return SkillRecord(
            name=name,
            description="",
            body=text.strip(),
            path=str(skill_md.resolve()),
            source_root=source_root,
        )

    rest = text[3:]
    end = rest.find("\n---")
    if end == -1:
        return None
    fm_block = rest[:end].strip()
    body = rest[end + 4 :].lstrip("\n")

    fields = _parse_simple_frontmatter(fm_block)
    name = fields.get("name") or skill_md.parent.name
    description = fields.get("description", "")
    meta_raw = fields.get("metadata", "")
    metadata = _try_parse_metadata(meta_raw) if meta_raw else None

    return SkillRecord(
        name=name.strip(),
        description=description.strip(),
        body=body.strip(),
        path=str(skill_md.resolve()),
        source_root=source_root,
        metadata=metadata,
    )


def discover_under(root: Path) -> list[SkillRecord]:
    if not root.is_dir():
        return []
    found: list[SkillRecord] = []
    for skill_md in root.glob("*/SKILL.md"):
        rec = parse_skill_file(skill_md, str(root.resolve()))
        if rec and rec.name:
            found.append(rec)
    return found
