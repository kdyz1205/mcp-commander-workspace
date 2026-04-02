"""
Slim quota tracker (from claude-tg-bot tracker/quota.py): rolling windows + cooldown per platform.

Persists under <workspace>/.claw/quota_tracker.json
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class _Platform:
    max_per_window: int
    window_seconds: int
    cooldown_seconds: int


_DEFAULTS: dict[str, _Platform] = {
    "openai": _Platform(max_per_window=80, window_seconds=3 * 3600, cooldown_seconds=3600),
}


class QuotaTracker:
    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self._path = self.workspace / ".claw" / "quota_tracker.json"
        self._platforms = {k: v for k, v in _DEFAULTS.items()}
        self.usage: dict[str, list[float]] = {k: [] for k in self._platforms}
        self.cooldown_until: dict[str, float] = {k: 0.0 for k in self._platforms}
        self._load()

    def _load(self) -> None:
        if not self._path.is_file():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self.cooldown_until = {
                k: float(v) for k, v in raw.get("cooldown_until", {}).items() if k in self._platforms
            }
            for plat, stamps in raw.get("usage", {}).items():
                if plat in self._platforms and isinstance(stamps, list):
                    self.usage[plat] = [float(x) for x in stamps if isinstance(x, (int, float))]
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            state = {
                "cooldown_until": self.cooldown_until,
                "usage": self.usage,
                "saved_at": time.time(),
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            tmp.replace(self._path)
        except OSError:
            pass

    def _cleanup(self, plat: str) -> None:
        spec = self._platforms[plat]
        cutoff = time.time() - spec.window_seconds * 2
        self.usage[plat] = [t for t in self.usage.get(plat, []) if t >= cutoff]

    def record_usage(self, platform: str, *, was_rate_limited: bool = False) -> None:
        if platform not in self._platforms:
            return
        now = time.time()
        self.usage.setdefault(platform, []).append(now)
        self._cleanup(platform)
        if was_rate_limited:
            spec = self._platforms[platform]
            self.cooldown_until[platform] = now + spec.cooldown_seconds
        self._save()

    def is_available(self, platform: str) -> bool:
        if platform not in self._platforms:
            return False
        if time.time() < self.cooldown_until.get(platform, 0):
            return False
        return self.remaining(platform) > 0

    def remaining(self, platform: str) -> int:
        spec = self._platforms[platform]
        window_start = time.time() - spec.window_seconds
        recent = [t for t in self.usage.get(platform, []) if t >= window_start]
        return max(0, spec.max_per_window - len(recent))

    def status_lines(self) -> list[str]:
        lines = ["Quota (rolling windows):"]
        for name in sorted(self._platforms.keys()):
            rem = self.remaining(name)
            tot = self._platforms[name].max_per_window
            cd = max(0.0, self.cooldown_until.get(name, 0) - time.time())
            lines.append(f"  {name}: {rem}/{tot} left; cooldown {int(cd)}s")
        return lines
