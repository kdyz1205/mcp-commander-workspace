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

    # All exhausted
    result = select_model(available={"claude": False, "groq": False})
    assert result is None


def test_prompt_builder():
    """Must build a prompt that includes tool schemas."""
    from core.web_llm_proxy import build_web_prompt

    schemas = [{"name": "ping", "description": "Ping system"}]
    prompt = build_web_prompt(schemas, "Call ping tool")
    assert "ping" in prompt
    assert "Call ping tool" in prompt
    assert len(prompt) < 5000
