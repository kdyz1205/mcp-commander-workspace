"""
Dimension 4 — Treasury / API survival (read-only on-chain probe + human-gated proposals).

Automatic MEV / airdrop farming / unsupervised wallet spend is NOT implemented (legal, custody, and safety).
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def probe_eth_balance(rpc_url: str, address: str) -> tuple[bool, str, int | None]:
    """eth_getBalance via JSON-RPC; no signing."""
    payload = json.dumps(
        {"jsonrpc": "2.0", "method": "eth_getBalance", "params": [address, "latest"], "id": 1}
    ).encode()
    req = urllib.request.Request(
        rpc_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = json.loads(resp.read().decode())
        if "error" in body:
            return False, str(body["error"]), None
        hexv = body.get("result", "0x0")
        wei = int(hexv, 16)
        return True, "ok", wei
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError, TypeError) as e:
        return False, str(e), None


def write_treasury_proposal_stub(workspace: Path, *, reason: str, suggested_actions: list[str]) -> Path:
    """
    Write a file for human approval — never executes chain txs from this function.
    """
    workspace = Path(workspace).resolve()
    out = workspace / ".claw" / "treasury_proposal.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "status": "pending_human_approval",
        "reason": reason,
        "suggested_actions": suggested_actions,
        "policy": (
            "Autonomous signing / MEV / airdrop bots are out of scope for this repository. "
            "Use a custody workflow you trust; integrate your own signer behind explicit UI approval."
        ),
    }
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def maybe_alert_low_survival(workspace: Path) -> Path | None:
    """
    If survival CRITICAL, write treasury proposal stub (alerts only).
    """
    from claw_runtime.survival_engine import SurvivalEngine, SurvivalState

    eng = SurvivalEngine(workspace)
    st, reason = eng.assess_survival_state()
    if st != SurvivalState.CRITICAL:
        return None
    return write_treasury_proposal_stub(
        workspace,
        reason=reason,
        suggested_actions=[
            "Top up OpenAI billing or switch keys",
            "Enable parasite / Ollama",
            "Review CURSOR_OUTBOX.md",
            "(Optional) Check ETH_TREASURY_ADDRESS via treasury-probe CLI if configured",
        ],
    )
