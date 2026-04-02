"""
Skill precedence (aligned with OpenClaw docs.openclaw.ai/skills):

Highest → Lowest for same `name`:
  workspace ./skills
  workspace ./.agents/skills
  user ~/.agents/skills
  user ~/.openclaw/skills
  bundled claw_runtime/bundled_skills
  extra dirs from claw.config.json skills.load.extraDirs

We hot-reload every agent iteration (stronger than default session snapshot).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Iterable

from claw_runtime.skill_loader import SkillRecord, discover_under


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _workspace_skills_dirs(workspace: Path, extra_dirs: list[str]) -> list[tuple[int, Path]]:
    """Return (precedence_rank, path). Lower rank = lower priority (applied first)."""
    ranked: list[tuple[int, Path]] = []
    rank = 0
    for d in extra_dirs:
        ranked.append((rank, Path(d).expanduser().resolve()))
        rank += 1
    bundled = Path(__file__).resolve().parent / "bundled_skills"
    ranked.append((rank, bundled))
    rank += 1
    ranked.append((rank, _home() / ".openclaw" / "skills"))
    rank += 1
    ranked.append((rank, _home() / ".agents" / "skills"))
    rank += 1
    ranked.append((rank, workspace / ".agents" / "skills"))
    rank += 1
    ranked.append((rank, workspace / "skills"))
    return ranked


def _load_config(workspace: Path) -> dict:
    p = workspace / "claw.config.json"
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _extra_dirs(cfg: dict, workspace: Path) -> list[str]:
    try:
        raw = cfg.get("skills", {}).get("load", {}).get("extraDirs", [])
        out: list[str] = []
        for item in raw:
            p = Path(item)
            if not p.is_absolute():
                p = workspace / p
            out.append(str(p))
        return out
    except Exception:
        return []


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _eligible(rec: SkillRecord) -> bool:
    meta = rec.metadata or {}
    oc = meta.get("openclaw") if isinstance(meta, dict) else None
    if not isinstance(oc, dict):
        return True
    if oc.get("always") is True:
        return True
    req = oc.get("requires") or {}
    if not isinstance(req, dict):
        return True
    bins = req.get("bins") or []
    if isinstance(bins, list) and bins:
        if not all(_which(str(b)) for b in bins):
            return False
    any_bins = req.get("anyBins") or []
    if isinstance(any_bins, list) and any_bins:
        if not any(_which(str(b)) for b in any_bins):
            return False
    envs = req.get("env") or []
    if isinstance(envs, list) and envs:
        if not all(os.environ.get(e) for e in envs):
            return False
    oss = oc.get("os")
    if isinstance(oss, list) and oss:
        plat = sys.platform
        map_ = {"win32": "win32", "darwin": "darwin", "linux": "linux"}
        cur = map_.get(plat, plat)
        if cur not in oss:
            return False
    return True


class SkillRegistry:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._cfg = _load_config(self.workspace)

    def refresh(self) -> dict[str, SkillRecord]:
        extra = _extra_dirs(self._cfg, self.workspace)
        merged: dict[str, SkillRecord] = {}
        for _rank, root in _workspace_skills_dirs(self.workspace, extra):
            for rec in discover_under(root):
                if not _eligible(rec):
                    continue
                merged[rec.name] = rec
        return merged

    def catalog_text(self, skills: dict[str, SkillRecord] | None = None) -> str:
        skills = skills or self.refresh()
        if not skills:
            return "## Skills\n(no SKILL.md discovered — add `skills/<name>/SKILL.md`)\n"
        lines = [
            "## Skills (OpenClaw-style, progressive disclosure)",
            "Only names + descriptions here. To load full steps, call tool `load_skill` with exact `name`.",
            "New skills under `./skills/` override bundled and home dirs on name conflict.",
            "",
        ]
        for name in sorted(skills.keys()):
            rec = skills[name]
            loc = rec.path.replace(str(self.workspace), ".")
            lines.append(f"- **{name}**: {rec.description} _(source: {loc})_")
        return "\n".join(lines) + "\n"

    def get(self, name: str) -> SkillRecord | None:
        return self.refresh().get(name)
