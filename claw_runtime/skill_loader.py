"""Parse AgentSkills / ClawHub SKILL.md: full YAML frontmatter + body."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class SkillRecord:
    name: str
    description: str
    body: str
    path: str
    source_root: str
    metadata: dict[str, Any] | None = None


def _normalize_metadata(meta: Any) -> dict[str, Any] | None:
    if not isinstance(meta, dict):
        return None
    out = dict(meta)
    oc = out.get("openclaw")
    if oc is None and "clawdbot" in out:
        oc = out.get("clawdbot")
    if oc is None and "clawdis" in out:
        oc = out.get("clawdis")
    if oc is not None and "openclaw" not in out:
        out["openclaw"] = oc
    return out


def _parse_yaml_frontmatter(fm_block: str) -> dict[str, Any] | None:
    try:
        import yaml
    except ImportError:
        return None
    try:
        data = yaml.safe_load(fm_block)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


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


def _metadata_from_simple(fields: dict[str, str]) -> dict[str, Any] | None:
    raw = fields.get("metadata", "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        return _normalize_metadata(obj)
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

    data = _parse_yaml_frontmatter(fm_block)
    if data:
        name = str(data.get("name") or skill_md.parent.name).strip()
        description = str(data.get("description", "")).strip()
        metadata = _normalize_metadata(data.get("metadata"))
        return SkillRecord(
            name=name,
            description=description,
            body=body.strip(),
            path=str(skill_md.resolve()),
            source_root=source_root,
            metadata=metadata,
        )

    fields = _parse_simple_frontmatter(fm_block)
    name = (fields.get("name") or skill_md.parent.name).strip()
    description = fields.get("description", "").strip()
    metadata = _metadata_from_simple(fields)
    return SkillRecord(
        name=name,
        description=description,
        body=body.strip(),
        path=str(skill_md.resolve()),
        source_root=source_root,
        metadata=metadata,
    )


def _skill_md_in_dir(d: Path) -> Path | None:
    for fname in ("SKILL.md", "skill.md"):
        p = d / fname
        if p.is_file():
            return p
    return None


def discover_under(root: Path) -> list[SkillRecord]:
    if not root.is_dir():
        return []
    found: list[SkillRecord] = []
    for child in sorted(root.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir():
            continue
        sm = _skill_md_in_dir(child)
        if sm:
            rec = parse_skill_file(sm, str(root.resolve()))
            if rec and rec.name:
                found.append(rec)
    return found
