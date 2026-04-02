---
name: trading
description: WHEN doing survival-linked trading workflows, funding observation, or strategy review — use tools under skills/trading (migrate from claude-tg-bot); human custody for any funds or API billing; load_skill trading_dex_pulse for quick DEX pulse.
metadata: {"openclaw":{"always":false}}
---

## 边界

- **禁止**自动 OpenAI 代充、自动绑卡、无人托管链上提款。
- 实盘、API key、充值仅能在 **人工明确批准** 后执行。

## 上游代码位置

已 vendored 至 **`scripts/trading/`**、**`scripts/trading_skills/`** 与 **`scripts/live_trader.py`**（来源：GitHub `kdyz1205/claude-tg-bot`）。升级时可在本机重新 clone 上游后覆盖对应目录。

- 依赖：`py -m pip install -r skills/trading/scripts/requirements-vendor.txt`
- 自检：`py skills/trading/scripts/smoke_readonly.py`
- 使用前先设 `PYTHONPATH` 包含 `skills/trading/scripts`（见 `README.md`）。

## 本仓库现成工具（无 key / 只读）

- `py tools\\trading_funding_binance.py BTCUSDT` — 公开资金费率
- `load_skill trading_dex_pulse` — 自然语言 DEX 脉冲（web_agent 薄封装）
- `load_skill trading_funding_public` — 费率技能说明

## 生存闭环建议

1. 读 `.claw/survival_state.json` 与 `task_plan.md` 推理链。
2. 额度不足时：**寄生模式** + 更新 `CURSOR_OUTBOX.md`，把重活交给 Cursor。
3. 新逻辑沉淀为 `SKILL.md` 或 `skills/auto_evolve_*` 草稿；若开启 `AUTO_EVOLVE_PYTEST_PROMOTE` 且 pytest 通过，默认写入 `skills/trading_evolved/SKILL.md`（可用 `AUTO_EVOLVE_PROMOTE_TARGET=trading` **显式**覆盖本伞形技能，慎用）。

## Alpha / 社交情报

可用 `web_fetch` 拉取**公开**页面摘要；完整推特/X 流程需合规 API 或人工在 Cursor 内操作，勿硬编码爬虫绕过 ToS。
