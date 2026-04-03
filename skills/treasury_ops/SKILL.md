---
name: treasury_ops
description: Post-profit treasury management — track balances, propose withdrawals, suggest API credit renewal
triggers: After successful trade, periodic treasury review, fund_balance change
category: funding
safety: proposal-only, requires human approval for actual transfers
---

## Purpose

Manages the operational treasury after profits are realised. Tracks balances across local state and exchange APIs, estimates burn rate, proposes API credit renewals, and maintains a dashboard of financial health. All actions are proposals — no automatic transfers or payments.

## Capabilities

1. **Balance check** — aggregates balances from `.claw/fund_estimate.json` and public exchange endpoints.
2. **Burn rate estimation** — projects API cost per day, days remaining, and recommendations.
3. **Renewal proposal** — suggests how much to allocate for API credits, which provider needs renewal first, and payment pathway.
4. **PnL tracking** — reads `.claw/trading/` logs to compute cumulative profit and loss.
5. **Treasury dashboard** — formatted summary of balances, burn rate, PnL, next renewal date, and health status.
6. **Auto treasury tick** — periodic check that writes alerts to `.claw/treasury_ops/alerts.jsonl` if funds are low.

## Runner

```
py skills/treasury_ops/runner.py --action dashboard --workspace .
py skills/treasury_ops/runner.py --action check_balances --workspace .
py skills/treasury_ops/runner.py --action burn_rate --workspace .
py skills/treasury_ops/runner.py --action tick --workspace .
```

## Safety

- **Proposal-only**: no transfers, payments, or API key rotations are executed automatically.
- Human approval is required for every financial action.
- All data comes from local state files and public endpoints.

## State

All state is stored under `<workspace>/.claw/treasury_ops/`.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `TREASURY_OPS_WORKSPACE` | `.` | Workspace root |
| `TREASURY_BURN_RATE_WINDOW_DAYS` | `7` | Days of history for burn rate estimation |
| `TREASURY_LOW_BALANCE_THRESHOLD_USD` | `5.0` | USD threshold that triggers low-balance alerts |
