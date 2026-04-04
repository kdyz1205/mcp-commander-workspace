"""
Three-Layer External Memory System.

Layer 1: Handover Note — task context survives restart
Layer 2: Code as Truth — git diff + traceback as ground truth
Layer 3: Rule Embeddings — lessons in .cursorrules

Enables "1-second memory download" on cold restart.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any


# ═══════════════════════════════════════
# Layer 1: Handover Note
# ═══════════════════════════════════════

def _state_path(workspace: Path) -> Path:
    ws = Path(workspace).resolve()
    (ws / ".claw").mkdir(parents=True, exist_ok=True)
    return ws / "CURRENT_EVOLUTION_STATE.json"


def save_handover(
    workspace: str | Path,
    task_id: str,
    step: str,
    memory: str,
) -> None:
    """Save handover note for the next restart."""
    path = _state_path(Path(workspace))
    state = {
        "status": "WORKING",
        "task_id": task_id,
        "step": step,
        "memory": memory[:2000],
        "last_memory": memory[:2000],
        "timestamp": time.time(),
    }
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def load_handover(workspace: str | Path) -> dict[str, Any]:
    """Load handover note from disk."""
    path = _state_path(Path(workspace))
    if not path.is_file():
        return {"status": "IDLE", "task_id": None, "step": None, "memory": "", "last_memory": ""}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"status": "IDLE"}
    except (OSError, json.JSONDecodeError):
        return {"status": "IDLE"}


def clear_handover(workspace: str | Path) -> None:
    """Clear handover after task completion, but preserve last_memory."""
    path = _state_path(Path(workspace))
    old = load_handover(workspace)
    state = {
        "status": "IDLE",
        "task_id": None,
        "step": None,
        "memory": "",
        "last_memory": old.get("memory", old.get("last_memory", "")),
        "timestamp": time.time(),
    }
    path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


# ═══════════════════════════════════════
# Layer 2: Code as Truth
# ═══════════════════════════════════════

def get_physical_context(workspace: str | Path) -> dict[str, str]:
    """Get real ground truth from the filesystem: git diff + recent commits."""
    ws = str(Path(workspace).resolve())
    ctx: dict[str, str] = {"git_diff": "", "recent_commits": "", "modified_files": ""}

    try:
        r = subprocess.run(
            ["git", "diff", "--stat", "HEAD~1"],
            capture_output=True, text=True, timeout=10,
            cwd=ws, encoding="utf-8", errors="replace",
        )
        ctx["git_diff"] = (r.stdout or "").strip()[:1000]
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            capture_output=True, text=True, timeout=10,
            cwd=ws, encoding="utf-8", errors="replace",
        )
        ctx["recent_commits"] = (r.stdout or "").strip()[:500]
    except Exception:
        pass

    try:
        r = subprocess.run(
            ["git", "status", "-s"],
            capture_output=True, text=True, timeout=10,
            cwd=ws, encoding="utf-8", errors="replace",
        )
        ctx["modified_files"] = (r.stdout or "").strip()[:500]
    except Exception:
        pass

    return ctx


def extract_error_context(traceback_text: str, workspace: str | Path) -> dict[str, Any]:
    """Parse a Python traceback to extract file, line, and error type."""
    result: dict[str, Any] = {
        "error_type": "Unknown",
        "file": "",
        "line": 0,
        "message": "",
        "code_slice": "",
    }

    # Extract error type (last line)
    lines = traceback_text.strip().split("\n")
    if lines:
        last = lines[-1].strip()
        match = re.match(r"(\w+Error|\w+Exception):\s*(.*)", last)
        if match:
            result["error_type"] = match.group(1)
            result["message"] = match.group(2)[:200]

    # Extract file and line
    file_matches = re.findall(r'File "([^"]+)", line (\d+)', traceback_text)
    if file_matches:
        # Take the last (most specific) file reference
        filepath, linenum = file_matches[-1]
        result["file"] = filepath
        result["line"] = int(linenum)

        # Try to read surrounding code
        ws = Path(workspace).resolve()
        target = ws / filepath if not os.path.isabs(filepath) else Path(filepath)
        if target.is_file():
            try:
                all_lines = target.read_text(encoding="utf-8").split("\n")
                start = max(0, result["line"] - 5)
                end = min(len(all_lines), result["line"] + 5)
                result["code_slice"] = "\n".join(
                    f"{i+1}: {all_lines[i]}" for i in range(start, end)
                )
            except OSError:
                pass

    return result


# ═══════════════════════════════════════
# Layer 3: Rule Embeddings
# ═══════════════════════════════════════

def _cursorrules_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".cursorrules"


def add_learned_rule(workspace: str | Path, rule: str) -> bool:
    """Append a rule to .cursorrules, skipping duplicates."""
    path = _cursorrules_path(Path(workspace))
    try:
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    except OSError:
        existing = ""

    # Skip if rule already exists (fuzzy: check if core content matches)
    rule_core = rule.split(":", 1)[-1].strip()[:50] if ":" in rule else rule[:50]
    if rule_core in existing:
        return False

    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"\n- {rule}")
        return True
    except OSError:
        return False


def load_learned_rules(workspace: str | Path) -> list[str]:
    """Load all RULE_* entries from .cursorrules."""
    path = _cursorrules_path(Path(workspace))
    if not path.is_file():
        return []
    try:
        content = path.read_text(encoding="utf-8")
        return [
            line.strip().lstrip("- ")
            for line in content.split("\n")
            if "RULE_" in line
        ]
    except OSError:
        return []


# ═══════════════════════════════════════
# Combined: Build Restart Prompt
# ═══════════════════════════════════════

def build_restart_prompt(
    workspace: str | Path,
    error_traceback: str = "",
) -> str:
    """
    Build a dense restart prompt from all 3 memory layers.

    This is what gets injected into the LLM on every cold start.
    Must be < 3000 chars to leave room for actual work.
    """
    ws = Path(workspace).resolve()
    parts: list[str] = []

    # Layer 1: Handover
    handover = load_handover(ws)
    if handover.get("task_id"):
        parts.append(
            f"[交接记忆] 任务:{handover['task_id']} | "
            f"步骤:{handover.get('step','')} | "
            f"上文:{handover.get('memory','')[:500]}"
        )
    elif handover.get("last_memory"):
        parts.append(f"[上轮记忆] {handover['last_memory'][:300]}")

    # Layer 2: Physical truth
    if error_traceback:
        err_ctx = extract_error_context(error_traceback, ws)
        parts.append(
            f"[报错] {err_ctx['error_type']}: {err_ctx['message']} "
            f"at {err_ctx['file']}:{err_ctx['line']}"
        )
        if err_ctx["code_slice"]:
            parts.append(f"[代码片段]\n{err_ctx['code_slice'][:300]}")
    else:
        phys = get_physical_context(ws)
        if phys.get("recent_commits"):
            parts.append(f"[最近提交]\n{phys['recent_commits'][:300]}")

    # Layer 3: Rules
    rules = load_learned_rules(ws)
    if rules:
        parts.append(f"[血泪教训 ({len(rules)}条)] " + " | ".join(r[:80] for r in rules[-5:]))

    prompt = "\n".join(parts)
    return prompt[:2800]  # Hard cap
