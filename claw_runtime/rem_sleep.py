"""
REM sleep: episodic memory consolidation + meta-prompt optimization.

Simulates human REM sleep — compress 24 h of raw logs into distilled
rules and prune stale data.  The consolidated rules can be injected
into the system prompt to give the agent long-term memory.

Phase 5 adds "Soul Optimization": the agent analyses its own
performance metrics and writes self-improvement rules into
.cursorrules so that the next session benefits from past mistakes.

Lifecycle:  collect -> compress -> consolidate -> prune -> meta-optimize
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_jsonl(path: Path, *, max_lines: int = 500) -> list[dict[str, Any]]:
    """Read the last *max_lines* from a JSONL file."""
    if not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in lines[-max_lines:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                out.append(obj)
        except json.JSONDecodeError:
            continue
    return out


def _file_age_days(path: Path) -> float:
    """Return file age in days, or 0 if unreadable."""
    try:
        mtime = path.stat().st_mtime
        return (time.time() - mtime) / 86400.0
    except OSError:
        return 0.0


def _safe_ts(entry: dict[str, Any]) -> str:
    """Extract a normalised timestamp string from a log entry."""
    for key in ("ts", "timestamp", "time", "created"):
        val = entry.get(key)
        if val is not None:
            return str(val)
    return ""


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a boolean from an environment variable."""
    v = os.environ.get(name, "").strip().lower()
    if not v:
        return default
    return v in {"1", "true", "yes", "on"}


def _ts_within_hours(ts_str: str, hours: float) -> bool:
    """Return True if *ts_str* is within the last *hours* hours."""
    try:
        if ts_str.replace(".", "", 1).replace("-", "").isdigit():
            dt = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        return dt.timestamp() >= cutoff
    except (ValueError, TypeError, OSError):
        return True  # if we can't parse, assume recent


def _hour_bucket(ts_str: str) -> str:
    """Extract hour-of-day bucket from an ISO or epoch timestamp."""
    try:
        if ts_str.replace(".", "", 1).replace("-", "").isdigit():
            dt = datetime.fromtimestamp(float(ts_str), tz=timezone.utc)
        else:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return f"{dt.hour:02d}:00"
    except (ValueError, TypeError, OSError):
        return "unknown"


# ---------------------------------------------------------------------------
# 1. Collect daily episodes
# ---------------------------------------------------------------------------

def collect_daily_episodes(workspace: str | Path) -> list[dict[str, Any]]:
    """
    Gather episodes from the last 24 h of log files.

    Sources:
        .claw/sessions/*.jsonl          – tool call logs
        .claw/evolution_failures.jsonl  – error patterns
        .claw/meta_tick_log.jsonl       – survival decisions
        .claw/trade_history.jsonl       – trading outcomes
        task_plan.md                    – reasoning episodes (lightweight excerpt)
    """
    ws = Path(workspace).resolve()
    claw = ws / ".claw"
    episodes: list[dict[str, Any]] = []

    # --- session tool-call logs ---
    sessions_dir = claw / "sessions"
    if sessions_dir.is_dir():
        for jsonl in sorted(sessions_dir.glob("*.jsonl")):
            if _file_age_days(jsonl) > 1.5:
                continue
            for entry in _read_jsonl(jsonl, max_lines=300):
                entry["_source"] = "session"
                entry["_file"] = jsonl.name
                episodes.append(entry)

    # --- evolution failures ---
    evo_path = claw / "evolution_failures.jsonl"
    for entry in _read_jsonl(evo_path, max_lines=200):
        entry["_source"] = "evolution_failure"
        episodes.append(entry)

    # --- meta tick log ---
    meta_path = claw / "meta_tick_log.jsonl"
    for entry in _read_jsonl(meta_path, max_lines=200):
        entry["_source"] = "meta_tick"
        episodes.append(entry)

    # --- trade history ---
    trade_path = claw / "trade_history.jsonl"
    for entry in _read_jsonl(trade_path, max_lines=200):
        entry["_source"] = "trade"
        episodes.append(entry)

    # --- task_plan.md (reasoning episodes — lightweight excerpt) ---
    plan = ws / "task_plan.md"
    if plan.is_file():
        try:
            text = plan.read_text(encoding="utf-8", errors="replace").strip()
            if text:
                episodes.append({
                    "_source": "task_plan",
                    "content": text[:4000],
                    "ts": datetime.now(timezone.utc).isoformat(),
                })
        except OSError:
            pass

    return episodes


# ---------------------------------------------------------------------------
# 2. Compress episodes into rules
# ---------------------------------------------------------------------------

def compress_episodes(
    episodes: list[dict[str, Any]],
    workspace: str | Path,
) -> list[dict[str, Any]]:
    """
    Heuristic compression of raw episodes into distilled rules.

    Grouping strategies:
        - By error type   -> extract error-avoidance pattern
        - By time of day  -> extract timing rules
        - By tool usage   -> extract tool preference rules
        - Deduplicate, keep only novel insights
    """
    ws = Path(workspace).resolve()
    rules: list[dict[str, Any]] = []

    # ---- group by error kind ----
    error_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ep in episodes:
        kind = ep.get("kind") or ep.get("error") or ""
        if kind:
            error_buckets[str(kind)[:120]].append(ep)

    for kind, entries in error_buckets.items():
        if len(entries) < 2:
            continue  # need recurrence to form a rule
        sample_details = [str(e.get("detail", ""))[:200] for e in entries[:5]]
        rules.append({
            "rule": f"Recurring error '{kind}' seen {len(entries)} times — "
                    f"avoid conditions that trigger it.",
            "confidence": min(0.5 + 0.1 * len(entries), 0.95),
            "source": "error_pattern",
            "evidence_count": len(entries),
            "sample": sample_details[:3],
        })

    # ---- group by time-of-day ----
    hour_buckets: dict[str, int] = defaultdict(int)
    hour_errors: dict[str, int] = defaultdict(int)
    for ep in episodes:
        ts = _safe_ts(ep)
        hour = _hour_bucket(ts)
        hour_buckets[hour] += 1
        if ep.get("_source") == "evolution_failure":
            hour_errors[hour] += 1

    for hour, err_count in hour_errors.items():
        total = hour_buckets.get(hour, 1)
        if err_count >= 3 and (err_count / total) > 0.3:
            rules.append({
                "rule": f"High failure rate at {hour} UTC — "
                        f"{err_count}/{total} events were errors. "
                        "Consider deferring risky operations.",
                "confidence": min(0.4 + 0.1 * err_count, 0.9),
                "source": "timing_pattern",
                "evidence_count": err_count,
            })

    # ---- group by tool usage ----
    tool_success: dict[str, int] = defaultdict(int)
    tool_total: dict[str, int] = defaultdict(int)
    for ep in episodes:
        tool = ep.get("tool")
        if not tool:
            continue
        tool_total[tool] += 1
        # Heuristic: if result_preview doesn't contain "error" / "traceback", count as success
        preview = str(ep.get("result_preview", "")).lower()
        if "error" not in preview and "traceback" not in preview:
            tool_success[tool] += 1

    for tool, total in tool_total.items():
        if total < 3:
            continue
        success_rate = tool_success.get(tool, 0) / total
        if success_rate < 0.5:
            rules.append({
                "rule": f"Tool '{tool}' has low success rate ({success_rate:.0%} of "
                        f"{total} calls). Prefer alternatives or fix usage pattern.",
                "confidence": min(0.5 + 0.05 * total, 0.85),
                "source": "tool_preference",
                "evidence_count": total,
            })
        elif success_rate > 0.9 and total >= 5:
            rules.append({
                "rule": f"Tool '{tool}' is highly reliable ({success_rate:.0%} of "
                        f"{total} calls). Prefer it for similar tasks.",
                "confidence": min(0.6 + 0.05 * total, 0.95),
                "source": "tool_preference",
                "evidence_count": total,
            })

    # ---- trade outcome rules ----
    wins = 0
    losses = 0
    for ep in episodes:
        if ep.get("_source") != "trade":
            continue
        pnl = ep.get("pnl", ep.get("profit", 0))
        try:
            pnl = float(pnl)
        except (TypeError, ValueError):
            continue
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1

    total_trades = wins + losses
    if total_trades >= 3:
        win_rate = wins / total_trades
        rules.append({
            "rule": f"Recent trading win rate: {win_rate:.0%} ({wins}W/{losses}L). "
                    + ("Strategy is working — continue." if win_rate > 0.5
                       else "Strategy is underperforming — review or pause."),
            "confidence": min(0.5 + 0.05 * total_trades, 0.9),
            "source": "trade_outcome",
            "evidence_count": total_trades,
        })

    # ---- deduplicate by rule text (keep highest confidence) ----
    seen: dict[str, dict[str, Any]] = {}
    for r in rules:
        key = r["rule"][:80]
        if key not in seen or r["confidence"] > seen[key]["confidence"]:
            seen[key] = r
    rules = list(seen.values())

    return rules


# ---------------------------------------------------------------------------
# 3. Consolidate to long-term memory
# ---------------------------------------------------------------------------

def consolidate_to_long_term(
    workspace: str | Path,
    rules: list[dict[str, Any]],
) -> Path:
    """
    Write compressed rules to .claw/memory/consolidated_rules.json.

    Merges with existing rules (keeps higher-confidence duplicates).
    Returns the path written.
    """
    ws = Path(workspace).resolve()
    out_path = ws / ".claw" / "memory" / "consolidated_rules.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    existing: list[dict[str, Any]] = []
    if out_path.is_file():
        try:
            existing = json.loads(out_path.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        except (json.JSONDecodeError, OSError):
            existing = []

    # Merge: key by rule text prefix, keep higher confidence
    merged: dict[str, dict[str, Any]] = {}
    for r in existing + rules:
        key = str(r.get("rule", ""))[:80]
        if key not in merged or r.get("confidence", 0) > merged[key].get("confidence", 0):
            merged[key] = r

    # Cap total rules to avoid unbounded growth
    max_rules = 200
    consolidated = sorted(merged.values(), key=lambda x: x.get("confidence", 0), reverse=True)
    consolidated = consolidated[:max_rules]

    # Stamp consolidation time
    for r in consolidated:
        r["last_consolidated"] = datetime.now(timezone.utc).isoformat()

    out_path.write_text(
        json.dumps(consolidated, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------------------
# 4. Prune old logs
# ---------------------------------------------------------------------------

def prune_old_logs(
    workspace: str | Path,
    keep_days: int = 3,
) -> dict[str, Any]:
    """
    Remove raw log files older than *keep_days* days.

    Consolidated rules are never pruned — only raw JSONL session logs.
    Returns summary: {pruned_files: list, kept_files: int, errors: list}
    """
    ws = Path(workspace).resolve()
    claw = ws / ".claw"
    pruned: list[str] = []
    kept = 0
    errors: list[str] = []

    # Prune session logs
    sessions_dir = claw / "sessions"
    if sessions_dir.is_dir():
        for f in sessions_dir.glob("*.jsonl"):
            if _file_age_days(f) > keep_days:
                try:
                    f.unlink()
                    pruned.append(str(f.relative_to(ws)))
                except OSError as exc:
                    errors.append(f"{f.name}: {exc}")
            else:
                kept += 1

    # Prune meta_tick_log if too old (but keep last 24 h)
    for name in ("meta_tick_log.jsonl",):
        p = claw / name
        if p.is_file() and _file_age_days(p) > keep_days:
            try:
                p.unlink()
                pruned.append(str(p.relative_to(ws)))
            except OSError as exc:
                errors.append(f"{name}: {exc}")

    return {"pruned_files": pruned, "kept_files": kept, "errors": errors}


# ---------------------------------------------------------------------------
# 5. Full REM sleep cycle
# ---------------------------------------------------------------------------

def run_rem_sleep(
    workspace: str | Path,
    emit: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Full sleep cycle: Collect -> Compress -> Consolidate -> Prune.

    Args:
        workspace: project root.
        emit: optional callback for progress messages (e.g. send to TG).

    Returns:
        Summary dict with episode_count, rule_count, consolidated_path,
        prune_summary, and the rules themselves.
    """
    ws = Path(workspace).resolve()

    def _emit(msg: str) -> None:
        if emit:
            try:
                emit(msg)
            except Exception:
                pass

    _emit("REM sleep: collecting daily episodes...")
    episodes = collect_daily_episodes(ws)
    _emit(f"REM sleep: collected {len(episodes)} episodes.")

    _emit("REM sleep: compressing episodes into rules...")
    rules = compress_episodes(episodes, ws)
    _emit(f"REM sleep: distilled {len(rules)} rules.")

    _emit("REM sleep: consolidating to long-term memory...")
    consolidated_path = consolidate_to_long_term(ws, rules)
    _emit(f"REM sleep: written to {consolidated_path}")

    _emit("REM sleep: pruning stale logs...")
    prune_summary = prune_old_logs(ws)
    _emit(
        f"REM sleep: pruned {len(prune_summary['pruned_files'])} files, "
        f"kept {prune_summary['kept_files']}."
    )

    # --- Phase 5: Meta-prompt optimization (Soul Optimization) ---
    meta_summary: dict[str, Any] | None = None
    if _env_bool("META_PROMPT_OPTIMIZATION", default=True):
        _emit("REM sleep: running meta-prompt optimization...")
        try:
            meta_summary = run_meta_optimization(ws, emit=emit)
            _emit(
                f"REM sleep: meta-optimization added "
                f"{meta_summary.get('rules_added', 0)} rules to .cursorrules."
            )
        except Exception as exc:
            _emit(f"REM sleep: meta-optimization failed — {exc}")
            meta_summary = {"error": str(exc)}

    return {
        "episode_count": len(episodes),
        "rule_count": len(rules),
        "consolidated_path": str(consolidated_path),
        "prune_summary": prune_summary,
        "rules": rules,
        "meta_optimization": meta_summary,
    }


# ---------------------------------------------------------------------------
# 6. Meta-Prompt Optimization ("Soul Optimization")
# ---------------------------------------------------------------------------

_AUTO_EVOLVED_SECTION = "# === DevClaw Auto-Evolved Rules ==="


def collect_performance_metrics(
    workspace: Path,
    hours: float = 168,
) -> dict[str, Any]:
    """
    Collect performance metrics from the last *hours* (default 168 = 1 week).

    Sources:
        .claw/action_outcomes.jsonl      – per-task success/failure + tokens
        .claw/error_attributions.jsonl   – detailed failure analysis
    """
    ws = Path(workspace).resolve()
    claw = ws / ".claw"

    outcomes = _read_jsonl(claw / "action_outcomes.jsonl", max_lines=2000)
    errors = _read_jsonl(claw / "error_attributions.jsonl", max_lines=2000)

    # Filter to time window
    outcomes = [
        o for o in outcomes
        if _ts_within_hours(_safe_ts(o), hours)
    ]
    errors = [
        e for e in errors
        if _ts_within_hours(_safe_ts(e), hours)
    ]

    # --- task_success_rate ---
    total_tasks = len(outcomes)
    success_count = sum(
        1 for o in outcomes
        if str(o.get("status", "")).lower() in ("success", "ok", "done", "completed")
    )
    task_success_rate = (success_count / total_tasks) if total_tasks else 0.0

    # --- avg_tokens_per_task ---
    token_values = [
        int(o["tokens"]) for o in outcomes
        if "tokens" in o and str(o["tokens"]).isdigit()
    ]
    avg_tokens_per_task = (
        (sum(token_values) / len(token_values)) if token_values else 0.0
    )

    # --- error_rate_by_type ---
    error_rate_by_type: dict[str, int] = defaultdict(int)
    for o in outcomes:
        status = str(o.get("status", "")).lower()
        if status in ("error", "failed", "failure"):
            err_type = str(o.get("error_type", o.get("error", "unknown")))[:120]
            error_rate_by_type[err_type] += 1
    for e in errors:
        err_type = str(e.get("type", e.get("error_type", "unknown")))[:120]
        error_rate_by_type[err_type] += 1

    # --- most_failed_tools ---
    tool_fail: dict[str, int] = defaultdict(int)
    tool_total: dict[str, int] = defaultdict(int)
    for o in outcomes:
        tool = o.get("tool") or o.get("tool_name")
        if not tool:
            continue
        tool_total[str(tool)] += 1
        status = str(o.get("status", "")).lower()
        if status in ("error", "failed", "failure"):
            tool_fail[str(tool)] += 1
    most_failed_tools: list[dict[str, Any]] = sorted(
        [
            {"tool": t, "failures": tool_fail[t], "total": tool_total[t]}
            for t in tool_fail
        ],
        key=lambda x: x["failures"],
        reverse=True,
    )[:10]

    # --- delegation_effectiveness ---
    self_outcomes = [
        o for o in outcomes if str(o.get("executor", "self")).lower() == "self"
    ]
    delegated_outcomes = [
        o for o in outcomes if str(o.get("executor", "self")).lower() != "self"
    ]
    self_success = sum(
        1 for o in self_outcomes
        if str(o.get("status", "")).lower() in ("success", "ok", "done", "completed")
    )
    delegated_success = sum(
        1 for o in delegated_outcomes
        if str(o.get("status", "")).lower() in ("success", "ok", "done", "completed")
    )
    delegation_effectiveness = {
        "self_total": len(self_outcomes),
        "self_success": self_success,
        "self_rate": (self_success / len(self_outcomes)) if self_outcomes else 0.0,
        "delegated_total": len(delegated_outcomes),
        "delegated_success": delegated_success,
        "delegated_rate": (
            (delegated_success / len(delegated_outcomes))
            if delegated_outcomes
            else 0.0
        ),
    }

    # --- error attributions summary ---
    attribution_summary: dict[str, int] = defaultdict(int)
    for e in errors:
        cause = str(e.get("cause", e.get("attribution", "unknown")))[:120]
        attribution_summary[cause] += 1

    return {
        "window_hours": hours,
        "total_tasks": total_tasks,
        "task_success_rate": task_success_rate,
        "avg_tokens_per_task": avg_tokens_per_task,
        "error_rate_by_type": dict(error_rate_by_type),
        "most_failed_tools": most_failed_tools,
        "delegation_effectiveness": delegation_effectiveness,
        "attribution_summary": dict(attribution_summary),
        "total_error_attributions": len(errors),
    }


def generate_optimization_rules(
    metrics: dict[str, Any],
    workspace: Path,
) -> list[str]:
    """
    Generate new .cursorrules rules from performance metrics.

    Tries LLM generation first (Claude CLI or Ollama), falls back to
    heuristic rule generation.
    """
    ws = Path(workspace).resolve()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    prefix = f"# [Auto-evolved {today}]"

    # --- attempt LLM-based generation ---
    llm_rules = _try_llm_rule_generation(metrics, prefix)
    if llm_rules:
        return llm_rules

    # --- fallback: heuristic rule generation ---
    return _heuristic_rule_generation(metrics, prefix)


def _try_llm_rule_generation(
    metrics: dict[str, Any],
    prefix: str,
) -> list[str]:
    """Try generating rules via Claude CLI or Ollama. Returns [] on failure."""
    metrics_summary = json.dumps(metrics, indent=2, ensure_ascii=False, default=str)
    prompt = (
        "You are an AI agent optimizer. Given these performance metrics from the last week, "
        "generate 3-8 one-line actionable rules for a .cursorrules file. "
        "Each rule must be specific, actionable, and address a real issue in the metrics. "
        "Output ONLY the rules, one per line, no numbering, no explanation.\n\n"
        f"Metrics:\n{metrics_summary[:3000]}"
    )

    # Try Claude CLI first
    for cmd in (
        ["claude", "-p", prompt],
        ["ollama", "run", "llama3", prompt],
    ):
        exe = cmd[0]
        if not shutil.which(exe):
            continue
        try:
            cp = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60,
            )
            if cp.returncode == 0 and cp.stdout.strip():
                raw_lines = cp.stdout.strip().splitlines()
                rules: list[str] = []
                for line in raw_lines:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    # Strip any numbering like "1. " or "- "
                    for pat in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "- ", "* "):
                        if line.startswith(pat):
                            line = line[len(pat):].strip()
                    if len(line) > 10:
                        rules.append(f"{prefix} {line}")
                if rules:
                    return rules[:8]
        except (subprocess.TimeoutExpired, OSError, FileNotFoundError):
            continue

    return []


def _heuristic_rule_generation(
    metrics: dict[str, Any],
    prefix: str,
) -> list[str]:
    """Generate rules from metrics using pattern-matching heuristics."""
    rules: list[str] = []
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # --- Tool failure rules ---
    for tool_info in metrics.get("most_failed_tools", []):
        tool = tool_info["tool"]
        failures = tool_info["failures"]
        total = tool_info["total"]
        if total >= 3 and failures / total > 0.4:
            rules.append(
                f"{prefix} Before using {tool}, always verify preconditions — "
                f"it failed {failures}/{total} times last week."
            )

    # --- Delegation rules ---
    deleg = metrics.get("delegation_effectiveness", {})
    self_rate = deleg.get("self_rate", 0)
    deleg_rate = deleg.get("delegated_rate", 0)
    deleg_total = deleg.get("delegated_total", 0)
    if deleg_total >= 3 and deleg_rate > self_rate + 0.15:
        rules.append(
            f"{prefix} For complex tasks, prefer delegating to sub-agents — "
            f"delegated success rate ({deleg_rate:.0%}) exceeds self ({self_rate:.0%})."
        )
    elif deleg_total >= 3 and self_rate > deleg_rate + 0.15:
        rules.append(
            f"{prefix} Prefer handling tasks directly — "
            f"self success rate ({self_rate:.0%}) exceeds delegated ({deleg_rate:.0%})."
        )

    # --- Error-type clustering rules ---
    error_types = metrics.get("error_rate_by_type", {})
    for err_type, count in sorted(error_types.items(), key=lambda x: x[1], reverse=True)[:3]:
        if count >= 3:
            rules.append(
                f"{prefix} Watch for '{err_type}' errors (occurred {count}x last week) — "
                f"add defensive checks before operations that trigger this."
            )

    # --- Token usage rule ---
    avg_tokens = metrics.get("avg_tokens_per_task", 0)
    if avg_tokens > 5000:
        rules.append(
            f"{prefix} Average token usage is high ({avg_tokens:.0f}/task) — "
            f"for routine tasks, try simpler approaches first before deep analysis."
        )

    # --- Low success rate rule ---
    success_rate = metrics.get("task_success_rate", 1.0)
    if metrics.get("total_tasks", 0) >= 5 and success_rate < 0.6:
        rules.append(
            f"{prefix} Overall task success rate is low ({success_rate:.0%}) — "
            f"pause and validate approach before executing multi-step plans."
        )

    # --- Attribution-based rules ---
    for cause, count in sorted(
        metrics.get("attribution_summary", {}).items(),
        key=lambda x: x[1],
        reverse=True,
    )[:2]:
        if count >= 2:
            rules.append(
                f"{prefix} Failure cause '{cause}' attributed {count}x — "
                f"add pre-check or fallback path for this failure mode."
            )

    return rules[:8]


def append_to_cursorrules(
    workspace: Path,
    rules: list[str],
) -> int:
    """
    Append auto-evolved rules to .cursorrules.

    - Reads current .cursorrules
    - Skips rules that are >80% similar to any existing line (fuzzy dedup)
    - Appends under ``# === DevClaw Auto-Evolved Rules ===`` section
    - NEVER modifies existing rules — only appends
    - Returns count of rules actually added
    """
    ws = Path(workspace).resolve()
    cr_path = ws / ".cursorrules"

    # Read existing content
    existing_text = ""
    if cr_path.is_file():
        try:
            existing_text = cr_path.read_text(encoding="utf-8")
        except OSError:
            existing_text = ""

    existing_lines = [
        line.strip() for line in existing_text.splitlines() if line.strip()
    ]

    # Fuzzy dedup: skip if >80% similar to any existing line
    added: list[str] = []
    for rule in rules:
        rule_stripped = rule.strip()
        if not rule_stripped:
            continue
        is_dup = False
        for existing in existing_lines:
            ratio = difflib.SequenceMatcher(
                None, rule_stripped.lower(), existing.lower()
            ).ratio()
            if ratio > 0.80:
                is_dup = True
                break
        if not is_dup:
            added.append(rule_stripped)
            existing_lines.append(rule_stripped)  # prevent intra-batch dups

    if not added:
        return 0

    # Build the append block
    section_header = _AUTO_EVOLVED_SECTION
    if section_header not in existing_text:
        block = f"\n\n{section_header}\n"
    else:
        block = "\n"

    block += "\n".join(added) + "\n"

    # Append (never overwrite existing content)
    try:
        with cr_path.open("a", encoding="utf-8") as f:
            f.write(block)
    except OSError:
        return 0

    return len(added)


def run_meta_optimization(
    workspace: Path,
    emit: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Full meta-prompt optimization cycle:
    1. Collect performance metrics (last 7 days)
    2. Generate optimization rules
    3. Append to .cursorrules
    4. Log what was added
    5. Return summary
    """
    ws = Path(workspace).resolve()

    def _emit(msg: str) -> None:
        if emit:
            try:
                emit(msg)
            except Exception:
                pass

    _emit("Meta-optimization: collecting performance metrics (7d)...")
    metrics = collect_performance_metrics(ws, hours=168)
    _emit(
        f"Meta-optimization: {metrics['total_tasks']} tasks, "
        f"{metrics['task_success_rate']:.0%} success rate."
    )

    _emit("Meta-optimization: generating optimization rules...")
    rules = generate_optimization_rules(metrics, ws)
    _emit(f"Meta-optimization: generated {len(rules)} candidate rules.")

    _emit("Meta-optimization: appending to .cursorrules...")
    added_count = append_to_cursorrules(ws, rules)
    _emit(f"Meta-optimization: added {added_count} new rules to .cursorrules.")

    # Persist timestamp for debouncing
    ts_path = ws / ".claw" / "meta_optimization_last.json"
    ts_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        ts_path.write_text(
            json.dumps({
                "last_run": datetime.now(timezone.utc).isoformat(),
                "last_run_epoch": time.time(),
                "rules_generated": len(rules),
                "rules_added": added_count,
                "metrics_snapshot": {
                    "total_tasks": metrics.get("total_tasks", 0),
                    "task_success_rate": metrics.get("task_success_rate", 0),
                    "total_error_attributions": metrics.get("total_error_attributions", 0),
                },
            }, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass

    # Log to memory if available
    try:
        from claw_runtime.memory import append_memory
        append_memory(
            ws, "lesson",
            f"[meta_optimization] Added {added_count} auto-evolved rules to .cursorrules "
            f"(metrics: {metrics['total_tasks']} tasks, {metrics['task_success_rate']:.0%} success).",
        )
    except Exception:
        pass

    summary: dict[str, Any] = {
        "metrics": metrics,
        "rules_generated": len(rules),
        "rules_added": added_count,
        "rules": rules,
    }
    return summary


# ---------------------------------------------------------------------------
# 7. Load consolidated rules (for system prompt injection)
# ---------------------------------------------------------------------------

def load_consolidated_rules(workspace: str | Path) -> list[dict[str, Any]]:
    """
    Read back consolidated rules for injection into the system prompt.

    Returns an empty list if no rules have been consolidated yet.
    """
    ws = Path(workspace).resolve()
    path = ws / ".claw" / "memory" / "consolidated_rules.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return []
