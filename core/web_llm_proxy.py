"""
WebLLM Proxy — Browser-automated compute proxy.

Controls web UIs of LLM services (Claude, Groq, Gemini) via Playwright
to extract free/subscription compute without API keys.

Architecture:
1. Session persistence (cookies/localStorage saved to .auth/)
2. DOM injection (type prompt, wait for response, scrape)
3. Multi-model fallback (claude → groq → gemini)
4. JSON extraction from web responses

This module has two layers:
- Pure functions (testable without browser): extract, route, build
- Browser automation (requires Playwright + headed browser for auth)
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════
# Layer 1: Pure functions (no browser needed)
# ═══════════════════════════════════════

def extract_json_tool_call(response_text: str) -> dict[str, Any] | None:
    """
    Extract JSON tool call from LLM web response.

    Handles: ```json blocks, raw JSON, embedded JSON in text.
    Returns dict with tool_name + parameters, or None.
    """
    if not response_text:
        return None

    # Try fenced JSON blocks first
    fenced = re.findall(r"```(?:json)?\s*\n?([\s\S]*?)```", response_text)
    for block in fenced:
        parsed = _try_parse(block.strip())
        if parsed and _is_tool_call(parsed):
            return parsed

    # Try raw JSON in text
    json_matches = re.findall(r"\{[^{}]*\"tool_name\"[^{}]*\}", response_text)
    for match in json_matches:
        parsed = _try_parse(match)
        if parsed and _is_tool_call(parsed):
            return parsed

    # Try any JSON object
    all_json = re.findall(r"\{[\s\S]*?\}", response_text)
    for match in all_json:
        parsed = _try_parse(match)
        if parsed and _is_tool_call(parsed):
            return parsed

    return None


def _try_parse(text: str) -> dict | None:
    try:
        d = json.loads(text)
        return d if isinstance(d, dict) else None
    except (json.JSONDecodeError, TypeError):
        return None


def _is_tool_call(d: dict) -> bool:
    return "tool_name" in d or "name" in d


def save_auth_state(path: str, state: dict) -> None:
    """Save browser auth state (cookies + localStorage) to disk."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_auth_state(path: str) -> dict | None:
    """Load auth state from disk. Returns None if missing."""
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


# Model priority order
_MODEL_PRIORITY = ["claude", "groq", "gemini", "chatgpt"]


def select_model(available: dict[str, bool]) -> str | None:
    """Select the best available model from the pool."""
    for model in _MODEL_PRIORITY:
        if available.get(model, False):
            return model
    return None


def build_web_prompt(tool_schemas: list[dict], instruction: str) -> str:
    """
    Build a prompt suitable for injection into web UI.

    Includes tool schemas so the web LLM can return tool calls.
    """
    schemas_text = json.dumps(tool_schemas, ensure_ascii=False, indent=2)
    prompt = (
        f"You have access to the following tools:\n"
        f"```json\n{schemas_text}\n```\n\n"
        f"When you want to use a tool, respond with a JSON object:\n"
        f'{{"tool_name": "name", "parameters": {{}}}}\n\n'
        f"User request: {instruction}"
    )
    return prompt[:4500]


# ═══════════════════════════════════════
# Layer 2: Browser automation (requires Playwright)
# ═══════════════════════════════════════

class WebLLMRouter:
    """
    Browser-automated LLM proxy.

    Uses Playwright to control web UIs. Requires initial manual auth
    to save session state.
    """

    def __init__(
        self,
        auth_state_path: str = ".auth/claude_session.json",
        primary_target: str = "claude",
        fallback_target: str = "groq",
    ):
        self.auth_state_path = auth_state_path
        self.primary = primary_target
        self.fallback = fallback_target
        self._browser = None
        self._page = None

    def _ensure_browser(self):
        """Launch browser with saved auth state."""
        if self._browser:
            return

        try:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()

            auth_state = load_auth_state(self.auth_state_path)
            launch_args = {"headless": True}

            self._browser = self._pw.chromium.launch(**launch_args)

            if auth_state:
                self._context = self._browser.new_context(storage_state=auth_state)
            else:
                self._context = self._browser.new_context()

            self._page = self._context.new_page()
        except ImportError:
            raise RuntimeError("Playwright not installed. Run: pip install playwright && python -m playwright install chromium")

    def save_current_auth(self):
        """Save current browser auth state for future sessions."""
        if self._context:
            state = self._context.storage_state()
            save_auth_state(self.auth_state_path, state)

    def prompt_web_ui(self, tools_schema: list[dict], instruction: str) -> str:
        """
        Send prompt to web UI and scrape response.

        This is the core automation method.
        """
        self._ensure_browser()
        prompt = build_web_prompt(tools_schema, instruction)

        # Target-specific DOM automation
        if self.primary == "claude":
            return self._prompt_claude(prompt)
        elif self.primary == "groq":
            return self._prompt_groq(prompt)
        else:
            raise ValueError(f"Unknown target: {self.primary}")

    def _prompt_claude(self, prompt: str) -> str:
        """Automate Claude web UI."""
        page = self._page
        page.goto("https://claude.ai/new", wait_until="networkidle", timeout=30000)

        # Find input area
        input_sel = '[contenteditable="true"], textarea[placeholder]'
        page.wait_for_selector(input_sel, timeout=10000)
        page.fill(input_sel, prompt)
        page.keyboard.press("Enter")

        # Wait for response (stop generating button disappears)
        page.wait_for_timeout(3000)
        try:
            page.wait_for_selector('button:has-text("Stop")', state="hidden", timeout=120000)
        except Exception:
            pass

        # Scrape last response
        messages = page.query_selector_all('[class*="font-claude"]')
        if messages:
            return messages[-1].inner_text()
        return page.content()[:5000]

    def _prompt_groq(self, prompt: str) -> str:
        """Automate Groq web UI."""
        page = self._page
        page.goto("https://groq.com/", wait_until="networkidle", timeout=30000)

        input_sel = 'textarea, [contenteditable="true"]'
        page.wait_for_selector(input_sel, timeout=10000)
        page.fill(input_sel, prompt)
        page.keyboard.press("Enter")

        page.wait_for_timeout(5000)
        # Scrape response
        return page.content()[:5000]

    def extract_json(self, raw_response: str) -> dict[str, Any] | None:
        """Extract JSON tool call from scraped response."""
        return extract_json_tool_call(raw_response)

    def close(self):
        """Shutdown browser."""
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
        if hasattr(self, "_pw") and self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
