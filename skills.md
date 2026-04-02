# Skill library（沉淀与复用）

本文件是**人类可读索引**；机器可读登记见 `skills/skills.json`。

## 已登记技能

（Agent 在工具验证通过后追加一行：名称、路径、用法、日期）

| 技能 | 脚本/路径 | 调用方式 | 备注 |
|------|-----------|----------|------|
| Dex/现货抓取 | `tools/web_agent.py` | `py tools\web_agent.py "自然语言查询"` | DexScreener + 可选 CoinGecko |
| 雇佣数字工程师 | `digital_engineer/hire.py` | `py digital_engineer\hire.py --bot-root <路径> "任务"` | OpenClaw 式本地循环，需 OPENAI_API_KEY |
| TG 遥控 DevClaw | `tg_dev_claw.py` | `py tg_dev_claw.py` + 环境变量见 `env.example` | 手机发指令，本机跑工具循环并回推进度 |
| DevClaw 裸循环 | `dev_claw/main.py` | `py dev_claw\main.py --workspace <路径> "任务"` | 通用终端+写文件；含 OpenClaw 式 SKILL 热重载 |
| OpenClaw 运行时 | `claw_runtime/` + `skills/*/SKILL.md` | `load_skill` / `claw.config.json` | YAML frontmatter、插件 skills、安装/扫描/多 Agent CLI |
| Claw CLI | `python -m claw_runtime.cli` | `skills-install` / `safety-scan` / `plugins-list` / `multi-agent` | 见 `--workspace` |
| 生存引擎 Phase1 | `claw_runtime/survival_engine.py` | DevClaw 每轮 `heartbeat` + API 失败 `record_api_error`；`DEVCLAW_SURVIVAL_GATE=0` 可关闭门禁 | 状态 `.claw/survival_state.json`；对齐 claude-tg-bot 的 vital/quota 思路 |

## 登记规范

1. 新技能先在终端**跑通**再登记。
2. 同步更新 `skills/skills.json`（`id`、`path`、`invoke`、`notes`）。
3. 若仅为一次性实验，放在 `temp_tools/` 或 `tools/scratch_*`，**不**登记，直到稳定复用。
