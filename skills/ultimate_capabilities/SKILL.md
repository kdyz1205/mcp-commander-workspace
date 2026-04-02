---
name: ultimate_capabilities
description: WHEN user wants Colab offload, cold zip+TG, proxy rotation, nomad snapshot, subagent mesh, skill synthesis, venv rebuild, or treasury probe — use claw_runtime.ultimate + CLI ultimate-* (all opt-in, policy-bound).
metadata: {"openclaw":{"always":false}}
---

## 四大维度（落地边界）

1. **资源寄生**：`build_colab_job_bundle`（手动上传 Colab）、`compress_paths` + `telegram_send_document_if_configured`（仅你自己的 Bot+chat_id）、`rotate_proxy_index`（仅 `PROXY_LIST_FILE` 自备列表）。不自动批量注册第三方云账号；不抓取不可信免费代理池。
2. **基因重组**：`synthesize_two_skills` 或 CLI `ultimate-synthesize`；自愈脚本 `emit_rebuild_venv_scripts` → `scripts/self_heal_rebuild_venv.*`（需人工执行）。
3. **算力流浪**：`register_nomad_handlers` + `NOMAD_GIT_PUSH`；参考 `scripts/nomad-actions-example.yml`。子进程分工：`run_subagent_mesh`（本地 spawn 演示骨架）。
4. **自负盈亏**：仅 `probe_eth_balance` + `write_treasury_proposal_stub`（**不**自动签名、不 MEV、不撸空投）；链上支出必须你走托管/多签/人工审批。

## CLI（工作区根）

- `py -m claw_runtime.cli --workspace . ultimate-colab-bundle --paths tools/x.py,task_plan.md`
- `py -m claw_runtime.cli --workspace . ultimate-cold-zip --paths logs/a.txt`
- `py -m claw_runtime.cli --workspace . ultimate-tg-upload`（需 TG_* 冷存变量）
- `py -m claw_runtime.cli --workspace . ultimate-proxy-rotate`
- `py -m claw_runtime.cli --workspace . ultimate-mesh-demo --text "套利策略草稿"`
- `py -m claw_runtime.cli --workspace . ultimate-synthesize --a trading_dex_pulse --b trading_funding_public`
- `py -m claw_runtime.cli --workspace . ultimate-self-heal-emit`
- `py -m claw_runtime.cli --workspace . ultimate-nomad-snapshot`
- `py -m claw_runtime.cli --workspace . ultimate-treasury-probe`（需 `ETH_RPC_URL` + `ETH_TREASURY_ADDRESS`）

环境变量见 `env.example`。
