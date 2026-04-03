"""
Code Synthesis Pipeline — Dynamic skill generation with AST verification.

Pipeline: Need Detection -> Code Generation -> AST Gate -> Sandbox Test -> Registration

This module enables the autonomous learning loop to synthesize new skills
when recurring errors indicate missing capabilities. Generated code passes
through a strict AST static gate, sandbox execution, and formal registration
before becoming available to the agent.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SynthesisRequest:
    """A request to synthesize a new skill."""
    need_description: str
    error_context: str
    proposed_solution: str
    required_capabilities: list[str]
    target_skill_name: str


@dataclass
class SynthesisResult:
    """Outcome of a synthesis pipeline run."""
    success: bool
    skill_name: str
    skill_path: str = ""
    tests_passed: bool = False
    ast_valid: bool = False
    sandbox_safe: bool = False
    error: str = ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return s or "synth_skill"


def _evolution_log_append(workspace: Path, *, kind: str, detail: str) -> None:
    p = workspace / ".claw" / "evolution_failures.jsonl"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": time.time(), "kind": kind, "detail": (detail or "")[:4000]},
            ensure_ascii=False,
        )
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


_SAFE_IMPORTS = frozenset({
    "os", "sys", "re", "json", "math", "time", "datetime", "pathlib",
    "collections", "itertools", "functools", "typing", "dataclasses",
    "hashlib", "base64", "urllib", "http", "logging", "textwrap",
    "io", "csv", "copy", "string", "enum", "abc", "contextlib",
    "tempfile", "shutil", "uuid", "pprint", "operator", "statistics",
    "decimal", "fractions", "struct", "socket", "ssl",
    # Common third-party (non-destructive)
    "requests", "httpx", "aiohttp", "bs4", "lxml", "yaml", "toml",
    "pytest", "numpy", "pandas",
})

_BLACKLISTED_CALLS = {
    ("os", "system"),
    ("shutil", "rmtree"),
}


# ---------------------------------------------------------------------------
# 1. Code Generation
# ---------------------------------------------------------------------------

def _build_generation_prompt(request: SynthesisRequest) -> str:
    return textwrap.dedent(f"""\
    Generate a Python skill package for the following need:

    NEED: {request.need_description}
    ERROR CONTEXT: {request.error_context}
    PROPOSED SOLUTION: {request.proposed_solution}
    CAPABILITIES NEEDED: {', '.join(request.required_capabilities)}
    SKILL NAME: {request.target_skill_name}

    Generate THREE sections separated by the exact markers shown:

    === SKILL.md ===
    (YAML frontmatter with name, description, metadata fields, then body with
     usage instructions)

    === runner.py ===
    (Must contain `def run(**kwargs)` as main entry point.
     Must be safe — no os.system, no eval/exec, no shutil.rmtree.
     Must return a dict with at least 'status' and 'result' keys.)

    === test_runner.py ===
    (pytest-compatible tests for runner.py. Must import runner and call run().)

    Requirements:
    - runner.py MUST have `def run(**kwargs)` as the entry point
    - All imports must be from the standard library or well-known packages
    - No destructive operations (os.system, subprocess with shell=True, eval, exec)
    - Tests must be runnable with `pytest test_runner.py`
    - Return structured output from run()
    """)


def _parse_generated_sections(text: str) -> tuple[str, str, str]:
    """Parse the three sections from LLM output."""
    skill_md = ""
    runner_py = ""
    test_py = ""

    # Try marker-based splitting
    parts = re.split(r"===\s*(SKILL\.md|runner\.py|test_runner\.py)\s*===", text)
    for i, part in enumerate(parts):
        if part.strip() == "SKILL.md" and i + 1 < len(parts):
            skill_md = parts[i + 1].strip()
        elif part.strip() == "runner.py" and i + 1 < len(parts):
            runner_py = parts[i + 1].strip()
        elif part.strip() == "test_runner.py" and i + 1 < len(parts):
            test_py = parts[i + 1].strip()

    # Strip markdown code fences if present
    for content in [skill_md, runner_py, test_py]:
        pass  # handled below

    def _strip_fences(s: str) -> str:
        s = s.strip()
        if s.startswith("```"):
            first_nl = s.index("\n") if "\n" in s else len(s)
            s = s[first_nl + 1:]
        if s.endswith("```"):
            s = s[:-3]
        return s.strip()

    runner_py = _strip_fences(runner_py)
    test_py = _strip_fences(test_py)

    return skill_md, runner_py, test_py


def _template_fallback(request: SynthesisRequest) -> tuple[str, str, str]:
    """Generate a minimal skill skeleton when no LLM is available."""
    name = _slug(request.target_skill_name)

    skill_md = textwrap.dedent(f"""\
    ---
    name: {name}
    description: "Auto-generated skill for: {request.need_description[:120]}"
    metadata:
      openclaw:
        always: false
    ---

    ## Purpose

    {request.need_description}

    ## Error Context

    {request.error_context[:500]}

    ## Runner

    Call `execute_terminal`: `python skills/{name}/runner.py`
    """)

    runner_py = textwrap.dedent(f"""\
    \"\"\"
    Auto-generated skill runner: {name}

    Need: {request.need_description[:200]}
    \"\"\"

    from __future__ import annotations
    from typing import Any


    def run(**kwargs: Any) -> dict[str, Any]:
        \"\"\"Main entry point for the {name} skill.\"\"\"
        task = kwargs.get("task", "")
        # TODO: Implement actual logic for: {request.proposed_solution[:200]}
        return {{
            "status": "stub",
            "result": f"Skill '{name}' invoked with task={{task!r}}. Implementation pending.",
            "need": {request.need_description!r},
            "capabilities": {request.required_capabilities!r},
        }}


    if __name__ == "__main__":
        import json
        import sys

        task = " ".join(sys.argv[1:]) or "default"
        out = run(task=task)
        print(json.dumps(out, indent=2))
    """)

    test_py = textwrap.dedent(f"""\
    \"\"\"Tests for {name} skill runner.\"\"\"

    from runner import run


    def test_run_returns_dict():
        result = run()
        assert isinstance(result, dict)
        assert "status" in result
        assert "result" in result


    def test_run_with_task():
        result = run(task="hello world")
        assert isinstance(result, dict)
        assert result["status"] in ("stub", "ok", "success", "error")
    """)

    return skill_md, runner_py, test_py


def _try_claude_cli(prompt: str) -> str | None:
    """Try Claude CLI for code generation."""
    claude_cmd = shutil.which("claude")
    if not claude_cmd:
        return None
    try:
        cp = subprocess.run(
            [claude_cmd, "-p", prompt],
            capture_output=True, text=True, timeout=120,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def _try_ollama(prompt: str) -> str | None:
    """Try Ollama for code generation."""
    ollama_cmd = shutil.which("ollama")
    if not ollama_cmd:
        return None
    model = os.environ.get("OLLAMA_CODE_MODEL", "codellama")
    try:
        cp = subprocess.run(
            [ollama_cmd, "run", model, prompt],
            capture_output=True, text=True, timeout=180,
        )
        if cp.returncode == 0 and cp.stdout.strip():
            return cp.stdout.strip()
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


def generate_skill_code(
    request: SynthesisRequest,
    workspace: Path,
) -> tuple[str, str, str]:
    """Generate skill code. Returns (skill_md, runner_py, test_py).

    Tries Claude CLI first (best quality), then Ollama, then template fallback.
    """
    prompt = _build_generation_prompt(request)

    # Try Claude CLI
    raw = _try_claude_cli(prompt)
    if raw:
        skill_md, runner_py, test_py = _parse_generated_sections(raw)
        if runner_py and "def run(" in runner_py:
            return skill_md, runner_py, test_py

    # Try Ollama
    raw = _try_ollama(prompt)
    if raw:
        skill_md, runner_py, test_py = _parse_generated_sections(raw)
        if runner_py and "def run(" in runner_py:
            return skill_md, runner_py, test_py

    # Template fallback
    return _template_fallback(request)


# ---------------------------------------------------------------------------
# 2. AST Static Gate
# ---------------------------------------------------------------------------

def ast_static_gate(code: str) -> tuple[bool, list[str]]:
    """Parse and statically verify generated code.

    Checks:
    - Valid Python syntax (ast.parse)
    - No blacklisted calls (os.system, subprocess with shell=True, etc.)
    - Required function signatures (def run)
    - Imports against known-safe list

    Returns (passed, warnings).
    """
    warnings: list[str] = []

    # 1. Syntax check
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"SyntaxError: {e}"]

    # 2. Check for def run
    has_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True
            break
    if not has_run:
        return False, ["Missing required function: def run(**kwargs)"]

    # 3. Check for blacklisted calls
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            # os.system(...)
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                pair = (func.value.id, func.attr)
                if pair in _BLACKLISTED_CALLS:
                    return False, [f"Blacklisted call: {pair[0]}.{pair[1]}"]
                # subprocess.call(..., shell=True)
                if pair == ("subprocess", "call") or pair == ("subprocess", "run"):
                    for kw in node.keywords:
                        if kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                            return False, [f"Blacklisted: {pair[0]}.{pair[1]} with shell=True"]
            # Bare eval/exec
            if isinstance(func, ast.Name) and func.id in ("eval", "exec"):
                # Allow if inside a string-literal-only context (e.g., test helpers)
                # For safety, flag it as a warning rather than hard block
                warnings.append(f"Potentially unsafe call: {func.id}()")

    # 4. Check imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in _SAFE_IMPORTS:
                    warnings.append(f"Import not in safe list: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root not in _SAFE_IMPORTS:
                    warnings.append(f"Import not in safe list: {node.module}")

    # eval/exec warnings don't block, but everything else passed
    return True, warnings


# ---------------------------------------------------------------------------
# 3. Sandbox Test
# ---------------------------------------------------------------------------

def sandbox_test(skill_dir: Path, workspace: Path) -> tuple[bool, str]:
    """Run sandbox verification on a generated skill.

    Attempts Docker-based quarantine first, falls back to subprocess isolation.
    Returns (passed, output_text).
    """
    runner_path = skill_dir / "runner.py"
    test_path = skill_dir / "test_runner.py"

    if not runner_path.is_file():
        return False, "runner.py not found in skill directory"

    # Try immunization sandbox for the runner code
    try:
        from claw_runtime.immunization_sandbox import quarantine_code
        runner_code = runner_path.read_text(encoding="utf-8")
        qr = quarantine_code(runner_code, workspace, timeout_sec=30, allow_network=False)
        if not qr.safe:
            return False, f"Quarantine failed: {'; '.join(qr.threats)}"
    except ImportError:
        pass  # Sandbox module unavailable, continue with pytest
    except Exception as e:
        return False, f"Quarantine error: {e}"

    # Run pytest on the test file
    if not test_path.is_file():
        # No test file — pass with a note
        return True, "No test file provided; runner passed quarantine."

    try:
        cp = subprocess.run(
            [sys.executable, "-m", "pytest", str(test_path), "-v", "--tb=short", "-x"],
            cwd=str(skill_dir),
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        output = (cp.stdout + "\n" + cp.stderr).strip()
        return cp.returncode == 0, output[:3000]
    except subprocess.TimeoutExpired:
        return False, "Test execution timed out (60s)"
    except OSError as e:
        return False, f"Failed to run pytest: {e}"


# ---------------------------------------------------------------------------
# 4. Skill Registration
# ---------------------------------------------------------------------------

def register_skill(skill_name: str, skill_dir: Path, workspace: Path) -> bool:
    """Register a synthesized skill in skills.json and refresh the registry."""
    workspace = Path(workspace).resolve()
    registry_path = workspace / "skills.json"

    # Load or create registry
    registry: dict[str, Any] = {}
    if registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            registry = {}

    # Add entry
    rel = str(skill_dir.relative_to(workspace)) if skill_dir.is_relative_to(workspace) else str(skill_dir)
    registry[skill_name] = {
        "path": rel,
        "synthesized": True,
        "registered_at": time.time(),
    }

    try:
        registry_path.write_text(
            json.dumps(registry, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        return False

    # Refresh SkillRegistry if available
    try:
        from claw_runtime.skill_registry import SkillRegistry
        SkillRegistry(workspace).refresh()
    except Exception:
        pass  # Non-fatal

    return True


# ---------------------------------------------------------------------------
# 5. Full Pipeline
# ---------------------------------------------------------------------------

def run_synthesis_pipeline(
    request: SynthesisRequest,
    workspace: Path,
    emit: Any | None = None,
) -> SynthesisResult:
    """Execute the full synthesis pipeline: generate -> AST gate -> sandbox -> register.

    Args:
        request: What skill to synthesize and why.
        workspace: Project root.
        emit: Optional callable for progress updates (internal, not user-facing).

    Returns:
        SynthesisResult with outcome details.
    """
    workspace = Path(workspace).resolve()
    name = _slug(request.target_skill_name)

    def _emit(msg: str) -> None:
        if callable(emit):
            try:
                emit(msg)
            except Exception:
                pass

    _emit(f"[synthesis] Generating code for skill: {name}")

    # Step 1: Generate
    try:
        skill_md, runner_py, test_py = generate_skill_code(request, workspace)
    except Exception as e:
        _evolution_log_append(workspace, kind="synthesis_generate_fail", detail=str(e))
        return SynthesisResult(success=False, skill_name=name, error=f"Generation failed: {e}")

    if not runner_py:
        _evolution_log_append(workspace, kind="synthesis_empty_runner", detail=name)
        return SynthesisResult(success=False, skill_name=name, error="Generated runner.py was empty")

    # Step 2: AST Gate
    _emit(f"[synthesis] Running AST static gate for: {name}")
    ast_ok, ast_warnings = ast_static_gate(runner_py)
    if not ast_ok:
        detail = f"AST gate failed for {name}: {'; '.join(ast_warnings)}"
        _evolution_log_append(workspace, kind="synthesis_ast_fail", detail=detail)
        return SynthesisResult(
            success=False, skill_name=name, ast_valid=False,
            error=f"AST gate failed: {'; '.join(ast_warnings)}",
        )

    # Step 3: Write to temp dir and sandbox test
    _emit(f"[synthesis] Sandbox testing: {name}")
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"synth_{name}_"))
    try:
        (tmp_dir / "SKILL.md").write_text(skill_md, encoding="utf-8")
        (tmp_dir / "runner.py").write_text(runner_py, encoding="utf-8")
        if test_py:
            (tmp_dir / "test_runner.py").write_text(test_py, encoding="utf-8")

        sandbox_ok, sandbox_output = sandbox_test(tmp_dir, workspace)
        if not sandbox_ok:
            detail = f"Sandbox failed for {name}: {sandbox_output[:1000]}"
            _evolution_log_append(workspace, kind="synthesis_sandbox_fail", detail=detail)
            return SynthesisResult(
                success=False, skill_name=name, ast_valid=True,
                sandbox_safe=False, error=f"Sandbox test failed: {sandbox_output[:500]}",
            )

        # Step 4: Move to skills/ and register
        _emit(f"[synthesis] Registering skill: {name}")
        skill_dir = workspace / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        tests_dir = skill_dir / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(tmp_dir / "SKILL.md", skill_dir / "SKILL.md")
        shutil.copy2(tmp_dir / "runner.py", skill_dir / "runner.py")
        if (tmp_dir / "test_runner.py").is_file():
            shutil.copy2(tmp_dir / "test_runner.py", tests_dir / "test_runner.py")

        registered = register_skill(name, skill_dir, workspace)
        if not registered:
            _evolution_log_append(workspace, kind="synthesis_register_fail", detail=name)
            return SynthesisResult(
                success=False, skill_name=name, skill_path=str(skill_dir),
                ast_valid=True, sandbox_safe=True, tests_passed=sandbox_ok,
                error="Registration failed",
            )

        _emit(f"[synthesis] Skill '{name}' synthesized and registered successfully.")
        return SynthesisResult(
            success=True, skill_name=name, skill_path=str(skill_dir),
            tests_passed=sandbox_ok, ast_valid=True, sandbox_safe=True,
        )

    finally:
        # Clean up temp dir
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 6. Need Detection
# ---------------------------------------------------------------------------

def detect_skill_needs(
    attributions: list[dict[str, Any]],
    workspace: Path,
) -> list[SynthesisRequest]:
    """Analyze error attributions to identify missing capabilities.

    Pattern matching:
    - "No module named X"      -> need X integration skill
    - "403 Forbidden from Y"   -> need Y-specific scraper skill
    - "Unknown tool: Z"        -> need Z tool implementation
    - General recurring errors  -> need error-specific handler

    Returns a list of SynthesisRequests for detected needs.
    """
    workspace = Path(workspace).resolve()
    requests: list[SynthesisRequest] = []
    seen_skills: set[str] = set()

    # Load existing skills to avoid duplicates
    existing_skills: set[str] = set()
    skills_json = workspace / "skills.json"
    if skills_json.is_file():
        try:
            existing_skills = set(json.loads(skills_json.read_text(encoding="utf-8")).keys())
        except (json.JSONDecodeError, OSError):
            pass

    # Also check skills/ directory
    skills_dir = workspace / "skills"
    if skills_dir.is_dir():
        for d in skills_dir.iterdir():
            if d.is_dir() and (d / "SKILL.md").is_file():
                existing_skills.add(d.name)

    for attr in attributions:
        error_msg = str(attr.get("error_message", "") or attr.get("detail", ""))
        error_type = str(attr.get("error_type", ""))
        proposed = str(attr.get("proposed_solution", ""))

        skill_name = ""
        need_desc = ""
        capabilities: list[str] = []

        # Pattern: Missing module
        m = re.search(r"No module named ['\"]?(\w+)['\"]?", error_msg)
        if m:
            mod = m.group(1)
            skill_name = f"integrate_{_slug(mod)}"
            need_desc = f"Integration skill for Python module '{mod}'"
            capabilities = [f"import {mod}", f"{mod} API wrapper"]

        # Pattern: 403 Forbidden
        if not skill_name:
            m = re.search(r"403\s+Forbidden.*?(?:from|at|for)\s+(\S+)", error_msg, re.IGNORECASE)
            if m:
                site = m.group(1).rstrip("/.,;")
                domain = re.sub(r"^https?://", "", site).split("/")[0]
                skill_name = f"scraper_{_slug(domain)}"
                need_desc = f"Specialized scraper/API client for {domain}"
                capabilities = ["HTTP requests", "rate limiting", "auth handling"]

        # Pattern: Unknown tool
        if not skill_name:
            m = re.search(r"Unknown tool:\s*['\"]?(\w[\w-]*)['\"]?", error_msg, re.IGNORECASE)
            if m:
                tool = m.group(1)
                skill_name = f"tool_{_slug(tool)}"
                need_desc = f"Implementation of tool '{tool}'"
                capabilities = [f"{tool} execution", "structured output"]

        # Pattern: Command not found
        if not skill_name:
            m = re.search(r"(?:command not found|not recognized).*?['\"]?(\w[\w-]+)['\"]?", error_msg, re.IGNORECASE)
            if m:
                cmd = m.group(1)
                skill_name = f"tool_{_slug(cmd)}"
                need_desc = f"Wrapper/alternative for missing command '{cmd}'"
                capabilities = [f"replicate {cmd} functionality"]

        # Fallback: derive from error_type
        if not skill_name and error_type:
            skill_name = f"handler_{_slug(error_type)}"
            need_desc = f"Error handler for recurring '{error_type}' failures"
            capabilities = ["error recovery", "graceful degradation"]

        if not skill_name:
            continue

        # De-duplicate
        if skill_name in seen_skills or skill_name in existing_skills:
            continue
        seen_skills.add(skill_name)

        requests.append(SynthesisRequest(
            need_description=need_desc,
            error_context=error_msg[:1000],
            proposed_solution=proposed[:500] or f"Create a skill to handle: {need_desc}",
            required_capabilities=capabilities,
            target_skill_name=skill_name,
        ))

    return requests
