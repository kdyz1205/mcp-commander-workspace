"""
CRITICAL survival reflex: CURSOR_OUTBOX.md + parasite mode + optional TG ping.

Triggered by tg_dev_claw heartbeat (debounced) or manually. Policy-safe: no illegal actions.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from claw_runtime.survival_engine import SurvivalEngine, SurvivalState


def _format_outbox(snapshot: dict[str, Any]) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    lines = [
        "# CURSOR_OUTBOX (auto — survival reflex)",
        "",
        f"**Generated:** {ts}",
        f"**State:** {snapshot.get('state')}",
        f"**Reason:** {snapshot.get('reason', '')}",
        "",
        "## Vitals snapshot",
        "",
        "```json",
        json.dumps(snapshot, indent=2, ensure_ascii=False)[:6000],
        "```",
        "",
        "## What to do in Cursor (human or Agent)",
        "",
        "1. If `insufficient_quota` / billing: add credits or switch API key; then clear parasite if you want cloud again.",
        "2. If rate limits: reduce `TG_DEVCLAW_MAX_ITERS`, wait, or use local **Ollama** (`OLLAMA_BASE_URL`, `OLLAMA_MODEL`).",
        "3. If disk/memory: free space or close apps; tune `SURVIVAL_*` env vars in `env.example`.",
        "4. Parasite mode is ON: DevClaw will prefer local OpenAI-compatible endpoint until you `/parasite_off` in TG or delete `.claw/parasite_mode.json`.",
        "",
    ]
    return "\n".join(lines)


def run_critical_reflex(
    workspace: Path,
    engine: SurvivalEngine,
    *,
    debounce_sec: float = 300.0,
    notify: Callable[[str], None] | None = None,
    outbox_path: Path | None = None,
) -> bool:
    """
    If state is CRITICAL and debounce allows: write CURSOR_OUTBOX.md, enable parasite, optional notify.
    Returns True if reflex actions ran.
    """
    workspace = Path(workspace).resolve()
    state, reason = engine.assess_survival_state()
    if state != SurvivalState.CRITICAL:
        return False
    if not engine.try_acquire_critical_reflex_slot(debounce_sec):
        return False

    snap = engine.snapshot()
    body = _format_outbox(snap)
    ob = outbox_path or (workspace / "CURSOR_OUTBOX.md")
    try:
        ob.write_text(body, encoding="utf-8")
    except OSError:
        pass

    engine.set_parasite_mode(True, reason)

    try:
        from claw_runtime.ultimate.treasury import write_treasury_proposal_stub

        write_treasury_proposal_stub(
            workspace,
            reason=reason,
            suggested_actions=[
                "Restore OpenAI billing or use Ollama parasite path",
                "Read CURSOR_OUTBOX.md",
                "Optional: ETH_TREASURY_ADDRESS + ultimate-treasury-probe (read-only)",
            ],
        )
    except Exception:
        pass

    if notify:
        msg = (
            "🚨 **CONDITION RED — CRITICAL**\n"
            f"{reason}\n\n"
            "Parasite mode **ON** (local LLM path). See `CURSOR_OUTBOX.md` in workspace root.\n"
            f"Workspace: `{workspace}`"
        )
        try:
            notify(msg)
        except Exception:
            pass
    return True
