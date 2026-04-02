# trading 技能目录

## 已内置上游代码

`scripts/` 目录已从公开仓库 **[kdyz1205/claude-tg-bot](https://github.com/kdyz1205/claude-tg-bot)** 同步：

- `scripts/trading/` — 核心交易包（OKX、回测、策略脑等）
- `scripts/trading_skills/` — 8 个量化技能模块（regime、funding scanner 等）
- `scripts/live_trader.py` — 根目录入口（`trading.live_trader` 动态加载依赖此文件）

布局假定「项目根」为 **`scripts/`**（即各模块里 `Path(__file__).parent.parent` 指向 `scripts/`）。勿单独移动 `trading/` 到别处，除非批量改路径。

## 依赖

```powershell
py -m pip install -r skills/trading/scripts/requirements-vendor.txt
```

完整 Bot 依赖见上游 `requirements.txt`（含 Solana、Playwright、Telegram 等）。

## 只读冒烟（无密钥、无网络）

```powershell
py skills/trading/scripts/smoke_readonly.py
```

## DevClaw / 终端

在仓库根执行时先把 `skills/trading/scripts` 加入 `PYTHONPATH`，或使用：

```powershell
$env:PYTHONPATH = "skills\trading\scripts"
py -c "import trading, trading_skills; print(trading.__doc__[:60])"
```

实盘、API key、充值仍须 **人工托管**。
