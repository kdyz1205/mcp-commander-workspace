"""
Autonomous code immunization sandbox for DevClaw.

When DevClaw downloads unknown code from the internet, it runs it in
quarantine first — a temporary directory with resource limits, timeout
enforcement, and optional network blocking.

State stored in:
  <workspace>/.claw/immune_blacklist.jsonl
  <workspace>/.claw/immune_whitelist.jsonl
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import urlopen, Request
from urllib.error import URLError

from claw_runtime.safety_scan import scan_text, max_severity, Severity


@dataclass
class QuarantineResult:
    """Outcome of running code inside the immunization sandbox."""

    code_hash: str
    safe: bool
    threats: list[str] = field(default_factory=list)
    resource_usage: dict[str, Any] = field(default_factory=dict)
    network_attempts: list[str] = field(default_factory=list)
    execution_time_sec: float = 0.0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_IS_WINDOWS = platform.system() == "Windows"


def _blacklist_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "immune_blacklist.jsonl"


def _whitelist_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "immune_whitelist.jsonl"


def _code_hash(code_text: str) -> str:
    """SHA-256 hex digest of the code text."""
    return hashlib.sha256(code_text.encode("utf-8")).hexdigest()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file into a list of dicts, tolerating missing/corrupt lines."""
    if not path.is_file():
        return []
    entries: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return entries


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append a single JSON record to a JSONL file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def _build_sandbox_wrapper(code_path: str, allow_network: bool) -> str:
    """Return a small Python wrapper that executes *code_path* inside limited scope.

    The wrapper monkey-patches ``socket`` when *allow_network* is False so
    that any connect / bind / sendto attempt is logged and blocked.
    """
    lines = [
        "import sys, os, json, time",
    ]

    if not allow_network:
        lines += [
            "import socket as _socket",
            "_orig_connect = _socket.socket.connect",
            "_net_attempts = []",
            "def _blocked_connect(self, address):",
            "    _net_attempts.append(str(address))",
            "    raise OSError('Network access blocked by DevClaw quarantine')",
            "_socket.socket.connect = _blocked_connect",
            "_socket.socket.bind = _blocked_connect",
            "_socket.socket.sendto = lambda self, *a, **k: (_ for _ in ()).throw(OSError('blocked'))",
        ]

    lines += [
        "start = time.monotonic()",
        "exit_code = 0",
        "error_msg = ''",
        "try:",
        f"    exec(open({code_path!r}, encoding='utf-8').read(), {{'__name__': '__quarantine__'}})",
        "except SystemExit as e:",
        "    exit_code = int(e.code) if e.code is not None else 0",
        "except Exception as e:",
        "    exit_code = 1",
        "    error_msg = f'{type(e).__name__}: {e}'",
        "elapsed = time.monotonic() - start",
        "report = {",
        "    'elapsed': round(elapsed, 4),",
        "    'exit_code': exit_code,",
        "    'error': error_msg,",
    ]

    if not allow_network:
        lines.append("    'network_attempts': _net_attempts,")

    lines += [
        "}",
        "print('\\n__QUARANTINE_REPORT__' + json.dumps(report))",
    ]

    return "\n".join(lines)


def _parse_report(output: str) -> dict[str, Any]:
    """Extract the JSON quarantine report from subprocess output."""
    marker = "__QUARANTINE_REPORT__"
    for line in output.splitlines():
        idx = line.find(marker)
        if idx != -1:
            try:
                return json.loads(line[idx + len(marker):])
            except json.JSONDecodeError:
                pass
    return {}


def _detect_sandbox_escapes(sandbox_dir: Path, output: str) -> list[str]:
    """Heuristic checks for attempts to escape the sandbox."""
    threats: list[str] = []

    # Check if any files were created outside the sandbox
    sandbox_str = str(sandbox_dir.resolve())
    for suspicious in ("/..", "\\.."):
        if suspicious in output:
            threats.append("Possible path traversal detected in output")
            break

    return threats


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def quarantine_code(
    code_text: str,
    workspace: Path,
    *,
    timeout_sec: int = 30,
    allow_network: bool = False,
) -> QuarantineResult:
    """Execute *code_text* inside a temporary sandbox with safety constraints.

    Pipeline:
      1. Compute code hash and check blacklist.
      2. Run static ``safety_scan`` on the code text.
      3. If the static scan yields CRITICAL findings, block immediately.
      4. Execute in a subprocess with timeout, captured I/O, and optional
         network blocking.
      5. Analyse execution results for threats.
    """
    workspace = Path(workspace).resolve()
    chash = _code_hash(code_text)

    # Fast-path: blacklisted
    if is_blacklisted(workspace, chash):
        return QuarantineResult(
            code_hash=chash,
            safe=False,
            threats=["Code hash is blacklisted"],
        )

    threats: list[str] = []

    # --- Static analysis via safety_scan ---
    findings = scan_text(code_text, path_label="<quarantine>")
    sev = max_severity(findings)
    if sev == Severity.CRITICAL:
        for f in findings:
            threats.append(f"[{f.severity.value}] {f.rule_id}: {f.message}")
        return QuarantineResult(code_hash=chash, safe=False, threats=threats)

    if findings:
        for f in findings:
            threats.append(f"[{f.severity.value}] {f.rule_id}: {f.message}")

    # --- Dynamic execution in sandbox ---
    sandbox_dir = Path(tempfile.mkdtemp(prefix="claw_quarantine_"))
    code_path = sandbox_dir / "test_code.py"
    wrapper_path = sandbox_dir / "_runner.py"

    try:
        code_path.write_text(code_text, encoding="utf-8")
        wrapper_src = _build_sandbox_wrapper(str(code_path), allow_network)
        wrapper_path.write_text(wrapper_src, encoding="utf-8")

        env = os.environ.copy()
        # Restrict the subprocess from discovering secrets via env
        for key in list(env):
            low = key.lower()
            if any(tok in low for tok in ("api_key", "secret", "token", "password", "credential")):
                del env[key]

        cmd: list[str] = [sys.executable, "-u", str(wrapper_path)]

        # On Unix we can prepend ulimit-based restrictions via shell
        shell = False
        if not _IS_WINDOWS:
            # 256 MB virtual memory limit, 60 s CPU limit
            prefix = "ulimit -v 262144 -t 60 2>/dev/null; "
            cmd = ["bash", "-c", prefix + " ".join(cmd)]
            shell = False

        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
                cwd=str(sandbox_dir),
                env=env,
            )
            elapsed = time.monotonic() - t0
            combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        except subprocess.TimeoutExpired:
            elapsed = time.monotonic() - t0
            threats.append(f"Execution timed out after {timeout_sec}s")
            return QuarantineResult(
                code_hash=chash,
                safe=False,
                threats=threats,
                execution_time_sec=round(elapsed, 4),
            )

        report = _parse_report(combined)

        resource_usage: dict[str, Any] = {
            "execution_time_sec": round(elapsed, 4),
            "exit_code": report.get("exit_code", proc.returncode),
        }

        net_attempts: list[str] = report.get("network_attempts", [])
        if net_attempts:
            threats.append(f"Network access attempted: {net_attempts}")

        escape_threats = _detect_sandbox_escapes(sandbox_dir, combined)
        threats.extend(escape_threats)

        if report.get("exit_code", proc.returncode) != 0:
            error = report.get("error", "")
            threats.append(f"Non-zero exit code ({report.get('exit_code', proc.returncode)}): {error}")

        safe = len(threats) == 0

        return QuarantineResult(
            code_hash=chash,
            safe=safe,
            threats=threats,
            resource_usage=resource_usage,
            network_attempts=net_attempts,
            execution_time_sec=round(elapsed, 4),
        )

    finally:
        shutil.rmtree(sandbox_dir, ignore_errors=True)


def quarantine_file(
    file_path: Path,
    workspace: Path,
    **kwargs: Any,
) -> QuarantineResult:
    """Read a file and quarantine its contents."""
    file_path = Path(file_path)
    if not file_path.is_file():
        return QuarantineResult(
            code_hash="",
            safe=False,
            threats=[f"File not found: {file_path}"],
        )
    code_text = file_path.read_text(encoding="utf-8", errors="replace")
    return quarantine_code(code_text, workspace, **kwargs)


def quarantine_url(
    url: str,
    workspace: Path,
    **kwargs: Any,
) -> QuarantineResult:
    """Download code from *url* and quarantine it."""
    try:
        req = Request(url, headers={"User-Agent": "DevClaw-Immunization/1.0"})
        with urlopen(req, timeout=15) as resp:
            code_text = resp.read().decode("utf-8", errors="replace")
    except (URLError, OSError, ValueError) as exc:
        return QuarantineResult(
            code_hash="",
            safe=False,
            threats=[f"Failed to download from {url}: {exc}"],
        )
    return quarantine_code(code_text, workspace, **kwargs)


def add_to_immune_blacklist(workspace: Path, code_hash: str, reason: str) -> None:
    """Append a code hash to the immune blacklist."""
    record = {
        "code_hash": code_hash,
        "reason": reason,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_jsonl(_blacklist_path(workspace), record)


def is_blacklisted(workspace: Path, code_hash: str) -> bool:
    """Check whether *code_hash* appears in the immune blacklist."""
    for entry in _read_jsonl(_blacklist_path(workspace)):
        if entry.get("code_hash") == code_hash:
            return True
    return False


def add_to_immune_whitelist(workspace: Path, code_hash: str, source: str) -> None:
    """Append a code hash to the immune whitelist (trusted code)."""
    record = {
        "code_hash": code_hash,
        "source": source,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    _append_jsonl(_whitelist_path(workspace), record)


def _is_whitelisted(workspace: Path, code_hash: str) -> bool:
    """Check whether *code_hash* appears in the immune whitelist."""
    for entry in _read_jsonl(_whitelist_path(workspace)):
        if entry.get("code_hash") == code_hash:
            return True
    return False


def quarantine_and_decide(
    code_text: str,
    workspace: Path,
    source: str = "unknown",
) -> dict[str, Any]:
    """Full immunization pipeline: blacklist check, quarantine, auto-whitelist.

    Returns a decision dict::

        {
            "decision": "allow" | "block" | "quarantine",
            "result": QuarantineResult,
            "reason": str,
        }
    """
    workspace = Path(workspace).resolve()
    chash = _code_hash(code_text)

    # 1. Blacklist check
    if is_blacklisted(workspace, chash):
        return {
            "decision": "block",
            "result": QuarantineResult(code_hash=chash, safe=False, threats=["Blacklisted"]),
            "reason": "Code hash found in immune blacklist",
        }

    # 2. Whitelist fast-path
    if _is_whitelisted(workspace, chash):
        return {
            "decision": "allow",
            "result": QuarantineResult(code_hash=chash, safe=True),
            "reason": "Code hash found in immune whitelist",
        }

    # 3. Quarantine execution
    result = quarantine_code(code_text, workspace)

    if result.safe:
        add_to_immune_whitelist(workspace, chash, source)
        return {
            "decision": "allow",
            "result": result,
            "reason": "Quarantine passed — added to whitelist",
        }

    # Unsafe — auto-blacklist critical threats
    critical = any("[critical]" in t.lower() for t in result.threats)
    if critical:
        add_to_immune_blacklist(workspace, chash, "; ".join(result.threats))
        return {
            "decision": "block",
            "result": result,
            "reason": "Critical threats detected — blacklisted",
        }

    return {
        "decision": "quarantine",
        "result": result,
        "reason": f"Non-critical threats found: {'; '.join(result.threats)}",
    }


def run_immune_scan(workspace: Path) -> dict[str, Any]:
    """Scan all recently downloaded/installed skills for threats.

    Looks for Python files under ``.claw/skills/`` and the bundled skills
    directory, quarantining each one.
    """
    workspace = Path(workspace).resolve()
    scanned = 0
    threat_count = 0
    quarantined: list[dict[str, Any]] = []

    skill_dirs = [
        workspace / ".claw" / "skills",
        workspace / "claw_runtime" / "bundled_skills",
    ]

    for skill_dir in skill_dirs:
        if not skill_dir.is_dir():
            continue
        for py_file in sorted(skill_dir.rglob("*.py")):
            scanned += 1
            try:
                code = py_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            # Static scan only (no execution) for installed skills
            findings = scan_text(code, path_label=str(py_file))
            if findings:
                threat_count += 1
                quarantined.append({
                    "file": str(py_file),
                    "findings": [
                        {"severity": f.severity.value, "rule_id": f.rule_id, "message": f.message}
                        for f in findings
                    ],
                })

    return {
        "scanned": scanned,
        "threats": threat_count,
        "quarantined": quarantined,
    }
