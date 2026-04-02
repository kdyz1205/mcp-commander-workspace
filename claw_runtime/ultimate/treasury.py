"""
Dimension 4 - Treasury and survival planning with read-only, simulation-only funding research.

No automatic wallet spend, exchange orders, top-up APIs, or unattended financial actions.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np


def probe_eth_balance(rpc_url: str, address: str) -> tuple[bool, str, int | None]:
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
    workspace = Path(workspace).resolve()
    out = workspace / ".claw" / "treasury_proposal.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "status": "pending_human_approval",
        "reason": reason,
        "suggested_actions": suggested_actions,
        "policy": (
            "Autonomous signing, exchange trading, top-up APIs, and wallet spend are out of scope. "
            "Human approval is required for any financial action."
        ),
    }
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def write_survival_revenue_plan_stub(workspace: Path, *, reason: str, okx_enabled: bool) -> Path:
    workspace = Path(workspace).resolve()
    out = workspace / ".claw" / "revenue_plan.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "status": "simulation_only",
        "reason": reason,
        "constraints": {
            "okx_enabled": okx_enabled,
            "live_orders_allowed": False,
            "requires_human_approval": True,
        },
        "tracks": [
            {
                "name": "paper_trade_replay",
                "description": "Use public data and backtests only; no credentialed exchange calls.",
            },
            {
                "name": "read_only_market_scan",
                "description": "Generate opportunity reports from public data before any live capital is considered.",
            },
            {
                "name": "ops_reliability",
                "description": "Prioritize restoring compute and billing health before any monetization attempt.",
            },
        ],
        "next_actions": [
            "Keep OKX/API keys untouched until explicitly approved.",
            "Review task_plan.md and treasury_proposal.json together.",
            "If live trading is ever enabled, add separate risk limits and human approval gates first.",
        ],
    }
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


@dataclass
class OpportunitySignal:
    symbol: str
    source: str
    confidence: float
    expected_edge_bps: float
    rationale: str


def _allow_network_research() -> bool:
    return os.environ.get("TREASURY_ALLOW_NETWORK_RESEARCH", "").strip().lower() in {"1", "true", "yes"}


def _load_public_market_snapshot(symbol: str) -> dict[str, Any]:
    if not _allow_network_research():
        return {"ok": False, "reason": "network research disabled"}
    try:
        from skills.trading.scripts.trading.backtest_engine import fetch_ohlcv  # type: ignore[import-not-found]
    except Exception as exc:
        return {"ok": False, "reason": f"backtest_engine unavailable: {exc}"}

    try:
        candles = asyncio.run(fetch_ohlcv(symbol=symbol, bar="4H", limit=240, use_cache=True))
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}
    if candles is None or len(candles) < 80:
        return {"ok": False, "reason": "insufficient candles"}
    close = candles[:, 4]
    returns = np.diff(close) / close[:-1]
    momentum = float((close[-1] / close[-25] - 1.0) * 10_000) if len(close) >= 25 else 0.0
    volatility = float(np.std(returns[-40:]) * 10_000) if len(returns) >= 40 else 0.0
    trend = float(np.mean(returns[-12:]) * 10_000) if len(returns) >= 12 else 0.0
    return {
        "ok": True,
        "symbol": symbol,
        "momentum_bps": momentum,
        "volatility_bps": volatility,
        "trend_bps": trend,
        "last_close": float(close[-1]),
    }


def _score_public_opportunity(symbol: str, snapshot: dict[str, Any]) -> OpportunitySignal:
    if not snapshot.get("ok"):
        return OpportunitySignal(
            symbol=symbol,
            source="unavailable",
            confidence=0.0,
            expected_edge_bps=0.0,
            rationale=str(snapshot.get("reason") or "no public market data"),
        )
    trend = float(snapshot.get("trend_bps", 0.0))
    momentum = float(snapshot.get("momentum_bps", 0.0))
    volatility = max(float(snapshot.get("volatility_bps", 0.0)), 1.0)
    confidence = max(0.0, min(1.0, abs(trend) / volatility))
    edge = trend * 0.35 + momentum * 0.05
    rationale = (
        f"Public market scan for {symbol}: trend={trend:.1f}bps, momentum={momentum:.1f}bps, "
        f"volatility={volatility:.1f}bps. Use this for paper-trade review only."
    )
    return OpportunitySignal(
        symbol=symbol,
        source="backtest_engine.fetch_ohlcv",
        confidence=confidence,
        expected_edge_bps=edge,
        rationale=rationale,
    )


def prepare_self_funding_review(
    workspace: Path | str,
    *,
    reason: str,
    symbols: list[str] | None = None,
) -> Path:
    workspace = Path(workspace).resolve()
    out = workspace / ".claw" / "self_funding_review.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    symbols = symbols or ["BTCUSDT", "ETHUSDT"]
    signals = []
    for symbol in symbols:
        snapshot = _load_public_market_snapshot(symbol)
        signals.append(asdict(_score_public_opportunity(symbol, snapshot)))
    best = max(signals, key=lambda item: item["confidence"], default=None)
    data = {
        "status": "research_only",
        "reason": reason,
        "policy": {
            "live_orders_allowed": False,
            "top_up_api_allowed": False,
            "okx_keys_touched": False,
        },
        "modules_considered": [
            "skills/trading/scripts/trading/alpha_evolver.py",
            "skills/trading/scripts/trading/smart_money_copy_hook.py",
            "skills/trading/scripts/trading/backtest_engine.py",
        ],
        "signals": signals,
        "recommended_action": (
            "paper_trade_review"
            if best and best["confidence"] >= float(os.environ.get("TREASURY_MIN_RESEARCH_CONFIDENCE", "0.45"))
            else "stay_in_survival_mode"
        ),
        "next_steps": [
            "If confidence is high, run a paper/backtest experiment only.",
            "Do not touch OKX credentials or submit live orders from this review.",
            "If losses or uncertainty accumulate, enter minimal survival mode and cut non-essential monitoring.",
        ],
    }
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def write_chain_action_draft(
    workspace: Path | str,
    *,
    reason: str,
    draft_type: str = "eth_transfer_template",
) -> Path:
    """
    Write an **unsigned** on-chain intent for human signing in an external wallet.

    This repository never holds private keys, never signs, and never broadcasts.
    """
    workspace = Path(workspace).resolve()
    out = workspace / ".claw" / "chain_action_draft.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    recipient = os.environ.get("ETH_TREASURY_ADDRESS", "").strip()
    data = {
        "status": "unsigned_draft_human_must_sign",
        "reason": reason,
        "draft_type": draft_type,
        "policy": (
            "No autonomous signing, broadcasting, MEV bots, or exchange orders from this repo. "
            "You must approve every chain transaction in your own wallet software."
        ),
        "suggested_unsigned_tx": {
            "chain": os.environ.get("ETH_CHAIN_NAME", "ethereum_mainnet_or_configure"),
            "to": recipient or "<SET_ETH_TREASURY_ADDRESS_IN_ENV>",
            "value_wei": "0x0",
            "data": "0x",
            "note": "Set value_wei and data only after human review; never paste secrets into this file.",
        },
        "execution_checklist": [
            "Verify recipient, amount, and network on the hardware or wallet screen.",
            "Reject any tool or script that asks for a raw private key or seed phrase.",
            "Broadcast only when you explicitly choose to submit the transaction.",
        ],
    }
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def maybe_alert_low_survival(workspace: Path) -> Path | None:
    from claw_runtime.survival_engine import SurvivalEngine, SurvivalState

    eng = SurvivalEngine(workspace)
    st, reason = eng.assess_survival_state()
    if st != SurvivalState.CRITICAL:
        return None
    return write_treasury_proposal_stub(
        workspace,
        reason=reason,
        suggested_actions=[
            "Top up OpenAI billing or switch keys manually",
            "Enable parasite or Ollama",
            "Review CURSOR_OUTBOX.md",
            "Run self_funding_review.json in research-only mode if you need a paper-trade plan",
        ],
    )
