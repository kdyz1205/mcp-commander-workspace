# DevClaw Evolution Backlog

## Active Tasks
- [x] 并发消息处理：input truncation + injection safety verified via pytest
- [x] 上下文记忆持久化：persistent_memory.py, 3/3 pytest passed
- [ ] 智能路由优化：Ollama说"不知道"时自动升级到Claude CLI，但"价格"类需要直接走Claude CLI不要先问Ollama
- [ ] consciousness_seed空转修复：Generation递增但实际没学到新东西
- [ ] 接入Solana链上监控：用户需要实时监控Solana meme币
- [ ] 语音消息处理：TG收到语音消息时自动转文字再处理
- [ ] 自动充值闭环：交易盈利后自动提议API续费方案

## Failed (需人工介入)
(empty)

## Completed
(empty)
- [RESEARCH] 为 WebLLMProxy 实现多源负载均衡：当 Claude 额度耗尽时自动切换到 Groq，两者都不可用时回退到 Ollama
