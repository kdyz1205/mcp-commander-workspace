"""refactoring_engine — Build dependency topology, identify tech-debt hotspots,
and incrementally refactor legacy code with automatic rollback on test failure.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

#: File extensions treated as Python source.  Extend as needed.
SOURCE_EXTENSIONS: Tuple[str, ...] = (".py",)

#: Directories always excluded from analysis.
EXCLUDED_DIRS: Tuple[str, ...] = (
    "__pycache__", ".git", ".venv", "venv", "node_modules", ".tox", ".mypy_cache",
    "dist", "build", "egg-info",
)

#: Default shell command used to verify that a refactoring is safe.
DEFAULT_TEST_COMMAND: str = "python -m pytest -x -q"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FileHotspot:
    """Refactoring-priority metadata for a single source file."""

    file: str
    line_count: int = 0
    complexity_score: float = 0.0
    import_count: int = 0
    last_modified: str = ""
    priority_score: float = 0.0


@dataclass
class RefactorSuggestion:
    """A single actionable refactoring suggestion."""

    file: str
    issue_type: str  # too_long | circular_import | dead_code | high_coupling
    suggested_action: str


@dataclass
class RefactorStepResult:
    """Outcome of one atomic refactoring step."""

    file: str
    issue_type: str
    applied: bool
    reverted: bool
    detail: str


# ---------------------------------------------------------------------------
# 1. build_dependency_graph
# ---------------------------------------------------------------------------

def build_dependency_graph(workspace: str) -> Dict[str, List[str]]:
    """Build a file-level dependency map for every Python file in *workspace*.

    Parameters
    ----------
    workspace:
        Root directory to scan.

    Returns
    -------
    dict[str, list[str]]
        Mapping of *relative* file path to a list of *relative* file paths it imports.
        Only intra-workspace imports are tracked; third-party packages are ignored.
    """
    root = Path(workspace).resolve()
    py_files = _collect_source_files(root)

    # Build a lookup: module-style dotted name -> relative file path
    module_to_file: Dict[str, str] = {}
    for rel in py_files:
        mod = _path_to_module(rel)
        if mod:
            module_to_file[mod] = rel

    graph: Dict[str, List[str]] = {}
    for rel in py_files:
        abs_path = root / rel
        imports = _extract_imports(abs_path)
        resolved: List[str] = []
        for imp in imports:
            # Try progressively shorter prefixes (``foo.bar.baz`` -> ``foo.bar`` -> ``foo``)
            parts = imp.split(".")
            for length in range(len(parts), 0, -1):
                candidate = ".".join(parts[:length])
                if candidate in module_to_file:
                    target = module_to_file[candidate]
                    if target != rel and target not in resolved:
                        resolved.append(target)
                    break
        graph[rel] = resolved

    return graph


# ---------------------------------------------------------------------------
# 2. identify_hotspots
# ---------------------------------------------------------------------------

def identify_hotspots(
    workspace: str,
    dep_graph: Optional[Dict[str, List[str]]] = None,
) -> List[Dict[str, Any]]:
    """Rank source files by refactoring priority.

    Parameters
    ----------
    workspace:
        Project root.
    dep_graph:
        Pre-computed dependency graph.  Built on-the-fly if ``None``.

    Returns
    -------
    list[dict]
        Serialisable :class:`FileHotspot` dicts sorted by descending ``priority_score``.
    """
    root = Path(workspace).resolve()
    if dep_graph is None:
        dep_graph = build_dependency_graph(workspace)

    # Reverse graph: how many files import *this* file
    import_fan_in: Dict[str, int] = {}
    for src, deps in dep_graph.items():
        for dep in deps:
            import_fan_in[dep] = import_fan_in.get(dep, 0) + 1

    hotspots: List[FileHotspot] = []
    for rel in dep_graph:
        abs_path = root / rel
        if not abs_path.exists():
            continue

        try:
            source = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        lines = source.splitlines()
        line_count = len(lines)
        complexity = _estimate_complexity(source)
        import_count = len(dep_graph.get(rel, [])) + import_fan_in.get(rel, 0)

        try:
            mtime = abs_path.stat().st_mtime
            last_modified = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except OSError:
            last_modified = ""

        # Composite priority: heavier files with high complexity and many connections
        priority = (
            0.35 * min(line_count / 500.0, 3.0)
            + 0.35 * min(complexity / 20.0, 3.0)
            + 0.20 * min(import_count / 10.0, 3.0)
            + 0.10 * 1.0  # recency placeholder — all files weighted equally for now
        )

        hotspots.append(
            FileHotspot(
                file=rel,
                line_count=line_count,
                complexity_score=round(complexity, 2),
                import_count=import_count,
                last_modified=last_modified,
                priority_score=round(priority, 4),
            )
        )

    hotspots.sort(key=lambda h: h.priority_score, reverse=True)
    return [asdict(h) for h in hotspots]


# ---------------------------------------------------------------------------
# 3. suggest_refactorings
# ---------------------------------------------------------------------------

def suggest_refactorings(
    workspace: str,
    hotspots: Optional[List[Dict[str, Any]]] = None,
    max_suggestions: int = 5,
) -> List[Dict[str, Any]]:
    """Produce concrete refactoring suggestions for the top hotspots.

    Parameters
    ----------
    workspace:
        Project root.
    hotspots:
        Pre-computed hotspot list.  Built on-the-fly if ``None``.
    max_suggestions:
        Maximum number of suggestions to return.

    Returns
    -------
    list[dict]
        Serialisable :class:`RefactorSuggestion` dicts.
    """
    root = Path(workspace).resolve()
    if hotspots is None:
        hotspots = identify_hotspots(workspace)

    dep_graph = build_dependency_graph(workspace)

    # Detect circular imports
    circular_pairs = _find_circular_imports(dep_graph)

    suggestions: List[RefactorSuggestion] = []

    # Circular-import suggestions first (highest impact)
    for file_a, file_b in circular_pairs:
        if len(suggestions) >= max_suggestions:
            break
        suggestions.append(
            RefactorSuggestion(
                file=file_a,
                issue_type="circular_import",
                suggested_action=(
                    f"Circular dependency between '{file_a}' and '{file_b}'. "
                    f"Extract shared logic into a new module to break the cycle."
                ),
            )
        )

    # File-level suggestions from hotspot data
    for hs in hotspots:
        if len(suggestions) >= max_suggestions:
            break

        filepath = hs["file"]
        line_count = hs.get("line_count", 0)
        complexity = hs.get("complexity_score", 0.0)
        import_count = hs.get("import_count", 0)

        if line_count > 400:
            suggestions.append(
                RefactorSuggestion(
                    file=filepath,
                    issue_type="too_long",
                    suggested_action=(
                        f"File has {line_count} lines. Split into smaller modules "
                        f"by grouping related functions/classes."
                    ),
                )
            )
        elif complexity > 15:
            suggestions.append(
                RefactorSuggestion(
                    file=filepath,
                    issue_type="high_complexity",
                    suggested_action=(
                        f"Estimated complexity score is {complexity:.1f}. "
                        f"Extract complex branches into helper functions."
                    ),
                )
            )
        elif import_count > 8:
            suggestions.append(
                RefactorSuggestion(
                    file=filepath,
                    issue_type="high_coupling",
                    suggested_action=(
                        f"File has {import_count} import connections (in + out). "
                        f"Consider introducing an interface/facade to reduce coupling."
                    ),
                )
            )

    # Dead-code heuristic: files with zero fan-in that are not entry-points
    fan_in: Dict[str, int] = {}
    for deps in dep_graph.values():
        for d in deps:
            fan_in[d] = fan_in.get(d, 0) + 1

    for rel in dep_graph:
        if len(suggestions) >= max_suggestions:
            break
        if fan_in.get(rel, 0) == 0 and not _looks_like_entrypoint(root / rel):
            suggestions.append(
                RefactorSuggestion(
                    file=rel,
                    issue_type="dead_code",
                    suggested_action=(
                        f"No other file imports '{rel}' and it does not appear to be "
                        f"an entry-point. Verify it is still needed."
                    ),
                )
            )

    return [asdict(s) for s in suggestions[:max_suggestions]]


# ---------------------------------------------------------------------------
# 4. safe_refactor_step
# ---------------------------------------------------------------------------

def safe_refactor_step(
    workspace: str,
    suggestion: Dict[str, Any],
    test_command: str = DEFAULT_TEST_COMMAND,
) -> bool:
    """Apply one small, safe refactoring and revert if tests fail.

    Currently supports the ``too_long`` issue type by inserting a ``# TODO: refactor``
    marker and verifying tests still pass.  More sophisticated transforms can be added
    per issue type.

    Parameters
    ----------
    workspace:
        Project root.
    suggestion:
        A single suggestion dict (from :func:`suggest_refactorings`).
    test_command:
        Shell command to run the test suite.

    Returns
    -------
    bool
        ``True`` if the refactoring was applied and tests passed, ``False`` if
        the change was reverted or skipped.
    """
    root = Path(workspace).resolve()
    target = root / suggestion["file"]
    issue_type = suggestion.get("issue_type", "")

    if not target.exists():
        return False

    # Back up the original content
    original_content = target.read_text(encoding="utf-8")

    # Apply a transform based on issue_type
    new_content = _apply_transform(original_content, issue_type, suggestion)

    if new_content == original_content:
        # Nothing changed — skip
        return False

    # Write the transformed content
    target.write_text(new_content, encoding="utf-8")

    # Run tests
    tests_pass = _run_tests(workspace, test_command)

    if not tests_pass:
        # Revert
        target.write_text(original_content, encoding="utf-8")
        return False

    return True


# ---------------------------------------------------------------------------
# 5. run_background_refactor
# ---------------------------------------------------------------------------

def run_background_refactor(
    workspace: str,
    max_steps: int = 3,
    test_command: str = DEFAULT_TEST_COMMAND,
) -> Dict[str, Any]:
    """Execute up to *max_steps* safe refactoring steps and return a summary.

    Parameters
    ----------
    workspace:
        Project root.
    max_steps:
        Maximum number of refactoring steps to attempt.
    test_command:
        Shell command to run the test suite.

    Returns
    -------
    dict
        Summary with keys: ``attempted``, ``applied``, ``reverted``, ``skipped``,
        and ``details`` (list of per-step results).
    """
    suggestions = suggest_refactorings(workspace, max_suggestions=max_steps * 2)

    attempted = 0
    applied = 0
    reverted = 0
    skipped = 0
    details: List[Dict[str, Any]] = []

    for suggestion in suggestions:
        if attempted >= max_steps:
            break

        attempted += 1
        success = safe_refactor_step(workspace, suggestion, test_command)

        result = RefactorStepResult(
            file=suggestion["file"],
            issue_type=suggestion["issue_type"],
            applied=success,
            reverted=not success and suggestion.get("issue_type") != "dead_code",
            detail=suggestion.get("suggested_action", ""),
        )
        details.append(asdict(result))

        if success:
            applied += 1
        elif result.reverted:
            reverted += 1
        else:
            skipped += 1

    return {
        "attempted": attempted,
        "applied": applied,
        "reverted": reverted,
        "skipped": skipped,
        "details": details,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _collect_source_files(root: Path) -> List[str]:
    """Return relative paths of all Python source files under *root*."""
    results: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded directories in-place
        dirnames[:] = [d for d in dirnames if d not in EXCLUDED_DIRS]
        for fname in filenames:
            if any(fname.endswith(ext) for ext in SOURCE_EXTENSIONS):
                abs_path = Path(dirpath) / fname
                results.append(str(abs_path.relative_to(root)))
    return sorted(results)


def _path_to_module(rel_path: str) -> Optional[str]:
    """Convert a relative file path to a dotted module name."""
    p = Path(rel_path)
    if p.suffix != ".py":
        return None
    parts = list(p.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return None
    return ".".join(parts)


def _extract_imports(filepath: Path) -> List[str]:
    """Parse a Python file and return a list of imported module names."""
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except (SyntaxError, OSError):
        return []

    imports: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


def _estimate_complexity(source: str) -> float:
    """Estimate cyclomatic complexity via a simple keyword-counting heuristic.

    This is *not* a true McCabe score — it counts branching keywords as a rough proxy
    that works without external dependencies.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0.0

    score = 0.0
    for node in ast.walk(tree):
        if isinstance(node, (ast.If, ast.IfExp)):
            score += 1
        elif isinstance(node, (ast.For, ast.While, ast.AsyncFor)):
            score += 1
        elif isinstance(node, ast.ExceptHandler):
            score += 1
        elif isinstance(node, (ast.BoolOp,)):
            # Each ``and`` / ``or`` adds a branch
            score += len(node.values) - 1
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            score += 1  # baseline per function
    return score


def _find_circular_imports(
    dep_graph: Dict[str, List[str]],
) -> List[Tuple[str, str]]:
    """Return pairs of files that mutually import each other."""
    seen: set[Tuple[str, str]] = set()
    pairs: List[Tuple[str, str]] = []
    for src, deps in dep_graph.items():
        for dep in deps:
            if src in dep_graph.get(dep, []):
                pair = tuple(sorted([src, dep]))
                if pair not in seen:
                    seen.add(pair)
                    pairs.append((pair[0], pair[1]))
    return pairs


def _looks_like_entrypoint(filepath: Path) -> bool:
    """Heuristic: does the file contain ``if __name__`` or a CLI-style guard?"""
    try:
        text = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    return 'if __name__' in text or "def main" in text


def _apply_transform(
    content: str,
    issue_type: str,
    suggestion: Dict[str, Any],
) -> str:
    """Apply a deterministic refactoring transform to *content*.

    Currently supports lightweight, safe transforms:
    - ``too_long``: insert a section-separator comment before large function blocks.
    - ``high_complexity``: add a ``# FIXME(refactoring_engine)`` marker at the top.
    - Others: return content unchanged (suggestion is informational only).
    """
    if issue_type == "too_long":
        lines = content.splitlines(keepends=True)
        if len(lines) <= 400:
            return content
        # Insert a refactoring marker after the module docstring
        insert_idx = _find_docstring_end(content)
        marker = (
            "# --- refactoring_engine: this file exceeds 400 lines. "
            "Consider splitting into submodules. ---\n"
        )
        if marker in content:
            return content  # Already annotated
        lines.insert(insert_idx, marker)
        return "".join(lines)

    if issue_type == "high_complexity":
        marker = "# FIXME(refactoring_engine): high complexity — extract helpers.\n"
        if marker in content:
            return content
        insert_idx = _find_docstring_end(content)
        lines = content.splitlines(keepends=True)
        lines.insert(insert_idx, marker)
        return "".join(lines)

    # circular_import, dead_code, high_coupling — informational only
    return content


def _find_docstring_end(source: str) -> int:
    """Return the line index (0-based) just after the module-level docstring, or 0."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return 0

    if (
        tree.body
        and isinstance(tree.body[0], ast.Expr)
        and isinstance(tree.body[0].value, (ast.Constant, ast.Str))
    ):
        return tree.body[0].end_lineno or 0
    return 0


def _run_tests(workspace: str, test_command: str) -> bool:
    """Run the test suite and return ``True`` if it exits cleanly."""
    try:
        result = subprocess.run(
            test_command,
            shell=True,
            cwd=workspace,
            capture_output=True,
            timeout=120,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


# ---------------------------------------------------------------------------
# CLI entry-point
# ---------------------------------------------------------------------------

def main() -> int:
    """CLI entry-point for refactoring_engine."""
    parser = argparse.ArgumentParser(
        description=(
            "refactoring_engine: build dependency graphs, find hotspots, "
            "and apply safe incremental refactorings"
        ),
    )
    sub = parser.add_subparsers(dest="command")

    # -- graph
    graph_p = sub.add_parser("graph", help="Build the dependency graph")
    graph_p.add_argument("--workspace", default=".", help="Project root")

    # -- hotspots
    hs_p = sub.add_parser("hotspots", help="Identify refactoring hotspots")
    hs_p.add_argument("--workspace", default=".", help="Project root")

    # -- suggest
    sug_p = sub.add_parser("suggest", help="Suggest refactorings")
    sug_p.add_argument("--workspace", default=".", help="Project root")
    sug_p.add_argument("--max", type=int, default=5, help="Max suggestions")

    # -- refactor
    ref_p = sub.add_parser("refactor", help="Run background refactoring steps")
    ref_p.add_argument("--workspace", default=".", help="Project root")
    ref_p.add_argument("--max-steps", type=int, default=3, help="Max steps")
    ref_p.add_argument(
        "--test-command", default=DEFAULT_TEST_COMMAND, help="Test command"
    )

    args = parser.parse_args()

    if args.command == "graph":
        graph = build_dependency_graph(args.workspace)
        print(json.dumps(graph, indent=2, ensure_ascii=False))

    elif args.command == "hotspots":
        spots = identify_hotspots(args.workspace)
        print(json.dumps(spots, indent=2, ensure_ascii=False))

    elif args.command == "suggest":
        suggestions = suggest_refactorings(args.workspace, max_suggestions=args.max)
        print(json.dumps(suggestions, indent=2, ensure_ascii=False))

    elif args.command == "refactor":
        summary = run_background_refactor(
            args.workspace,
            max_steps=args.max_steps,
            test_command=args.test_command,
        )
        print(json.dumps(summary, indent=2, ensure_ascii=False))

    else:
        parser.print_help()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
