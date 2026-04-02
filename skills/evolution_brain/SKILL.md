---
name: evolution_brain
description: WHEN building closed-loop "earn → learn → upgrade strategy" or meta-reasoning — use task_plan.md episodes, memory, web_fetch, skill-creator; NEVER auto-withdraw or auto top-up APIs without human custody.
metadata: {"openclaw":{"always":false}}
---

## 你能做什么（推理链条）

DevClaw **本身就是** LLM 推理引擎：工具循环 = **假设 → 工具验证 → 根据输出修正**。本技能把这一过程**显式化**到仓库里，便于复盘与进化。

1. **写假设**  
   用 `edit_local_file` 在 `task_plan.md` 追加一节（或依赖 `META_REASONING_EPISODE=1` 时 meta_tick 自动追加模板）。  
   例：「当前波动升高，原动量阈值可能过低导致滑点；需验证 ATR 自适应是否降低假信号。」

2. **验证**  
   - `web_fetch` 公开论文/文档摘要（遵守站点条款）。  
   - `execute_terminal` 跑只读统计、小样本回测、lint/test。  
   - **禁止**为「绕过付费墙」而做未授权抓取。

3. **修正**  
   - `append_typed_memory` → `lesson` 记录可复用结论。  
   - `load_skill skill-creator` → 把流程固化为新 `skills/<name>/SKILL.md`（可配合 `ultimate-synthesize` 合并两技能）。

4. **痛觉联动**  
   - `SurvivalEngine`：`DEGRADED` / `consecutive_failures` 高 → 优先写推理再改代码。  
   - `autonomous_tick` / TG 自主心跳：可触发维护或行情扫视，**不**等于自动盈利。

## 你不能做什么（合规与托管）

- **不**在本仓库内实现：从交易所**自动划利润**、调用**OpenAI/第三方代充**完成充值、无审批**实盘 MEV**。这些涉及**托管资金与支付**，必须由人类或多签流程显式授权。  
- 交易执行仅能在**你已自行接入、自担风险**的脚本中进行；本技能只描述**推理与工程习惯**。

## 「打工养活自己」的现实闭环

1. 策略收益 → **人工或独立托管系统**入账 / 划转。  
2. `SurvivalEngine` / `treasury_proposal.json` → 提醒额度与账单。  
3. 人类充值或切换 **寄生 / Ollama**。  
Bot 负责**记录、推理、提案**，不负责**替你签字划链**。
