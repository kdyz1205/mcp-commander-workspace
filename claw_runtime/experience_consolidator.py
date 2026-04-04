"""
Experience Consolidator — DevClaw's hippocampus.

Turns one-time bug fixes into permanent physical instincts.
Blood must become genes.

Pipeline:
1. extract_rule_from_delta: Compare failed vs success code → structured rule
2. persist_rule: Write to disk (JSONL), deduplicate
3. retrieve_relevant_rules: Keyword-based RAG (no vector DB needed)
4. build_subconscious_prompt: Inject rules into system prompt

Triggered ONLY after TDD engine returns Exit Code 0.
"""
from __future__ import annotations

import json
import os
import re
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


def _rules_path(workspace: Path) -> Path:
    ws = Path(workspace).resolve()
    d = ws / ".claw" / "learned_experience"
    d.mkdir(parents=True, exist_ok=True)
    return d / "rules.jsonl"


# ═══════════════════════════════════════
# Defense 1: Delta Extraction
# ═══════════════════════════════════════

def extract_rule_from_delta(
    failed_code: str,
    traceback_log: str,
    success_code: str,
) -> dict[str, str]:
    """
    Extract a structured rule by comparing failed and successful code.

    Uses heuristic diff analysis — no LLM needed for basic cases.
    Returns {trigger_condition, forbidden_action, enforced_action}.
    """
    # Find what changed between failed and success
    failed_lines = set(failed_code.strip().split("\n"))
    success_lines = set(success_code.strip().split("\n"))

    removed = failed_lines - success_lines
    added = success_lines - failed_lines

    # Extract error type from traceback
    error_type = "Unknown"
    error_msg = ""
    tb_lines = traceback_log.strip().split("\n")
    for line in reversed(tb_lines):
        m = re.match(r"(\w+(?:Error|Exception)):\s*(.*)", line.strip())
        if m:
            error_type = m.group(1)
            error_msg = m.group(2)[:100]
            break

    # Extract imports to understand the domain
    imports_in_code = re.findall(r"import (\w+)", failed_code + success_code)
    domain = ", ".join(set(imports_in_code)) if imports_in_code else "general"

    # Build rule
    removed_str = "; ".join(l.strip() for l in removed if l.strip() and not l.strip().startswith("#"))[:200]
    added_str = "; ".join(l.strip() for l in added if l.strip() and not l.strip().startswith("#"))[:200]

    trigger = f"When using {domain} and encountering {error_type}"
    if error_msg:
        trigger += f" ({error_msg[:50]})"

    forbidden = f"Don't: {removed_str}" if removed_str else f"Don't trigger {error_type}"
    enforced = f"Must: {added_str}" if added_str else f"Handle {error_type} properly"

    return {
        "trigger_condition": trigger,
        "forbidden_action": forbidden,
        "enforced_action": enforced,
        "error_type": error_type,
        "timestamp": time.strftime("%Y-%m-%d %H:%M"),
    }


# ═══════════════════════════════════════
# Defense 2: Physical Gene Splicing
# ═══════════════════════════════════════

def persist_rule(workspace: str | Path, rule: dict[str, str]) -> bool:
    """
    Write rule to disk. Rejects duplicates.

    Rules stored in .claw/learned_experience/rules.jsonl
    """
    ws = Path(workspace).resolve()
    path = _rules_path(ws)

    # Check for duplicates (>70% similarity in trigger_condition)
    existing = load_all_rules(ws)
    new_trigger = rule.get("trigger_condition", "")
    for existing_rule in existing:
        old_trigger = existing_rule.get("trigger_condition", "")
        similarity = SequenceMatcher(None, new_trigger.lower(), old_trigger.lower()).ratio()
        if similarity > 0.7:
            return False  # Duplicate

    # Append to JSONL
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rule, ensure_ascii=False) + "\n")
        return True
    except OSError:
        return False


def load_all_rules(workspace: str | Path) -> list[dict[str, str]]:
    """Load all persisted rules from disk."""
    ws = Path(workspace).resolve()
    path = _rules_path(ws)
    if not path.is_file():
        return []
    rules = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                try:
                    rules.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return rules


# ═══════════════════════════════════════
# Defense 3: Runtime RAG (keyword-based)
# ═══════════════════════════════════════

def retrieve_relevant_rules(
    workspace: str | Path,
    task_description: str,
    top_k: int = 5,
) -> list[dict[str, str]]:
    """
    Retrieve rules most relevant to current task.

    Uses keyword overlap scoring (poor man's RAG — no vector DB needed).
    """
    rules = load_all_rules(workspace)
    if not rules:
        return []

    task_words = set(task_description.lower().split())

    scored: list[tuple[float, dict]] = []
    for rule in rules:
        # Combine all rule fields for matching
        rule_text = " ".join(str(v) for v in rule.values()).lower()
        rule_words = set(rule_text.split())

        # Jaccard-like overlap
        overlap = len(task_words & rule_words)
        if overlap > 0:
            score = overlap / max(len(task_words), 1)
            scored.append((score, rule))

    # Sort by relevance
    scored.sort(key=lambda x: -x[0])
    return [rule for _, rule in scored[:top_k]]


# ═══════════════════════════════════════
# Defense 4: Subconscious Prompt Builder
# ═══════════════════════════════════════

def build_subconscious_prompt(
    workspace: str | Path,
    current_task: str,
    max_rules: int = 5,
    max_chars: int = 2000,
) -> str:
    """
    Build the "subconscious" injection for the system prompt.

    Retrieves relevant rules and formats them as hard constraints.
    """
    relevant = retrieve_relevant_rules(workspace, current_task, top_k=max_rules)
    if not relevant:
        return ""

    lines = ["# 物理潜意识（血泪教训，不可违背）"]
    for i, rule in enumerate(relevant, 1):
        lines.append(
            f"{i}. WHEN: {rule.get('trigger_condition', '?')}\n"
            f"   NEVER: {rule.get('forbidden_action', '?')}\n"
            f"   ALWAYS: {rule.get('enforced_action', '?')}"
        )

    prompt = "\n".join(lines)
    return prompt[:max_chars]


# ═══════════════════════════════════════
# Full cycle (called by TDD engine on success)
# ═══════════════════════════════════════

def consolidate_from_tdd(
    workspace: str | Path,
    failed_code: str,
    traceback_log: str,
    success_code: str,
) -> dict[str, Any]:
    """
    Full consolidation cycle: extract → persist → confirm.

    Called ONLY after TDD engine returns Exit Code 0.
    """
    ws = Path(workspace).resolve()

    rule = extract_rule_from_delta(failed_code, traceback_log, success_code)
    saved = persist_rule(ws, rule)

    return {
        "rule": rule,
        "saved": saved,
        "total_rules": len(load_all_rules(ws)),
    }
