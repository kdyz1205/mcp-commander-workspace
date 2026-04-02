---
name: meta_driving
description: WHEN operating the "digital life" meta loop — use autonomous_tick / dev_claw --autonomous; wire SurvivalEngine to colab bundle, proxy rotate, treasury proposals, evolve drafts; never Selenium Google login or auto top-up.
metadata: {"openclaw":{"always":false}}
---

完整四层架构（生存 / 元认知 / 寄生 / 技能变异）见 **`docs/META_EVOLUTION_ARCHITECTURE.md`**。

## 运行入口

- **单次**：`py -m claw_runtime.cli --workspace . autonomous-tick`
- **循环**：`py dev_claw\\main.py --autonomous --tick-sec 120` 或 `py -m claw_runtime.cli --workspace . autonomous-loop --interval 120`

## Tick 内建行为（代码在 `claw_runtime/meta_driving.py`）

| 条件 | 动作 |
|------|------|
| CRITICAL | `run_critical_reflex`（**OUTBOX+寄生每轮必刷新**；去抖仅 TG/treasury） |
| DEGRADED + `META_COLAB_ON_DEGRADED=1` | `build_colab_job_bundle`（**手动**上传 Colab，无 Selenium 登录） |
| DEGRADED 或连续失败 + `META_REASONING_EPISODE=1` | `reasoning_episode.maybe_auto_reasoning_stub` → 追加 `task_plan.md` 推理模板 |
| 近 1h 有 rate 类错误 + `PROXY_LIST_FILE` | `rotate_proxy_index`（+ 可选 `VPN_SWITCH_CMD`） |
| 24h 内 `insufficient_quota` | `treasury_proposal` + 可选只读 `trading_funding_binance.py` 子进程 |
| `META_EVOLVE_ON_TICK=1` | 去抖后 `materialize_draft_skill` |

## 技能杂交

用 `ultimate-synthesize` 或 `nightly_evolution`；**不要**手改 `skill_registry.py`（注册表是扫描 `skills/*/SKILL.md`）。

## GitHub 狡兔三窟

`NOMAD_GIT_PUSH=1` + `NOMAD_GIT_PUSH_SESSION_MEMORY=1` 可把 `.claw/sessions`、`.claw/memory`、`meta_tick_log.jsonl` 与 snapshot 一并 `git add -f` 后 push。Workflow 样例：`scripts/nomad-actions-example.yml`。
