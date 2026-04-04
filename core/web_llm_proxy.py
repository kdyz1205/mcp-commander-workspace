"""
WebLLM Proxy — Browser-automated compute proxy with multi-source load balancing.

Controls web UIs of LLM services (Claude, Groq) via Playwright and falls back
to Ollama API when browser-based sources are exhausted.

Architecture:
1. Session persistence (cookies/localStorage saved to .auth/)
2. DOM injection (type prompt, wait for response, scrape)
3. Cascade fallback: Claude → Groq → Ollama (API)
4. JSON extraction from web responses
5. Per-source health tracking with cooldown periods

This module has two layers:
- Pure functions (testable without browser): extract, route, build, health
- Browser automation (requires Playwright + headed browser for auth)
"""
from __future__ import annotations

import json
import os
import re
import time
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


# Model priority order (cascade fallback)
_MODEL_PRIORITY = ["claude", "groq", "ollama"]

# Default cooldown: 5 minutes before retrying a failed source
_DEFAULT_COOLDOWN_SECONDS = 300


def select_model(available: dict[str, bool]) -> str | None:
    """Select the best available model from the pool."""
    for model in _MODEL_PRIORITY:
        if available.get(model, False):
            return model
    return None


class SourceHealth:
    """
    Tracks per-source health status with cooldown periods.

    When a source fails, it enters cooldown. After the cooldown expires,
    it becomes eligible again.
    """

    def __init__(self, cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS):
        self.cooldown = cooldown_seconds
        # source_name -> timestamp of failure (0 = healthy)
        self._failures: dict[str, float] = {}
        # source_name -> consecutive failure count
        self._fail_counts: dict[str, int] = {}

    def mark_failed(self, source: str) -> None:
        """Record a source failure. Starts cooldown timer."""
        self._failures[source] = time.time()
        self._fail_counts[source] = self._fail_counts.get(source, 0) + 1

    def mark_healthy(self, source: str) -> None:
        """Clear failure state for a source."""
        self._failures.pop(source, None)
        self._fail_counts.pop(source, None)

    def is_available(self, source: str) -> bool:
        """Check if source is available (never failed or cooldown expired)."""
        fail_time = self._failures.get(source)
        if fail_time is None:
            return True
        return (time.time() - fail_time) >= self.cooldown

    def available_sources(self) -> dict[str, bool]:
        """Return availability map for all sources in priority order."""
        return {s: self.is_available(s) for s in _MODEL_PRIORITY}

    def next_available(self) -> str | None:
        """Return the highest-priority available source, or None."""
        return select_model(self.available_sources())

    def fail_count(self, source: str) -> int:
        return self._fail_counts.get(source, 0)

    def reset_all(self) -> None:
        self._failures.clear()
        self._fail_counts.clear()


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

# Quota / rate-limit error signatures detected in web UI responses
_QUOTA_PATTERNS = [
    "usage limit",
    "rate limit",
    "quota exceeded",
    "too many requests",
    "capacity",
    "try again later",
    "limit reached",
]


def is_quota_error(response_text: str) -> bool:
    """Detect quota / rate-limit errors in scraped LLM web responses."""
    lower = response_text.lower()
    return any(p in lower for p in _QUOTA_PATTERNS)


class WebLLMRouter:
    """
    Browser-automated LLM proxy with multi-source cascade fallback.

    Fallback order: Claude → Groq → Ollama (API).
    When a source fails or quota is exhausted, the router automatically
    tries the next source. Failed sources enter a cooldown period before
    being retried.
    """

    def __init__(
        self,
        auth_state_path: str = ".auth/claude_session.json",
        primary_target: str = "claude",
        fallback_target: str = "groq",
        ollama_base_url: str | None = None,
        ollama_model: str | None = None,
        cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS,
    ):
        self.auth_state_path = auth_state_path
        self.primary = primary_target
        self.fallback = fallback_target
        self.ollama_base_url = ollama_base_url or os.environ.get(
            "OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"
        )
        self.ollama_model = ollama_model or os.environ.get(
            "OLLAMA_MODEL", "gemma3:4b"
        )
        self.health = SourceHealth(cooldown_seconds)
        self._browser = None
        self._page = None
        # Track which source produced the last successful response
        self.last_source: str | None = None

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
            raise RuntimeError(
                "Playwright not installed. "
                "Run: pip install playwright && python -m playwright install chromium"
            )

    def save_current_auth(self):
        """Save current browser auth state for future sessions."""
        if self._context:
            state = self._context.storage_state()
            save_auth_state(self.auth_state_path, state)

    def prompt_web_ui(self, tools_schema: list[dict], instruction: str) -> str:
        """
        Send prompt with automatic cascade fallback.

        Tries sources in priority order (Claude → Groq → Ollama).
        On quota/error, marks source as failed and tries the next one.
        Returns the response from the first successful source.
        Raises RuntimeError if all sources are exhausted.
        """
        prompt = build_web_prompt(tools_schema, instruction)
        errors: list[str] = []

        for source in _MODEL_PRIORITY:
            if not self.health.is_available(source):
                continue

            try:
                response = self._dispatch(source, prompt)

                # Check for quota errors in response
                if is_quota_error(response):
                    self.health.mark_failed(source)
                    errors.append(f"{source}: quota/rate-limit detected")
                    continue

                self.health.mark_healthy(source)
                self.last_source = source
                return response

            except Exception as exc:
                self.health.mark_failed(source)
                errors.append(f"{source}: {exc}")
                continue

        raise RuntimeError(
            f"All LLM sources exhausted. Errors: {'; '.join(errors)}"
        )

    def _dispatch(self, source: str, prompt: str) -> str:
        """Route to the correct backend."""
        if source == "claude":
            self._ensure_browser()
            return self._prompt_claude(prompt)
        elif source == "groq":
            self._ensure_browser()
            return self._prompt_groq(prompt)
        elif source == "ollama":
            return self._prompt_ollama(prompt)
        else:
            raise ValueError(f"Unknown source: {source}")

    def _prompt_claude(self, prompt: str) -> str:
        """Automate Claude web UI."""
        page = self._page
        page.goto("https://claude.ai/new", wait_until="networkidle", timeout=30000)

        input_sel = '[contenteditable="true"], textarea[placeholder]'
        page.wait_for_selector(input_sel, timeout=10000)
        page.fill(input_sel, prompt)
        page.keyboard.press("Enter")

        page.wait_for_timeout(3000)
        try:
            page.wait_for_selector(
                'button:has-text("Stop")', state="hidden", timeout=120000
            )
        except Exception:
            pass

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
        return page.content()[:5000]

    def _prompt_ollama(self, prompt: str) -> str:
        """Call Ollama via OpenAI-compatible API (no browser needed)."""
        import urllib.request
        import urllib.error

        url = self.ollama_base_url.rstrip("/") + "/chat/completions"
        payload = json.dumps({
            "model": self.ollama_model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
        }).encode()

        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ.get('OLLAMA_API_KEY', 'ollama')}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode())
                return body["choices"][0]["message"]["content"]
        except urllib.error.URLError as exc:
            raise ConnectionError(f"Ollama unreachable at {url}: {exc}") from exc
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Ollama response parse error: {exc}") from exc

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
