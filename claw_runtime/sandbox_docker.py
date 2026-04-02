"""Optional Docker-isolated terminal execution (workspace bind-mount)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from claw_runtime.config_load import load_claw_config


def docker_enabled(workspace: Path) -> tuple[bool, str, str]:
    if os.environ.get("DEVCLAW_USE_DOCKER_SANDBOX", "").strip() in {"1", "true", "yes"}:
        image = os.environ.get("DEVCLAW_DOCKER_IMAGE", "python:3.12-slim")
        network = os.environ.get("DEVCLAW_DOCKER_NETWORK", "none")
        return True, image, network
    cfg = load_claw_config(workspace).get("sandbox") or {}
    d = cfg.get("docker") or {}
    if d.get("enabled") is True:
        return True, str(d.get("image", "python:3.12-slim")), str(d.get("network", "none"))
    return False, "", ""


def run_shell_in_docker(workspace: str, command: str, *, image: str, network: str, timeout: int) -> subprocess.CompletedProcess[str]:
    ws = str(Path(workspace).resolve())
    argv = [
        "docker",
        "run",
        "--rm",
        "-v",
        f"{ws}:/workspace",
        "-w",
        "/workspace",
    ]
    if network and network != "bridge":
        argv.extend(["--network", network])
    argv.extend([image, "sh", "-c", command])
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
        shell=False,
    )
