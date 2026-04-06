"""
Screenshot capture tool for the DevClaw agent framework.

Provides browser screenshot delegation, desktop capture with multiple
fallback backends, element capture, and image comparison utilities.
All captures are stored in .claw/screenshots/ with ISO timestamp filenames
and sidecar JSON metadata.
"""

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


SCREENSHOT_DIR = Path(".claw/screenshots")


def _ensure_dir():
    """Create the screenshots directory if it does not exist."""
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


def _iso_filename(prefix: str = "capture", ext: str = ".png") -> str:
    """Generate an ISO-8601 timestamp filename."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%f")
    return f"{prefix}_{ts}{ext}"


def _write_metadata(image_path: str, source: str, dimensions: tuple = None, extra: dict = None):
    """Write a sidecar .json metadata file next to the image."""
    meta = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "image_path": image_path,
        "dimensions": list(dimensions) if dimensions else None,
    }
    if extra:
        meta.update(extra)
    meta_path = image_path + ".json"
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def _get_image_dimensions(path: str) -> Optional[tuple]:
    """Try to read image dimensions using PIL, return (width, height) or None."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return None


class ScreenshotTool:
    """Screenshot capture and comparison tool for DevClaw."""

    # ------------------------------------------------------------------
    # Browser screenshots -- delegate to BrowserTool
    # ------------------------------------------------------------------

    def capture_browser(
        self,
        page_id: str,
        path: str = None,
        full_page: bool = False,
        browser_tool=None,
    ) -> str:
        """Capture a screenshot of a browser page via BrowserTool.

        Args:
            page_id: Identifier for the browser page / tab.
            path: Destination file path. Auto-generated if None.
            full_page: Whether to capture the full scrollable page.
            browser_tool: An instance of BrowserTool (or compatible) to delegate to.

        Returns:
            The absolute path to the saved screenshot.
        """
        _ensure_dir()

        if path is None:
            path = str(SCREENSHOT_DIR / _iso_filename("browser"))

        if browser_tool is None:
            try:
                from tools.web_agent import BrowserTool  # noqa: F811
                browser_tool = BrowserTool()
            except Exception as exc:
                raise RuntimeError(
                    "No browser_tool provided and could not import BrowserTool from tools.web_agent"
                ) from exc

        # Delegate -- BrowserTool is expected to expose a screenshot method.
        if hasattr(browser_tool, "screenshot"):
            browser_tool.screenshot(page_id=page_id, path=path, full_page=full_page)
        elif hasattr(browser_tool, "capture"):
            browser_tool.capture(page_id=page_id, path=path, full_page=full_page)
        else:
            raise AttributeError(
                "browser_tool has no 'screenshot' or 'capture' method"
            )

        dims = _get_image_dimensions(path)
        _write_metadata(path, source="browser", dimensions=dims, extra={
            "page_id": page_id,
            "full_page": full_page,
        })
        return os.path.abspath(path)

    # ------------------------------------------------------------------
    # Desktop screenshots -- system-level capture with fallbacks
    # ------------------------------------------------------------------

    def capture_desktop(
        self,
        path: str = None,
        region: tuple = None,
    ) -> str:
        """Capture a desktop screenshot.

        Tries three backends in order:
        1. pyautogui.screenshot()
        2. mss (python-mss)
        3. scrot CLI

        Args:
            path: Destination file path. Auto-generated if None.
            region: Optional (x, y, width, height) to capture a sub-region.

        Returns:
            The absolute path to the saved screenshot.
        """
        _ensure_dir()

        if path is None:
            path = str(SCREENSHOT_DIR / _iso_filename("desktop"))

        captured = False
        backend_used = None

        # --- Backend 1: pyautogui ---
        if not captured:
            try:
                import pyautogui  # type: ignore

                if region:
                    img = pyautogui.screenshot(region=region)
                else:
                    img = pyautogui.screenshot()
                img.save(path)
                captured = True
                backend_used = "pyautogui"
            except Exception:
                pass

        # --- Backend 2: mss ---
        if not captured:
            try:
                import mss  # type: ignore
                import mss.tools  # type: ignore

                with mss.mss() as sct:
                    if region:
                        monitor = {
                            "left": region[0],
                            "top": region[1],
                            "width": region[2],
                            "height": region[3],
                        }
                    else:
                        monitor = sct.monitors[0]  # full virtual screen
                    sct_img = sct.grab(monitor)
                    mss.tools.to_png(sct_img.rgb, sct_img.size, output=path)
                captured = True
                backend_used = "mss"
            except Exception:
                pass

        # --- Backend 3: scrot ---
        if not captured:
            try:
                cmd = ["scrot", path]
                if region:
                    # scrot doesn't natively support region via CLI in older versions;
                    # capture full and crop later if PIL is available.
                    pass
                subprocess.run(cmd, check=True, capture_output=True, timeout=10)
                captured = True
                backend_used = "scrot"

                # Crop if region was requested and PIL is available
                if region:
                    try:
                        from PIL import Image
                        x, y, w, h = region
                        with Image.open(path) as img:
                            cropped = img.crop((x, y, x + w, y + h))
                            cropped.save(path)
                    except Exception:
                        pass  # full-screen fallback is acceptable
            except Exception:
                pass

        if not captured:
            raise RuntimeError(
                "Desktop screenshot failed: none of pyautogui, mss, or scrot are available"
            )

        dims = _get_image_dimensions(path)
        _write_metadata(path, source="desktop", dimensions=dims, extra={
            "backend": backend_used,
            "region": list(region) if region else None,
        })
        return os.path.abspath(path)

    # ------------------------------------------------------------------
    # Element screenshot -- capture a specific DOM element
    # ------------------------------------------------------------------

    def capture_element(
        self,
        page_id: str,
        selector: str,
        path: str = None,
        browser_tool=None,
    ) -> str:
        """Capture a screenshot of a specific element on a browser page.

        Args:
            page_id: Identifier for the browser page / tab.
            selector: CSS selector for the target element.
            path: Destination file path. Auto-generated if None.
            browser_tool: An instance of BrowserTool to delegate to.

        Returns:
            The absolute path to the saved screenshot.
        """
        _ensure_dir()

        if path is None:
            path = str(SCREENSHOT_DIR / _iso_filename("element"))

        if browser_tool is None:
            try:
                from tools.web_agent import BrowserTool  # noqa: F811
                browser_tool = BrowserTool()
            except Exception as exc:
                raise RuntimeError(
                    "No browser_tool provided and could not import BrowserTool"
                ) from exc

        if hasattr(browser_tool, "screenshot_element"):
            browser_tool.screenshot_element(
                page_id=page_id, selector=selector, path=path
            )
        elif hasattr(browser_tool, "screenshot"):
            # Fallback: some BrowserTool impls accept a selector kwarg
            browser_tool.screenshot(
                page_id=page_id, path=path, selector=selector
            )
        else:
            raise AttributeError(
                "browser_tool has no 'screenshot_element' or 'screenshot' method"
            )

        dims = _get_image_dimensions(path)
        _write_metadata(path, source="element", dimensions=dims, extra={
            "page_id": page_id,
            "selector": selector,
        })
        return os.path.abspath(path)

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------

    def compare(self, path_a: str, path_b: str) -> dict:
        """Compare two screenshots.

        Uses pixel-level diff via PIL if available, otherwise falls back
        to a simple file-size comparison heuristic.

        Returns:
            dict with keys:
                same (bool): Whether the images are considered identical.
                diff_percent (float): Percentage of differing pixels (0.0-100.0).
        """
        # --- Attempt pixel-level comparison with PIL ---
        try:
            from PIL import Image
            import itertools

            img_a = Image.open(path_a).convert("RGB")
            img_b = Image.open(path_b).convert("RGB")

            if img_a.size != img_b.size:
                return {"same": False, "diff_percent": 100.0}

            pixels_a = list(img_a.getdata())
            pixels_b = list(img_b.getdata())
            total = len(pixels_a)
            diff_count = sum(1 for a, b in zip(pixels_a, pixels_b) if a != b)
            diff_pct = (diff_count / total) * 100.0 if total else 0.0

            return {"same": diff_pct == 0.0, "diff_percent": round(diff_pct, 4)}
        except Exception:
            pass

        # --- Fallback: file size comparison ---
        try:
            size_a = os.path.getsize(path_a)
            size_b = os.path.getsize(path_b)
            same = size_a == size_b
            if same:
                # Also compare raw bytes for certainty
                with open(path_a, "rb") as fa, open(path_b, "rb") as fb:
                    same = fa.read() == fb.read()
            diff_pct = 0.0 if same else abs(size_a - size_b) / max(size_a, size_b, 1) * 100.0
            return {"same": same, "diff_percent": round(diff_pct, 4)}
        except Exception as exc:
            return {"same": False, "diff_percent": -1.0}

    # ------------------------------------------------------------------
    # Listing
    # ------------------------------------------------------------------

    def list_captures(self, limit: int = 20) -> list:
        """List recent screenshot captures with their metadata.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of dicts, each containing at least {path, timestamp, source}.
        """
        _ensure_dir()

        results = []
        image_files = sorted(
            [
                f
                for f in SCREENSHOT_DIR.iterdir()
                if f.is_file() and not f.name.endswith(".json")
            ],
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

        for img_path in image_files[:limit]:
            meta_path = Path(str(img_path) + ".json")
            if meta_path.exists():
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    meta = {}
            else:
                meta = {}

            entry = {
                "path": str(img_path.resolve()),
                "filename": img_path.name,
                "timestamp": meta.get("timestamp", datetime.fromtimestamp(
                    img_path.stat().st_mtime, tz=timezone.utc
                ).isoformat()),
                "source": meta.get("source", "unknown"),
                "dimensions": meta.get("dimensions"),
                "size_bytes": img_path.stat().st_size,
            }
            results.append(entry)

        return results
