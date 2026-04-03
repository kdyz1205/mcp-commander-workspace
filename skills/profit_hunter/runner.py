"""
Profit Hunter — emergency low-risk arbitrage scanner activated by survival_engine fund_low.

Safety policy:
  - All scanning uses PUBLIC, unauthenticated API endpoints only.
  - Trade proposals are NEVER executed unless LIVE_TRADING=1 is explicitly set.
  - Position sizing uses half-Kelly with a hard 25% cap.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public API endpoints (no auth required)
# ---------------------------------------------------------------------------
BINANCE_FUNDING_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens"

# Default symbols for funding rate scans
BINANCE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT", "XRPUSDT",
    "AVAXUSDT", "LINKUSDT", "ADAUSDT", "DOTUSDT", "MATICUSDT",
]
OKX_SYMBOLS = [
    "BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP",
    "DOGE-USDT-SWAP", "XRP-USDT-SWAP", "AVAX-USDT-SWAP",
    "LINK-USDT-SWAP", "ADA-USDT-SWAP", "DOT-USDT-SWAP",
]

# DEX arb scan tokens (well-known tokens with cross-DEX presence)
DEFAULT_ARB_TOKENS: dict[str, dict[str, str]] = {
    "solana": {
        "SOL": "So11111111111111111111111111111111111111112",
        "JUP": "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",
        "RAY": "4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R",
        "BONK": "DezXAZ8z7PnrnRJjz3wXBoRgixCa6xjnB7YaB1pPB263",
        "WIF": "EKpQGSJtjMFqKZ9KQanSqYXRcF8fBopzLHYxdM65zcjm",
    },
    "ethereum": {
        "WETH": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        "LINK": "0x514910771AF9Ca656af840dff83E8264EcF986CA",
        "UNI": "0x1f9840a85d5aF5bf1D1762F925BDADdC4201F984",
    },
}

# Funding settlement: 3 times/day (every 8h) on most exchanges
FUNDING_PERIODS_PER_DAY = 3
FUNDING_PERIODS_PER_YEAR = FUNDING_PERIODS_PER_DAY * 365

# Non-essential task prefixes (for suspension)
NON_ESSENTIAL_TASK_PREFIXES = (
    "nightly_evolution", "research_lab", "code_review",
    "evolution_brain", "compute_scavenger", "meta_driving",
)

HTTP_TIMEOUT = 15


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class FundingOpportunity:
    symbol: str
    rate: float  # raw fraction per period
    annual_pct: float
    exchange: str


@dataclass
class DexArbOpportunity:
    token: str
    buy_dex: str
    sell_dex: str
    spread_pct: float
    chain: str
    buy_price: float = 0.0
    sell_price: float = 0.0
    liquidity_usd: float = 0.0


@dataclass
class ProfitEstimate:
    opportunity_type: str
    symbol: str
    capital_usd: float
    expected_profit_usd: float
    expected_annual_return_pct: float
    risk_score: str  # "low", "medium", "high"
    rationale: str


@dataclass
class TradeProposal:
    symbol: str
    opportunity_type: str
    direction: str
    entry_strategy: str
    exit_strategy: str
    position_size_usd: float
    kelly_fraction: float
    stop_loss_pct: float
    expected_profit_usd: float
    risk_score: str
    requires_live_trading: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only, no auth)
# ---------------------------------------------------------------------------
def _http_get_json(url: str, *, timeout: int = HTTP_TIMEOUT) -> Any:
    """Perform a public GET and return parsed JSON. No authentication."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "profit-hunter/1.0 (public-scan)",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("HTTP GET failed: %s — %s", url[:120], exc)
        return None


# ---------------------------------------------------------------------------
# Kelly sizing (inline to avoid circular imports)
# ---------------------------------------------------------------------------
def _kelly_fraction(
    win_prob: float,
    win_loss_ratio: float,
    *,
    half_kelly: bool = True,
    cap: float = 0.25,
) -> float:
    """Kelly fraction clipped to [0, cap]. Half-Kelly by default."""
    p = float(win_prob)
    b = float(win_loss_ratio)
    if not (0 < p < 1) or b <= 0:
        return 0.0
    q = 1.0 - p
    raw = (b * p - q) / b
    if raw <= 0:
        return 0.0
    if half_kelly:
        raw *= 0.5
    return float(min(raw, max(0.0, min(cap, 1.0))))


# ---------------------------------------------------------------------------
# 1. scan_funding_rates
# ---------------------------------------------------------------------------
def scan_funding_rates(
    exchanges: list[str] | None = None,
) -> list[FundingOpportunity]:
    """Scan public funding rate endpoints for carry opportunities.

    Parameters
    ----------
    exchanges:
        List of exchange names to scan. Supported: ``"binance"``, ``"okx"``.
        Defaults to both.

    Returns
    -------
    list[FundingOpportunity]
        Sorted by absolute annualised rate descending.
    """
    exchanges = exchanges or ["okx", "binance"]
    opportunities: list[FundingOpportunity] = []

    if "binance" in exchanges:
        opportunities.extend(_scan_binance_funding())

    if "okx" in exchanges:
        opportunities.extend(_scan_okx_funding())

    opportunities.sort(key=lambda o: abs(o.annual_pct), reverse=True)
    logger.info("Funding scan: %d opportunities across %s", len(opportunities), exchanges)
    return opportunities


def _scan_binance_funding() -> list[FundingOpportunity]:
    """Fetch funding rates from Binance public premium index."""
    data = _http_get_json(BINANCE_FUNDING_URL)
    if not isinstance(data, list):
        logger.warning("Binance funding: unexpected response type %s", type(data))
        return []

    results: list[FundingOpportunity] = []
    for item in data:
        symbol = item.get("symbol", "")
        if symbol not in BINANCE_SYMBOLS:
            continue
        try:
            rate = float(item.get("lastFundingRate", 0))
        except (TypeError, ValueError):
            continue
        if abs(rate) < 1e-8:
            continue
        annual_pct = rate * 100 * FUNDING_PERIODS_PER_YEAR
        results.append(FundingOpportunity(
            symbol=symbol,
            rate=rate,
            annual_pct=round(annual_pct, 2),
            exchange="binance",
        ))
    return results


def _scan_okx_funding() -> list[FundingOpportunity]:
    """Fetch funding rates from OKX public endpoint."""
    results: list[FundingOpportunity] = []
    for inst_id in OKX_SYMBOLS:
        url = f"{OKX_FUNDING_URL}?instId={inst_id}"
        body = _http_get_json(url)
        if not isinstance(body, dict):
            continue
        data_list = body.get("data", [])
        if not data_list:
            continue
        rec = data_list[0]
        try:
            rate = float(rec.get("fundingRate", 0))
        except (TypeError, ValueError):
            continue
        if abs(rate) < 1e-8:
            continue
        annual_pct = rate * 100 * FUNDING_PERIODS_PER_YEAR
        # Normalise OKX instId to base symbol
        base = inst_id.replace("-USDT-SWAP", "").replace("-", "") + "USDT"
        results.append(FundingOpportunity(
            symbol=base,
            rate=rate,
            annual_pct=round(annual_pct, 2),
            exchange="okx",
        ))
    return results


# ---------------------------------------------------------------------------
# 2. scan_dex_arbitrage
# ---------------------------------------------------------------------------
def scan_dex_arbitrage(
    chains: list[str] | None = None,
) -> list[DexArbOpportunity]:
    """Scan DexScreener for cross-DEX price discrepancies.

    Parameters
    ----------
    chains:
        Chains to scan. Supported: ``"solana"``, ``"ethereum"``.
        Defaults to both.

    Returns
    -------
    list[DexArbOpportunity]
        Sorted by spread descending.
    """
    chains = chains or ["solana", "ethereum"]
    opportunities: list[DexArbOpportunity] = []

    for chain in chains:
        tokens = DEFAULT_ARB_TOKENS.get(chain, {})
        for token_name, address in tokens.items():
            arbs = _scan_token_arb(token_name, address, chain)
            opportunities.extend(arbs)

    opportunities.sort(key=lambda o: o.spread_pct, reverse=True)
    logger.info("DEX arb scan: %d opportunities across %s", len(opportunities), chains)
    return opportunities


def _scan_token_arb(
    token_name: str,
    address: str,
    chain: str,
) -> list[DexArbOpportunity]:
    """Find cross-DEX price differences for a single token via DexScreener."""
    url = f"{DEXSCREENER_TOKENS_URL}/{address}"
    body = _http_get_json(url)
    if not isinstance(body, dict):
        return []

    pairs = body.get("pairs")
    if not isinstance(pairs, list) or len(pairs) < 2:
        return []

    # Filter to matching chain and collect per-DEX best prices
    dex_prices: dict[str, dict[str, float]] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        pair_chain = (pair.get("chainId") or "").lower()
        if chain == "solana" and pair_chain != "solana":
            continue
        if chain == "ethereum" and pair_chain != "ethereum":
            continue

        dex_id = pair.get("dexId", "unknown")
        try:
            price = float(pair.get("priceUsd") or 0)
        except (TypeError, ValueError):
            continue
        if price <= 0:
            continue

        liq = 0.0
        liq_data = pair.get("liquidity")
        if isinstance(liq_data, dict):
            try:
                liq = float(liq_data.get("usd", 0) or 0)
            except (TypeError, ValueError):
                pass

        # Only consider pools with meaningful liquidity
        if liq < 5_000:
            continue

        if dex_id not in dex_prices or price < dex_prices[dex_id].get("price", math.inf):
            dex_prices[dex_id] = {"price": price, "liquidity": liq}

    if len(dex_prices) < 2:
        return []

    # Find best buy (lowest price) and best sell (highest price)
    sorted_dexes = sorted(dex_prices.items(), key=lambda x: x[1]["price"])
    buy_dex, buy_info = sorted_dexes[0]
    sell_dex, sell_info = sorted_dexes[-1]

    if buy_dex == sell_dex:
        return []

    spread_pct = ((sell_info["price"] - buy_info["price"]) / buy_info["price"]) * 100

    # Only report spreads above 0.1% (below that, fees eat profits)
    if spread_pct < 0.1:
        return []

    return [DexArbOpportunity(
        token=token_name,
        buy_dex=buy_dex,
        sell_dex=sell_dex,
        spread_pct=round(spread_pct, 4),
        chain=chain,
        buy_price=round(buy_info["price"], 8),
        sell_price=round(sell_info["price"], 8),
        liquidity_usd=round(min(buy_info["liquidity"], sell_info["liquidity"]), 2),
    )]


# ---------------------------------------------------------------------------
# 3. estimate_profit
# ---------------------------------------------------------------------------
def estimate_profit(
    opportunity: FundingOpportunity | DexArbOpportunity,
    capital_usd: float = 100.0,
) -> ProfitEstimate:
    """Estimate expected profit and risk score for an opportunity.

    Parameters
    ----------
    opportunity:
        A funding rate or DEX arbitrage opportunity.
    capital_usd:
        Capital to deploy in USD.

    Returns
    -------
    ProfitEstimate
    """
    if isinstance(opportunity, FundingOpportunity):
        return _estimate_funding_profit(opportunity, capital_usd)
    elif isinstance(opportunity, DexArbOpportunity):
        return _estimate_arb_profit(opportunity, capital_usd)
    else:
        return ProfitEstimate(
            opportunity_type="unknown",
            symbol="?",
            capital_usd=capital_usd,
            expected_profit_usd=0.0,
            expected_annual_return_pct=0.0,
            risk_score="high",
            rationale="Unknown opportunity type",
        )


def _estimate_funding_profit(
    opp: FundingOpportunity,
    capital_usd: float,
) -> ProfitEstimate:
    """Estimate delta-neutral funding carry profit."""
    # 7-day projection, net of round-trip costs (10 bps total)
    days = 7
    periods = days * FUNDING_PERIODS_PER_DAY
    gross = capital_usd * abs(opp.rate) * periods
    round_trip_cost = capital_usd * 0.0015  # 15 bps total (entry+exit, both legs)
    net = gross - round_trip_cost
    annual_return = (net / max(capital_usd, 1e-12)) * (365 / days) * 100

    # Risk scoring
    abs_annual = abs(opp.annual_pct)
    if abs_annual < 15:
        risk = "low"
    elif abs_annual < 40:
        risk = "medium"
    else:
        risk = "high"  # Extreme rates often mean-revert quickly

    return ProfitEstimate(
        opportunity_type="funding_carry",
        symbol=opp.symbol,
        capital_usd=capital_usd,
        expected_profit_usd=round(net, 4),
        expected_annual_return_pct=round(annual_return, 2),
        risk_score=risk,
        rationale=(
            f"{opp.exchange} {opp.symbol}: {opp.annual_pct:.1f}% annualised funding. "
            f"7d gross ${gross:.4f}, cost ${round_trip_cost:.4f}, net ${net:.4f}. "
            f"Risk: {risk} (extreme rates may revert)."
        ),
    )


def _estimate_arb_profit(
    opp: DexArbOpportunity,
    capital_usd: float,
) -> ProfitEstimate:
    """Estimate cross-DEX arbitrage profit for a single execution."""
    # Assume 0.3% total fees (swap fees both sides + gas)
    fee_pct = 0.3
    net_spread = opp.spread_pct - fee_pct
    profit = capital_usd * (net_spread / 100)

    # Constrain by liquidity — can only arb up to the smaller pool
    effective_capital = min(capital_usd, opp.liquidity_usd * 0.02)  # Max 2% of pool
    if effective_capital < capital_usd:
        profit = effective_capital * (net_spread / 100)

    # Risk scoring
    if opp.spread_pct < 0.5:
        risk = "high"  # Spread too thin for reliable profit
    elif opp.spread_pct < 1.5 and opp.liquidity_usd > 50_000:
        risk = "low"
    elif opp.spread_pct < 3.0:
        risk = "medium"
    else:
        risk = "high"  # Very wide spread often means stale data or rug

    return ProfitEstimate(
        opportunity_type="dex_arb",
        symbol=opp.token,
        capital_usd=round(effective_capital, 2),
        expected_profit_usd=round(profit, 4),
        expected_annual_return_pct=0.0,  # Single execution, not annualised
        risk_score=risk,
        rationale=(
            f"{opp.chain} {opp.token}: {opp.spread_pct:.2f}% spread "
            f"({opp.buy_dex} -> {opp.sell_dex}). "
            f"Net after fees: {net_spread:.2f}%. "
            f"Effective capital ${effective_capital:.2f} "
            f"(pool liq ${opp.liquidity_usd:.0f}). "
            f"Risk: {risk}."
        ),
    )


# ---------------------------------------------------------------------------
# 4. generate_trade_proposal
# ---------------------------------------------------------------------------
def generate_trade_proposal(
    opportunities: list[FundingOpportunity | DexArbOpportunity],
    max_risk: str = "low",
    capital_usd: float | None = None,
) -> dict[str, Any]:
    """Generate a structured trade proposal from scanned opportunities.

    Parameters
    ----------
    opportunities:
        Combined list of funding and DEX arb opportunities.
    max_risk:
        Maximum acceptable risk level: ``"low"``, ``"medium"``, ``"high"``.
    capital_usd:
        Total capital available. Defaults to env PROFIT_HUNTER_CAPITAL_USD or 100.

    Returns
    -------
    dict
        Proposal with filtered opportunities, sizing, and entry/exit strategy.
        DOES NOT EXECUTE — for human approval or explicit live execution only.
    """
    if capital_usd is None:
        capital_usd = float(os.environ.get("PROFIT_HUNTER_CAPITAL_USD", "100"))

    risk_levels = {"low": 1, "medium": 2, "high": 3}
    max_risk_val = risk_levels.get(max_risk, 1)

    live_trading = os.environ.get("LIVE_TRADING", "0").strip() in ("1", "true", "yes")

    estimates: list[ProfitEstimate] = []
    for opp in opportunities:
        est = estimate_profit(opp, capital_usd)
        if risk_levels.get(est.risk_score, 3) <= max_risk_val:
            estimates.append(est)

    # Sort by expected profit descending
    estimates.sort(key=lambda e: e.expected_profit_usd, reverse=True)

    proposals: list[dict[str, Any]] = []
    remaining_capital = capital_usd

    for est in estimates:
        if remaining_capital <= 0:
            break

        # Kelly sizing
        win_prob = 0.55 if est.risk_score == "low" else 0.50 if est.risk_score == "medium" else 0.45
        win_loss = 1.3 if est.opportunity_type == "funding_carry" else 1.1
        kf = _kelly_fraction(win_prob, win_loss, half_kelly=True, cap=0.25)

        position_size = min(remaining_capital * kf, remaining_capital) if kf > 0 else 0
        if position_size < 1.0:
            continue

        # Stop loss based on risk
        stop_loss_pct = {"low": 2.0, "medium": 5.0, "high": 10.0}.get(est.risk_score, 5.0)

        if est.opportunity_type == "funding_carry":
            direction = "short_perp + long_spot (delta-neutral)"
            entry = "Open delta-neutral position: short perpetual + long spot/DEX at market"
            exit_strat = "Close both legs after 7 funding periods or if rate flips sign"
        else:
            direction = "buy_low_sell_high (cross-DEX)"
            entry = "Atomic swap: buy on cheaper DEX, sell on more expensive DEX"
            exit_strat = "Single execution — profit realised on completion"

        proposal = TradeProposal(
            symbol=est.symbol,
            opportunity_type=est.opportunity_type,
            direction=direction,
            entry_strategy=entry,
            exit_strategy=exit_strat,
            position_size_usd=round(position_size, 2),
            kelly_fraction=round(kf, 4),
            stop_loss_pct=stop_loss_pct,
            expected_profit_usd=round(est.expected_profit_usd * (position_size / max(est.capital_usd, 1)), 4),
            risk_score=est.risk_score,
            requires_live_trading=True,
        )
        proposals.append(proposal.to_dict())
        remaining_capital -= position_size

    result: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "proposal_only",
        "live_trading_enabled": live_trading,
        "total_capital_usd": capital_usd,
        "allocated_capital_usd": round(capital_usd - remaining_capital, 2),
        "max_risk_filter": max_risk,
        "num_opportunities_scanned": len(opportunities),
        "num_proposals": len(proposals),
        "proposals": proposals,
        "policy": (
            "These proposals require human approval before execution. "
            "No orders will be placed unless LIVE_TRADING=1 is explicitly set "
            "and the execution function is called directly."
        ),
    }

    logger.info(
        "Trade proposal: %d proposals from %d opportunities, $%.2f allocated",
        len(proposals), len(opportunities), capital_usd - remaining_capital,
    )
    return result


# ---------------------------------------------------------------------------
# 5. suspend_non_essential_tasks
# ---------------------------------------------------------------------------
def suspend_non_essential_tasks(
    workspace: str | Path,
) -> list[str]:
    """Suspend non-essential tasks to conserve compute and API quota.

    Reads ``.claw/persisted_tasks.jsonl`` and marks non-essential tasks as
    suspended by writing ``.claw/profit_hunter/suspended_tasks.json``.

    Parameters
    ----------
    workspace:
        Workspace root path.

    Returns
    -------
    list[str]
        List of suspended task IDs.
    """
    workspace = Path(workspace).resolve()
    tasks_file = workspace / ".claw" / "persisted_tasks.jsonl"
    out_dir = workspace / ".claw" / "profit_hunter"
    out_dir.mkdir(parents=True, exist_ok=True)

    suspended: list[str] = []

    if tasks_file.is_file():
        try:
            for line in tasks_file.read_text(encoding="utf-8").strip().splitlines():
                if not line.strip():
                    continue
                try:
                    task = json.loads(line)
                except json.JSONDecodeError:
                    continue
                task_id = task.get("id") or task.get("task_id", "")
                task_type = task.get("type") or task.get("skill", "")
                status = task.get("status", "")

                if status in ("suspended", "completed", "failed"):
                    continue

                if any(task_type.startswith(prefix) for prefix in NON_ESSENTIAL_TASK_PREFIXES):
                    suspended.append(str(task_id))
        except OSError as exc:
            logger.warning("Failed to read persisted_tasks: %s", exc)

    # Also check runtime_control for running background tasks
    runtime_ctrl = workspace / ".claw" / "runtime_control.json"
    if runtime_ctrl.is_file():
        try:
            ctrl = json.loads(runtime_ctrl.read_text(encoding="utf-8"))
            for key in list(ctrl.keys()):
                if any(key.startswith(prefix) for prefix in NON_ESSENTIAL_TASK_PREFIXES):
                    if ctrl[key].get("enabled", True):
                        suspended.append(f"runtime:{key}")
        except (OSError, json.JSONDecodeError):
            pass

    # Write suspension record
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "suspended_task_ids": suspended,
        "reason": "profit_hunter: conserving resources during low-fund mode",
        "policy": "Tasks will be resumed when fund balance recovers above threshold.",
    }
    out_file = out_dir / "suspended_tasks.json"
    out_file.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    logger.info("Suspended %d non-essential tasks", len(suspended))
    return suspended


# ---------------------------------------------------------------------------
# 6. run_hunger_scan
# ---------------------------------------------------------------------------
def run_hunger_scan(
    workspace: str | Path,
    fund_balance_usd: float,
) -> dict[str, Any]:
    """Full hunger scan: suspend tasks, scan markets, generate proposals.

    Parameters
    ----------
    workspace:
        Workspace root path.
    fund_balance_usd:
        Current fund balance in USD (from survival_engine or manual input).

    Returns
    -------
    dict
        Complete scan report written to ``.claw/profit_hunter/hunger_scan.json``.
    """
    workspace = Path(workspace).resolve()
    out_dir = workspace / ".claw" / "profit_hunter"
    out_dir.mkdir(parents=True, exist_ok=True)

    max_risk = os.environ.get("PROFIT_HUNTER_MAX_RISK", "low")

    # Step 1: Suspend non-essential tasks
    suspended = suspend_non_essential_tasks(workspace)

    # Step 2: Scan funding rates
    funding_opps: list[FundingOpportunity] = []
    try:
        funding_opps = scan_funding_rates(exchanges=["okx", "binance"])
    except Exception as exc:
        logger.warning("Funding scan failed: %s", exc)

    # Step 3: Scan DEX arbitrage
    dex_opps: list[DexArbOpportunity] = []
    try:
        dex_opps = scan_dex_arbitrage(chains=["solana", "ethereum"])
    except Exception as exc:
        logger.warning("DEX arb scan failed: %s", exc)

    # Step 4: Generate trade proposal
    all_opps: list[FundingOpportunity | DexArbOpportunity] = []
    all_opps.extend(funding_opps)
    all_opps.extend(dex_opps)

    capital = min(fund_balance_usd, float(os.environ.get("PROFIT_HUNTER_CAPITAL_USD", "100")))
    proposal = generate_trade_proposal(all_opps, max_risk=max_risk, capital_usd=capital)

    # Step 5: Compile report
    report: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fund_balance_usd": fund_balance_usd,
        "status": "scan_complete",
        "suspended_tasks": suspended,
        "funding_opportunities": [asdict(o) for o in funding_opps],
        "dex_arb_opportunities": [asdict(o) for o in dex_opps],
        "trade_proposal": proposal,
        "summary": {
            "num_funding_opps": len(funding_opps),
            "num_dex_arb_opps": len(dex_opps),
            "num_proposals": proposal["num_proposals"],
            "total_allocated_usd": proposal["allocated_capital_usd"],
            "num_tasks_suspended": len(suspended),
        },
        "next_steps": [
            "Review trade proposals before any execution.",
            "Set LIVE_TRADING=1 only after manual approval of specific proposals.",
            "Monitor fund balance and re-run scan if conditions change.",
            "Resume suspended tasks when fund balance recovers.",
        ],
    }

    # Write report
    report_path = out_dir / "hunger_scan.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(
        "Hunger scan complete: %d funding, %d arb, %d proposals, %d suspended — %s",
        len(funding_opps), len(dex_opps), proposal["num_proposals"],
        len(suspended), report_path,
    )
    return report


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Profit Hunter — low-risk arbitrage scanner")
    parser.add_argument(
        "--action",
        choices=["scan_funding", "scan_dex", "estimate", "proposal", "suspend", "hunger_scan"],
        default="hunger_scan",
        help="Action to perform",
    )
    parser.add_argument("--workspace", default=".", help="Workspace root")
    parser.add_argument("--balance", type=float, default=10.0, help="Fund balance in USD")
    parser.add_argument("--max-risk", default=None, help="Max risk: low, medium, high")
    parser.add_argument("--exchanges", default="okx,binance", help="Comma-separated exchanges")
    parser.add_argument("--chains", default="solana,ethereum", help="Comma-separated chains")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.max_risk:
        os.environ["PROFIT_HUNTER_MAX_RISK"] = args.max_risk

    if args.action == "scan_funding":
        opps = scan_funding_rates(exchanges=args.exchanges.split(","))
        print(json.dumps([asdict(o) for o in opps], indent=2, ensure_ascii=False))

    elif args.action == "scan_dex":
        opps = scan_dex_arbitrage(chains=args.chains.split(","))
        print(json.dumps([asdict(o) for o in opps], indent=2, ensure_ascii=False))

    elif args.action == "suspend":
        suspended = suspend_non_essential_tasks(args.workspace)
        print(json.dumps({"suspended": suspended}, indent=2))

    elif args.action == "hunger_scan":
        report = run_hunger_scan(args.workspace, args.balance)
        print(json.dumps(report, indent=2, ensure_ascii=False))

    elif args.action == "proposal":
        funding = scan_funding_rates(exchanges=args.exchanges.split(","))
        dex = scan_dex_arbitrage(chains=args.chains.split(","))
        all_opps: list[FundingOpportunity | DexArbOpportunity] = []
        all_opps.extend(funding)
        all_opps.extend(dex)
        proposal = generate_trade_proposal(all_opps, max_risk=args.max_risk or "low", capital_usd=args.balance)
        print(json.dumps(proposal, indent=2, ensure_ascii=False))

    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
