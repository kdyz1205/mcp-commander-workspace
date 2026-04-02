"""
CRITICAL survival reflex: downgrade brain (parasite / local Ollama) + CURSOR_OUTBOX.md + optional TG ping.

On every CRITICAL call, OUTBOX + parasite are **always** refreshed. Debounce applies only to
treasury rewrite + TG notify (avoids spam while still guaranteeing Cursor sees fresh OUTBOX).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

from claw_runtime.survival_engine import SurvivalEngine, SurvivalState

# Shown in .claw/parasite_mode.json — DevClaw reads this and routes to Ollama-compatible API.
PARASITE_REASON_REFLEX = "API Quota Exhausted - Auto Switch to Local Brain"


def _format_outbox(snapshot: dict[str, Any]) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
    lines = [
        "# CURSOR_OUTBOX (auto — survival reflex)",
        "",
        f"**Generated:** {ts}",
        f"**State:** {snapshot.get('state')}",
        f"**Reason:** {snapshot.get('reason', '')}",
        "",
        "## Brain downgrade",
        "",
        f"Parasite mode was **forced ON** with reason: `{PARASITE_REASON_REFLEX}`.",
        "Configure `OLLAMA_BASE_URL` + `OLLAMA_MODEL` (see `env.example`). Clear with TG `/parasite_off` or delete `.claw/parasite_mode.json` when cloud is healthy again.",
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
        "2. If rate limits: reduce `TG_DEVCLAW_MAX_ITERS`, wait, or keep local **Ollama**.",
        "3. If disk/memory: free space or close apps; tune `SURVIVAL_*` env vars in `env.example`.",
        "4. Parasite mode is ON: DevClaw uses local OpenAI-compatible endpoint until you explicitly turn it off.",
        "",
    ]
    return "\n".join(lines)


def write_cursor_outbox(
    workspace: Path,
    snapshot: dict[str, Any],
    *,
    outbox_path: Path | None = None,
) -> Path | None:
    """
    Write CURSOR_OUTBOX.md from a survival snapshot (for Cursor collaboration / human handoff).
    Returns the path written, or None on failure.
    """
    workspace = Path(workspace).resolve()
    ob = outbox_path or (workspace / "CURSOR_OUTBOX.md")
    body = _format_outbox(snapshot)
    try:
        ob.write_text(body, encoding="utf-8")
        return ob
    except OSError:
        return None


def run_critical_reflex(
    workspace: Path,
    engine: SurvivalEngine,
    *,
    debounce_sec: float = 300.0,
    notify: Callable[[str], None] | None = None,
    outbox_path: Path | None = None,
) -> bool:
    """
    When state is CRITICAL:

    **Always (no debounce):** force parasite mode + refresh `CURSOR_OUTBOX.md` with latest snapshot.

    **Debounced:** treasury stub + optional TG `notify` (so admins are not spammed every survival tick).

    Returns True if state was CRITICAL (artifacts written); False if not CRITICAL.
    """
    workspace = Path(workspace).resolve()
    state, reason = engine.assess_survival_state()
    if state != SurvivalState.CRITICAL:
        return False

    snap = engine.snapshot()

    # 1–2) 每次 CRITICAL 都刷新：降级大脑 + 求救信（TG 心跳与 API 拦截均依赖此保证）
    engine.set_parasite_mode(True, PARASITE_REASON_REFLEX)
    write_cursor_outbox(workspace, snap, outbox_path=outbox_path)

    if not engine.try_acquire_critical_reflex_slot(debounce_sec):
        return True

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
            f"**Brain:** parasite ON — `{PARASITE_REASON_REFLEX}`\n"
            "See `CURSOR_OUTBOX.md` in workspace root.\n"
            f"Workspace: `{workspace}`"
        )
        try:
            notify(msg)
        except Exception:
            pass
    return True
