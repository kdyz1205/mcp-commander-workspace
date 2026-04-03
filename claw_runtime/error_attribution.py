"""
Error Attribution Pipeline -- AOP interceptor for tool calls.

Wraps every tool execution with:
1. Context snapshot (input params, state)
2. Try-except with structured error capture
3. LLM-powered root cause diagnosis (or heuristic fallback)
4. Structured JSONL logging to .claw/error_attributions.jsonl
5. Forced reasoning_episode write on failure
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional


@dataclass
class ErrorAttribution:
    """Structured record of a tool execution outcome with diagnostic metadata."""

    timestamp: float
    tool_name: str
    instruction_context: str  # first 500 chars of user instruction
    input_params: dict[str, Any]
    stdout: str
    stderr: str
    exit_code: Optional[int]
    error_type: str
    root_cause_analysis: str
    proposed_solution: str
    confidence_score: float  # 0.0 - 1.0
    model_used: str


# ---------------------------------------------------------------------------
# 1. Context capture
# ---------------------------------------------------------------------------

def capture_tool_context(
    tool_name: str,
    args: dict[str, Any],
    user_instruction: str,
) -> dict[str, Any]:
    """Snapshot the execution context before a tool runs.

    Returns a dict suitable for diagnostic prompts and logging.
    """
    return {
        "tool_name": tool_name,
        "input_params": args,
        "instruction_context": (user_instruction or "")[:500],
        "capture_ts": time.time(),
    }


# ---------------------------------------------------------------------------
# 2. Heuristic error patterns (>=15)
# ---------------------------------------------------------------------------

_HEURISTIC_PATTERNS: list[tuple[re.Pattern[str], str, str, float]] = [
    # (regex, error_type, proposed_solution, confidence)
    (re.compile(r"403\b.*Forbidden", re.I),
     "anti_scraping", "Site blocks automated requests. Use a headless browser, add headers, or try an API.", 0.75),
    (re.compile(r"ECONNREFUSED|Connection refused", re.I),
     "service_down", "Target service is not running. Start the service or check the host/port.", 0.80),
    (re.compile(r"SyntaxError", re.I),
     "bad_code", "Python syntax error in generated code. Review and fix the syntax.", 0.85),
    (re.compile(r"PermissionError|Permission denied", re.I),
     "access_denied", "Insufficient file-system permissions. Check ownership or run with elevated rights.", 0.80),
    (re.compile(r"FileNotFoundError|No such file or directory", re.I),
     "file_missing", "Referenced file does not exist. Verify the path or create the file first.", 0.85),
    (re.compile(r"ModuleNotFoundError|No module named", re.I),
     "missing_dependency", "A required Python package is missing. Install it with pip.", 0.85),
    (re.compile(r"TimeoutError|timed?\s*out", re.I),
     "timeout", "Operation timed out. Increase the timeout or check network/service health.", 0.70),
    (re.compile(r"MemoryError|OOM|Out of memory", re.I),
     "out_of_memory", "Process ran out of memory. Reduce data size or increase available RAM.", 0.80),
    (re.compile(r"JSONDecodeError|Expecting value", re.I),
     "bad_json", "Failed to parse JSON. Validate the response or input format.", 0.80),
    (re.compile(r"KeyError", re.I),
     "missing_key", "Dictionary key not found. Check the data structure for expected keys.", 0.75),
    (re.compile(r"IndexError", re.I),
     "index_out_of_range", "List index out of range. Validate collection length before access.", 0.75),
    (re.compile(r"TypeError.*argument", re.I),
     "type_mismatch", "Wrong argument type passed to function. Check function signature.", 0.70),
    (re.compile(r"AttributeError", re.I),
     "attribute_error", "Object does not have the expected attribute. Verify the object type.", 0.70),
    (re.compile(r"ValueError", re.I),
     "value_error", "Invalid value for operation. Check input constraints and ranges.", 0.65),
    (re.compile(r"ConnectionError|ConnectionReset|BrokenPipe", re.I),
     "network_error", "Network connection failed or was reset. Retry or check connectivity.", 0.70),
    (re.compile(r"SSLError|CERTIFICATE_VERIFY_FAILED|SSL", re.I),
     "ssl_error", "SSL/TLS handshake or certificate error. Update certs or disable verification for dev.", 0.75),
    (re.compile(r"429\b|Too Many Requests|rate.?limit", re.I),
     "rate_limited", "API rate limit hit. Back off, add delay, or switch to a different key/endpoint.", 0.85),
    (re.compile(r"401\b|Unauthorized|authentication", re.I),
     "auth_failure", "Authentication failed. Check API key, token, or credentials.", 0.80),
    (re.compile(r"404\b|Not Found", re.I),
     "not_found", "Resource not found (404). Verify the URL or endpoint path.", 0.80),
    (re.compile(r"500\b|Internal Server Error", re.I),
     "server_error", "Remote server returned 500. This is a server-side issue; retry later.", 0.65),
    (re.compile(r"UnicodeDecodeError|UnicodeEncodeError|codec can't", re.I),
     "encoding_error", "Text encoding mismatch. Specify encoding explicitly (e.g. utf-8).", 0.80),
    (re.compile(r"RecursionError|maximum recursion", re.I),
     "recursion_overflow", "Infinite recursion detected. Add a base case or increase the limit.", 0.85),
    (re.compile(r"OSError|IOError|Errno", re.I),
     "os_error", "OS-level I/O error. Check disk space, file handles, or device availability.", 0.60),
]


def _heuristic_diagnose(error_text: str) -> dict[str, Any]:
    """Pattern-match the error against known heuristics.

    Returns a diagnosis dict or a generic fallback.
    """
    for pattern, err_type, solution, confidence in _HEURISTIC_PATTERNS:
        if pattern.search(error_text):
            return {
                "error_type": err_type,
                "root_cause_analysis": f"Heuristic match: {err_type} pattern detected in error output.",
                "proposed_solution": solution,
                "confidence_score": confidence,
            }
    return {
        "error_type": "unknown",
        "root_cause_analysis": "No heuristic pattern matched. Manual investigation required.",
        "proposed_solution": "Inspect the full traceback and tool inputs for root cause.",
        "confidence_score": 0.2,
    }


# ---------------------------------------------------------------------------
# 3. LLM-powered diagnosis
# ---------------------------------------------------------------------------

def _build_diagnostic_prompt(context: dict[str, Any], error: str, traceback_text: str) -> str:
    """Compose a prompt for LLM-based root-cause analysis."""
    return (
        "You are a senior DevOps engineer diagnosing a tool failure.\n\n"
        "[Task Context]\n"
        f"Tool: {context.get('tool_name', '?')}\n"
        f"User instruction (truncated): {context.get('instruction_context', '?')}\n\n"
        "[Input Params]\n"
        f"{json.dumps(context.get('input_params', {}), ensure_ascii=False, indent=2)[:2000]}\n\n"
        "[Error]\n"
        f"{error[:2000]}\n\n"
        "[Traceback]\n"
        f"{traceback_text[:3000]}\n\n"
        "Respond with ONLY a JSON object (no markdown fences):\n"
        '{"error_type": "...", "root_cause_analysis": "...", '
        '"proposed_solution": "...", "confidence_score": 0.0-1.0}'
    )


def _try_claude_cli(prompt: str) -> Optional[dict[str, Any]]:
    """Attempt diagnosis via Claude CLI (free tier)."""
    try:
        result = subprocess.run(
            ["claude", "--print", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_llm_json(result.stdout.strip()), "claude_cli"  # type: ignore[return-value]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _try_ollama(prompt: str) -> Optional[dict[str, Any]]:
    """Attempt diagnosis via local Ollama."""
    try:
        import os
        model = os.environ.get("OLLAMA_DIAG_MODEL", "llama3.2")
        result = subprocess.run(
            ["ollama", "run", model, prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0 and result.stdout.strip():
            return _parse_llm_json(result.stdout.strip()), "ollama/" + model  # type: ignore[return-value]
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _parse_llm_json(raw: str) -> Optional[dict[str, Any]]:
    """Extract a JSON object from LLM output, tolerating markdown fences."""
    # Strip markdown code fences if present
    cleaned = re.sub(r"```(?:json)?\s*", "", raw)
    cleaned = cleaned.strip().rstrip("`")
    try:
        obj = json.loads(cleaned)
        if isinstance(obj, dict) and "error_type" in obj:
            # Clamp confidence
            obj["confidence_score"] = max(0.0, min(1.0, float(obj.get("confidence_score", 0.5))))
            return obj
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return None


def diagnose_failure(
    context: dict[str, Any],
    error: str,
    traceback_text: str,
    workspace: Path,
) -> ErrorAttribution:
    """Diagnose a tool failure using LLM cascade then heuristic fallback.

    Tries Claude CLI first, then Ollama, then heuristic pattern-matching.
    Returns a structured ErrorAttribution with the diagnosis.
    """
    prompt = _build_diagnostic_prompt(context, error, traceback_text)
    diagnosis: Optional[dict[str, Any]] = None
    model_used = "heuristic"

    # Cascade: Claude CLI -> Ollama -> heuristic
    result = _try_claude_cli(prompt)
    if result is not None:
        diagnosis, model_used = result  # type: ignore[misc]

    if diagnosis is None:
        result = _try_ollama(prompt)
        if result is not None:
            diagnosis, model_used = result  # type: ignore[misc]

    if diagnosis is None:
        diagnosis = _heuristic_diagnose(error + "\n" + traceback_text)
        model_used = "heuristic"

    return ErrorAttribution(
        timestamp=time.time(),
        tool_name=context.get("tool_name", "unknown"),
        instruction_context=context.get("instruction_context", ""),
        input_params=context.get("input_params", {}),
        stdout="",
        stderr=error[:4000],
        exit_code=None,
        error_type=diagnosis.get("error_type", "unknown"),
        root_cause_analysis=diagnosis.get("root_cause_analysis", ""),
        proposed_solution=diagnosis.get("proposed_solution", ""),
        confidence_score=float(diagnosis.get("confidence_score", 0.3)),
        model_used=model_used,
    )


# ---------------------------------------------------------------------------
# 4. JSONL logging
# ---------------------------------------------------------------------------

def _attribution_log_path(workspace: Path) -> Path:
    """Return the path to the error attributions JSONL file."""
    return Path(workspace).resolve() / ".claw" / "error_attributions.jsonl"


def log_attribution(workspace: Path, attr: ErrorAttribution) -> None:
    """Append one ErrorAttribution record to .claw/error_attributions.jsonl."""
    path = _attribution_log_path(workspace)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(attr), ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass  # Silent — never crash the main loop for logging


# ---------------------------------------------------------------------------
# 5. Forced reflection on failure
# ---------------------------------------------------------------------------

def force_reflection(workspace: Path, attr: ErrorAttribution) -> None:
    """Create a paper trail for the learning loop on failure.

    Appends a reasoning episode and an evolution failure record so that
    nightly evolution and meta-driving can learn from this error.
    """
    ws = Path(workspace).resolve()
    try:
        from claw_runtime.reasoning_episode import append_reasoning_episode

        append_reasoning_episode(
            ws,
            trigger=f"Error attribution: {attr.tool_name} -> {attr.error_type}",
            hypothesis=(
                f"Tool '{attr.tool_name}' failed with {attr.error_type}. "
                f"Root cause: {attr.root_cause_analysis[:300]}"
            ),
            verify_plan=[
                f"Proposed fix: {attr.proposed_solution[:200]}",
                f"Confidence: {attr.confidence_score:.2f} (model: {attr.model_used})",
                "Re-run the tool with corrected parameters or environment.",
            ],
            revise_hint=(
                f"If fix does not resolve: escalate. "
                f"Error stderr snippet: {attr.stderr[:200]}"
            ),
        )
    except Exception:
        pass  # Never crash the main loop

    try:
        from claw_runtime.nightly_evolution import append_evolution_failure

        append_evolution_failure(
            ws,
            kind=f"tool_error:{attr.error_type}",
            detail=(
                f"Tool: {attr.tool_name}\n"
                f"Root cause: {attr.root_cause_analysis}\n"
                f"Solution: {attr.proposed_solution}\n"
                f"Confidence: {attr.confidence_score:.2f}\n"
                f"Stderr: {attr.stderr[:500]}"
            ),
        )
    except Exception:
        pass  # Never crash the main loop


# ---------------------------------------------------------------------------
# 6. Main middleware
# ---------------------------------------------------------------------------

def tool_execution_middleware(
    tool_name: str,
    args: dict[str, Any],
    executor_fn: Callable[[], str],
    workspace: Path,
    user_instruction: str,
) -> str:
    """AOP middleware wrapping a single tool execution.

    Captures context, runs the tool, and on failure performs diagnosis,
    logging, and reflection. On success, logs the outcome for positive
    learning. Returns the tool result string (same interface as before).

    This middleware is SILENT to the user — no _emit calls. If the
    middleware itself fails internally, it falls back to returning
    the raw tool result (or re-raising the original error).
    """
    context = capture_tool_context(tool_name, args, user_instruction)

    try:
        result = executor_fn()
    except Exception as exc:
        tb_text = traceback.format_exc()
        error_str = f"{type(exc).__name__}: {exc}"

        try:
            attr = diagnose_failure(context, error_str, tb_text, Path(workspace))
            attr.exit_code = getattr(exc, "returncode", None)
            log_attribution(workspace, attr)
            force_reflection(workspace, attr)
        except Exception:
            pass  # Middleware must never swallow the original error path

        raise  # Re-raise so the caller can handle as before

    # Success path — still log for positive learning
    try:
        result_str = str(result)
        _looks_like_error = (
            "error" in result_str.lower()[:500]
            or "traceback" in result_str.lower()[:500]
            or "failed" in result_str.lower()[:200]
        )
        if _looks_like_error:
            # Soft failure — tool returned but output suggests an error
            attr = diagnose_failure(context, result_str[:2000], "", Path(workspace))
            attr.stdout = result_str[:4000]
            attr.exit_code = 0
            log_attribution(workspace, attr)
            force_reflection(workspace, attr)
        else:
            # Clean success — lightweight log entry
            success_attr = ErrorAttribution(
                timestamp=time.time(),
                tool_name=tool_name,
                instruction_context=(user_instruction or "")[:500],
                input_params=args,
                stdout=result_str[:2000],
                stderr="",
                exit_code=0,
                error_type="success",
                root_cause_analysis="",
                proposed_solution="",
                confidence_score=1.0,
                model_used="none",
            )
            log_attribution(workspace, success_attr)
    except Exception:
        pass  # Never crash for logging

    return result


# ---------------------------------------------------------------------------
# 7. Load recent attributions
# ---------------------------------------------------------------------------

def load_recent_attributions(
    workspace: Path,
    hours: float = 24,
) -> list[ErrorAttribution]:
    """Read back ErrorAttribution records from the last *hours* hours.

    Returns a list sorted newest-first. Silently returns [] on any I/O error.
    """
    path = _attribution_log_path(workspace)
    if not path.is_file():
        return []

    cutoff = time.time() - (hours * 3600)
    results: list[ErrorAttribution] = []

    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ts = float(obj.get("timestamp", 0))
                    if ts >= cutoff:
                        results.append(ErrorAttribution(**obj))
                except (json.JSONDecodeError, TypeError, KeyError, ValueError):
                    continue
    except OSError:
        return []

    results.sort(key=lambda a: a.timestamp, reverse=True)
    return results
