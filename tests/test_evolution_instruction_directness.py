"""
TDD Test: Instruction Directness — DevClaw must never say "你好" when given a task.

When input contains a token address + number, or an explicit task command,
the response must be ACTION, not greeting.
"""
import os, sys, re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


_GREETINGS = ["你好", "hello", "hi there", "我可以帮你", "有什么我可以", "想做什么"]


def _contains_greeting(text: str) -> bool:
    lower = text.lower()
    return any(g in lower for g in _GREETINGS)


def test_token_monitor_no_greeting():
    """Token address + target mcap must trigger action, not greeting."""
    from core.routing import classify_intent

    text = "6iA73gWCKkLWKbVr8rgibV57MMRxzsaqS9cWpgKBpump 当突破620万美金时通知我"
    intent = classify_intent(text)
    assert intent["action"] == "monitor"
    assert intent["token_address"] is not None
    assert intent["target_mcap"] > 0


def test_price_query_no_greeting():
    """Price query must route to action, not chat."""
    from core.routing import classify_intent

    text = "BTC现在多少钱"
    intent = classify_intent(text)
    assert intent["action"] in ("price_query", "tool_task")


def test_task_command_no_greeting():
    """Explicit task commands must route to execution."""
    from core.routing import classify_intent

    for cmd in ["读取README.md", "修复这个bug", "执行回测"]:
        intent = classify_intent(cmd)
        assert intent["action"] != "chat", f"'{cmd}' wrongly routed to chat"


def test_simple_chat_allowed():
    """Simple greetings CAN get a greeting response."""
    from core.routing import classify_intent

    text = "你好啊"
    intent = classify_intent(text)
    assert intent["action"] == "chat"  # This IS a chat


def test_short_followup_uses_context():
    """Short follow-ups like '做' '继续' must route to context-aware execution."""
    from core.routing import classify_intent

    for cmd in ["做", "继续", "开始", "执行吧"]:
        intent = classify_intent(cmd)
        assert intent["action"] in ("followup", "tool_task"), f"'{cmd}' not recognized as followup"
