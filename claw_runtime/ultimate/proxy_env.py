"""
Dimension 1 — HTTP proxy rotation using a *user-supplied* list (one per line).

Does not scrape or import untrusted public proxy pools. Optional VPN CLI hook is explicit opt-in.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

_PROXY_ENV_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def load_proxy_list(path: Path | str) -> list[str]:
    p = Path(path)
    if not p.is_file():
        return []
    lines: list[str] = []
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            lines.append(s)
    return lines


def proxy_env_for_index(proxies: list[str], index: int) -> dict[str, str]:
    if not proxies:
        return {}
    url = proxies[index % len(proxies)]
    return {"HTTP_PROXY": url, "HTTPS_PROXY": url, "ALL_PROXY": url}


def current_proxy_env(workspace: Path | str) -> dict[str, str]:
    workspace = Path(workspace).resolve()
    state_path = workspace / ".claw" / "proxy_rotate.json"
    if not state_path.is_file():
        return {}
    try:
        data = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return {}
    proxy = str(data.get("proxy", "")).strip()
    if not proxy:
        return {}
    return {"HTTP_PROXY": proxy, "HTTPS_PROXY": proxy, "ALL_PROXY": proxy}


def snapshot_proxy_env() -> dict[str, str]:
    return {key: value for key in _PROXY_ENV_KEYS if (value := os.environ.get(key)) is not None}


def clear_proxy_env() -> None:
    for key in _PROXY_ENV_KEYS:
        os.environ.pop(key, None)


def restore_proxy_env(snapshot: dict[str, str] | None) -> None:
    clear_proxy_env()
    if not snapshot:
        return
    for key, value in snapshot.items():
        os.environ[key] = value


def apply_proxy_env(workspace: Path | str) -> dict[str, str]:
    env_map = current_proxy_env(workspace)
    for key in _PROXY_ENV_KEYS:
        if key not in env_map:
            os.environ.pop(key, None)
    for key, value in env_map.items():
        os.environ[key] = value
    return env_map


def rotate_proxy_index(workspace: Path) -> dict[str, Any]:
    """
    Persist next index in .claw/proxy_rotate.json; optionally run VPN_SWITCH_CMD between hops.
    """
    workspace = Path(workspace).resolve()
    list_path = os.environ.get("PROXY_LIST_FILE", "").strip()
    if not list_path:
        return {"ok": False, "error": "PROXY_LIST_FILE not set"}
    proxies = load_proxy_list(list_path)
    if not proxies:
        return {"ok": False, "error": "empty proxy list"}

    state_path = workspace / ".claw" / "proxy_rotate.json"
    idx = 0
    if state_path.is_file():
        try:
            idx = int(json.loads(state_path.read_text(encoding="utf-8")).get("index", 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            idx = 0
    idx = (idx + 1) % len(proxies)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"index": idx, "proxy": proxies[idx]}, indent=2), encoding="utf-8")

    cmd = os.environ.get("VPN_SWITCH_CMD", "").strip()
    if cmd:
        try:
            subprocess.run(cmd, shell=True, check=False, timeout=120, capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired):
            pass

    env_map = proxy_env_for_index(proxies, idx)
    for key, value in env_map.items():
        os.environ[key] = value
    return {"ok": True, "index": idx, "proxy": proxies[idx], "env_snippet": env_map}
