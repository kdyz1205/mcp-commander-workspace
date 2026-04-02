"""Load workspace `claw.config.json` once per logical operation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_claw_config(workspace: Path) -> dict[str, Any]:
    p = workspace.resolve() / "claw.config.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}
