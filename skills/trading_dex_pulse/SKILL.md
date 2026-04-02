---
name: trading_dex_pulse
description: WHEN user asks DEX pairs, liquidity, token discovery, or “链上/Dex 扫一眼” — use web_agent or public Dex HTTP, not paid chain indexers unless configured.
metadata: {"openclaw":{"requires":{"env":[]}}}
---

## 流程

1. `execute_terminal`: `py tools\\web_agent.py "<自然语言查询>"`（工作区根相对路径按 OS 调整）。
2. 若需原始 JSON：优先 `web_fetch` 公开文档或 DexScreener 等**允许**的端点；遵守 robots/ToS。
3. 将结论写短：价格/池子/风险提示；**非投资建议**。

## 与旧 bot 的关系

重逻辑曾在 [claude-tg-bot trading_skills](https://github.com/kdyz1205/claude-tg-bot/tree/master/trading_skills)；此处为可插拔 Skill，不捆绑整个仓库。
