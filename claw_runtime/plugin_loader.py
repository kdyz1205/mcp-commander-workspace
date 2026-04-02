"""Resolve plugin-attached skill directories (OpenClaw-style bundles on disk)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claw_runtime.config_load import load_claw_config


def plugin_skill_dirs(workspace: Path) -> list[Path]:
    cfg = load_claw_config(workspace)
    raw = cfg.get("plugins")
    entries: list[Any] = []
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("entries") or raw.get("load") or []

    out: list[Path] = []
    ws = workspace.resolve()
    for ent in entries:
        if not isinstance(ent, dict):
            continue
        if ent.get("enabled") is False:
            continue
        rel = ent.get("path") or ent.get("root")
        if not rel:
            continue
        root = Path(rel)
        if not root.is_absolute():
            root = ws / root
        root = root.resolve()
        if not root.is_dir():
            continue
        manifest = root / "openclaw.plugin.json"
        added_from_manifest = False
        if manifest.is_file():
            try:
                import json

                meta = json.loads(manifest.read_text(encoding="utf-8"))
                for rel_skill in meta.get("skills") or []:
                    sd = (root / str(rel_skill)).resolve()
                    if sd.is_dir() and str(sd).startswith(str(root)):
                        out.append(sd)
                        added_from_manifest = True
            except Exception:
                pass
        if not added_from_manifest:
            skills_sub = root / "skills"
            if skills_sub.is_dir():
                out.append(skills_sub.resolve())
    return list(dict.fromkeys(out))
