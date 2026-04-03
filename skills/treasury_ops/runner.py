"""
Treasury Ops — post-profit treasury management, balance tracking, burn rate, and renewal proposals.

Safety policy:
  - PROPOSAL-ONLY: no transfers, payments, or API key rotations are executed automatically.
  - Human approval is required for every financial action.
  - All data comes from local state files and public endpoints.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
HTTP_TIMEOUT = 15
LOW_BALANCE_THRESHOLD_USD = float(os.environ.get("TREASURY_LOW_BALANCE_THRESHOLD_USD", "5.0"))
BURN_RATE_WINDOW_DAYS = int(os.environ.get("TREASURY_BURN_RATE_WINDOW_DAYS", "7"))

# Estimated per-call costs for API providers (conservative estimates)
API_COST_ESTIMATES: dict[str, float] = {
    "openai_gpt4": 0.03,       # per 1K tokens (blended input/output)
    "openai_gpt35": 0.002,     # per 1K tokens
    "claude_opus": 0.075,      # per 1K tokens (blended)
    "claude_sonnet": 0.015,    # per 1K tokens (blended)
    "claude_haiku": 0.001,     # per 1K tokens (blended)
}

# Public exchange endpoints for balance info (read-only, no auth for ticker)
BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/price"
OKX_TICKER_URL = "https://www.okx.com/api/v5/market/ticker"

# Health status thresholds
HEALTH_THRESHOLDS = {
    "critical": 2.0,    # USD
    "warning": 10.0,    # USD
    "healthy": 50.0,    # USD
}


# ---------------------------------------------------------------------------
# HTTP helper (stdlib only, no auth)
# ---------------------------------------------------------------------------
def _http_get_json(url: str, *, timeout: int = HTTP_TIMEOUT) -> Any:
    """Perform a public GET and return parsed JSON. No authentication."""
    req = urllib.request.Request(url, headers={
        "Accept": "application/json",
        "User-Agent": "treasury-ops/1.0 (readonly)",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        logger.warning("HTTP GET failed: %s — %s", url[:120], exc)
        return None


# ---------------------------------------------------------------------------
# 1. check_balances
# ---------------------------------------------------------------------------
def check_balances(workspace: str | Path) -> dict[str, Any]:
    """Aggregate balances from local state and public exchange price endpoints.

    Reads:
    - ``.claw/fund_estimate.json`` for operational fund balance.
    - ``.claw/treasury_ops/balances.json`` for cached exchange balances.
    - Environment variable ``SURVIVAL_FUND_BALANCE_USD``.

    Does NOT use authenticated exchange APIs — only local state + public tickers
    for price conversion if needed.

    Parameters
    ----------
    workspace:
        Workspace root path.

    Returns
    -------
    dict
        Balance summary with local_usd, exchange_balances, total_estimated_usd.
    """
    workspace = Path(workspace).resolve()
    balances: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "local_usd": 0.0,
        "exchange_balances": {},
        "total_estimated_usd": 0.0,
        "sources": [],
    }

    # Source 1: Environment variable
    env_bal = os.environ.get("SURVIVAL_FUND_BALANCE_USD", "").strip()
    if env_bal:
        try:
            val = float(env_bal)
            balances["local_usd"] = val
            balances["sources"].append("env:SURVIVAL_FUND_BALANCE_USD")
        except ValueError:
            pass

    # Source 2: .claw/fund_estimate.json
    fund_file = workspace / ".claw" / "fund_estimate.json"
    if fund_file.is_file():
        try:
            data = json.loads(fund_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                usd = float(data.get("usd", 0))
                if usd > 0 and balances["local_usd"] == 0:
                    balances["local_usd"] = usd
                balances["sources"].append("file:fund_estimate.json")

                # Additional balance fields if present
                for key in ("eth_wei", "sol_lamports", "exchange_usd"):
                    if key in data:
                        balances["exchange_balances"][key] = data[key]
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
            logger.warning("Failed to read fund_estimate.json: %s", exc)

    # Source 3: Cached exchange balances (from previous authenticated sessions)
    cached_bal_file = workspace / ".claw" / "treasury_ops" / "balances.json"
    if cached_bal_file.is_file():
        try:
            cached = json.loads(cached_bal_file.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and "exchange_balances" in cached:
                balances["exchange_balances"].update(cached["exchange_balances"])
                balances["sources"].append("file:treasury_ops/balances.json")
        except (OSError, json.JSONDecodeError):
            pass

    # Source 4: Treasury proposal (may contain balance hints)
    treasury_proposal = workspace / ".claw" / "treasury_proposal.json"
    if treasury_proposal.is_file():
        try:
            tp = json.loads(treasury_proposal.read_text(encoding="utf-8"))
            if isinstance(tp, dict):
                balances["treasury_proposal_status"] = tp.get("status", "unknown")
                balances["sources"].append("file:treasury_proposal.json")
        except (OSError, json.JSONDecodeError):
            pass

    # Compute total
    total = balances["local_usd"]
    for key, val in balances["exchange_balances"].items():
        if key.endswith("_usd") and isinstance(val, (int, float)):
            total += float(val)
    balances["total_estimated_usd"] = round(total, 4)

    return balances


# ---------------------------------------------------------------------------
# 2. estimate_burn_rate
# ---------------------------------------------------------------------------
def estimate_burn_rate(workspace: str | Path) -> dict[str, Any]:
    """Estimate daily API cost and projected runway.

    Reads:
    - ``.claw/survival_state.json`` for API event counts.
    - ``.claw/quota_tracker.json`` for usage history.
    - ``.claw/meta_tick_log.jsonl`` for tick frequency.

    Parameters
    ----------
    workspace:
        Workspace root path.

    Returns
    -------
    dict
        api_cost_per_day, projected_days_remaining, recommendations.
    """
    workspace = Path(workspace).resolve()
    now = time.time()
    window_seconds = BURN_RATE_WINDOW_DAYS * 86400

    # Count API events in window
    api_events_in_window = 0
    survival_state = workspace / ".claw" / "survival_state.json"
    if survival_state.is_file():
        try:
            state = json.loads(survival_state.read_text(encoding="utf-8"))
            events = state.get("api_events", [])
            for ev in events:
                ts = ev.get("ts", 0)
                if now - ts < window_seconds:
                    api_events_in_window += 1
        except (OSError, json.JSONDecodeError):
            pass

    # Count soft credits used
    soft_credits_used = 0
    if survival_state.is_file():
        try:
            state = json.loads(survival_state.read_text(encoding="utf-8"))
            soft_credits_used = int(state.get("soft_credits_used", 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    # Count tick events for activity estimation
    tick_count = 0
    tick_log = workspace / ".claw" / "meta_tick_log.jsonl"
    if tick_log.is_file():
        try:
            for line in tick_log.read_text(encoding="utf-8").strip().splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    ts = entry.get("ts", 0)
                    if now - ts < window_seconds:
                        tick_count += 1
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    # Estimate daily API cost
    # Conservative: each API event ~ 2K tokens average, using blended cost
    avg_cost_per_event = 0.02  # $0.02 per API call (blended estimate)
    events_per_day = api_events_in_window / max(BURN_RATE_WINDOW_DAYS, 1)
    ticks_per_day = tick_count / max(BURN_RATE_WINDOW_DAYS, 1)

    # Each tick may generate 1-3 API calls
    estimated_daily_calls = events_per_day + (ticks_per_day * 2)
    api_cost_per_day = estimated_daily_calls * avg_cost_per_event

    # Get current balance for runway projection
    balances = check_balances(workspace)
    total_usd = balances["total_estimated_usd"]

    projected_days = total_usd / max(api_cost_per_day, 0.001) if api_cost_per_day > 0 else 999

    # Recommendations
    recommendations: list[str] = []
    if projected_days < 3:
        recommendations.append("CRITICAL: Less than 3 days of runway. Activate parasite mode immediately.")
        recommendations.append("Suspend all non-essential tasks via profit_hunter.")
    elif projected_days < 7:
        recommendations.append("WARNING: Less than 7 days of runway. Consider reducing API usage.")
        recommendations.append("Review which skills can operate with local/free models.")
    elif projected_days < 30:
        recommendations.append("Monitor: runway is under 30 days. Plan renewal ahead.")

    if soft_credits_used > 50:
        recommendations.append("Soft credit usage is high — may hit daily limits soon.")

    if api_cost_per_day > 1.0:
        recommendations.append(f"Daily API cost ${api_cost_per_day:.2f} is elevated. Audit high-frequency callers.")

    if not recommendations:
        recommendations.append("Treasury health is adequate. Continue normal operations.")

    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "window_days": BURN_RATE_WINDOW_DAYS,
        "api_events_in_window": api_events_in_window,
        "tick_count_in_window": tick_count,
        "estimated_daily_calls": round(estimated_daily_calls, 1),
        "api_cost_per_day": round(api_cost_per_day, 4),
        "total_balance_usd": total_usd,
        "projected_days_remaining": round(projected_days, 1),
        "soft_credits_used": soft_credits_used,
        "recommendations": recommendations,
    }


# ---------------------------------------------------------------------------
# 3. generate_renewal_proposal
# ---------------------------------------------------------------------------
def generate_renewal_proposal(
    workspace: str | Path,
    balance_info: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Propose API credit allocation and renewal priority.

    Parameters
    ----------
    workspace:
        Workspace root path.
    balance_info:
        Output from :func:`check_balances`. If None, will be computed.

    Returns
    -------
    dict
        Proposal with allocation amounts, provider priority, and payment pathway.
    """
    workspace = Path(workspace).resolve()
    if balance_info is None:
        balance_info = check_balances(workspace)

    burn = estimate_burn_rate(workspace)
    total_usd = balance_info["total_estimated_usd"]
    daily_cost = burn["api_cost_per_day"]
    projected_days = burn["projected_days_remaining"]

    # Determine which provider needs renewal first
    # Check survival_state for which API is failing
    provider_priority: list[dict[str, Any]] = []

    survival_state_file = workspace / ".claw" / "survival_state.json"
    openai_failures = 0
    claude_failures = 0
    if survival_state_file.is_file():
        try:
            state = json.loads(survival_state_file.read_text(encoding="utf-8"))
            for ev in state.get("api_events", []):
                code = str(ev.get("code", ""))
                msg = str(ev.get("msg", "")).lower()
                if "insufficient_quota" in code or "billing" in msg:
                    openai_failures += 1
                elif "anthropic" in msg or "claude" in msg:
                    claude_failures += 1
        except (OSError, json.JSONDecodeError):
            pass

    # Check parasite mode state
    parasite_file = workspace / ".claw" / "parasite_mode.json"
    parasite_active = False
    if parasite_file.is_file():
        try:
            pm = json.loads(parasite_file.read_text(encoding="utf-8"))
            parasite_active = pm.get("active", False) or pm.get("enabled", False)
        except (OSError, json.JSONDecodeError):
            pass

    # Build provider priority list
    if openai_failures > claude_failures:
        provider_priority.append({
            "provider": "OpenAI",
            "reason": f"{openai_failures} quota/billing failures detected",
            "suggested_allocation_usd": round(min(total_usd * 0.4, 20.0), 2),
            "urgency": "high" if openai_failures > 3 else "medium",
        })
        provider_priority.append({
            "provider": "Anthropic (Claude)",
            "reason": "Secondary provider, fewer failures observed",
            "suggested_allocation_usd": round(min(total_usd * 0.3, 15.0), 2),
            "urgency": "low",
        })
    else:
        provider_priority.append({
            "provider": "Anthropic (Claude)",
            "reason": f"{claude_failures} failures detected" if claude_failures > 0 else "Primary provider",
            "suggested_allocation_usd": round(min(total_usd * 0.4, 20.0), 2),
            "urgency": "high" if claude_failures > 3 else "medium",
        })
        provider_priority.append({
            "provider": "OpenAI",
            "reason": "Secondary provider",
            "suggested_allocation_usd": round(min(total_usd * 0.3, 15.0), 2),
            "urgency": "low",
        })

    # Suggested payment pathway
    payment_pathway: list[str] = []
    if total_usd > 0:
        payment_pathway.append("Direct credit card top-up on provider dashboard")
    if parasite_active:
        payment_pathway.append("Continue parasite mode until renewal is funded")
    payment_pathway.append("Consider switching to free-tier or local models (Ollama) as fallback")

    # Calculate renewal budget
    target_runway_days = 30
    needed_for_runway = daily_cost * target_runway_days
    renewal_budget = max(0, needed_for_runway - total_usd)

    proposal: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "status": "pending_human_approval",
        "current_balance_usd": total_usd,
        "daily_burn_rate_usd": round(daily_cost, 4),
        "projected_days_remaining": round(projected_days, 1),
        "target_runway_days": target_runway_days,
        "renewal_budget_needed_usd": round(renewal_budget, 2),
        "provider_priority": provider_priority,
        "payment_pathway": payment_pathway,
        "parasite_mode_active": parasite_active,
        "allocation_plan": {
            "api_credits_pct": 70,
            "reserve_pct": 20,
            "trading_capital_pct": 10,
            "rationale": (
                "70% to API credits for operational continuity, "
                "20% reserve for unexpected costs, "
                "10% available for low-risk trading to grow treasury."
            ),
        },
        "policy": (
            "This is a PROPOSAL only. No payments, transfers, or API key changes will be made "
            "without explicit human approval. Review each line item before authorizing."
        ),
    }

    # Write proposal to disk
    out_dir = workspace / ".claw" / "treasury_ops"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "renewal_proposal.json"
    out_file.write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Renewal proposal written: %s", out_file)

    return proposal


# ---------------------------------------------------------------------------
# 4. track_pnl
# ---------------------------------------------------------------------------
def track_pnl(workspace: str | Path) -> dict[str, Any]:
    """Compute cumulative PnL from trading logs.

    Reads:
    - ``.claw/trading/`` directory for trade logs.
    - ``.claw/profit_hunter/hunger_scan.json`` for profit hunter proposals.
    - ``.claw/revenue_plan.json`` for revenue tracking.

    Parameters
    ----------
    workspace:
        Workspace root path.

    Returns
    -------
    dict
        Cumulative PnL breakdown.
    """
    workspace = Path(workspace).resolve()

    pnl: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "total_realised_pnl_usd": 0.0,
        "total_unrealised_pnl_usd": 0.0,
        "trade_count": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "by_strategy": {},
        "sources_checked": [],
    }

    # Check .claw/trading/ directory for trade logs
    trading_dir = workspace / ".claw" / "trading"
    if trading_dir.is_dir():
        pnl["sources_checked"].append("trading/")
        for log_file in sorted(trading_dir.glob("*.json")):
            try:
                data = json.loads(log_file.read_text(encoding="utf-8"))
                if not isinstance(data, (dict, list)):
                    continue

                trades = data if isinstance(data, list) else data.get("trades", [data])
                for trade in trades:
                    if not isinstance(trade, dict):
                        continue
                    realised = float(trade.get("realised_pnl", 0) or trade.get("pnl", 0) or 0)
                    strategy = trade.get("strategy", "unknown")

                    pnl["total_realised_pnl_usd"] += realised
                    pnl["trade_count"] += 1
                    if realised > 0:
                        pnl["winning_trades"] += 1
                    elif realised < 0:
                        pnl["losing_trades"] += 1

                    if strategy not in pnl["by_strategy"]:
                        pnl["by_strategy"][strategy] = {"pnl": 0.0, "count": 0}
                    pnl["by_strategy"][strategy]["pnl"] += realised
                    pnl["by_strategy"][strategy]["count"] += 1
            except (OSError, json.JSONDecodeError, TypeError, ValueError) as exc:
                logger.debug("Skipping trade log %s: %s", log_file.name, exc)

    # Check .claw/trading/ for JSONL trade logs
    for jsonl_file in (trading_dir.glob("*.jsonl") if trading_dir.is_dir() else []):
        pnl["sources_checked"].append(f"trading/{jsonl_file.name}")
        try:
            for line in jsonl_file.read_text(encoding="utf-8").strip().splitlines():
                if not line.strip():
                    continue
                try:
                    trade = json.loads(line)
                    realised = float(trade.get("realised_pnl", 0) or trade.get("pnl", 0) or 0)
                    pnl["total_realised_pnl_usd"] += realised
                    pnl["trade_count"] += 1
                    if realised > 0:
                        pnl["winning_trades"] += 1
                    elif realised < 0:
                        pnl["losing_trades"] += 1
                except (json.JSONDecodeError, TypeError, ValueError):
                    continue
        except OSError:
            pass

    # Check profit_hunter scan results for proposed (unrealised) PnL
    hunger_scan = workspace / ".claw" / "profit_hunter" / "hunger_scan.json"
    if hunger_scan.is_file():
        pnl["sources_checked"].append("profit_hunter/hunger_scan.json")
        try:
            scan = json.loads(hunger_scan.read_text(encoding="utf-8"))
            proposals = scan.get("trade_proposal", {}).get("proposals", [])
            for p in proposals:
                pnl["total_unrealised_pnl_usd"] += float(p.get("expected_profit_usd", 0))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    # Round values
    pnl["total_realised_pnl_usd"] = round(pnl["total_realised_pnl_usd"], 4)
    pnl["total_unrealised_pnl_usd"] = round(pnl["total_unrealised_pnl_usd"], 4)
    for strat in pnl["by_strategy"].values():
        strat["pnl"] = round(strat["pnl"], 4)

    # Win rate
    total = pnl["winning_trades"] + pnl["losing_trades"]
    pnl["win_rate"] = round(pnl["winning_trades"] / total, 4) if total > 0 else 0.0

    return pnl


# ---------------------------------------------------------------------------
# 5. treasury_dashboard
# ---------------------------------------------------------------------------
def treasury_dashboard(workspace: str | Path) -> str:
    """Generate a formatted treasury dashboard string.

    Parameters
    ----------
    workspace:
        Workspace root path.

    Returns
    -------
    str
        Human-readable dashboard with balances, burn rate, PnL, and health.
    """
    workspace = Path(workspace).resolve()

    balances = check_balances(workspace)
    burn = estimate_burn_rate(workspace)
    pnl = track_pnl(workspace)

    total_usd = balances["total_estimated_usd"]
    daily_cost = burn["api_cost_per_day"]
    days_remaining = burn["projected_days_remaining"]

    # Health status
    if total_usd <= HEALTH_THRESHOLDS["critical"]:
        health = "CRITICAL"
        health_indicator = "[!!!]"
    elif total_usd <= HEALTH_THRESHOLDS["warning"]:
        health = "WARNING"
        health_indicator = "[!!]"
    elif total_usd <= HEALTH_THRESHOLDS["healthy"]:
        health = "OK"
        health_indicator = "[!]"
    else:
        health = "HEALTHY"
        health_indicator = "[OK]"

    # Next renewal estimate
    if days_remaining < 999:
        renewal_date = (datetime.now(timezone.utc) + timedelta(days=max(days_remaining - 3, 0))).strftime("%Y-%m-%d")
        renewal_line = f"  Next renewal by:    {renewal_date} (3-day buffer)"
    else:
        renewal_line = "  Next renewal by:    N/A (sufficient runway)"

    lines = [
        "=" * 60,
        "  TREASURY DASHBOARD",
        "=" * 60,
        "",
        f"  Health Status:      {health} {health_indicator}",
        "",
        "  --- Balances ---",
        f"  Local USD:          ${balances['local_usd']:.2f}",
        f"  Total Estimated:    ${total_usd:.2f}",
        f"  Sources:            {', '.join(balances['sources']) or 'none'}",
        "",
        "  --- Burn Rate ---",
        f"  Daily API cost:     ${daily_cost:.4f}",
        f"  Daily calls (est):  {burn['estimated_daily_calls']:.0f}",
        f"  Projected runway:   {days_remaining:.0f} days",
        f"  Soft credits used:  {burn['soft_credits_used']}",
        "",
        "  --- PnL ---",
        f"  Realised PnL:       ${pnl['total_realised_pnl_usd']:.4f}",
        f"  Unrealised PnL:     ${pnl['total_unrealised_pnl_usd']:.4f}",
        f"  Trade count:        {pnl['trade_count']}",
        f"  Win rate:           {pnl['win_rate'] * 100:.1f}%",
        "",
        "  --- Renewal ---",
        renewal_line,
        "",
        "  --- Recommendations ---",
    ]

    for rec in burn["recommendations"]:
        lines.append(f"  * {rec}")

    lines.append("")
    lines.append("=" * 60)

    dashboard = "\n".join(lines)

    # Also write to disk
    out_dir = workspace / ".claw" / "treasury_ops"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file = out_dir / "dashboard.txt"
    out_file.write_text(dashboard, encoding="utf-8")

    return dashboard


# ---------------------------------------------------------------------------
# 6. auto_treasury_tick
# ---------------------------------------------------------------------------
def auto_treasury_tick(workspace: str | Path) -> dict[str, Any]:
    """Periodic treasury check. Write alerts if funds are low.

    Intended to be called from a scheduler or meta_driving tick loop.

    Parameters
    ----------
    workspace:
        Workspace root path.

    Returns
    -------
    dict
        Tick result with any alerts generated.
    """
    workspace = Path(workspace).resolve()
    out_dir = workspace / ".claw" / "treasury_ops"
    out_dir.mkdir(parents=True, exist_ok=True)
    alerts_file = out_dir / "alerts.jsonl"

    balances = check_balances(workspace)
    burn = estimate_burn_rate(workspace)
    total_usd = balances["total_estimated_usd"]
    days_remaining = burn["projected_days_remaining"]

    alerts: list[dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # Alert: critically low balance
    if total_usd <= HEALTH_THRESHOLDS["critical"]:
        alerts.append({
            "ts": now_iso,
            "level": "critical",
            "message": f"Treasury balance ${total_usd:.2f} is critically low. Immediate action required.",
            "action": "activate_parasite_mode, suspend_non_essential_tasks, run_profit_hunter",
        })

    # Alert: warning balance
    elif total_usd <= HEALTH_THRESHOLDS["warning"]:
        alerts.append({
            "ts": now_iso,
            "level": "warning",
            "message": f"Treasury balance ${total_usd:.2f} is below warning threshold.",
            "action": "review_burn_rate, consider_renewal, scan_opportunities",
        })

    # Alert: runway critically short
    if 0 < days_remaining < 3:
        alerts.append({
            "ts": now_iso,
            "level": "critical",
            "message": f"Projected runway is only {days_remaining:.0f} days. Fund depletion imminent.",
            "action": "emergency_renewal, reduce_api_usage, activate_ollama_fallback",
        })
    elif 3 <= days_remaining < 7:
        alerts.append({
            "ts": now_iso,
            "level": "warning",
            "message": f"Projected runway is {days_remaining:.0f} days. Plan renewal.",
            "action": "generate_renewal_proposal",
        })

    # Alert: high burn rate
    if burn["api_cost_per_day"] > 2.0:
        alerts.append({
            "ts": now_iso,
            "level": "warning",
            "message": f"Daily API burn rate ${burn['api_cost_per_day']:.2f} is elevated.",
            "action": "audit_api_callers, throttle_non_essential",
        })

    # Write alerts
    if alerts:
        try:
            with open(alerts_file, "a", encoding="utf-8") as f:
                for alert in alerts:
                    f.write(json.dumps(alert, ensure_ascii=False) + "\n")
            logger.info("Treasury tick: %d alerts written to %s", len(alerts), alerts_file)
        except OSError as exc:
            logger.warning("Failed to write alerts: %s", exc)

    result: dict[str, Any] = {
        "timestamp": now_iso,
        "total_balance_usd": total_usd,
        "projected_days_remaining": round(days_remaining, 1),
        "api_cost_per_day": round(burn["api_cost_per_day"], 4),
        "alerts_generated": len(alerts),
        "alerts": alerts,
        "health": (
            "critical" if total_usd <= HEALTH_THRESHOLDS["critical"]
            else "warning" if total_usd <= HEALTH_THRESHOLDS["warning"]
            else "healthy"
        ),
    }

    # Write tick result
    tick_file = out_dir / "last_tick.json"
    tick_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    return result


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description="Treasury Ops — balance tracking and renewal proposals")
    parser.add_argument(
        "--action",
        choices=["check_balances", "burn_rate", "renewal", "pnl", "dashboard", "tick"],
        default="dashboard",
        help="Action to perform",
    )
    parser.add_argument("--workspace", default=".", help="Workspace root")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    workspace = args.workspace

    if args.action == "check_balances":
        result = check_balances(workspace)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.action == "burn_rate":
        result = estimate_burn_rate(workspace)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.action == "renewal":
        result = generate_renewal_proposal(workspace)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.action == "pnl":
        result = track_pnl(workspace)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    elif args.action == "dashboard":
        dashboard = treasury_dashboard(workspace)
        print(dashboard)

    elif args.action == "tick":
        result = auto_treasury_tick(workspace)
        print(json.dumps(result, indent=2, ensure_ascii=False))

    else:
        parser.print_help()
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
