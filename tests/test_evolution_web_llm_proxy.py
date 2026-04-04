"""
TDD Test: WebLLM Proxy — browser-automated compute proxy.

Tests the abstraction layer (not actual browser — that needs headed mode).
Tests: session management, JSON extraction, fallback routing, auth state.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_extract_json_from_response():
    """Must extract JSON tool calls from LLM web response text."""
    from core.web_llm_proxy import extract_json_tool_call

    response = '''Here's what I'll do:

```json
{"tool_name": "system_ping", "parameters": {"message": "hello"}}
```

I've called the tool for you.'''

    result = extract_json_tool_call(response)
    assert result is not None
    assert result["tool_name"] == "system_ping"
    assert result["parameters"]["message"] == "hello"


def test_extract_json_no_json():
    """Must return None when no JSON in response."""
    from core.web_llm_proxy import extract_json_tool_call

    result = extract_json_tool_call("I don't know how to do that.")
    assert result is None


def test_extract_json_malformed():
    """Must handle malformed JSON gracefully."""
    from core.web_llm_proxy import extract_json_tool_call

    result = extract_json_tool_call('```json\n{"broken": \n```')
    assert result is None


def test_auth_state_save_load():
    """Auth state must persist to disk."""
    from core.web_llm_proxy import save_auth_state, load_auth_state

    with tempfile.TemporaryDirectory() as td:
        state = {"cookies": [{"name": "session", "value": "abc123"}]}
        path = os.path.join(td, "auth.json")
        save_auth_state(path, state)

        loaded = load_auth_state(path)
        assert loaded["cookies"][0]["value"] == "abc123"


def test_auth_state_missing_file():
    """Load must return None for missing auth file."""
    from core.web_llm_proxy import load_auth_state

    result = load_auth_state("/nonexistent/path/auth.json")
    assert result is None


def test_model_router_selection():
    """Router must select best available model."""
    from core.web_llm_proxy import select_model

    # Claude available
    result = select_model(available={"claude": True, "groq": True})
    assert result == "claude"

    # Claude exhausted, fallback to groq
    result = select_model(available={"claude": False, "groq": True})
    assert result == "groq"

    # Claude + Groq exhausted, fallback to ollama
    result = select_model(available={"claude": False, "groq": False, "ollama": True})
    assert result == "ollama"

    # All exhausted
    result = select_model(available={"claude": False, "groq": False, "ollama": False})
    assert result is None


def test_prompt_builder():
    """Must build a prompt that includes tool schemas."""
    from core.web_llm_proxy import build_web_prompt

    schemas = [{"name": "ping", "description": "Ping system"}]
    prompt = build_web_prompt(schemas, "Call ping tool")
    assert "ping" in prompt
    assert "Call ping tool" in prompt
    assert len(prompt) < 5000


def test_source_health_basic():
    """SourceHealth must track failures and cooldowns."""
    from core.web_llm_proxy import SourceHealth

    health = SourceHealth(cooldown_seconds=0.1)

    # Initially all available
    assert health.is_available("claude") is True
    assert health.next_available() == "claude"

    # Mark claude failed → groq becomes next
    health.mark_failed("claude")
    assert health.is_available("claude") is False
    assert health.next_available() == "groq"
    assert health.fail_count("claude") == 1

    # Mark groq failed → ollama becomes next
    health.mark_failed("groq")
    assert health.next_available() == "ollama"

    # Mark ollama failed → nothing available
    health.mark_failed("ollama")
    assert health.next_available() is None


def test_source_health_cooldown_recovery():
    """Source must recover after cooldown expires."""
    import time
    from core.web_llm_proxy import SourceHealth

    health = SourceHealth(cooldown_seconds=0.05)
    health.mark_failed("claude")
    assert health.is_available("claude") is False

    time.sleep(0.06)
    assert health.is_available("claude") is True


def test_source_health_mark_healthy():
    """mark_healthy must clear failure state immediately."""
    from core.web_llm_proxy import SourceHealth

    health = SourceHealth(cooldown_seconds=9999)
    health.mark_failed("claude")
    assert health.is_available("claude") is False

    health.mark_healthy("claude")
    assert health.is_available("claude") is True
    assert health.fail_count("claude") == 0


def test_source_health_reset_all():
    """reset_all must clear all failure tracking."""
    from core.web_llm_proxy import SourceHealth

    health = SourceHealth(cooldown_seconds=9999)
    health.mark_failed("claude")
    health.mark_failed("groq")
    health.reset_all()
    assert health.next_available() == "claude"


def test_is_quota_error():
    """Must detect quota/rate-limit errors in response text."""
    from core.web_llm_proxy import is_quota_error

    assert is_quota_error("You've reached your usage limit") is True
    assert is_quota_error("Rate limit exceeded, try later") is True
    assert is_quota_error("Too many requests") is True
    assert is_quota_error("Here is the answer to your question") is False
    assert is_quota_error("") is False


def test_cascade_fallback_on_quota():
    """Router must cascade to next source when quota error detected."""
    from core.web_llm_proxy import WebLLMRouter, is_quota_error

    router = WebLLMRouter(cooldown_seconds=9999)

    call_log = []

    def mock_dispatch(source, prompt):
        call_log.append(source)
        if source == "claude":
            return "You've reached your usage limit"
        elif source == "groq":
            return "Rate limit exceeded"
        elif source == "ollama":
            return '{"tool_name": "ping", "parameters": {}}'
        raise ValueError(f"unexpected source {source}")

    router._dispatch = mock_dispatch

    result = router.prompt_web_ui([], "test")
    assert "ping" in result
    assert call_log == ["claude", "groq", "ollama"]
    assert router.last_source == "ollama"
    assert router.health.is_available("claude") is False
    assert router.health.is_available("groq") is False
    assert router.health.is_available("ollama") is True


def test_cascade_fallback_on_exception():
    """Router must cascade on exceptions (connection errors, timeouts)."""
    from core.web_llm_proxy import WebLLMRouter

    router = WebLLMRouter(cooldown_seconds=9999)

    call_log = []

    def mock_dispatch(source, prompt):
        call_log.append(source)
        if source == "claude":
            raise ConnectionError("browser crash")
        elif source == "groq":
            raise TimeoutError("page timeout")
        return "success from ollama"

    router._dispatch = mock_dispatch

    result = router.prompt_web_ui([], "test")
    assert result == "success from ollama"
    assert call_log == ["claude", "groq", "ollama"]


def test_cascade_all_exhausted():
    """Router must raise RuntimeError when all sources fail."""
    import pytest
    from core.web_llm_proxy import WebLLMRouter

    router = WebLLMRouter(cooldown_seconds=9999)

    def mock_dispatch(source, prompt):
        raise ConnectionError(f"{source} down")

    router._dispatch = mock_dispatch

    with pytest.raises(RuntimeError, match="All LLM sources exhausted"):
        router.prompt_web_ui([], "test")


def test_cascade_skips_cooled_down_sources():
    """Router must skip sources still in cooldown."""
    from core.web_llm_proxy import WebLLMRouter

    router = WebLLMRouter(cooldown_seconds=9999)
    router.health.mark_failed("claude")
    router.health.mark_failed("groq")

    call_log = []

    def mock_dispatch(source, prompt):
        call_log.append(source)
        return "ollama response"

    router._dispatch = mock_dispatch

    result = router.prompt_web_ui([], "test")
    assert call_log == ["ollama"]
    assert result == "ollama response"
