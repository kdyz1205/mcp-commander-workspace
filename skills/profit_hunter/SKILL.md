---
name: profit_hunter
description: When survival reports low funds, auto-suspend non-essential tasks, scan for low-risk arbitrage opportunities (funding rate, DEX arb)
triggers: survival_engine fund_low, API quota exhausted, explicit invocation
category: funding
safety: read-only scanning by default, requires explicit LIVE_TRADING=1 for execution
---

## Purpose

Emergency profit-seeking skill activated when the survival engine detects low fund balance or API quota exhaustion. Scans public market data for low-risk arbitrage opportunities (funding rate carry, cross-DEX spreads) and generates human-reviewable trade proposals. Never auto-executes unless `LIVE_TRADING=1` is explicitly set.

## Capabilities

1. **Funding rate scan** — queries OKX and Binance public endpoints for extreme funding rates suitable for delta-neutral carry.
2. **DEX arbitrage scan** — uses DexScreener public API to find cross-DEX price discrepancies on Solana and Ethereum.
3. **Profit estimation** — projects expected profit and assigns a risk score (low/medium/high) per opportunity.
4. **Trade proposal generation** — filters by risk, applies Kelly sizing, sets stop loss, and produces a structured proposal dict for human approval.
5. **Task suspension** — reads `.claw/persisted_tasks.jsonl` and suspends non-essential tasks to conserve compute and API quota.
6. **Hunger scan** — full pipeline: suspend tasks, scan markets, estimate profits, generate proposals, write report.

## Runner

```
py skills/profit_hunter/runner.py --action scan_funding
py skills/profit_hunter/runner.py --action scan_dex
py skills/profit_hunter/runner.py --action hunger_scan --workspace . --balance 5.0
```

## Safety

- All scanning uses **public, unauthenticated** API endpoints.
- Trade proposals are **never executed** unless `LIVE_TRADING=1` is set in environment.
- Proposals require human review before any capital is deployed.
- Position sizing uses half-Kelly with a hard 25% cap.

## State

Reports are written to `<workspace>/.claw/profit_hunter/`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `LIVE_TRADING` | `0` | Set to `1` to allow proposal execution (still requires explicit call) |
| `PROFIT_HUNTER_MAX_RISK` | `low` | Maximum risk level for auto-generated proposals |
| `PROFIT_HUNTER_CAPITAL_USD` | `100` | Default capital for profit estimation |
