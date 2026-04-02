# 自进化元驱动引擎 — 核心架构

本文档把「数字生命」栈的四根支柱映射到仓库内**具体模块**与**安全边界**（无无人托管资金：不自动链上提款、不自动 OpenAI/代充 API）。

---

## 1. 痛觉与饥饿感（Survival Engine）

**职责：** 把主机与 API 的「生理指标」压成可机读状态，供门禁与反射使用。

| 信号 | 实现 | 说明 |
|------|------|------|
| API 余额 / 计费失败 | `SurvivalEngine.record_api_error` + `check_quota` | `insufficient_quota`、软预算 `SURVIVAL_SOFT_CREDIT_DAILY` |
| 硬盘与内存 | `check_vitals` + `assess_survival_state` | `SURVIVAL_DISK_*_GB`、`SURVIVAL_MEM_*_PCT` |
| 任务失败率 | `record_task_outcome` → `consecutive_failures` | 与越狱 escalation、`META_REASONING_*` 联动 |

**「饥饿」语义：** `snapshot()["hunger"]` 汇总「计费饥饿、软预算耗尽、无 Key、连续任务失败」等布尔/计数，便于日志与 TG 展示；**本能反应**在代码层对应为：

- **打工（廉价算力）：** 寄生模式 + Ollama（`DEVCLAW_PARASITE_MODE` / `run_critical_reflex`）
- **求援（人类/前台）：** `CURSOR_OUTBOX.md`、TG 广播、`treasury_proposal`（仅提案，不自动划账）

**入口文件：** `claw_runtime/survival_engine.py`

---

## 2. 元认知推理环（Meta-Cognition Loop）

**思维草稿纸：** 工作区根目录 `task_plan.md`（与 `.cursorrules`、数字工程师 hire 流一致）。

**假设–验证–修正：**

- 结构化追加：`claw_runtime/reasoning_episode.py` → `append_reasoning_episode`
- 自动模板（去抖）：`META_REASONING_EPISODE=1` 时由 `meta_driving.autonomous_tick` 在 DEGRADED 或连续失败时调用 `maybe_auto_reasoning_stub`
- **DevClaw 单次循环**在系统提示中要求：行动前在脑中走「若策略 A 失败 → 可能原因 B → 查资料 C → 改代码 D」，并**优先**把结论写入 `task_plan.md`（见 `dev_claw/main.py` 内 `base_core`）

**入口：** `dev_claw/main.py`（人机对话主环）、`claw_runtime/meta_driving.py`（自治 tick）

---

## 3. 资源掠夺与寄生（Parasite Protocol）

**触发：** `insufficient_quota`（及可选 `DEVCLAW_REFLEX_ON_429`）→ `run_critical_reflex`；TG 心跳在 CRITICAL 时同样可走反射。

**行为（每轮 CRITICAL 必做）：** 打开寄生标记、**刷新** `CURSOR_OUTBOX.md`，把前台 Cursor 当作「代工厂」承接上下文与下一步指令；TG 侧去抖仅影响重复告警，不阻止 OUTBOX 刷新。

**入口文件：** `claw_runtime/survival_reflex.py`，协同 `tg_dev_claw.py`、`dev_claw/main.py`

---

## 4. 自主学习与基因重载（Skill Mutation）

| 能力 | 实现 |
|------|------|
| 抓取外部文档 | DevClaw 工具 `web_fetch`；Cursor 侧可用 Web MCP |
| 技能编码 | 遵循 OpenClaw 约定：主要载体为 `skills/<name>/SKILL.md`（YAML frontmatter） |
| 安装 / 扩展 | `load_skill`、`install_claw_skill`（需 `DEVCLAW_ALLOW_SKILL_INSTALL=1`）、CLI `skills-install` |
| 草稿物化 | `META_EVOLVE_ON_TICK=1` → `materialize_draft_skill`（`nightly_evolution`） |

**「热加载」：** 每轮工具循环内 `SkillRegistry` 会重新 `catalog_text()`，新安装的 SKILL.md 可在**下一轮**被模型 `load_skill` 拉取。若需可执行逻辑，放在 `tools/` 或 `claw_runtime/` 并由文档或终端调用——**不把任意 `.py` 当作技能解释器执行**（安全边界）。

**参考：** `skills/evolution_brain/SKILL.md`、`claw_runtime/skill_registry.py`

---

## 环境变量速查

见根目录 `env.example`：`META_REASONING_*`、`META_COLAB_*`、`SURVIVAL_*`、`DEVCLAW_REFLEX_*`、`META_EVOLVE_ON_TICK` 等。

---

## 相关技能

- `meta_driving` — 自治 tick 组合动作表
- `evolution_brain` — 闭环行为与禁止项
