"""
Intent Router — Classifies user messages into action types.

This is the "prefrontal cortex" that prevents DevClaw from
saying "你好" when given a task. Zero LLM needed — pure pattern matching.

Actions:
- monitor: Token address + target mcap → launch sk_mcap_monitor
- price_query: Price/market keywords → Claude CLI for live data
- tool_task: File/code/execute keywords → Claude CLI with tools
- followup: Short commands referencing previous context
- chat: Simple conversation → Ollama fast-path
"""
from __future__ import annotations

import re
from typing import Any


# Solana token address pattern (Base58, 32-50 chars)
_TOKEN_PATTERN = re.compile(r'([A-HJ-NP-Za-km-z1-9]{32,50})')

# Market cap / price target patterns
_MCAP_KEYWORDS = ("突破", "监控", "alert", "通知", "watch", "到达", "超过", "recurring", "repeat", "追踪")
_MCAP_NUMBER = re.compile(r'(\d+(?:\.\d+)?)\s*(?:万|百万|M|million|美金|美元|usd|\$)', re.IGNORECASE)

# Price query keywords
_PRICE_KEYWORDS = ("价格", "price", "多少钱", "行情", "市价", "现价", "报价", "币价", "走势", "btc价", "eth价", "sol价")

# Task execution keywords
_TASK_KEYWORDS = (
    "读取", "read", "写入", "write", "修改", "modify", "检查", "check",
    "文件", "file", "代码", "code", "执行", "execute", "运行", "run",
    "修复", "fix", "重构", "refactor", "创建", "create", "部署", "deploy",
    "测试", "test", "分析", "analyze", "扫描", "scan", "安装", "install",
    "开发", "develop", "搜索", "search", "抓取", "fetch",
    "回测", "backtest", "策略", "strategy", "因子", "factor",
    "改进", "improve", "优化", "optimize", "进化", "evolve",
    "学习", "learn", "项目", "project",
)

# Short follow-up commands
_FOLLOWUP_KEYWORDS = ("做", "继续", "开始", "执行吧", "去做", "做啊", "好的做", "做吧", "干")


def classify_intent(text: str) -> dict[str, Any]:
    """
    Classify user message into an action type.

    Returns: {action, token_address, target_mcap, keywords_matched}
    """
    text = (text or "").strip()
    if not text:
        return {"action": "chat"}

    lower = text.lower()
    result: dict[str, Any] = {
        "action": "chat",
        "token_address": None,
        "target_mcap": 0,
        "keywords_matched": [],
    }

    # Priority 1: Token monitoring (address + number + monitor keyword)
    token_match = _TOKEN_PATTERN.search(text)
    if token_match:
        text_after = text[token_match.end():]
        mcap_match = _MCAP_NUMBER.search(text_after)
        has_monitor_kw = any(kw in text for kw in _MCAP_KEYWORDS)

        if mcap_match and has_monitor_kw:
            raw = float(mcap_match.group(1))
            if "万" in text_after and "百万" not in text_after:
                target = raw * 10000
            elif "百万" in text_after:
                target = raw * 1000000
            elif raw < 1000:
                target = raw * 1000000
            else:
                target = raw

            result["action"] = "monitor"
            result["token_address"] = token_match.group(1)
            result["target_mcap"] = target
            return result

    # Priority 2: Price queries
    if any(kw in lower for kw in _PRICE_KEYWORDS):
        result["action"] = "price_query"
        return result

    # Priority 3: Task execution
    matched_tasks = [kw for kw in _TASK_KEYWORDS if kw in lower]
    if matched_tasks:
        result["action"] = "tool_task"
        result["keywords_matched"] = matched_tasks
        return result

    # Priority 4: Short follow-ups
    if len(text) <= 10 and any(kw in text for kw in _FOLLOWUP_KEYWORDS):
        result["action"] = "followup"
        return result

    # Default: chat
    result["action"] = "chat"
    return result
