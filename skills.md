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
| 生存反射 + 寄生 | `claw_runtime/survival_reflex.py` + `tg_dev_claw.py` 后台线程 | CRITICAL 时写根目录 `CURSOR_OUTBOX.md`、TG 广播、`set_parasite_mode`；`/parasite_off` 清除 | 与 `SURVIVAL_TICK_SEC` / `SURVIVAL_REFLEX_DEBOUNCE_SEC` 配合 |
| 意识路由 | `claw_runtime/consciousness_router.py` | TG 文本合并进 `system_append`；`TG_CONSCIOUSNESS_ROUTER=0` 关闭 | 轻量关键词路由，非云端大模型 |
| 夜间进化草稿 | `claw_runtime/nightly_evolution.py` | `py -m claw_runtime.cli --workspace . evolve-draft` 或 TG `/evolve` | 从 `.claw/evolution_failures.jsonl` 生成 `skills/auto_evolve_*` |
| 交易技能（可插拔） | `skills/trading_*` + `tools/trading_funding_binance.py` | `load_skill` 名见各 SKILL frontmatter | 重逻辑见 [claude-tg-bot/trading_skills](https://github.com/kdyz1205/claude-tg-bot/tree/master/trading_skills) |
| Ultimate 四维度（合规骨架） | `claw_runtime/ultimate/` + `ultimate-*` CLI | `load_skill ultimate_capabilities` | Colab 包、冷存+TG、自备代理轮换、nomad 快照+GHA 样例、子进程 mesh、技能杂交、venv 自愈脚本、只读 ETH 探测；**不**含自动 MEV/批量注册账号/黑产代理 |
| 元驱动 autonomous_tick | `claw_runtime/meta_driving.py` | `py dev_claw\\main.py --autonomous --tick-sec 120` 或 `py -m claw_runtime.cli autonomous-tick` | 无用户指令周期性自检；`load_skill meta_driving` / `proxy_rotator` |
| 进化大脑 / 推理闭环 | `claw_runtime/reasoning_episode.py` + `skills/evolution_brain` | `append_reasoning_episode` 或 `META_REASONING_EPISODE=1` + `autonomous_tick` | 假设→验证→修正写入 `task_plan.md`；**不**含自动提款/代充 |

## 登记规范

1. 新技能先在终端**跑通**再登记。
2. 同步更新 `skills/skills.json`（`id`、`path`、`invoke`、`notes`）。
3. 若仅为一次性实验，放在 `temp_tools/` 或 `tools/scratch_*`，**不**登记，直到稳定复用。
