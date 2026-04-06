"""
BrowserTool — Playwright-based browser control for the DevClaw autonomous agent framework.

Provides page lifecycle management, navigation, interaction, screenshot capture,
and automatic tracking of console logs, page errors, and network failures.

Usage:
    from tools.browser_tool import BrowserTool

    browser = BrowserTool()
    browser.launch(headless=True)
    page_id = browser.new_page()
    result = browser.goto(page_id, "https://example.com")
    browser.close()
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    sync_playwright,
)

logger = logging.getLogger(__name__)

SCREENSHOT_DIR = Path(".claw/screenshots")


class _PageState:
    """Internal bookkeeping for a single managed page."""

    def __init__(self, page: Page, page_id: str, max_console: int = 50) -> None:
        self.page = page
        self.page_id = page_id
        self.console_logs: deque[dict] = deque(maxlen=max_console)
        self.page_errors: list[str] = []
        self.network_failures: list[dict] = []

        # --- attach listeners ------------------------------------------------
        page.on("console", self._on_console)
        page.on("pageerror", self._on_page_error)
        page.on("requestfailed", self._on_request_failed)
        page.on("response", self._on_response)

    # -- listener callbacks ---------------------------------------------------

    def _on_console(self, msg: Any) -> None:
        self.console_logs.append(
            {
                "type": msg.type,
                "text": msg.text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _on_page_error(self, error: Any) -> None:
        self.page_errors.append(str(error))
        logger.warning("Page error on %s: %s", self.page_id, error)

    def _on_request_failed(self, request: Any) -> None:
        failure = request.failure
        self.network_failures.append(
            {
                "url": request.url,
                "method": request.method,
                "failure": failure if failure else "unknown",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        logger.debug("Request failed on %s: %s %s", self.page_id, request.method, request.url)

    def _on_response(self, response: Any) -> None:
        if response.status >= 400:
            self.network_failures.append(
                {
                    "url": response.url,
                    "method": response.request.method,
                    "failure": f"HTTP {response.status}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )


class BrowserTool:
    """Playwright-backed browser automation for DevClaw agents."""

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._pages: dict[str, _PageState] = {}

    # -- helpers --------------------------------------------------------------

    def _require_browser(self) -> None:
        if self._browser is None or not self._browser.is_connected():
            raise RuntimeError(
                "Browser is not launched. Call launch() before performing browser actions."
            )

    def _get_page_state(self, page_id: str) -> _PageState:
        self._require_browser()
        state = self._pages.get(page_id)
        if state is None:
            raise KeyError(
                f"No page found with id '{page_id}'. "
                f"Active pages: {list(self._pages.keys())}"
            )
        return state

    @staticmethod
    def _screenshot_path(path: str | None) -> str:
        if path:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            return str(p)
        SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
        return str(SCREENSHOT_DIR / f"screenshot_{ts}.png")

    # -- public API -----------------------------------------------------------

    def launch(self, headless: bool = True, browser_type: str = "chromium") -> None:
        """Start a Playwright browser instance.

        Args:
            headless: Run without a visible window when True.
            browser_type: One of 'chromium', 'firefox', or 'webkit'.
        """
        if self._browser is not None and self._browser.is_connected():
            logger.info("Browser already running; closing existing instance first.")
            self.close()

        self._playwright = sync_playwright().start()

        launcher = getattr(self._playwright, browser_type, None)
        if launcher is None:
            self._playwright.stop()
            self._playwright = None
            raise ValueError(
                f"Unknown browser_type '{browser_type}'. "
                "Choose from 'chromium', 'firefox', or 'webkit'."
            )

        self._browser = launcher.launch(headless=headless)
        self._context = self._browser.new_context()
        logger.info("Launched %s (headless=%s)", browser_type, headless)

    def new_page(self) -> str:
        """Create a new browser tab and return its unique page_id."""
        self._require_browser()
        assert self._context is not None

        page = self._context.new_page()
        page_id = uuid.uuid4().hex[:12]
        self._pages[page_id] = _PageState(page, page_id)
        logger.info("Created new page %s", page_id)
        return page_id

    def goto(self, page_id: str, url: str, timeout_ms: int = 30000) -> dict:
        """Navigate a page to *url*.

        Returns:
            dict with keys: url, status, title.
        """
        state = self._get_page_state(page_id)
        try:
            response = state.page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except Exception as exc:
            logger.error("Navigation to %s failed on %s: %s", url, page_id, exc)
            raise

        status = response.status if response else None
        title = state.page.title()
        logger.info("Navigated %s to %s (status=%s, title=%r)", page_id, url, status, title)
        return {"url": state.page.url, "status": status, "title": title}

    def click(self, page_id: str, selector: str, timeout_ms: int = 5000) -> dict:
        """Click an element matching *selector*.

        Returns:
            dict with selector and a success flag.
        """
        state = self._get_page_state(page_id)
        try:
            state.page.click(selector, timeout=timeout_ms)
            logger.debug("Clicked '%s' on %s", selector, page_id)
            return {"selector": selector, "clicked": True}
        except Exception as exc:
            logger.error("Click failed for '%s' on %s: %s", selector, page_id, exc)
            raise

    def fill(self, page_id: str, selector: str, value: str) -> dict:
        """Fill an input element matching *selector* with *value*.

        Returns:
            dict with selector, value, and success flag.
        """
        state = self._get_page_state(page_id)
        try:
            state.page.fill(selector, value)
            logger.debug("Filled '%s' with value on %s", selector, page_id)
            return {"selector": selector, "value": value, "filled": True}
        except Exception as exc:
            logger.error("Fill failed for '%s' on %s: %s", selector, page_id, exc)
            raise

    def press(self, page_id: str, selector: str, key: str) -> dict:
        """Press a keyboard *key* on the element matching *selector*.

        Returns:
            dict with selector, key, and success flag.
        """
        state = self._get_page_state(page_id)
        try:
            state.page.press(selector, key)
            logger.debug("Pressed '%s' on '%s' (%s)", key, selector, page_id)
            return {"selector": selector, "key": key, "pressed": True}
        except Exception as exc:
            logger.error("Press failed for key '%s' on '%s' (%s): %s", key, selector, page_id, exc)
            raise

    def wait_for(
        self,
        page_id: str,
        selector: str | None = None,
        text: str | None = None,
        timeout_ms: int = 10000,
    ) -> dict:
        """Wait for a selector to appear or for specific text content on the page.

        At least one of *selector* or *text* must be provided.

        Returns:
            dict describing what was waited for and whether it was found.
        """
        if selector is None and text is None:
            raise ValueError("At least one of 'selector' or 'text' must be provided.")

        state = self._get_page_state(page_id)
        result: dict[str, Any] = {"found": False}

        try:
            if selector is not None:
                state.page.wait_for_selector(selector, timeout=timeout_ms)
                result["selector"] = selector
                result["found"] = True
                logger.debug("Selector '%s' appeared on %s", selector, page_id)

            if text is not None:
                state.page.wait_for_function(
                    f"() => document.body && document.body.innerText.includes({text!r})",
                    timeout=timeout_ms,
                )
                result["text"] = text
                result["found"] = True
                logger.debug("Text %r appeared on %s", text, page_id)

        except Exception as exc:
            logger.error("wait_for failed on %s: %s", page_id, exc)
            raise

        return result

    def screenshot(
        self, page_id: str, path: str | None = None, full_page: bool = False
    ) -> str:
        """Capture a screenshot and return the file path.

        Args:
            page_id: Target page.
            path: Explicit output path. Defaults to `.claw/screenshots/<timestamp>.png`.
            full_page: Capture the full scrollable page when True.

        Returns:
            Absolute path to the saved screenshot.
        """
        state = self._get_page_state(page_id)
        dest = self._screenshot_path(path)
        state.page.screenshot(path=dest, full_page=full_page)
        abs_dest = str(Path(dest).resolve())
        logger.info("Screenshot saved to %s", abs_dest)
        return abs_dest

    def get_console_logs(self, page_id: str, last_n: int = 50) -> list[dict]:
        """Return the most recent console log entries for a page."""
        state = self._get_page_state(page_id)
        logs = list(state.console_logs)
        return logs[-last_n:]

    def get_page_errors(self, page_id: str) -> list[str]:
        """Return all captured JavaScript errors for a page."""
        state = self._get_page_state(page_id)
        return list(state.page_errors)

    def get_network_failures(self, page_id: str) -> list[dict]:
        """Return all captured network failures (request failures and HTTP >= 400)."""
        state = self._get_page_state(page_id)
        return list(state.network_failures)

    def get_page_content(self, page_id: str) -> str:
        """Return the inner text of the page body."""
        state = self._get_page_state(page_id)
        try:
            return state.page.inner_text("body")
        except Exception as exc:
            logger.error("get_page_content failed on %s: %s", page_id, exc)
            raise

    def evaluate(self, page_id: str, expression: str) -> Any:
        """Evaluate a JavaScript *expression* in the page context and return the result."""
        state = self._get_page_state(page_id)
        try:
            return state.page.evaluate(expression)
        except Exception as exc:
            logger.error("evaluate failed on %s: %s", page_id, exc)
            raise

    def close_page(self, page_id: str) -> None:
        """Close a single page and remove it from tracking."""
        state = self._pages.pop(page_id, None)
        if state is None:
            logger.warning("close_page: page_id '%s' not found; ignoring.", page_id)
            return
        try:
            state.page.close()
        except Exception as exc:
            logger.debug("Error closing page %s: %s", page_id, exc)
        logger.info("Closed page %s", page_id)

    def close(self) -> None:
        """Shut down all pages, the browser context, and the Playwright instance."""
        for pid in list(self._pages):
            self.close_page(pid)

        if self._context is not None:
            try:
                self._context.close()
            except Exception as exc:
                logger.debug("Error closing context: %s", exc)
            self._context = None

        if self._browser is not None:
            try:
                self._browser.close()
            except Exception as exc:
                logger.debug("Error closing browser: %s", exc)
            self._browser = None

        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception as exc:
                logger.debug("Error stopping playwright: %s", exc)
            self._playwright = None

        logger.info("Browser tool fully closed.")

    # -- context manager support ----------------------------------------------

    def __enter__(self) -> "BrowserTool":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()
