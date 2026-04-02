---
name: trading_funding_public
description: WHEN user asks funding rate, 资金费, perp 费率 for major symbols — call the small public Binance futures script (no key).
metadata: {"openclaw":{"requires":{"bins":["python"]}}}
---

## 步骤

1. `execute_terminal`: `py tools\\trading_funding_binance.py BTCUSDT`（可换 ETHUSDT 等 U 本位合约代码）。
2. 解析终端 JSON：`lastFundingRate`、`nextFundingTime`。
3. 说明费率方向（多付空 / 空付多）用一句话；声明数据来自公开接口、延迟与交易所规则。

## 扩展

更复杂的 `funding_scanner.py` 等仍在 [claude-tg-bot](https://github.com/kdyz1205/claude-tg-bot)；需要时可 `web_fetch` 其 README 再逐步移植。
