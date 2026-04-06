"""
Intent Classifier — Smart routing that understands WHAT the user wants.

Replaces fragile substring keyword matching with a scoring-based intent system.
No LLM needed — uses linguistic structure, word boundaries, and context.

Intent categories:
  question    — asking about something (为什么/怎么/吗/what/why/how)
  execute     — wants something done NOW (跑/执行/开始/run/do)
  hardwire    — specific action keywords (扫描/回测/模拟买/vitals)
  chat        — casual conversation, greetings, feelings
  monitor     — set up watching/alerting (监控/突破/alert)
  control     — runtime control (暂停/继续/面板/pause/resume)

Key insight: "为什么要买入" is a QUESTION even though it contains "买入".
Structure matters more than individual keywords.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class IntentResult:
    primary: str          # "question" | "execute" | "hardwire" | "chat" | "monitor" | "control"
    confidence: float     # 0.0 - 1.0
    scores: dict[str, float] = field(default_factory=dict)
    matched_keywords: list[str] = field(default_factory=list)
    reasoning: str = ""


# ---------------------------------------------------------------------------
# Pattern definitions — compiled once at import time
# ---------------------------------------------------------------------------

# Question indicators: these override action keywords
_QUESTION_STRONG = re.compile(
    r"为什么|为社么|为啥|怎么回事|怎么了|咋回事|怎样|如何|"
    r"什么意思|什么是|是什么|啥意思|啥是|"
    r"why\b|how\s+(to|do|does|can|should|is|are|come)|"
    r"what\s+(is|are|does|do|should|happened|was)|"
    r"explain|告诉我为|说说为|解释一下",
    re.I,
)

_QUESTION_WEAK = re.compile(
    r"吗\s*[？?]?$|吗$|么\s*[？?]?$|呢\s*[？?]?$|"
    r"[？?]$|"
    r"是不是|能不能|可以吗|对吗|对不对|好不好|行不行|"
    r"有没有|会不会|要不要|哪个|哪些|几个|多少|多久|"
    r"谁|哪里|何时|何处",
    re.I,
)

# Hardwire action patterns — must be word-boundary aware
_HARDWIRE_PATTERNS: list[tuple[re.Pattern, str, float]] = [
    # (pattern, label, base_score)
    # Trading/scanning
    (re.compile(r"跑策略|执行策略|run\s+strategy", re.I), "run_strategy", 0.9),
    (re.compile(r"全市场扫描|MA\s*Ribbon|扫描市场|market\s+scan", re.I), "market_scan", 0.9),
    (re.compile(r"回测|backtest", re.I), "backtest", 0.9),
    (re.compile(r"套利扫描|profit\s+hunt|arbitrage", re.I), "arbitrage", 0.9),
    (re.compile(r"资金费率|funding\s+rate", re.I), "funding_rate", 0.8),
    (re.compile(r"模拟买入|模拟卖出|模拟交易|paper\s+trade", re.I), "paper_trade", 0.9),
    # Explicit short commands (not substrings)
    (re.compile(r"^扫描$|^回测$|^套利$", re.I), "short_action", 1.0),
    # System
    (re.compile(r"生存状态|vitals|survival\s+status", re.I), "vitals", 0.8),
    (re.compile(r"^psi$|^压强$|生存压强", re.I), "psi", 0.8),
    (re.compile(r"钱包状态|持仓状态|portfolio\s+status", re.I), "portfolio", 0.8),
    (re.compile(r"^余额$|^balance$|查看余额", re.I), "balance", 0.8),
    # Engineering
    (re.compile(r"修复bug|fix\s+bug|自我修复|self[- ]heal|自愈", re.I), "fix_bug", 0.8),
    (re.compile(r"开始赚钱|start\s+trading|start\s+earning", re.I), "start_earning", 0.9),
]

# Execute intent — user wants something done
_EXECUTE_PATTERNS = re.compile(
    r"^(跑|执行|运行|启动|开始|做|去做|干|搞|部署|创建|写|改|删|更新|安装|"
    r"run|start|do|execute|deploy|create|write|build|install|fix|update|delete)\b",
    re.I,
)

# Chat patterns — casual, emotional, social
_CHAT_PATTERNS = re.compile(
    r"^(你好|hi|hello|hey|嗨|早|晚安|good\s+morning|谢谢|thanks|ok|好的|嗯|"
    r"哈哈|lol|666|厉害|牛|不错|你在吗|在吗|醒了吗)\b|"
    r"^[0-9]{1,3}$",  # pure numbers like "1", "7"
    re.I,
)

# Monitor patterns
_MONITOR_PATTERNS = re.compile(
    r"监控|突破|alert|watch|通知我|提醒我|当.*到达|当.*超过",
    re.I,
)

# Control patterns
_CONTROL_PATTERNS = re.compile(
    r"暂停|继续|面板|pause|resume|panel|stop|开始干活|静默|安静",
    re.I,
)


# ---------------------------------------------------------------------------
# Classifier
# ---------------------------------------------------------------------------

class IntentClassifier:
    """Score-based intent classification with structural awareness."""

    def classify(self, text: str) -> IntentResult:
        text = (text or "").strip()
        if not text:
            return IntentResult(primary="chat", confidence=0.5, reasoning="empty input")

        scores: dict[str, float] = {
            "question": 0.0,
            "execute": 0.0,
            "hardwire": 0.0,
            "chat": 0.0,
            "monitor": 0.0,
            "control": 0.0,
        }
        matched: list[str] = []

        # --- Question scoring ---
        strong_q = _QUESTION_STRONG.findall(text)
        weak_q = _QUESTION_WEAK.findall(text)
        if strong_q:
            scores["question"] += 0.8
            matched.extend(strong_q[:3])
        if weak_q:
            scores["question"] += 0.4
            matched.extend(weak_q[:3])

        # --- Hardwire scoring ---
        for pat, label, base in _HARDWIRE_PATTERNS:
            if pat.search(text):
                scores["hardwire"] = max(scores["hardwire"], base)
                matched.append(label)

        # --- Execute scoring ---
        if _EXECUTE_PATTERNS.search(text):
            scores["execute"] += 0.6
            # Longer messages with action verbs at the start are likely commands
            if len(text) > 10:
                scores["execute"] += 0.2

        # --- Chat scoring ---
        if _CHAT_PATTERNS.search(text):
            scores["chat"] += 0.7
        if len(text) < 5:
            scores["chat"] += 0.3

        # --- Monitor scoring ---
        if _MONITOR_PATTERNS.search(text):
            scores["monitor"] += 0.8

        # --- Control scoring ---
        if _CONTROL_PATTERNS.search(text):
            scores["control"] += 0.9

        # --- Structural overrides ---

        # KEY RULE: question words SUPPRESS hardwire/execute
        # "为什么要买入" → question wins over hardwire
        if scores["question"] >= 0.4 and scores["hardwire"] > 0:
            scores["hardwire"] *= 0.3  # heavily discount
            scores["execute"] *= 0.3

        # Short pure numbers/letters → always chat
        if re.match(r"^[0-9a-zA-Z]{1,3}$", text):
            scores["chat"] = 1.0
            scores["hardwire"] = 0.0
            scores["execute"] = 0.0

        # If message ends with ? or 吗/呢 → boost question
        if re.search(r"[？?]\s*$|吗\s*$|呢\s*$", text):
            scores["question"] += 0.3

        # Find winner
        primary = max(scores, key=scores.get)  # type: ignore[arg-type]
        confidence = scores[primary]

        # Normalize confidence to 0-1
        total = sum(scores.values()) or 1.0
        confidence = min(scores[primary] / total * 1.5, 1.0) if total > 0 else 0.5

        reasoning = f"scores={{{', '.join(f'{k}:{v:.2f}' for k, v in sorted(scores.items(), key=lambda x: -x[1]) if v > 0)}}}"

        return IntentResult(
            primary=primary,
            confidence=confidence,
            scores=scores,
            matched_keywords=matched,
            reasoning=reasoning,
        )

    def is_question(self, text: str) -> bool:
        """Quick check: is this message a question?"""
        r = self.classify(text)
        return r.primary == "question"

    def is_hardwire(self, text: str) -> bool:
        """Quick check: should this trigger hardwire execution?"""
        r = self.classify(text)
        return r.primary == "hardwire"

    def is_chat(self, text: str) -> bool:
        """Quick check: is this casual chat?"""
        r = self.classify(text)
        return r.primary == "chat"

    def needs_llm_disambiguation(self, result: IntentResult, *, threshold: float = 0.55) -> bool:
        """Should we ask an LLM to double-check this classification?

        Returns True when the top score is too close to the runner-up,
        meaning the regex classifier isn't confident enough.
        """
        if result.confidence >= 0.85:
            return False
        sorted_scores = sorted(result.scores.values(), reverse=True)
        if len(sorted_scores) >= 2 and sorted_scores[0] > 0:
            gap = sorted_scores[0] - sorted_scores[1]
            if gap < 0.15:
                return True
        return result.confidence < threshold

    def classify_with_llm_fallback(
        self,
        text: str,
        *,
        llm_fn: "Callable[[str], str | None] | None" = None,
    ) -> IntentResult:
        """Classify with optional LLM fallback for ambiguous cases.

        ``llm_fn`` should accept a short prompt and return one of:
        question / execute / hardwire / chat / monitor / control
        (or None on failure).  If not provided, falls back to regex-only.
        """
        result = self.classify(text)

        if llm_fn is None or not self.needs_llm_disambiguation(result):
            return result

        prompt = (
            "Classify the user intent into exactly ONE category. "
            "Reply with ONLY the category name, nothing else.\n"
            "Categories: question, execute, hardwire, chat, monitor, control\n"
            "- question: asking why/how/what, seeking information\n"
            "- execute: wants something done (run/create/deploy)\n"
            "- hardwire: specific action keyword (scan/backtest/trade/vitals)\n"
            "- chat: greeting, casual talk, emoji, short acknowledgment\n"
            "- monitor: set up alerts/watching\n"
            "- control: pause/resume/panel runtime control\n\n"
            f"User message: {text[:500]}\n"
            "Category:"
        )
        try:
            raw = llm_fn(prompt)
            if raw:
                category = raw.strip().lower().split()[0].rstrip(".,;:")
                if category in result.scores:
                    result = IntentResult(
                        primary=category,
                        confidence=max(result.confidence, 0.8),
                        scores=result.scores,
                        matched_keywords=result.matched_keywords,
                        reasoning=f"llm_override={category}; {result.reasoning}",
                    )
        except Exception:
            pass  # LLM failed — keep regex result
        return result


# Singleton for convenience
_default = IntentClassifier()
classify = _default.classify
is_question = _default.is_question
is_hardwire = _default.is_hardwire
is_chat = _default.is_chat
needs_llm_disambiguation = _default.needs_llm_disambiguation
classify_with_llm_fallback = _default.classify_with_llm_fallback
