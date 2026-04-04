"""
TDD Test: Conversation history must persist to disk.

ASSERTION: After writing conversation history and "restarting" (reloading),
the history must still be readable from disk.
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_conversation_history_persists_to_disk():
    """History must be saved to .claw/conversation_history.json and survive restart."""
    from claw_runtime.persistent_memory import save_history, load_history

    with tempfile.TemporaryDirectory() as td:
        # Save 3 messages
        history = [
            {"role": "user", "text": "hello", "ts": time.time()},
            {"role": "assistant", "text": "hi there", "ts": time.time()},
            {"role": "user", "text": "what is 1+1", "ts": time.time()},
        ]
        save_history(td, chat_id=123, history=history)

        # "Restart" — load from disk
        loaded = load_history(td, chat_id=123)
        assert len(loaded) == 3, f"Expected 3 messages, got {len(loaded)}"
        assert loaded[0]["text"] == "hello"
        assert loaded[2]["text"] == "what is 1+1"


def test_history_max_limit():
    """History must be capped at max entries to prevent bloat."""
    from claw_runtime.persistent_memory import save_history, load_history

    with tempfile.TemporaryDirectory() as td:
        # Save 100 messages
        history = [
            {"role": "user", "text": f"msg{i}", "ts": time.time()}
            for i in range(100)
        ]
        save_history(td, chat_id=456, history=history, max_entries=20)

        loaded = load_history(td, chat_id=456)
        assert len(loaded) <= 20, f"History should be capped at 20, got {len(loaded)}"
        # Should keep the LATEST messages
        assert loaded[-1]["text"] == "msg99"


def test_separate_chat_ids():
    """Different chat_ids must have separate histories."""
    from claw_runtime.persistent_memory import save_history, load_history

    with tempfile.TemporaryDirectory() as td:
        save_history(td, chat_id=1, history=[{"role": "user", "text": "alice"}])
        save_history(td, chat_id=2, history=[{"role": "user", "text": "bob"}])

        h1 = load_history(td, chat_id=1)
        h2 = load_history(td, chat_id=2)
        assert h1[0]["text"] == "alice"
        assert h2[0]["text"] == "bob"
