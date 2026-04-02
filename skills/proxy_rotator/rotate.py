"""CLI: rotate proxy index for workspace (repo root = parent of skills/)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

# workspace = .../skills/proxy_rotator -> .. -> ..
WS = Path(__file__).resolve().parent.parent.parent
if str(WS) not in sys.path:
    sys.path.insert(0, str(WS))


def main() -> int:
    from claw_runtime.ultimate.proxy_env import rotate_proxy_index

    netsh = os.environ.get("META_NETSH_CMD", "").strip()
    if netsh:
        subprocess.run(netsh, shell=True, check=False, timeout=60)
    out = rotate_proxy_index(WS)
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
