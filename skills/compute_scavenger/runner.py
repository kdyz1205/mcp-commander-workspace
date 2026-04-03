"""
sk_compute_scavenger — Automated compute resource scavenger for DevClaw.

Detects high local resource usage, packages workloads for remote execution
(Google Colab, GitHub Actions), and pulls results back.

State directory: <workspace>/.claw/compute_scavenger/
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import textwrap
import time
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Configuration helpers (match claw_runtime patterns)
# ---------------------------------------------------------------------------

def _env_float(name: str, default: float) -> float:
    """Read a float from an environment variable, falling back to *default*."""
    try:
        return float(os.environ.get(name, "").strip() or default)
    except (ValueError, TypeError):
        return default


def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _state_dir(workspace: Path) -> Path:
    """Return (and create) the per-workspace state directory."""
    d = workspace.resolve() / ".claw" / "compute_scavenger"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# 1. check_local_resources
# ---------------------------------------------------------------------------

@dataclass
class ResourceSnapshot:
    """Point-in-time resource usage of the local machine."""
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    timestamp: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_local_resources() -> ResourceSnapshot:
    """Return current CPU%, memory%, and disk% usage.

    Uses *psutil* when available.  Falls back to platform-specific commands
    so the skill still works on machines without psutil installed.
    """
    cpu: float = 0.0
    mem: float = 0.0
    disk: float = 0.0

    try:
        import psutil  # type: ignore[import-untyped]

        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent
    except ImportError:
        cpu = _fallback_cpu()
        mem = _fallback_mem()
        disk = _fallback_disk()

    return ResourceSnapshot(
        cpu_percent=round(cpu, 1),
        memory_percent=round(mem, 1),
        disk_percent=round(disk, 1),
        timestamp=time.time(),
    )


# -- fallback helpers (no psutil) ------------------------------------------

def _fallback_cpu() -> float:
    """Best-effort CPU usage without psutil."""
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["wmic", "cpu", "get", "loadpercentage"],
                text=True,
                timeout=5,
            )
            for line in out.strip().splitlines():
                line = line.strip()
                if line.isdigit():
                    return float(line)
        except Exception:
            pass
    else:
        # Unix: 1-minute load average as a rough proxy
        try:
            load1 = os.getloadavg()[0]
            ncpu = os.cpu_count() or 1
            return min(round(load1 / ncpu * 100, 1), 100.0)
        except Exception:
            pass
    return 0.0


def _fallback_mem() -> float:
    """Best-effort memory usage without psutil."""
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                [
                    "wmic",
                    "OS",
                    "get",
                    "FreePhysicalMemory,TotalVisibleMemorySize",
                    "/Value",
                ],
                text=True,
                timeout=5,
            )
            vals: dict[str, float] = {}
            for line in out.strip().splitlines():
                if "=" in line:
                    k, v = line.split("=", 1)
                    try:
                        vals[k.strip()] = float(v.strip())
                    except ValueError:
                        pass
            total = vals.get("TotalVisibleMemorySize", 0)
            free = vals.get("FreePhysicalMemory", 0)
            if total > 0:
                return round((1 - free / total) * 100, 1)
        except Exception:
            pass
    else:
        try:
            with open("/proc/meminfo") as f:
                info: dict[str, int] = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])
                total = info.get("MemTotal", 0)
                available = info.get("MemAvailable", 0)
                if total > 0:
                    return round((1 - available / total) * 100, 1)
        except Exception:
            pass
    return 0.0


def _fallback_disk() -> float:
    """Best-effort disk usage without psutil."""
    try:
        usage = shutil.disk_usage("/")
        return round(usage.used / usage.total * 100, 1)
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# 2. should_offload
# ---------------------------------------------------------------------------

def should_offload(
    threshold_cpu: float | None = None,
    threshold_mem: float | None = None,
    snapshot: ResourceSnapshot | None = None,
) -> bool:
    """Decide whether local resources are strained enough to offload work.

    Parameters
    ----------
    threshold_cpu:
        CPU% ceiling.  Defaults to env ``SCAVENGER_CPU_THRESHOLD`` or 85.
    threshold_mem:
        Memory% ceiling.  Defaults to env ``SCAVENGER_MEM_THRESHOLD`` or 90.
    snapshot:
        Pre-captured resource snapshot (avoids a second measurement).

    Returns ``True`` when *either* threshold is exceeded.
    """
    if threshold_cpu is None:
        threshold_cpu = _env_float("SCAVENGER_CPU_THRESHOLD", 85.0)
    if threshold_mem is None:
        threshold_mem = _env_float("SCAVENGER_MEM_THRESHOLD", 90.0)

    snap = snapshot or check_local_resources()
    return snap.cpu_percent > threshold_cpu or snap.memory_percent > threshold_mem


# ---------------------------------------------------------------------------
# 3. prepare_colab_bundle
# ---------------------------------------------------------------------------

def _build_colab_notebook(task_instruction: str, target_files: list[str]) -> dict[str, Any]:
    """Generate a Colab-ready .ipynb notebook as a Python dict."""
    file_list_str = "\n".join(f"  - {f}" for f in target_files) if target_files else "  (none)"
    cells: list[dict[str, Any]] = [
        {
            "cell_type": "markdown",
            "metadata": {},
            "source": [
                "# DevClaw Remote Offload\n",
                "\n",
                f"**Task:** {task_instruction}\n",
                "\n",
                "**Bundled files:**\n",
                f"{file_list_str}\n",
            ],
        },
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# 1. Install dependencies\n",
                "!pip install -q psutil\n",
                "# Add project-specific deps below:\n",
                "# !pip install -q ccxt pandas numpy\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# 2. Unpack bundled workspace files\n",
                "import zipfile, pathlib\n",
                "bundle = pathlib.Path('colab_bundle.zip')\n",
                "if bundle.exists():\n",
                "    with zipfile.ZipFile(bundle) as zf:\n",
                "        zf.extractall('workspace')\n",
                "    print('Extracted to ./workspace')\n",
                "else:\n",
                "    print('Upload colab_bundle.zip first')\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# 3. Run the task\n",
                "import subprocess, sys\n",
                f"task = {task_instruction!r}\n",
                "result = subprocess.run(\n",
                "    [sys.executable, '-c', f'print(\"Executing: {task}\")'],\n",
                "    capture_output=True, text=True,\n",
                ")\n",
                "print(result.stdout)\n",
                "if result.returncode != 0:\n",
                "    print('STDERR:', result.stderr)\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
        {
            "cell_type": "code",
            "metadata": {},
            "source": [
                "# 4. Package results for download\n",
                "import zipfile, pathlib, datetime\n",
                "ts = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%S')\n",
                "out = pathlib.Path(f'results_{ts}.zip')\n",
                "with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:\n",
                "    for f in pathlib.Path('workspace').rglob('*'):\n",
                "        if f.is_file():\n",
                "            zf.write(f, f.relative_to('workspace'))\n",
                "print(f'Results packaged: {out}')\n",
                "# Download via Colab sidebar or:\n",
                "# from google.colab import files; files.download(str(out))\n",
            ],
            "outputs": [],
            "execution_count": None,
        },
    ]
    return {
        "nbformat": 4,
        "nbformat_minor": 0,
        "metadata": {
            "colab": {"provenance": []},
            "kernelspec": {
                "name": "python3",
                "display_name": "Python 3",
            },
            "language_info": {"name": "python"},
        },
        "cells": cells,
    }


def prepare_colab_bundle(
    workspace: Path,
    task_instruction: str,
    target_files: list[str] | None = None,
) -> Path:
    """Create a zip bundle ready for upload to Google Colab.

    The bundle contains:
      - ``devclaw_offload.ipynb`` — a Colab-ready notebook that installs deps,
        unpacks workspace files, runs the task, and packages results.
      - ``job.json`` — machine-readable task manifest.
      - Selected workspace files (paths relative to *workspace*).

    Parameters
    ----------
    workspace:
        Project root directory.
    task_instruction:
        Human-readable description of what the remote notebook should do.
    target_files:
        Workspace-relative paths to include in the bundle.  If ``None``,
        no extra files are bundled (only the notebook and manifest).

    Returns
    -------
    Path to the created ``.zip`` file under ``<workspace>/.claw/compute_scavenger/``.
    """
    workspace = Path(workspace).resolve()
    target_files = target_files or []
    state = _state_dir(workspace)

    # -- Validate target files (must be inside workspace) --
    validated_files: list[str] = []
    for rel in target_files:
        p = (workspace / rel).resolve()
        if p.is_file() and str(p).startswith(str(workspace)):
            validated_files.append(rel)

    # -- Build notebook --
    notebook = _build_colab_notebook(task_instruction, validated_files)

    # -- Build manifest --
    manifest: dict[str, Any] = {
        "skill": "compute_scavenger",
        "target": "colab",
        "workspace_hint": str(workspace),
        "task_instruction": task_instruction,
        "files": [
            {"rel": rel, "size": (workspace / rel).stat().st_size}
            for rel in validated_files
        ],
        "created_at": time.time(),
    }

    # -- Write zip --
    bundle_path = state / "colab_bundle.zip"
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("devclaw_offload.ipynb", json.dumps(notebook, indent=1))
        zf.writestr("job.json", json.dumps(manifest, indent=2, ensure_ascii=False))
        for rel in validated_files:
            zf.write(workspace / rel, arcname=rel.replace("\\", "/"))

    # -- Persist last-bundle record --
    (state / "last_bundle.json").write_text(
        json.dumps({"path": str(bundle_path), "target": "colab", **manifest}, indent=2),
        encoding="utf-8",
    )

    return bundle_path


# ---------------------------------------------------------------------------
# 4. prepare_github_actions_workflow
# ---------------------------------------------------------------------------

_ACTIONS_WORKFLOW_TEMPLATE = textwrap.dedent("""\
    # Auto-generated by sk_compute_scavenger
    # Upload this to .github/workflows/devclaw-offload.yml in your repo.

    name: DevClaw Remote Offload

    on:
      workflow_dispatch:
        inputs:
          task_instruction:
            description: "Task to execute"
            required: true
            default: "{task_instruction}"

    permissions:
      contents: read
      actions: write

    jobs:
      offload:
        runs-on: ubuntu-latest
        timeout-minutes: 30

        steps:
          - name: Checkout repository
            uses: actions/checkout@v4

          - name: Set up Python
            uses: actions/setup-python@v5
            with:
              python-version: "3.11"
              cache: pip

          - name: Install dependencies
            run: |
              python -m pip install --upgrade pip
              if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
              pip install psutil

          - name: Run DevClaw task
            env:
              TASK_INSTRUCTION: ${{{{ inputs.task_instruction }}}}
            run: |
              echo "Task: $TASK_INSTRUCTION"
              python dev_claw/main.py --task "$TASK_INSTRUCTION" || echo "::warning::Task exited non-zero"

          - name: Upload artifacts
            uses: actions/upload-artifact@v4
            if: always()
            with:
              name: devclaw-offload-results
              path: |
                .claw/compute_scavenger/
                results/
                *.csv
                *.json
              retention-days: 7
""")


def prepare_github_actions_workflow(
    workspace: Path,
    task_instruction: str,
) -> Path:
    """Generate a GitHub Actions workflow file for remote task execution.

    The workflow:
      - Checks out the repository.
      - Installs Python 3.11 + project dependencies.
      - Runs the task via ``dev_claw/main.py``.
      - Uploads result artifacts.

    Parameters
    ----------
    workspace:
        Project root directory.
    task_instruction:
        Human-readable task description (embedded as default input).

    Returns
    -------
    Path to the generated ``.yml`` workflow file.
    """
    workspace = Path(workspace).resolve()

    # Sanitise instruction for YAML embedding
    safe_instruction = task_instruction.replace('"', '\\"').replace("\n", " ")

    content = _ACTIONS_WORKFLOW_TEMPLATE.replace("{task_instruction}", safe_instruction)

    # Write into .github/workflows so it can be committed directly
    workflows_dir = workspace / ".github" / "workflows"
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_path = workflows_dir / "devclaw-offload.yml"
    workflow_path.write_text(content, encoding="utf-8")

    # Also store a copy in state dir for tracking
    state = _state_dir(workspace)
    (state / "last_workflow.json").write_text(
        json.dumps(
            {
                "skill": "compute_scavenger",
                "target": "github_actions",
                "workflow_path": str(workflow_path),
                "task_instruction": task_instruction,
                "created_at": time.time(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    return workflow_path


# ---------------------------------------------------------------------------
# 5. pull_remote_results
# ---------------------------------------------------------------------------

def pull_remote_results(
    workspace: Path,
    source: str = "colab",
) -> dict[str, Any]:
    """Check for and download results from a remote execution source.

    Parameters
    ----------
    workspace:
        Project root directory.
    source:
        Either ``"colab"`` or ``"github_actions"``.

    Returns
    -------
    A dict with ``status`` (``"found"`` | ``"not_found"`` | ``"error"``),
    ``files`` (list of paths), and optional ``message``.
    """
    workspace = Path(workspace).resolve()
    state = _state_dir(workspace)
    results_dir = state / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    if source == "colab":
        return _pull_colab_results(workspace, state, results_dir)
    elif source == "github_actions":
        return _pull_github_actions_results(workspace, state, results_dir)
    else:
        return {
            "status": "error",
            "files": [],
            "message": f"Unknown source: {source!r}. Use 'colab' or 'github_actions'.",
        }


def _pull_colab_results(
    workspace: Path,
    state: Path,
    results_dir: Path,
) -> dict[str, Any]:
    """Look for Colab result zips in conventional locations and unpack them."""
    search_locations = [
        workspace / "results_colab.zip",
        workspace / "Downloads" / "results_colab.zip",
        state / "results_colab.zip",
    ]

    # Also check for any results_*.zip in the state dir
    for p in state.glob("results_*.zip"):
        if p not in search_locations:
            search_locations.append(p)

    found_files: list[str] = []
    for zp in search_locations:
        if zp.is_file():
            try:
                with zipfile.ZipFile(zp, "r") as zf:
                    zf.extractall(results_dir)
                    found_files.extend(
                        str(results_dir / name) for name in zf.namelist()
                    )
                # Move the consumed zip so we don't reprocess it
                consumed = zp.with_suffix(".consumed.zip")
                zp.rename(consumed)
            except zipfile.BadZipFile:
                continue

    if found_files:
        return {"status": "found", "files": found_files, "results_dir": str(results_dir)}
    return {
        "status": "not_found",
        "files": [],
        "message": (
            "No Colab result zips found. Download results from Colab and place "
            f"the zip in {state} or {workspace}."
        ),
    }


def _pull_github_actions_results(
    workspace: Path,
    state: Path,
    results_dir: Path,
) -> dict[str, Any]:
    """Query GitHub Actions for the latest offload-run artifacts via gh CLI."""
    repo = _env_str("GITHUB_REPOSITORY", "")
    if not repo:
        # Try to detect from git remote
        try:
            out = subprocess.check_output(
                ["git", "remote", "get-url", "origin"],
                cwd=str(workspace),
                text=True,
                timeout=5,
            )
            # Extract owner/repo from git URL
            url = out.strip()
            for prefix in ("https://github.com/", "git@github.com:"):
                if url.startswith(prefix):
                    repo = url[len(prefix):].removesuffix(".git")
                    break
        except Exception:
            pass

    if not repo:
        return {
            "status": "error",
            "files": [],
            "message": (
                "Cannot determine GitHub repository. "
                "Set GITHUB_REPOSITORY=owner/repo or ensure a git remote is configured."
            ),
        }

    # Use gh CLI to download latest artifacts
    try:
        list_out = subprocess.check_output(
            [
                "gh", "run", "list",
                "--repo", repo,
                "--workflow", "devclaw-offload.yml",
                "--limit", "1",
                "--json", "databaseId,status,conclusion",
            ],
            text=True,
            timeout=30,
        )
        runs = json.loads(list_out)
        if not runs:
            return {
                "status": "not_found",
                "files": [],
                "message": "No workflow runs found for devclaw-offload.yml.",
            }

        run = runs[0]
        if run.get("status") != "completed":
            return {
                "status": "not_found",
                "files": [],
                "message": f"Latest run (id={run['databaseId']}) status: {run.get('status')}. Wait for completion.",
            }

        run_id = run["databaseId"]
        download_dir = results_dir / f"run_{run_id}"
        download_dir.mkdir(parents=True, exist_ok=True)

        subprocess.check_call(
            [
                "gh", "run", "download", str(run_id),
                "--repo", repo,
                "--dir", str(download_dir),
            ],
            timeout=120,
        )

        found_files = [str(p) for p in download_dir.rglob("*") if p.is_file()]
        return {
            "status": "found",
            "files": found_files,
            "results_dir": str(download_dir),
        }

    except FileNotFoundError:
        return {
            "status": "error",
            "files": [],
            "message": "gh CLI not found. Install GitHub CLI: https://cli.github.com/",
        }
    except subprocess.CalledProcessError as exc:
        return {
            "status": "error",
            "files": [],
            "message": f"gh command failed (exit {exc.returncode}): {exc.stderr or exc.stdout or ''}",
        }
    except Exception as exc:
        return {
            "status": "error",
            "files": [],
            "message": f"Unexpected error pulling GitHub Actions results: {exc}",
        }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI interface for the compute scavenger skill."""
    parser = argparse.ArgumentParser(
        description="sk_compute_scavenger — detect resource pressure and offload work",
    )
    parser.add_argument(
        "--action",
        choices=["check", "offload", "pull"],
        default="check",
        help="Action to perform (default: check)",
    )
    parser.add_argument(
        "--target",
        choices=["colab", "github_actions"],
        default="colab",
        help="Remote target for offload (default: colab)",
    )
    parser.add_argument(
        "--source",
        choices=["colab", "github_actions"],
        default="colab",
        help="Source to pull results from (default: colab)",
    )
    parser.add_argument("--task", default="", help="Task instruction for the remote run")
    parser.add_argument("--files", default="", help="Comma-separated workspace-relative file paths")
    parser.add_argument(
        "--workspace",
        default=_env_str("SCAVENGER_WORKSPACE", "."),
        help="Workspace root (default: cwd or SCAVENGER_WORKSPACE)",
    )

    args = parser.parse_args()
    workspace = Path(args.workspace).resolve()

    if args.action == "check":
        snap = check_local_resources()
        offload = should_offload(snapshot=snap)
        result = {
            "resources": snap.to_dict(),
            "should_offload": offload,
            "recommendation": (
                "Local resources are strained. Consider offloading heavy tasks."
                if offload
                else "Local resources are within safe thresholds."
            ),
        }
        print(json.dumps(result, indent=2))

    elif args.action == "offload":
        if not args.task:
            print(json.dumps({"error": "--task is required for offload"}))
            return 1

        target_files = [f.strip() for f in args.files.split(",") if f.strip()]

        if args.target == "colab":
            bundle = prepare_colab_bundle(workspace, args.task, target_files)
            print(json.dumps({
                "target": "colab",
                "bundle_path": str(bundle),
                "instructions": "Upload the zip to Google Colab and run the notebook.",
            }, indent=2))
        else:
            wf = prepare_github_actions_workflow(workspace, args.task)
            print(json.dumps({
                "target": "github_actions",
                "workflow_path": str(wf),
                "instructions": (
                    "Commit the workflow file, push, then trigger via "
                    "Actions tab or: gh workflow run devclaw-offload.yml"
                ),
            }, indent=2))

    elif args.action == "pull":
        result = pull_remote_results(workspace, source=args.source)
        print(json.dumps(result, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
