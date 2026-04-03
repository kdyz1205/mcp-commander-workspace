"""
sk_logic_healer — automated scientific error recovery loop for DevClaw.

Analyze → Research → Generate fixes → Sandbox test → Hot-swap.
Writes reasoning episodes on success; logs to evolution_failures.jsonl on exhaustion.
"""

from __future__ import annotations

import ast
import importlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
import traceback as tb_module
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("logic_healer")

# ---------------------------------------------------------------------------
# Lazy imports from claw_runtime — guarded so the module remains importable
# even when claw_runtime is not on sys.path (e.g. during isolated unit tests).
# ---------------------------------------------------------------------------


def _import_reasoning_episode():
    from claw_runtime.reasoning_episode import append_reasoning_episode
    return append_reasoning_episode


def _import_evolution_failure():
    from claw_runtime.nightly_evolution import append_evolution_failure
    return append_evolution_failure


def _import_research_lab():
    from claw_runtime.research_lab import run_public_research_scout
    return run_public_research_scout


def _import_safety_scan():
    from claw_runtime.safety_scan import scan_text, should_block_install
    return scan_text, should_block_install


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ErrorAnalysis:
    """Structured output of the error analysis phase."""

    error_type: str
    root_cause_hypothesis: str
    affected_files: list[str]
    suggested_fix_strategy: str


@dataclass
class ResearchFindings:
    """Aggregated research results from public sources."""

    sources_consulted: list[str] = field(default_factory=list)
    relevant_snippets: list[str] = field(default_factory=list)
    suggested_patterns: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class FixCandidate:
    """A single proposed code fix."""

    file_path: str
    original_code: str
    fixed_code: str
    confidence: float  # 0.0 – 1.0
    rationale: str


@dataclass
class HealingResult:
    """Outcome of one full healing-loop invocation."""

    success: bool
    applied_fix: FixCandidate | None = None
    attempts: int = 0
    error_analysis: ErrorAnalysis | None = None
    research_findings: ResearchFindings | None = None
    all_candidates: list[FixCandidate] = field(default_factory=list)
    failure_reason: str = ""


# ---------------------------------------------------------------------------
# 1. Analyze error
# ---------------------------------------------------------------------------

# Common Python exception families and their typical root causes.
_ERROR_PATTERNS: list[tuple[str, str, str]] = [
    (r"KeyError:\s*['\"]?(\w+)", "KeyError", "Missing dictionary key — likely a schema change or unvalidated input."),
    (r"AttributeError:\s*'(\w+)'\s+object\s+has\s+no\s+attribute\s+'(\w+)'", "AttributeError", "Object API mismatch — class interface changed or wrong type passed."),
    (r"ImportError:\s*cannot\s+import\s+name\s+'(\w+)'", "ImportError", "Missing or renamed export — dependency version mismatch or refactor artifact."),
    (r"ModuleNotFoundError:\s*No\s+module\s+named\s+'([\w.]+)'", "ModuleNotFoundError", "Missing package — needs install or path configuration."),
    (r"TypeError:\s*(.+?)(?:\n|$)", "TypeError", "Type contract violation — wrong argument count/type or incompatible operation."),
    (r"ValueError:\s*(.+?)(?:\n|$)", "ValueError", "Invalid value — data format or range assumption broken."),
    (r"FileNotFoundError:\s*(.+?)(?:\n|$)", "FileNotFoundError", "Missing file or directory — path assumption incorrect."),
    (r"IndexError:\s*(.+?)(?:\n|$)", "IndexError", "Sequence index out of range — off-by-one or empty collection."),
    (r"SyntaxError:\s*(.+?)(?:\n|$)", "SyntaxError", "Malformed Python — broken edit, merge conflict marker, or encoding issue."),
    (r"AssertionError", "AssertionError", "Test assertion failed — logic regression or stale expected value."),
    (r"TimeoutError|timed?\s*out", "TimeoutError", "Operation timed out — network, I/O, or compute bottleneck."),
    (r"ConnectionError|ConnectionRefused", "ConnectionError", "Network connection failure — service down, proxy, or firewall."),
]

_FILE_RE = re.compile(r'File\s+"([^"]+)",\s+line\s+(\d+)')


def analyze_error(
    error_text: str,
    traceback_text: str,
    file_path: str | None = None,
) -> dict[str, Any]:
    """Classify an error and produce a structured analysis dict.

    Returns a plain dict (also convertible to :class:`ErrorAnalysis`) with keys:
    ``error_type``, ``root_cause_hypothesis``, ``affected_files``,
    ``suggested_fix_strategy``.
    """
    combined = f"{traceback_text}\n{error_text}"

    # Determine error type and hypothesis from patterns.
    error_type = "UnknownError"
    hypothesis = "Unable to determine root cause from error text alone; manual inspection recommended."
    for pattern, etype, cause_hint in _ERROR_PATTERNS:
        match = re.search(pattern, combined, re.IGNORECASE)
        if match:
            error_type = etype
            hypothesis = cause_hint
            break

    # Extract affected files from traceback.
    affected: list[str] = []
    for fmatch in _FILE_RE.finditer(traceback_text):
        fpath = fmatch.group(1)
        # Skip stdlib / site-packages noise.
        if "site-packages" in fpath or "lib/python" in fpath.replace("\\", "/"):
            continue
        if fpath not in affected:
            affected.append(fpath)
    if file_path and file_path not in affected:
        affected.insert(0, file_path)

    # Build a fix strategy based on the error type.
    strategy = _suggest_strategy(error_type, combined)

    analysis = ErrorAnalysis(
        error_type=error_type,
        root_cause_hypothesis=hypothesis,
        affected_files=affected,
        suggested_fix_strategy=strategy,
    )
    return asdict(analysis)


def _suggest_strategy(error_type: str, combined_text: str) -> str:
    """Return a short human-readable fix strategy string."""
    strategies: dict[str, str] = {
        "KeyError": "Add a .get() default or validate key existence before access.",
        "AttributeError": "Check the object type at call site; add hasattr guard or update interface.",
        "ImportError": "Verify the export name in the source module; update import or add alias.",
        "ModuleNotFoundError": "Install the missing package or fix sys.path / PYTHONPATH.",
        "TypeError": "Review function signature and call arguments; add type checks if needed.",
        "ValueError": "Add input validation or broaden the accepted value range.",
        "FileNotFoundError": "Create the expected path or add an existence check with a fallback.",
        "IndexError": "Add bounds checking or handle the empty-collection case.",
        "SyntaxError": "Fix the syntax at the indicated line; check for merge conflict markers.",
        "AssertionError": "Update the assertion's expected value or fix the logic that produces the actual.",
        "TimeoutError": "Increase timeout, add retry with backoff, or check the upstream service.",
        "ConnectionError": "Verify the endpoint is reachable; add retry logic or proxy configuration.",
    }
    return strategies.get(error_type, "Inspect the traceback, reproduce minimally, and apply a targeted patch.")


# ---------------------------------------------------------------------------
# 2. Research fix
# ---------------------------------------------------------------------------


def research_fix(
    error_analysis: dict[str, Any],
    workspace: str | Path,
) -> ResearchFindings:
    """Use research_lab to search for solutions relevant to the error.

    Queries arXiv for academic errors and constructs search terms from the
    error type and hypothesis for general issues.
    """
    workspace = Path(workspace).resolve()
    error_type = error_analysis.get("error_type", "UnknownError")
    hypothesis = error_analysis.get("root_cause_hypothesis", "")
    strategy = error_analysis.get("suggested_fix_strategy", "")

    findings = ResearchFindings()
    findings.sources_consulted.append("local_pattern_db")

    # Build a search instruction from the error context.
    search_instruction = (
        f"Python {error_type}: {hypothesis}. "
        f"Strategy hint: {strategy}"
    )

    # Attempt research_lab query (arXiv / public sources).
    try:
        run_scout = _import_research_lab()
        result = run_scout(workspace, search_instruction)
        findings.sources_consulted.append("arxiv")
        for paper in result.papers[:3]:
            findings.relevant_snippets.append(
                f"[{paper.query_label}] {paper.title}: {paper.summary[:300]}"
            )
        for factor in result.factors[:2]:
            findings.suggested_patterns.append(
                f"{factor.name}: {factor.implementation_hint}"
            )
        findings.note = result.note
    except Exception as exc:
        logger.warning("research_lab query failed: %s", exc)
        findings.note = f"research_lab unavailable: {exc}"

    # Always add deterministic pattern-based suggestions.
    _add_pattern_suggestions(error_analysis, findings)

    return findings


def _add_pattern_suggestions(
    error_analysis: dict[str, Any],
    findings: ResearchFindings,
) -> None:
    """Append well-known fix patterns based on error type."""
    patterns: dict[str, list[str]] = {
        "KeyError": [
            "Use dict.get(key, default) instead of dict[key].",
            "Validate schema with a guard clause before access.",
        ],
        "ImportError": [
            "Check __all__ in the source module.",
            "Use try/except ImportError with a fallback import.",
        ],
        "ModuleNotFoundError": [
            "pip install <missing-package> or add to requirements.txt.",
            "Ensure the package directory has __init__.py.",
        ],
        "AttributeError": [
            "Use hasattr() or getattr() with a default.",
            "Check isinstance() before calling the attribute.",
        ],
        "TypeError": [
            "Review the function signature for recent parameter changes.",
            "Add *args/**kwargs forwarding if wrapping a changing interface.",
        ],
        "FileNotFoundError": [
            "Use Path.exists() guard before open().",
            "Create parent directories with Path.mkdir(parents=True, exist_ok=True).",
        ],
        "SyntaxError": [
            "Run py_compile.compile() to pinpoint the exact line.",
            "Check for stray merge-conflict markers (<<<<<<<).",
        ],
    }
    etype = error_analysis.get("error_type", "")
    for pat in patterns.get(etype, []):
        if pat not in findings.suggested_patterns:
            findings.suggested_patterns.append(pat)
    findings.sources_consulted.append("builtin_patterns")


# ---------------------------------------------------------------------------
# 3. Generate fix candidates
# ---------------------------------------------------------------------------


def generate_fix_candidates(
    error_analysis: dict[str, Any],
    research_results: ResearchFindings,
    workspace: str | Path,
) -> list[FixCandidate]:
    """Produce a ranked list of concrete fix candidates.

    Each candidate targets a specific file and provides original + fixed code
    along with a confidence score and rationale.
    """
    workspace = Path(workspace).resolve()
    candidates: list[FixCandidate] = []
    affected = error_analysis.get("affected_files", [])
    error_type = error_analysis.get("error_type", "UnknownError")
    strategy = error_analysis.get("suggested_fix_strategy", "")

    for fpath_str in affected[:5]:  # Cap to avoid runaway generation.
        fpath = Path(fpath_str)
        if not fpath.is_absolute():
            fpath = workspace / fpath
        if not fpath.is_file():
            continue

        try:
            source = fpath.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        generated = _generate_for_file(
            source=source,
            file_path=str(fpath),
            error_type=error_type,
            strategy=strategy,
            research=research_results,
        )
        candidates.extend(generated)

    # Sort by confidence descending.
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return candidates


def _generate_for_file(
    *,
    source: str,
    file_path: str,
    error_type: str,
    strategy: str,
    research: ResearchFindings,
) -> list[FixCandidate]:
    """Generate fix candidates for a single source file using deterministic heuristics."""
    candidates: list[FixCandidate] = []
    lines = source.splitlines(keepends=True)

    if error_type == "KeyError":
        candidates.extend(_fix_key_errors(lines, file_path))
    elif error_type == "ImportError":
        candidates.extend(_fix_import_errors(lines, file_path, source))
    elif error_type == "ModuleNotFoundError":
        candidates.extend(_fix_module_not_found(lines, file_path, source))
    elif error_type == "AttributeError":
        candidates.extend(_fix_attribute_errors(lines, file_path))
    elif error_type == "FileNotFoundError":
        candidates.extend(_fix_file_not_found(lines, file_path))
    elif error_type == "SyntaxError":
        candidates.extend(_fix_syntax_errors(lines, file_path, source))

    # If nothing matched, produce a generic guarded-import candidate for the
    # most common failure mode (import/module).
    if not candidates:
        candidates.append(
            FixCandidate(
                file_path=file_path,
                original_code=source,
                fixed_code=source,  # No-op: signals manual review needed.
                confidence=0.05,
                rationale=f"No automatic fix pattern for {error_type}. {strategy}",
            )
        )

    return candidates


# --- Per-error-type heuristic fixers ---

def _fix_key_errors(lines: list[str], file_path: str) -> list[FixCandidate]:
    """Replace bare dict[key] with dict.get(key) where safe."""
    candidates: list[FixCandidate] = []
    # Pattern: var["key"] or var['key'] not already inside .get(
    key_access = re.compile(r'''(\w+)\[(['"])([\w.]+)\2\]''')
    new_lines = list(lines)
    changes = 0
    for i, line in enumerate(lines):
        if ".get(" in line or line.lstrip().startswith("#"):
            continue
        match = key_access.search(line)
        if match:
            var, quote, key = match.group(1, 2, 3)
            old_expr = f'{var}[{quote}{key}{quote}]'
            new_expr = f'{var}.get({quote}{key}{quote})'
            new_lines[i] = line.replace(old_expr, new_expr, 1)
            changes += 1

    if changes:
        candidates.append(
            FixCandidate(
                file_path=file_path,
                original_code="".join(lines),
                fixed_code="".join(new_lines),
                confidence=min(0.7, 0.4 + 0.1 * changes),
                rationale=f"Replaced {changes} bare dict[key] access(es) with dict.get(key) to avoid KeyError.",
            )
        )
    return candidates


def _fix_import_errors(lines: list[str], file_path: str, source: str) -> list[FixCandidate]:
    """Wrap failing imports in try/except ImportError."""
    candidates: list[FixCandidate] = []
    import_re = re.compile(r"^(from\s+\S+\s+import\s+.+|import\s+\S+)", re.MULTILINE)
    matches = list(import_re.finditer(source))
    if not matches:
        return candidates

    # Wrap each top-level import in a try/except.
    new_source = source
    for m in reversed(matches):
        stmt = m.group(0)
        indent = " " * (m.start() - source.rfind("\n", 0, m.start()) - 1)
        if indent.startswith(" ") and len(indent) > 0:
            pass  # keep detected indent
        else:
            indent = ""
        wrapped = (
            f"{indent}try:\n"
            f"{indent}    {stmt}\n"
            f"{indent}except ImportError:\n"
            f"{indent}    pass  # logic_healer: guarded import\n"
        )
        new_source = new_source[: m.start()] + wrapped + new_source[m.end():]

    if new_source != source:
        candidates.append(
            FixCandidate(
                file_path=file_path,
                original_code=source,
                fixed_code=new_source,
                confidence=0.45,
                rationale="Wrapped imports in try/except ImportError to prevent crash on missing names.",
            )
        )
    return candidates


def _fix_module_not_found(lines: list[str], file_path: str, source: str) -> list[FixCandidate]:
    """Same strategy as ImportError — guard the import."""
    return _fix_import_errors(lines, file_path, source)


def _fix_attribute_errors(lines: list[str], file_path: str) -> list[FixCandidate]:
    """Add getattr guards for bare attribute access."""
    candidates: list[FixCandidate] = []
    attr_re = re.compile(r"(\w+)\.(\w+)")
    new_lines = list(lines)
    changes = 0
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("#") or stripped.startswith("import ") or stripped.startswith("from "):
            continue
        if "getattr(" in line or "hasattr(" in line:
            continue
        m = attr_re.search(line)
        if m and not line.lstrip().startswith("def ") and not line.lstrip().startswith("class "):
            obj, attr = m.group(1), m.group(2)
            old_expr = f"{obj}.{attr}"
            new_expr = f"getattr({obj}, {attr!r}, None)"
            new_lines[i] = line.replace(old_expr, new_expr, 1)
            changes += 1
            if changes >= 5:
                break  # Limit scope of changes.

    if changes:
        candidates.append(
            FixCandidate(
                file_path=file_path,
                original_code="".join(lines),
                fixed_code="".join(new_lines),
                confidence=min(0.5, 0.2 + 0.1 * changes),
                rationale=f"Replaced {changes} bare attribute access(es) with getattr() to avoid AttributeError.",
            )
        )
    return candidates


def _fix_file_not_found(lines: list[str], file_path: str) -> list[FixCandidate]:
    """Add Path.exists() guards before open() calls."""
    candidates: list[FixCandidate] = []
    open_re = re.compile(r"(\s*)(.*open\s*\(\s*(.+?)\s*[,)])")
    new_lines = list(lines)
    changes = 0
    for i, line in enumerate(lines):
        m = open_re.match(line)
        if m:
            indent = m.group(1)
            path_arg = m.group(3).split(",")[0].strip().strip("'\"")
            guard = f"{indent}if not Path({m.group(3).split(',')[0].strip()}).exists():\n"
            guard += f"{indent}    raise FileNotFoundError(f\"logic_healer: {{" + m.group(3).split(",")[0].strip() + "}} does not exist\")\n"
            new_lines[i] = guard + line
            changes += 1

    if changes:
        # Ensure Path is imported.
        joined = "".join(new_lines)
        if "from pathlib import Path" not in joined and "import pathlib" not in joined:
            joined = "from pathlib import Path\n" + joined
        candidates.append(
            FixCandidate(
                file_path=file_path,
                original_code="".join(lines),
                fixed_code=joined,
                confidence=0.5,
                rationale=f"Added {changes} Path.exists() guard(s) before open() calls.",
            )
        )
    return candidates


def _fix_syntax_errors(lines: list[str], file_path: str, source: str) -> list[FixCandidate]:
    """Attempt to fix merge-conflict markers and common syntax issues."""
    candidates: list[FixCandidate] = []
    # Remove merge conflict markers.
    conflict_re = re.compile(r"^(<{7}|={7}|>{7}).*$", re.MULTILINE)
    if conflict_re.search(source):
        cleaned = conflict_re.sub("", source)
        candidates.append(
            FixCandidate(
                file_path=file_path,
                original_code=source,
                fixed_code=cleaned,
                confidence=0.6,
                rationale="Removed merge-conflict markers (<<<<<<< / ======= / >>>>>>>).",
            )
        )

    # Try ast.parse to validate; if it fails, not much we can do heuristically.
    try:
        ast.parse(source)
    except SyntaxError as exc:
        if exc.lineno and exc.lineno <= len(lines):
            # Comment out the offending line as a last resort.
            new_lines = list(lines)
            idx = exc.lineno - 1
            new_lines[idx] = f"# logic_healer: commented out (SyntaxError): {lines[idx]}"
            candidates.append(
                FixCandidate(
                    file_path=file_path,
                    original_code=source,
                    fixed_code="".join(new_lines),
                    confidence=0.2,
                    rationale=f"Commented out line {exc.lineno} that caused SyntaxError (low-confidence last resort).",
                )
            )
    return candidates


# ---------------------------------------------------------------------------
# 4. Test fix in sandbox
# ---------------------------------------------------------------------------


def test_fix_in_sandbox(
    workspace: str | Path,
    fix_candidate: FixCandidate,
) -> bool:
    """Apply *fix_candidate* to a temporary copy and validate it.

    Validation stages (each must pass):
    1. Safety scan — reject if CRITICAL findings.
    2. Syntax check — ``ast.parse`` on the patched file.
    3. Import test — ``python -c "import <module>"`` in a subprocess.
    4. Pytest — run relevant test files if they exist.

    Returns ``True`` only when all applicable checks pass.
    """
    workspace = Path(workspace).resolve()
    target = Path(fix_candidate.file_path)
    if not target.is_absolute():
        target = workspace / target

    # --- Safety scan ---
    try:
        scan_text, should_block = _import_safety_scan()
        findings = scan_text(fix_candidate.fixed_code, path_label=str(target))
        blocked, reason = should_block(findings, block_critical=True, block_high=True)
        if blocked:
            logger.warning("Safety scan blocked fix: %s", reason)
            return False
    except Exception as exc:
        logger.warning("Safety scan import failed, skipping: %s", exc)

    # --- Syntax check ---
    try:
        ast.parse(fix_candidate.fixed_code)
    except SyntaxError as exc:
        logger.info("Fix candidate failed syntax check: %s", exc)
        return False

    # --- Sandbox test in temp directory ---
    tmp_dir: str | None = None
    try:
        tmp_dir = tempfile.mkdtemp(prefix="logic_healer_")
        tmp_workspace = Path(tmp_dir)

        # Copy only necessary files to keep the sandbox lightweight.
        # Copy the target file's parent package and any test files.
        rel_target = target.relative_to(workspace)
        tmp_target = tmp_workspace / rel_target
        tmp_target.parent.mkdir(parents=True, exist_ok=True)
        tmp_target.write_text(fix_candidate.fixed_code, encoding="utf-8")

        # Copy __init__.py files along the path for importability.
        for parent in rel_target.parents:
            if parent == Path("."):
                continue
            init_src = workspace / parent / "__init__.py"
            init_dst = tmp_workspace / parent / "__init__.py"
            if init_src.is_file() and not init_dst.is_file():
                init_dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(init_src, init_dst)

        # --- Import test ---
        module_parts = list(rel_target.with_suffix("").parts)
        module_name = ".".join(module_parts)
        import_cmd = [
            sys.executable, "-c",
            f"import sys; sys.path.insert(0, {str(tmp_workspace)!r}); import {module_name}",
        ]
        try:
            cp = subprocess.run(
                import_cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=str(tmp_workspace),
            )
            if cp.returncode != 0:
                logger.info("Import test failed: %s", cp.stderr[:500])
                return False
        except (subprocess.TimeoutExpired, OSError) as exc:
            logger.info("Import test error: %s", exc)
            return False

        # --- Pytest ---
        test_dir = target.parent / "tests"
        if test_dir.is_dir():
            # Copy test files into sandbox.
            tmp_test_dir = tmp_workspace / test_dir.relative_to(workspace)
            if not tmp_test_dir.exists():
                shutil.copytree(test_dir, tmp_test_dir, dirs_exist_ok=True)
            pytest_cmd = [sys.executable, "-m", "pytest", str(tmp_test_dir), "-q", "--tb=short"]
            try:
                cp = subprocess.run(
                    pytest_cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=str(tmp_workspace),
                )
                if cp.returncode != 0:
                    logger.info("Pytest failed in sandbox: %s", cp.stdout[:500])
                    return False
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.info("Pytest error: %s", exc)
                return False

        return True

    except Exception as exc:
        logger.warning("Sandbox testing error: %s", exc)
        return False
    finally:
        if tmp_dir:
            try:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 5. Apply verified fix
# ---------------------------------------------------------------------------


def apply_verified_fix(
    workspace: str | Path,
    fix_candidate: FixCandidate,
) -> None:
    """Write the verified fix to the real file, keeping a backup.

    Raises ``RuntimeError`` if the current file content no longer matches the
    candidate's ``original_code`` (concurrent modification guard).
    """
    workspace = Path(workspace).resolve()
    target = Path(fix_candidate.file_path)
    if not target.is_absolute():
        target = workspace / target

    if not target.is_file():
        raise FileNotFoundError(f"Target file does not exist: {target}")

    current = target.read_text(encoding="utf-8", errors="replace")
    if current != fix_candidate.original_code:
        raise RuntimeError(
            f"File {target} has been modified since analysis; refusing to overwrite. "
            "Re-run the healing loop on the updated file."
        )

    # Create backup.
    backup = target.with_suffix(f".bak.{int(time.time())}{target.suffix}")
    shutil.copy2(target, backup)
    logger.info("Backup saved: %s", backup)

    target.write_text(fix_candidate.fixed_code, encoding="utf-8")
    logger.info("Fix applied to %s", target)


# ---------------------------------------------------------------------------
# 6. Run healing loop (orchestrator)
# ---------------------------------------------------------------------------


def run_healing_loop(
    workspace: str | Path,
    error_text: str,
    traceback_text: str,
    *,
    file_path: str | None = None,
    max_attempts: int = 3,
) -> HealingResult:
    """Orchestrate the full analyze → research → fix → test → apply cycle.

    Parameters
    ----------
    workspace:
        Project root directory.
    error_text:
        The exception message (e.g. ``"KeyError: 'x'"``).
    traceback_text:
        Full traceback string.
    file_path:
        Optional explicit path to the file that raised the error.
    max_attempts:
        Maximum number of fix candidates to try before giving up.

    Returns
    -------
    HealingResult
        Dataclass with ``success``, ``applied_fix``, and diagnostic info.
    """
    workspace = Path(workspace).resolve()
    result = HealingResult(success=False)

    # --- Analyze ---
    logger.info("Analyzing error ...")
    analysis = analyze_error(error_text, traceback_text, file_path)
    result.error_analysis = ErrorAnalysis(**analysis)
    logger.info(
        "Error type: %s | Hypothesis: %s",
        analysis["error_type"],
        analysis["root_cause_hypothesis"],
    )

    # --- Research ---
    logger.info("Researching fix ...")
    findings = research_fix(analysis, workspace)
    result.research_findings = findings

    # --- Generate candidates ---
    logger.info("Generating fix candidates ...")
    candidates = generate_fix_candidates(analysis, findings, workspace)
    result.all_candidates = candidates
    if not candidates:
        result.failure_reason = "No fix candidates could be generated."
        _on_all_attempts_failed(workspace, result, error_text, traceback_text)
        return result

    # --- Test & apply ---
    for idx, candidate in enumerate(candidates[:max_attempts]):
        result.attempts = idx + 1
        logger.info(
            "Testing candidate %d/%d (confidence=%.2f) for %s ...",
            idx + 1,
            min(len(candidates), max_attempts),
            candidate.confidence,
            candidate.file_path,
        )

        if candidate.original_code == candidate.fixed_code:
            logger.info("Skipping no-op candidate.")
            continue

        if test_fix_in_sandbox(workspace, candidate):
            logger.info("Candidate %d passed sandbox tests — applying.", idx + 1)
            try:
                apply_verified_fix(workspace, candidate)
                result.success = True
                result.applied_fix = candidate
                _on_fix_applied(workspace, result, error_text, traceback_text)
                return result
            except (RuntimeError, FileNotFoundError, OSError) as exc:
                logger.warning("Could not apply fix: %s", exc)
                result.failure_reason = str(exc)
        else:
            logger.info("Candidate %d failed sandbox tests.", idx + 1)

    result.failure_reason = result.failure_reason or (
        f"All {result.attempts} candidate(s) failed sandbox validation."
    )
    _on_all_attempts_failed(workspace, result, error_text, traceback_text)
    return result


# ---------------------------------------------------------------------------
# Post-loop actions
# ---------------------------------------------------------------------------


def _on_fix_applied(
    workspace: Path,
    result: HealingResult,
    error_text: str,
    traceback_text: str,
) -> None:
    """Write a reasoning episode to task_plan.md on successful fix."""
    try:
        append_episode = _import_reasoning_episode()
        fix = result.applied_fix
        append_episode(
            workspace,
            trigger=f"logic_healer: auto-fixed {result.error_analysis.error_type if result.error_analysis else 'error'}",
            hypothesis=(
                f"Root cause: {result.error_analysis.root_cause_hypothesis if result.error_analysis else 'unknown'}. "
                f"Fix applied to {fix.file_path if fix else 'unknown'} with confidence {fix.confidence if fix else 0:.0%}."
            ),
            verify_plan=[
                f"Confirm the fix in {fix.file_path if fix else 'unknown'} does not regress other tests.",
                "Run full test suite: pytest -q",
                "Review the .bak backup file if rollback is needed.",
            ],
            revise_hint=(
                f"If the fix causes new failures, restore from the .bak file and try a different strategy. "
                f"Rationale: {fix.rationale if fix else 'N/A'}"
            ),
        )
    except Exception as exc:
        logger.warning("Failed to write reasoning episode: %s", exc)


def _on_all_attempts_failed(
    workspace: Path,
    result: HealingResult,
    error_text: str,
    traceback_text: str,
) -> None:
    """Log to evolution_failures.jsonl when all fix attempts are exhausted."""
    try:
        append_failure = _import_evolution_failure()
        append_failure(
            workspace,
            kind="logic_healer_exhausted",
            detail=json.dumps(
                {
                    "error_type": result.error_analysis.error_type if result.error_analysis else "unknown",
                    "hypothesis": result.error_analysis.root_cause_hypothesis if result.error_analysis else "",
                    "attempts": result.attempts,
                    "candidate_count": len(result.all_candidates),
                    "failure_reason": result.failure_reason,
                    "error_text": error_text[:500],
                },
                ensure_ascii=False,
            ),
        )
    except Exception as exc:
        logger.warning("Failed to log evolution failure: %s", exc)

    # Also write a reasoning episode noting the failure.
    try:
        append_episode = _import_reasoning_episode()
        append_episode(
            workspace,
            trigger=f"logic_healer: FAILED to fix {result.error_analysis.error_type if result.error_analysis else 'error'}",
            hypothesis=(
                f"Attempted {result.attempts} fix(es) for: "
                f"{result.error_analysis.root_cause_hypothesis if result.error_analysis else 'unknown'}. "
                "None passed sandbox validation."
            ),
            verify_plan=[
                "Manual review required — inspect the traceback and affected files.",
                "Check evolution_failures.jsonl for historical pattern.",
                "Consider filing a GitHub issue if the root cause is upstream.",
            ],
            revise_hint=(
                "Escalate to nightly_evolution for deeper skill synthesis, or "
                "invoke a human-in-the-loop review."
            ),
        )
    except Exception as exc:
        logger.warning("Failed to write failure reasoning episode: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Run the healing loop from the command line."""
    import argparse

    parser = argparse.ArgumentParser(
        description="sk_logic_healer: automated error recovery loop",
    )
    parser.add_argument("--workspace", default=os.environ.get("DEVCLAW_WORKSPACE", "."),
                        help="Workspace root directory")
    parser.add_argument("--error", required=True, help="Error message text")
    parser.add_argument("--traceback", default="", help="Full traceback text")
    parser.add_argument("--file", default=None, help="Path to the file that raised the error")
    parser.add_argument("--max-attempts", type=int, default=3, help="Max fix attempts")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(name)s | %(message)s")

    result = run_healing_loop(
        workspace=args.workspace,
        error_text=args.error,
        traceback_text=args.traceback,
        file_path=args.file,
        max_attempts=args.max_attempts,
    )

    print(json.dumps(asdict(result), indent=2, default=str, ensure_ascii=False))
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
