"""
Skill precedence (OpenClaw docs.openclaw.ai/skills), extended with plugin skill dirs:

Low → High merge (later wins on same name):
  extraDirs → plugin skill dirs → bundled → ~/.openclaw/skills → ~/.agents/skills
  → <workspace>/.agents/skills → <workspace>/skills
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from claw_runtime.config_load import load_claw_config
from claw_runtime.plugin_loader import plugin_skill_dirs
from claw_runtime.skill_loader import SkillRecord, discover_under


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def _workspace_skills_dirs(
    workspace: Path,
    extra_dirs: list[str],
    plugin_dirs: list[Path],
) -> list[tuple[int, Path]]:
    ranked: list[tuple[int, Path]] = []
    rank = 0
    for d in extra_dirs:
        ranked.append((rank, Path(d).expanduser().resolve()))
        rank += 1
    for pd in plugin_dirs:
        ranked.append((rank, pd.resolve()))
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


def _extra_dirs(cfg: dict, workspace: Path) -> list[str]:
    try:
        raw = cfg.get("skills", {}).get("load", {}).get("extraDirs", [])
        out: list[str] = []
        for item in raw:
            p = Path(str(item))
            if not p.is_absolute():
                p = workspace / p
            out.append(str(p))
        return out
    except Exception:
        return []


def _which(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _normalize_os_list(oss: list[str]) -> set[str]:
    out: set[str] = set()
    for o in oss:
        o = str(o).lower().strip()
        if o in ("macos", "darwin"):
            out.add("darwin")
        elif o in ("win32", "windows"):
            out.add("win32")
        elif o == "linux":
            out.add("linux")
        else:
            out.add(o)
    return out


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
        allowed = _normalize_os_list([str(x) for x in oss])
        if cur not in allowed and plat not in allowed:
            return False
    return True


class SkillRegistry:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self._cfg = load_claw_config(self.workspace)

    def refresh(self) -> dict[str, SkillRecord]:
        extra = _extra_dirs(self._cfg, self.workspace)
        plug = plugin_skill_dirs(self.workspace)
        merged: dict[str, SkillRecord] = {}
        for _rank, root in _workspace_skills_dirs(self.workspace, extra, plug):
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
            "Plugin skills → `plugins/*/skills/` via `claw.config.json`; `py -m claw_runtime.cli skills install <url>`.",
            "",
        ]
        for name in sorted(skills.keys()):
            rec = skills[name]
            loc = rec.path.replace(str(self.workspace), ".")
            lines.append(f"- **{name}**: {rec.description} _(source: {loc})_")
        return "\n".join(lines) + "\n"

    def get(self, name: str) -> SkillRecord | None:
        return self.refresh().get(name)
