# DevClaw Evolution Backlog

## Active Tasks
- [x] 新任务：实现 LOB 做市策略核心引擎。完整规格见 docs/lob_market_making_spec.md。核心要求：1) NumPy/Polars 向量化实现 25 因子（OFI、WAP、Imbalance、Depth Entropy、Micro-price）2) 交叉盘硬过滤 3) SHAP 权重反推 4) MC Dropout 确信度校验。→ skills/sk_lob_engine/ (28/28 tests passed)
- [DONE] [RESEARCH] 实现 LOB 做市策略核心引擎：基于 docs/lob_market_making_spec.md 规格，用 NumPy/Polars 实现 25 因子毫秒级计算（OFI、WAP、Imbalance、Depth Entropy、Micro-price），含 SHAP 权重反推 + MC Dropout 确信度校验
- [x] 并发消息处理：input truncation + injection safety verified via pytest
- [x] 上下文记忆持久化：persistent_memory.py, 3/3 pytest passed
- [x] 智能路由优化：Ollama说"不知道"时自动升级到Claude CLI，但"价格"类需要直接走Claude CLI不要先问Ollama
- [x] consciousness_seed空转修复：Generation递增但实际没学到新东西
- [x] 接入Solana链上监控：用户需要实时监控Solana meme币
- [x] 语音消息处理：TG收到语音消息时自动转文字再处理
- [x] 自动充值闭环：交易盈利后自动提议API续费方案

## Failed (需人工介入)
(empty)

## Completed
(empty)
- [DONE] [RESEARCH] 为 WebLLMProxy 实现多源负载均衡：当 Claude 额度耗尽时自动切换到 Groq，两者都不可用时回退到 Ollama
- [DONE] [RESEARCH] 实时物理监控：编写 skills/sk_sys_info/runner.py，获取当前系统CPU占用率和磁盘剩余空间，写入 .auth/vitals.json 的 cpu_percent 和 disk_free_gb 字段
- [DONE] [PROFIT] 编写 skills/sk_market_scout/runner.py，通过 Jupiter/DexScreener 公开 API 获取 SOL/USDC 实时价格，写入 .auth/market_vitals.json
- [RESEARCH] 构建 LOB 特征处理管道：实现 mid-price/WAP/imbalance 计算，输出 [1,20,40] 张量 (sk_lob_processor)
- [RESEARCH] 构建 GA 进化引擎：高斯/镜像/灾变变异 + API限流惩罚适应度函数 (core/evolution_engine.py + core/fitness.py)
