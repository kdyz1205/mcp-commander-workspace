"""
Dimension 2 — Emit rebuild scripts for broken venv / Python (human runs them).

Does not auto-uninstall without explicit user execution.
"""

from __future__ import annotations

from pathlib import Path


def emit_rebuild_venv_scripts(workspace: Path) -> tuple[Path, Path]:
    workspace = Path(workspace).resolve()
    scripts = workspace / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)

    ps1 = scripts / "self_heal_rebuild_venv.ps1"
    ps1.write_text(
        r"""# Self-heal: recreate local venv (review paths, then run in elevated shell if needed)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root
if (Test-Path .venv) { Remove-Item -Recurse -Force .venv }
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -U pip
if (Test-Path requirements.txt) { & .\.venv\Scripts\pip.exe install -r requirements.txt }
if (Test-Path dev_claw\requirements.txt) { & .\.venv\Scripts\pip.exe install -r dev_claw\requirements.txt }
Write-Host "Done. Activate: .\.venv\Scripts\Activate.ps1"
""",
        encoding="utf-8",
    )

    sh = scripts / "self_heal_rebuild_venv.sh"
    sh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
rm -rf .venv
python3 -m venv .venv
. .venv/bin/activate
pip install -U pip
[[ -f requirements.txt ]] && pip install -r requirements.txt
[[ -f dev_claw/requirements.txt ]] && pip install -r dev_claw/requirements.txt
echo "Done. Activate: source .venv/bin/activate"
""",
        encoding="utf-8",
    )
    return ps1, sh
