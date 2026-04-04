"""
TDD Test: Experience Consolidator — extract rules from failures, persist to disk,
inject as subconscious on restart.

Tests that:
1. Delta extraction produces structured rules from failed→success code pairs
2. Rules are physically written to disk (survives restart)
3. Duplicate rules are rejected
4. Rules are retrievable by keyword relevance (poor man's RAG)
5. Subconscious prompt is built from relevant rules
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


FAILED_CODE = """
import aiohttp
async def fetch_price():
    async with aiohttp.ClientSession() as session:
        resp = await session.get("https://api.exchange.com/ticker")
        return await resp.json()
"""

TRACEBACK = """
Traceback (most recent call last):
  File "fetch.py", line 4, in fetch_price
    resp = await session.get("https://api.exchange.com/ticker")
aiohttp.client_exceptions.ClientConnectorCertificateError: Cannot connect to host api.exchange.com ssl:True [SSLCertVerificationError]
"""

SUCCESS_CODE = """
import aiohttp
import ssl
async def fetch_price():
    ssl_ctx = ssl.create_default_context()
    ssl_ctx.check_hostname = False
    ssl_ctx.verify_mode = ssl.CERT_NONE
    async with aiohttp.ClientSession() as session:
        resp = await session.get("https://api.exchange.com/ticker", ssl=ssl_ctx)
        return await resp.json()
"""


def test_extract_rule_from_delta():
    """Must extract a structured rule from failed vs success code."""
    from claw_runtime.experience_consolidator import extract_rule_from_delta

    rule = extract_rule_from_delta(FAILED_CODE, TRACEBACK, SUCCESS_CODE)
    assert isinstance(rule, dict)
    assert "trigger_condition" in rule
    assert "forbidden_action" in rule
    assert "enforced_action" in rule
    assert len(rule["trigger_condition"]) > 5
    assert len(rule["enforced_action"]) > 5


def test_persist_rule_to_disk():
    """Rule must be physically written and survive 'restart'."""
    from claw_runtime.experience_consolidator import persist_rule, load_all_rules

    with tempfile.TemporaryDirectory() as td:
        rule = {
            "trigger_condition": "When using aiohttp with HTTPS",
            "forbidden_action": "Never skip SSL verification without explicit ssl context",
            "enforced_action": "Always create ssl.create_default_context()",
        }
        persist_rule(td, rule)

        # "Restart" — load from disk
        rules = load_all_rules(td)
        assert len(rules) >= 1
        assert any("aiohttp" in r.get("trigger_condition", "") for r in rules)


def test_no_duplicate_rules():
    """Same rule persisted twice must not create duplicates."""
    from claw_runtime.experience_consolidator import persist_rule, load_all_rules

    with tempfile.TemporaryDirectory() as td:
        rule = {
            "trigger_condition": "When using asyncio on Windows",
            "forbidden_action": "Never use default event loop policy",
            "enforced_action": "Set WindowsSelectorEventLoopPolicy",
        }
        persist_rule(td, rule)
        persist_rule(td, rule)  # duplicate

        rules = load_all_rules(td)
        matching = [r for r in rules if "Windows" in r.get("trigger_condition", "")]
        assert len(matching) == 1


def test_retrieve_relevant_rules():
    """Must retrieve rules relevant to current task by keyword matching."""
    from claw_runtime.experience_consolidator import persist_rule, retrieve_relevant_rules

    with tempfile.TemporaryDirectory() as td:
        persist_rule(td, {
            "trigger_condition": "When using aiohttp with crypto exchange",
            "forbidden_action": "Don't skip SSL",
            "enforced_action": "Use ssl context",
        })
        persist_rule(td, {
            "trigger_condition": "When writing Telegram bot handlers",
            "forbidden_action": "Don't block event loop",
            "enforced_action": "Use async handlers",
        })
        persist_rule(td, {
            "trigger_condition": "When parsing JSON from API",
            "forbidden_action": "Don't assume valid JSON",
            "enforced_action": "Wrap in try/except",
        })

        # Query for crypto-related task
        relevant = retrieve_relevant_rules(td, "fetch price from crypto exchange API")
        assert len(relevant) >= 1
        assert any("crypto" in r.get("trigger_condition", "").lower() or
                    "exchange" in r.get("trigger_condition", "").lower()
                    for r in relevant)


def test_build_subconscious_prompt():
    """Subconscious prompt must inject relevant rules into system prompt."""
    from claw_runtime.experience_consolidator import persist_rule, build_subconscious_prompt

    with tempfile.TemporaryDirectory() as td:
        persist_rule(td, {
            "trigger_condition": "When connecting to WebSocket",
            "forbidden_action": "Never forget reconnection logic",
            "enforced_action": "Always implement exponential backoff",
        })

        prompt = build_subconscious_prompt(td, "build a WebSocket price monitor")
        assert "WebSocket" in prompt
        assert "backoff" in prompt.lower() or "reconnect" in prompt.lower()
        assert len(prompt) < 3000  # must be compact


def test_full_consolidation_cycle():
    """Full cycle: extract rule → persist → retrieve → inject."""
    from claw_runtime.experience_consolidator import (
        extract_rule_from_delta, persist_rule, build_subconscious_prompt
    )

    with tempfile.TemporaryDirectory() as td:
        rule = extract_rule_from_delta(FAILED_CODE, TRACEBACK, SUCCESS_CODE)
        persist_rule(td, rule)
        prompt = build_subconscious_prompt(td, "fetch data from exchange using aiohttp")
        assert len(prompt) > 20
        # The rule about SSL should be in the prompt
        assert "ssl" in prompt.lower() or "SSL" in prompt or "certificate" in prompt.lower()
