"""
Resource Registry -- the "Resource Awareness Brain" for DevClaw.

Maintains a unified inventory of every resource the agent can use:
CLI tools, desktop apps, browser sessions, and AI model/API endpoints.

All discovery is *authorized* -- we only check for tools the operator
has declared relevant, not arbitrary filesystem scanning.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class Resource:
    resource_id: str
    category: str  # "cli" | "desktop" | "browser_session" | "model" | "api"
    name: str
    available: bool
    path: str  # binary path or URL
    cost_tier: str  # "free" | "low" | "medium" | "high"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class BrowserSessionResource:
    resource_id: str
    session_type: str  # "playwright" | "chrome_devtools" | "manual"
    available: bool
    quota_observable: bool
    suitable_for: list[str] = field(default_factory=list)  # e.g. ["engineering", "text", "research", "auxiliary"]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CLI_TOOLS: list[dict[str, str]] = [
    {"name": "claude",      "cost_tier": "free"},
    {"name": "git",         "cost_tier": "free"},
    {"name": "python",      "cost_tier": "free"},
    {"name": "python3",     "cost_tier": "free"},
    {"name": "node",        "cost_tier": "free"},
    {"name": "npm",         "cost_tier": "free"},
    {"name": "npx",         "cost_tier": "free"},
    {"name": "pytest",      "cost_tier": "free"},
    {"name": "ruff",        "cost_tier": "free"},
    {"name": "mypy",        "cost_tier": "free"},
    {"name": "flake8",      "cost_tier": "free"},
    {"name": "eslint",      "cost_tier": "free"},
    {"name": "playwright",  "cost_tier": "free"},
    {"name": "docker",      "cost_tier": "free"},
    {"name": "ffmpeg",      "cost_tier": "free"},
    {"name": "curl",        "cost_tier": "free"},
    {"name": "wget",        "cost_tier": "free"},
    {"name": "ollama",      "cost_tier": "free"},
    {"name": "cargo",       "cost_tier": "free"},
    {"name": "go",          "cost_tier": "free"},
]

DESKTOP_APPS: list[dict[str, Any]] = [
    {"name": "Cursor",           "linux_bins": ["cursor"],                        "win_names": ["Cursor"]},
    {"name": "VS Code",          "linux_bins": ["code", "code-insiders"],         "win_names": ["Microsoft VS Code", "Code.exe"]},
    {"name": "Chrome",           "linux_bins": ["google-chrome", "chromium-browser", "chromium"], "win_names": ["Google Chrome"]},
    {"name": "Firefox",          "linux_bins": ["firefox"],                       "win_names": ["Mozilla Firefox"]},
    {"name": "Telegram Desktop", "linux_bins": ["telegram-desktop"],              "win_names": ["Telegram Desktop"]},
    {"name": "Claude Desktop",   "linux_bins": ["claude-desktop"],                "win_names": ["Claude"]},
]

MODEL_COST_MAP: dict[str, str] = {
    # OpenAI
    "gpt-4o-mini": "low",
    "gpt-4o": "medium",
    "gpt-4-turbo": "medium",
    "o1": "high",
    "o1-mini": "medium",
    "o1-preview": "high",
    "o3": "high",
    "o3-mini": "medium",
    # Anthropic
    "claude-3-haiku": "low",
    "claude-3-5-haiku": "low",
    "claude-3-sonnet": "medium",
    "claude-3-5-sonnet": "medium",
    "claude-3-opus": "high",
    "claude-4-opus": "high",
    "claude-opus-4": "high",
    "claude-sonnet-4": "medium",
}

OLLAMA_HEALTH_URL = "http://127.0.0.1:11434/api/tags"

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ResourceRegistry:
    """Unified inventory of all resources available to the agent."""

    def __init__(self, workspace: str = ".") -> None:
        self._workspace = Path(workspace).resolve()
        self._claw_dir = self._workspace / ".claw"
        self._state_file = self._claw_dir / "resource_registry.json"
        self._resources: dict[str, Resource] = {}
        self._browser_sessions: dict[str, BrowserSessionResource] = {}
        self._system = platform.system()  # "Windows" | "Linux" | "Darwin"

        # Load persisted state if it exists, then do a full scan.
        self._load_state()
        self.scan_all()

    # ------------------------------------------------------------------
    # Public scan methods
    # ------------------------------------------------------------------

    def scan_all(self) -> dict[str, Resource]:
        """Run full discovery across every category and persist."""
        self.scan_cli()
        self.scan_desktop()
        self.scan_models()
        self.scan_browser()
        self._save_state()
        return dict(self._resources)

    def scan_cli(self) -> dict[str, Resource]:
        """Detect available CLI tools via shutil.which()."""
        results: dict[str, Resource] = {}
        for tool in CLI_TOOLS:
            name = tool["name"]
            rid = f"cli:{name}"
            bin_path = shutil.which(name)
            version = ""
            if bin_path:
                version = self._get_cli_version(name, bin_path)
            res = Resource(
                resource_id=rid,
                category="cli",
                name=name,
                available=bin_path is not None,
                path=bin_path or "",
                cost_tier=tool["cost_tier"],
                metadata={"version": version} if version else {},
            )
            self._resources[rid] = res
            results[rid] = res
        return results

    def scan_desktop(self) -> dict[str, Resource]:
        """Best-effort detection of desktop applications."""
        results: dict[str, Resource] = {}
        for app in DESKTOP_APPS:
            rid = f"desktop:{app['name'].lower().replace(' ', '_')}"
            found_path = self._detect_desktop_app(app)
            res = Resource(
                resource_id=rid,
                category="desktop",
                name=app["name"],
                available=found_path is not None,
                path=found_path or "",
                cost_tier="free",
                metadata={},
            )
            self._resources[rid] = res
            results[rid] = res
        return results

    def scan_models(self) -> dict[str, Resource]:
        """Detect available AI providers (API keys, Ollama, Claude CLI)."""
        results: dict[str, Resource] = {}

        # -- OpenAI API --
        openai_key = os.environ.get("OPENAI_API_KEY", "")
        rid = "api:openai"
        res = Resource(
            resource_id=rid,
            category="api",
            name="OpenAI API",
            available=bool(openai_key),
            path="https://api.openai.com",
            cost_tier="medium",
            metadata={"key_set": bool(openai_key)},
        )
        self._resources[rid] = res
        results[rid] = res

        # -- Anthropic API --
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        rid = "api:anthropic"
        res = Resource(
            resource_id=rid,
            category="api",
            name="Anthropic API",
            available=bool(anthropic_key),
            path="https://api.anthropic.com",
            cost_tier="medium",
            metadata={"key_set": bool(anthropic_key)},
        )
        self._resources[rid] = res
        results[rid] = res

        # -- Ollama (local) --
        ollama_bin = shutil.which("ollama")
        ollama_models: list[str] = []
        ollama_healthy = False
        if ollama_bin:
            ollama_models = self._get_ollama_models()
            ollama_healthy = self._ollama_health_check()
        rid = "model:ollama"
        res = Resource(
            resource_id=rid,
            category="model",
            name="Ollama",
            available=ollama_bin is not None and ollama_healthy,
            path=ollama_bin or "",
            cost_tier="free",
            metadata={
                "binary_found": ollama_bin is not None,
                "server_healthy": ollama_healthy,
                "models": ollama_models,
            },
        )
        self._resources[rid] = res
        results[rid] = res

        # -- Claude CLI --
        claude_bin = shutil.which("claude")
        rid = "model:claude_cli"
        res = Resource(
            resource_id=rid,
            category="model",
            name="Claude CLI",
            available=claude_bin is not None,
            path=claude_bin or "",
            cost_tier="free",
            metadata={},
        )
        self._resources[rid] = res
        results[rid] = res

        return results

    def scan_browser(self) -> dict[str, Resource]:
        """Register abstract browser session resources."""
        results: dict[str, Resource] = {}

        # Playwright session
        pw_bin = shutil.which("playwright")
        pw_session = BrowserSessionResource(
            resource_id="browser:playwright",
            session_type="playwright",
            available=pw_bin is not None,
            quota_observable=False,
            suitable_for=["engineering", "text", "research", "auxiliary"],
        )
        self._browser_sessions[pw_session.resource_id] = pw_session
        rid = pw_session.resource_id
        res = Resource(
            resource_id=rid,
            category="browser_session",
            name="Playwright Browser Session",
            available=pw_session.available,
            path=pw_bin or "",
            cost_tier="free",
            metadata=asdict(pw_session),
        )
        self._resources[rid] = res
        results[rid] = res

        # Chrome DevTools session (available if chrome/chromium detected)
        chrome_bin = (
            shutil.which("google-chrome")
            or shutil.which("chromium-browser")
            or shutil.which("chromium")
            or shutil.which("chrome")
        )
        cdp_session = BrowserSessionResource(
            resource_id="browser:chrome_devtools",
            session_type="chrome_devtools",
            available=chrome_bin is not None,
            quota_observable=True,
            suitable_for=["engineering", "research", "auxiliary"],
        )
        self._browser_sessions[cdp_session.resource_id] = cdp_session
        rid = cdp_session.resource_id
        res = Resource(
            resource_id=rid,
            category="browser_session",
            name="Chrome DevTools Session",
            available=cdp_session.available,
            path=chrome_bin or "",
            cost_tier="free",
            metadata=asdict(cdp_session),
        )
        self._resources[rid] = res
        results[rid] = res

        # Manual browser session placeholder
        manual_session = BrowserSessionResource(
            resource_id="browser:manual",
            session_type="manual",
            available=False,
            quota_observable=False,
            suitable_for=["text", "research"],
        )
        self._browser_sessions[manual_session.resource_id] = manual_session
        rid = manual_session.resource_id
        res = Resource(
            resource_id=rid,
            category="browser_session",
            name="Manual Browser Session",
            available=False,
            path="",
            cost_tier="free",
            metadata=asdict(manual_session),
        )
        self._resources[rid] = res
        results[rid] = res

        return results

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    def get(self, resource_id: str) -> Resource | None:
        """Return a single resource by ID, or None."""
        return self._resources.get(resource_id)

    def available(self, resource_id: str) -> bool:
        """Check whether a resource is currently marked available."""
        res = self._resources.get(resource_id)
        return res.available if res else False

    def list_by_category(self, category: str) -> list[Resource]:
        """Return all resources in the given category."""
        return [r for r in self._resources.values() if r.category == category]

    def list_available(self) -> list[Resource]:
        """Return every resource that is currently available."""
        return [r for r in self._resources.values() if r.available]

    def export_summary(self) -> dict[str, Any]:
        """Return a JSON-serializable summary suitable for LLM context injection."""
        by_category: dict[str, list[dict[str, Any]]] = {}
        for res in self._resources.values():
            cat = res.category
            if cat not in by_category:
                by_category[cat] = []
            by_category[cat].append(asdict(res))

        available_count = sum(1 for r in self._resources.values() if r.available)
        return {
            "total_resources": len(self._resources),
            "available_resources": available_count,
            "system": self._system,
            "workspace": str(self._workspace),
            "categories": by_category,
        }

    def refresh(self) -> None:
        """Re-scan everything and persist."""
        self.scan_all()

    def register_manual(self, resource: Resource) -> None:
        """Manually add (or override) a resource and persist."""
        self._resources[resource.resource_id] = resource
        self._save_state()

    # ------------------------------------------------------------------
    # Internal helpers -- CLI
    # ------------------------------------------------------------------

    @staticmethod
    def _get_cli_version(name: str, bin_path: str) -> str:
        """Try to obtain a version string for a CLI tool."""
        version_flags = ["--version", "-V", "version"]
        for flag in version_flags:
            try:
                result = subprocess.run(
                    [bin_path, flag],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                output = (result.stdout or result.stderr).strip()
                if output:
                    # Return just the first line to keep it tidy.
                    return output.splitlines()[0]
            except Exception:
                continue
        return ""

    # ------------------------------------------------------------------
    # Internal helpers -- Desktop
    # ------------------------------------------------------------------

    def _detect_desktop_app(self, app: dict[str, Any]) -> str | None:
        """Return the path to the desktop app if detected, else None."""
        if self._system == "Linux":
            return self._detect_desktop_linux(app)
        elif self._system == "Windows":
            return self._detect_desktop_windows(app)
        # macOS / other -- very basic fallback
        for b in app.get("linux_bins", []):
            p = shutil.which(b)
            if p:
                return p
        return None

    @staticmethod
    def _detect_desktop_linux(app: dict[str, Any]) -> str | None:
        bins: list[str] = app.get("linux_bins", [])
        search_dirs = ["/usr/bin", "/usr/local/bin", str(Path.home() / ".local" / "bin")]

        # 1. Direct which()
        for b in bins:
            p = shutil.which(b)
            if p:
                return p

        # 2. Explicit directory check
        for d in search_dirs:
            for b in bins:
                candidate = Path(d) / b
                if candidate.is_file() and os.access(candidate, os.X_OK):
                    return str(candidate)

        # 3. Snap
        try:
            result = subprocess.run(
                ["snap", "list"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                for b in bins:
                    if b in result.stdout:
                        snap_path = f"/snap/bin/{b}"
                        if Path(snap_path).exists():
                            return snap_path
        except Exception:
            pass

        # 4. Flatpak
        try:
            result = subprocess.run(
                ["flatpak", "list", "--columns=application"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                name_lower = app["name"].lower().replace(" ", "")
                for line in result.stdout.splitlines():
                    if name_lower in line.lower().replace(".", ""):
                        return f"flatpak:{line.strip()}"
        except Exception:
            pass

        return None

    @staticmethod
    def _detect_desktop_windows(app: dict[str, Any]) -> str | None:
        win_names: list[str] = app.get("win_names", [])

        # 1. Common install directories
        env_dirs = [
            os.environ.get("LOCALAPPDATA", ""),
            os.environ.get("APPDATA", ""),
            os.environ.get("PROGRAMFILES", ""),
            os.environ.get("PROGRAMFILES(X86)", ""),
        ]
        for env_dir in env_dirs:
            if not env_dir:
                continue
            for wn in win_names:
                candidate = Path(env_dir) / wn
                if candidate.is_dir():
                    # Look for an .exe inside
                    exes = list(candidate.glob("*.exe"))
                    if exes:
                        return str(exes[0])
                # Could also be a direct exe
                candidate_exe = Path(env_dir) / f"{wn}.exe"
                if candidate_exe.is_file():
                    return str(candidate_exe)

        # 2. Windows Registry query (best-effort)
        for wn in win_names:
            try:
                result = subprocess.run(
                    [
                        "reg",
                        "query",
                        r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
                        "/s",
                        "/f",
                        wn,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if result.returncode == 0 and wn in result.stdout:
                    # Parse out InstallLocation if present
                    for line in result.stdout.splitlines():
                        if "InstallLocation" in line:
                            parts = line.split("REG_SZ")
                            if len(parts) > 1:
                                loc = parts[1].strip()
                                if loc and Path(loc).exists():
                                    return loc
            except Exception:
                pass

        return None

    # ------------------------------------------------------------------
    # Internal helpers -- Models / Ollama
    # ------------------------------------------------------------------

    @staticmethod
    def _get_ollama_models() -> list[str]:
        """Run 'ollama list' and return model names."""
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return []
            models: list[str] = []
            for line in result.stdout.strip().splitlines()[1:]:  # skip header
                parts = line.split()
                if parts:
                    models.append(parts[0])
            return models
        except Exception:
            return []

    @staticmethod
    def _ollama_health_check() -> bool:
        """Ping the Ollama HTTP server to see if it is running."""
        try:
            req = urllib.request.Request(OLLAMA_HEALTH_URL, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist the registry to .claw/resource_registry.json."""
        try:
            self._claw_dir.mkdir(parents=True, exist_ok=True)
            payload = {rid: asdict(res) for rid, res in self._resources.items()}
            self._state_file.write_text(json.dumps(payload, indent=2, default=str))
        except Exception as exc:
            logger.warning("Failed to persist resource registry: %s", exc)

    def _load_state(self) -> None:
        """Load previously persisted state (if any)."""
        if not self._state_file.is_file():
            return
        try:
            data = json.loads(self._state_file.read_text())
            for rid, fields in data.items():
                self._resources[rid] = Resource(**fields)
        except Exception as exc:
            logger.warning("Failed to load resource registry state: %s", exc)
