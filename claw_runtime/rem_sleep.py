"""
REM sleep: episodic memory consolidation.

Simulates human REM sleep — compress 24 h of raw logs into distilled
rules and prune stale data.  The consolidated rules can be injected
into the system prompt to give the agent long-term memory.

Lifecycle:  collect -> compress -> consolidate -> prune
"""

from __future__ import annotations

import json
import os
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

    return {
        "episode_count": len(episodes),
        "rule_count": len(rules),
        "consolidated_path": str(consolidated_path),
        "prune_summary": prune_summary,
        "rules": rules,
    }


# ---------------------------------------------------------------------------
# 6. Load consolidated rules (for system prompt injection)
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
