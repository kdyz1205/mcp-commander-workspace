"""
Pain Sensor — detect user frustration and auto-generate fix tasks.

When the user expresses frustration (repeated requests, angry words, complaints),
this module detects it and creates repair tasks in EVOLUTION_BACKLOG.md.
"""
from __future__ import annotations

import re
import time
from pathlib import Path


# Frustration patterns (Chinese + English)
_FRUSTRATION_PATTERNS = [
    r"无法使用|不能用|没用|废物|垃圾|broken|useless|doesn'?t work",
    r"怎么又|又来了|又出错|again|still broken|still not",
    r"根本没有|完全没|根本不|从来没|never works?|completely",
    r"我说了\d+次|说了好多次|repeated|told you",
    r"什么都做不到|什么都不会|can'?t do anything",
    r"太慢了|太笨了|太蠢|too slow|too stupid|idiot",
    r"烦死了|受不了|崩溃了|frustrated|fed up|annoyed",
]

_COMPILED = [re.compile(p, re.IGNORECASE) for p in _FRUSTRATION_PATTERNS]

# Debounce: don't spam fix tasks
_last_pain_ts: float = 0.0
_PAIN_COOLDOWN = 300  # 5 min


def detect_frustration(text: str, conversation_context: str = "") -> int:
    """Return frustration score 0-10."""
    score = 0
    combined = f"{conversation_context}\n{text}"
    for pat in _COMPILED:
        if pat.search(combined):
            score += 2

    # Repeated identical messages in context = high frustration
    if conversation_context:
        lines = conversation_context.strip().split("\n")
        user_msgs = [l.strip() for l in lines if l.strip().startswith("user:")]
        if len(user_msgs) >= 2:
            # Check if last N user messages are very similar
            recent = user_msgs[-3:]
            if len(set(m[:50] for m in recent)) == 1:
                score += 4  # Same message repeated 3+ times

    return min(score, 10)


def process_pain(
    workspace: Path,
    text: str,
    conversation_context: str = "",
) -> str | None:
    """
    Analyze user message for frustration. If detected:
    1. Log the pain signal
    2. Add fix task to EVOLUTION_BACKLOG
    3. Return acknowledgment message (or None if no frustration)
    """
    global _last_pain_ts

    score = detect_frustration(text, conversation_context)
    if score < 3:
        return None

    now = time.time()
    if now - _last_pain_ts < _PAIN_COOLDOWN:
        return None
    _last_pain_ts = now

    ws = Path(workspace).resolve()

    # Log pain signal
    pain_log = ws / ".claw" / "pain_log.jsonl"
    pain_log.parent.mkdir(parents=True, exist_ok=True)
    try:
        import json
        entry = {
            "ts": now,
            "score": score,
            "text": text[:500],
            "context_len": len(conversation_context),
        }
        with pain_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # Add fix task to backlog
    try:
        backlog = ws / "EVOLUTION_BACKLOG.md"
        if backlog.is_file():
            bl = backlog.read_text(encoding="utf-8")
            task = f"[PAIN-{score}] 用户不满: {text[:100]}"
            if task not in bl:
                bl = bl.replace(
                    "## Active Tasks\n",
                    f"## Active Tasks\n- [ ] {task}\n",
                )
                backlog.write_text(bl, encoding="utf-8")
    except Exception:
        pass

    if score >= 6:
        return "⚠️ 收到。我意识到之前的回复没有帮到你，正在调整方式直接处理你的请求。"
    return None
