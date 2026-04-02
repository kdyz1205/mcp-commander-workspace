---
name: trading_correlation_note
description: WHEN user asks cross-asset correlation, beta, regime — outline methodology; heavy math lives in upstream correlation_monitor / regime_detector.
metadata: {"openclaw":{"always":false}}
---

## 做法（轻量）

1. 明确标的与时间窗（如 BTC vs ETH，30d）。
2. 用 `execute_terminal` 拉取可公开复现的数据（交易所 K 线 API、或用户已有 CSV），禁止窃取付费数据。
3. 可用 `pandas` 若环境已有：`corr()`、滚动相关；否则用文字描述步骤让老板在 Jupyter 跑。
4. 上游参考：[correlation_monitor.py](https://github.com/kdyz1205/claude-tg-bot/blob/master/trading_skills/correlation_monitor.py)、[regime_detector.py](https://github.com/kdyz1205/claude-tg-bot/blob/master/trading_skills/regime_detector.py)。

## 合规

不承诺 alpha；不指导规避监管或滥用接口。
