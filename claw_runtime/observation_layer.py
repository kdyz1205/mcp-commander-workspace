"""
Unified signal collection layer for DevClaw.

Collects, classifies, and buffers observable signals from terminal execution,
browser automation, patches, tests, and system events. Provides structured
observation data for autonomous diagnosis and decision-making.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional


# ---------------------------------------------------------------------------
# Signal dataclass
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    """A single observable event captured by the observation layer."""

    timestamp: float
    source: str  # "terminal" | "browser" | "patch" | "test" | "git" | "system"
    signal_type: str  # "stdout" | "stderr" | "exit_code" | "console_log" | ...
    severity: str  # "info" | "warning" | "error" | "critical"
    content: str  # the actual data
    metadata: dict = field(default_factory=dict)
    task_id: str = ""


# ---------------------------------------------------------------------------
# ObservationBuffer — ring buffer of recent signals for a task
# ---------------------------------------------------------------------------

class ObservationBuffer:
    """Ring buffer of recent signals for a task."""

    def __init__(self, max_size: int = 500) -> None:
        self._max_size = max_size
        self._buf: deque[Signal] = deque(maxlen=max_size)

    # -- mutate --

    def push(self, signal: Signal) -> None:
        self._buf.append(signal)

    def clear(self) -> None:
        self._buf.clear()

    # -- query --

    def get_all(self) -> list[Signal]:
        return list(self._buf)

    def get_by_source(self, source: str) -> list[Signal]:
        return [s for s in self._buf if s.source == source]

    def get_by_severity(self, severity: str) -> list[Signal]:
        return [s for s in self._buf if s.severity == severity]

    def get_errors(self) -> list[Signal]:
        return [s for s in self._buf if s.severity in ("error", "critical")]

    def get_recent(self, n: int = 20) -> list[Signal]:
        items = list(self._buf)
        return items[-n:] if len(items) > n else items

    def summary(self) -> dict:
        by_source: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        error_count = 0
        latest_error: Optional[Signal] = None

        for s in self._buf:
            by_source[s.source] = by_source.get(s.source, 0) + 1
            by_severity[s.severity] = by_severity.get(s.severity, 0) + 1
            if s.severity in ("error", "critical"):
                error_count += 1
                latest_error = s

        return {
            "total": len(self._buf),
            "by_source": by_source,
            "by_severity": by_severity,
            "error_count": error_count,
            "latest_error": latest_error,
        }


# ---------------------------------------------------------------------------
# Error-classification patterns
# ---------------------------------------------------------------------------

_ERROR_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"401|unauthorized|forbidden", re.IGNORECASE), "auth_401"),
    (re.compile(r"500|internal server error", re.IGNORECASE), "api_500"),
    (re.compile(r"timeout|timed out|ETIMEDOUT", re.IGNORECASE), "timeout"),
    (re.compile(r"No module named|ImportError|ModuleNotFoundError", re.IGNORECASE), "import_error"),
    (re.compile(r"SyntaxError|IndentationError", re.IGNORECASE), "syntax_error"),
    (re.compile(r"ECONNREFUSED|ENOTFOUND|fetch failed", re.IGNORECASE), "network_error"),
    (re.compile(r"exceeded|quota|rate limit|429", re.IGNORECASE), "quota_exceeded"),
    (re.compile(r"No such file|FileNotFoundError|ENOENT", re.IGNORECASE), "file_not_found"),
    (re.compile(r"selector|element not found|locator", re.IGNORECASE), "selector_stale"),
    (re.compile(r"env|environment|not set|undefined", re.IGNORECASE), "env_missing"),
]


# ---------------------------------------------------------------------------
# ObservationLayer
# ---------------------------------------------------------------------------

class ObservationLayer:
    """Unified signal collection and analysis hub."""

    def __init__(self) -> None:
        self._buffers: dict[str, ObservationBuffer] = {}

    # -- helpers --

    def _ensure_buffer(self, task_id: str) -> ObservationBuffer:
        if task_id not in self._buffers:
            self._buffers[task_id] = ObservationBuffer()
        return self._buffers[task_id]

    def _make_signal(
        self,
        task_id: str,
        source: str,
        signal_type: str,
        severity: str,
        content: str,
        metadata: dict | None = None,
    ) -> Signal:
        sig = Signal(
            timestamp=time.time(),
            source=source,
            signal_type=signal_type,
            severity=severity,
            content=content,
            metadata=metadata or {},
            task_id=task_id,
        )
        self._ensure_buffer(task_id).push(sig)
        return sig

    # ------------------------------------------------------------------ #
    # Ingest — terminal
    # ------------------------------------------------------------------ #

    def ingest_terminal(
        self,
        task_id: str,
        stdout: str,
        stderr: str,
        exit_code: int,
        duration_sec: float = 0,
        cwd: str = "",
    ) -> list[Signal]:
        signals: list[Signal] = []
        meta = {"duration_sec": duration_sec, "cwd": cwd}

        # stdout
        if stdout.strip():
            signals.append(
                self._make_signal(task_id, "terminal", "stdout", "info", stdout.strip(), meta)
            )

        # stderr
        if stderr.strip():
            sev = "error" if exit_code != 0 else "warning"
            signals.append(
                self._make_signal(task_id, "terminal", "stderr", sev, stderr.strip(), meta)
            )

        # exit code
        sev = "error" if exit_code != 0 else "info"
        signals.append(
            self._make_signal(
                task_id,
                "terminal",
                "exit_code",
                sev,
                f"Exit code {exit_code}",
                {**meta, "exit_code": exit_code},
            )
        )

        # cwd change
        if cwd:
            signals.append(
                self._make_signal(
                    task_id, "terminal", "cwd_change", "info", f"cwd: {cwd}", {"cwd": cwd}
                )
            )

        return signals

    # ------------------------------------------------------------------ #
    # Ingest — browser
    # ------------------------------------------------------------------ #

    def ingest_browser(
        self,
        task_id: str,
        console_logs: list[dict],
        page_errors: list[str],
        network_failures: list[dict],
        http_errors: list[dict] | None = None,
        url: str = "",
        title: str = "",
    ) -> list[Signal]:
        signals: list[Signal] = []
        base_meta = {"url": url, "title": title}

        # console logs
        for entry in console_logs:
            text = entry.get("text", str(entry))
            level = entry.get("type", "log").lower()
            if level in ("error",):
                sev = "error"
            elif level in ("warn", "warning"):
                sev = "warning"
            else:
                sev = "info"
            signals.append(
                self._make_signal(
                    task_id,
                    "browser",
                    "console_log",
                    sev,
                    text,
                    {**base_meta, "console_type": level},
                )
            )

        # page errors
        for err in page_errors:
            signals.append(
                self._make_signal(
                    task_id, "browser", "page_error", "error", err, base_meta
                )
            )

        # network failures (requestfailed)
        for nf in network_failures:
            url_failed = nf.get("url", "unknown")
            reason = nf.get("failure", nf.get("reason", "unknown"))
            signals.append(
                self._make_signal(
                    task_id,
                    "browser",
                    "network_fail",
                    "error",
                    f"Request failed: {url_failed} ({reason})",
                    {**base_meta, "failed_url": url_failed, "reason": reason},
                )
            )

        # HTTP errors (status >= 400)
        for he in http_errors or []:
            status = he.get("status", 0)
            req_url = he.get("url", "unknown")
            sev = "error" if status >= 500 else "warning"
            signals.append(
                self._make_signal(
                    task_id,
                    "browser",
                    "http_error",
                    sev,
                    f"HTTP {status} on {req_url}",
                    {**base_meta, "status": status, "request_url": req_url},
                )
            )

        return signals

    # ------------------------------------------------------------------ #
    # Ingest — patch
    # ------------------------------------------------------------------ #

    def ingest_patch(
        self,
        task_id: str,
        files_changed: list[str],
        diff: str,
        backup_paths: list[str] | None = None,
    ) -> list[Signal]:
        signals: list[Signal] = []
        meta = {
            "files_changed": files_changed,
            "backup_paths": backup_paths or [],
            "num_files": len(files_changed),
        }

        signals.append(
            self._make_signal(
                task_id,
                "patch",
                "diff",
                "info",
                diff if diff else f"Changed {len(files_changed)} files (no diff text)",
                meta,
            )
        )

        return signals

    # ------------------------------------------------------------------ #
    # Ingest — test
    # ------------------------------------------------------------------ #

    def ingest_test(
        self,
        task_id: str,
        passed: bool,
        stdout: str,
        stderr: str,
        duration_sec: float,
        tool_used: str = "",
    ) -> list[Signal]:
        signals: list[Signal] = []
        sev = "info" if passed else "error"
        status_label = "PASSED" if passed else "FAILED"
        meta = {
            "passed": passed,
            "duration_sec": duration_sec,
            "tool_used": tool_used,
        }

        # main result signal
        signals.append(
            self._make_signal(
                task_id,
                "test",
                "test_result",
                sev,
                f"Tests {status_label} ({duration_sec:.1f}s)",
                meta,
            )
        )

        # attach stdout / stderr as additional signals when tests fail
        if not passed:
            combined = ""
            if stdout.strip():
                combined += stdout.strip()
            if stderr.strip():
                if combined:
                    combined += "\n"
                combined += stderr.strip()
            if combined:
                signals.append(
                    self._make_signal(
                        task_id,
                        "test",
                        "stderr",
                        "error",
                        combined,
                        meta,
                    )
                )

        return signals

    # ------------------------------------------------------------------ #
    # Ingest — system / generic
    # ------------------------------------------------------------------ #

    def ingest_system(
        self,
        task_id: str,
        signal_type: str,
        content: str,
        severity: str = "info",
        metadata: dict | None = None,
    ) -> Signal:
        return self._make_signal(task_id, "system", signal_type, severity, content, metadata)

    # ------------------------------------------------------------------ #
    # Query
    # ------------------------------------------------------------------ #

    def get_buffer(self, task_id: str) -> ObservationBuffer:
        return self._ensure_buffer(task_id)

    def get_all_errors(self, task_id: str) -> list[Signal]:
        return self._ensure_buffer(task_id).get_errors()

    def get_diagnosis_context(self, task_id: str, max_signals: int = 30) -> str:
        buf = self._ensure_buffer(task_id)
        info = buf.summary()

        lines: list[str] = []
        lines.append(f"=== Observation Summary for task {task_id} ===")
        lines.append(
            f"Total signals: {info['total']} | "
            f"Errors: {info['error_count']} | "
            f"Warnings: {info.get('by_severity', {}).get('warning', 0)}"
        )
        lines.append("")

        # Errors section
        errors = buf.get_errors()
        if errors:
            lines.append("--- ERRORS ---")
            for s in errors[-max_signals:]:
                first_line = s.content.split("\n")[0][:200]
                lines.append(f"[{s.source}/{s.severity}] {first_line}")
            lines.append("")

        # Warnings section
        warnings = buf.get_by_severity("warning")
        if warnings:
            lines.append("--- WARNINGS ---")
            for s in warnings[-max_signals:]:
                first_line = s.content.split("\n")[0][:200]
                lines.append(f"[{s.source}/{s.severity}] {first_line}")
            lines.append("")

        # Recent non-error/non-warning signals (fill remaining budget)
        used = len(errors) + len(warnings)
        remaining = max(0, max_signals - used)
        if remaining:
            recent_info = [
                s
                for s in buf.get_recent(remaining)
                if s.severity not in ("error", "critical", "warning")
            ]
            if recent_info:
                lines.append("--- RECENT ---")
                for s in recent_info[-remaining:]:
                    first_line = s.content.split("\n")[0][:200]
                    lines.append(f"[{s.source}/{s.signal_type}] {first_line}")
                lines.append("")

        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Analysis helpers
    # ------------------------------------------------------------------ #

    def classify_error(self, signal: Signal) -> str:
        """Classify an error signal into a known category using regex matching."""
        text = f"{signal.content} {json.dumps(signal.metadata)}"
        for pattern, label in _ERROR_PATTERNS:
            if pattern.search(text):
                return label
        return "unknown"

    def detect_pattern(self, task_id: str) -> list[str]:
        """Detect recurring patterns in the observation buffer for a task."""
        buf = self._ensure_buffer(task_id)
        errors = buf.get_errors()
        if not errors:
            return []

        patterns: list[str] = []

        # Count error classifications
        class_counts: dict[str, int] = {}
        for e in errors:
            cls = self.classify_error(e)
            class_counts[cls] = class_counts.get(cls, 0) + 1

        for cls, count in class_counts.items():
            if count >= 2:
                patterns.append(f"repeated_{cls} (x{count})")

        # Flaky test detection: test_result signals that alternate pass/fail
        test_signals = buf.get_by_source("test")
        test_results = [s for s in test_signals if s.signal_type == "test_result"]
        if len(test_results) >= 3:
            outcomes = [s.severity == "info" for s in test_results]
            flips = sum(1 for i in range(1, len(outcomes)) if outcomes[i] != outcomes[i - 1])
            if flips >= 2:
                patterns.append("flaky_test")

        # Repeated HTTP status codes
        http_signals = [s for s in buf.get_all() if s.signal_type == "http_error"]
        status_counts: dict[int, int] = {}
        for s in http_signals:
            status = s.metadata.get("status", 0)
            if status:
                status_counts[status] = status_counts.get(status, 0) + 1
        for status, count in status_counts.items():
            if count >= 3:
                patterns.append(f"repeated_http_{status} (x{count})")

        # Crash loop: multiple non-zero exit codes in a row
        exit_signals = [s for s in buf.get_by_source("terminal") if s.signal_type == "exit_code"]
        consecutive_fails = 0
        max_consecutive = 0
        for s in exit_signals:
            if s.metadata.get("exit_code", 0) != 0:
                consecutive_fails += 1
                max_consecutive = max(max_consecutive, consecutive_fails)
            else:
                consecutive_fails = 0
        if max_consecutive >= 3:
            patterns.append(f"crash_loop ({max_consecutive} consecutive failures)")

        return patterns

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def save_to_artifacts(self, task_id: str, artifact_dir: str) -> str:
        """Save the full observation log for a task to a JSON file."""
        os.makedirs(artifact_dir, exist_ok=True)
        buf = self._ensure_buffer(task_id)

        records = []
        for s in buf.get_all():
            records.append(
                {
                    "timestamp": s.timestamp,
                    "source": s.source,
                    "signal_type": s.signal_type,
                    "severity": s.severity,
                    "content": s.content,
                    "metadata": s.metadata,
                    "task_id": s.task_id,
                }
            )

        path = os.path.join(artifact_dir, f"observations_{task_id}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "task_id": task_id,
                    "saved_at": time.time(),
                    "summary": buf.summary() | {"latest_error": None},  # strip non-serializable
                    "signals": records,
                },
                f,
                indent=2,
            )

        return path
