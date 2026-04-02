"""
Dimension 1 — Colab / remote compute offload (user-operated).

Does not auto-create Google accounts or bypass quotas. Produces a bundle you upload to Colab manually.
"""

from __future__ import annotations

import json
import os
import zipfile
from pathlib import Path
from typing import Any


def build_colab_job_bundle(
    workspace: Path,
    *,
    relative_paths: list[str],
    instruction: str = "",
    out_zip: Path | None = None,
) -> Path:
    """
    Zip selected workspace-relative files + job.json + COLAB_README.txt for manual Colab run.
    """
    workspace = Path(workspace).resolve()
    export_dir = workspace / ".claw" / "colab_export"
    export_dir.mkdir(parents=True, exist_ok=True)
    job: dict[str, Any] = {
        "workspace_hint": str(workspace),
        "instruction": instruction,
        "files": [],
    }
    for rel in relative_paths:
        p = (workspace / rel).resolve()
        if not str(p).startswith(str(workspace)) or not p.is_file():
            continue
        job["files"].append({"rel": rel, "size": p.stat().st_size})
    job_path = export_dir / "job.json"
    job_path.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")

    readme = export_dir / "COLAB_README.txt"
    readme.write_text(
        "1. Download this folder as ZIP from your machine (or use `colab_bundle.zip`).\n"
        "2. In Google Colab: Upload → unpack.\n"
        "3. Run your backtest / heavy code against `job.json` + copied files.\n"
        "4. Download results; pull back with `scp`, Drive, or paste into TG.\n\n"
        "This repo does not automate Colab auth or ToS — you operate the notebook.\n",
        encoding="utf-8",
    )

    zpath = out_zip or (export_dir / "colab_bundle.zip")
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(job_path, arcname="job.json")
        zf.write(readme, arcname="COLAB_README.txt")
        for rel in relative_paths:
            p = (workspace / rel).resolve()
            if p.is_file() and str(p).startswith(str(workspace)):
                zf.write(p, arcname=rel.replace("\\", "/"))
    return zpath
