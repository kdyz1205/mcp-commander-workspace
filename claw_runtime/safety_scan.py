"""
Heuristic dangerous-pattern scanner for SKILL.md bodies and install hooks.
Inspired by OpenClaw gateway scanner philosophy; conservative block list.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass
class Finding:
    severity: Severity
    rule_id: str
    message: str
    line_hint: str | None = None


_RULES: list[tuple[Severity, str, str, re.Pattern[str]]] = [
    (
        Severity.CRITICAL,
        "destructive-rm-root",
        "Possible recursive delete of system root",
        re.compile(r"rm\s+(-[a-z]*f[a-z]*\s+)?(-[a-z]*r[a-z]*\s+)?/\s", re.I),
    ),
    (
        Severity.CRITICAL,
        "fork-bomb",
        "Fork bomb / self-replicating shell pattern",
        re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;:"),
    ),
    (
        Severity.CRITICAL,
        "disk-destroy",
        "Destructive disk/block write pattern",
        re.compile(r"\bdd\s+if=", re.I),
    ),
    (
        Severity.HIGH,
        "curl-pipe-shell",
        "curl/wget piped to shell",
        re.compile(r"(curl|wget)\s+[^|]+\|\s*(ba)?sh", re.I),
    ),
    (
        Severity.HIGH,
        "powershell-encoded",
        "Encoded PowerShell execution",
        re.compile(r"-enc(odedcommand)?\s+", re.I),
    ),
    (
        Severity.HIGH,
        "invoke-expression",
        "PowerShell Invoke-Expression",
        re.compile(r"Invoke-Expression|IEX\s+", re.I),
    ),
    (
        Severity.MEDIUM,
        "eval-call",
        "eval/exec of dynamic code",
        re.compile(r"\beval\s*\(|\bexec\s*\(", re.I),
    ),
    (
        Severity.MEDIUM,
        "sudo",
        "sudo elevation",
        re.compile(r"\bsudo\b", re.I),
    ),
    (
        Severity.LOW,
        "hardcoded-secret-shape",
        "Possible hardcoded API key shape",
        re.compile(r"(api[_-]?key|secret|token)\s*[=:]\s*['\"][a-zA-Z0-9]{20,}", re.I),
    ),
]


def scan_text(text: str, *, path_label: str = "") -> list[Finding]:
    findings: list[Finding] = []
    if not text:
        return findings
    for sev, rid, msg, pat in _RULES:
        m = pat.search(text)
        if m:
            snippet = text[max(0, m.start() - 20) : m.end() + 20].replace("\n", " ")
            findings.append(Finding(sev, rid, f"{msg} ({path_label})" if path_label else msg, snippet[:120]))
    return findings


def scan_path(path: Path) -> list[Finding]:
    if not path.is_file():
        return []
    try:
        t = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return scan_text(t, path_label=str(path))


def max_severity(findings: list[Finding]) -> Severity | None:
    order = [Severity.CRITICAL, Severity.HIGH, Severity.MEDIUM, Severity.LOW]
    best: Severity | None = None
    for f in findings:
        if best is None or order.index(f.severity) < order.index(best):
            best = f.severity
    return best


def should_block_install(findings: list[Finding], *, block_critical: bool = True, block_high: bool = False) -> tuple[bool, str]:
    if not findings:
        return False, ""
    ms = max_severity(findings)
    if ms == Severity.CRITICAL and block_critical:
        return True, f"blocked critical: {findings[0].rule_id} — {findings[0].message}"
    if ms == Severity.HIGH and block_high:
        return True, f"blocked high: {findings[0].rule_id}"
    return False, ""
