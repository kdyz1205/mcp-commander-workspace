---
name: logic_healer
description: When code errors occur, automatically analyze → research → fix → sandbox test → hot-swap. Scientific trial-and-error closed loop.
triggers: Exception in dev_claw_run, test failure, consecutive_failures >= 2
category: evolution
---

## Purpose

**sk_logic_healer** is an automated scientific error recovery loop for DevClaw.
When runtime exceptions, test failures, or repeated consecutive failures are
detected, this skill orchestrates a closed-loop cycle:

1. **Analyze** — classify the error, form a root-cause hypothesis, identify affected files.
2. **Research** — query public sources (GitHub issues, Stack Overflow patterns, arXiv if academic) via `research_lab.py`.
3. **Generate fix candidates** — produce one or more concrete code patches ranked by confidence.
4. **Sandbox test** — apply each candidate to a temporary copy and run pytest / syntax + import checks.
5. **Hot-swap** — apply the first verified fix to the real workspace.
6. **Record** — write a reasoning episode to `task_plan.md`; log to `evolution_failures.jsonl` if all attempts fail.

## When to trigger

- An unhandled exception surfaces during `dev_claw_run`.
- A pytest or test-runner invocation returns non-zero.
- `consecutive_failures >= 2` in the survival state snapshot.

## Safety constraints

- Fixes are always validated in an isolated temp copy before touching real files.
- `safety_scan` is run on every generated patch; CRITICAL findings block application.
- Maximum of `max_attempts` (default 3) fix cycles per healing invocation.
- All mutations are logged with full before/after context for rollback.

## Agent usage

```
from skills.logic_healer.runner import run_healing_loop

result = run_healing_loop(
    workspace="/path/to/workspace",
    error_text="KeyError: 'missing_field'",
    traceback_text="Traceback (most recent call last):\n  ...",
    max_attempts=3,
)
```
