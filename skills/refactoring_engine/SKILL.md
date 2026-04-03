---
name: refactoring_engine
description: Background daemon that builds dependency topology, identifies tech debt hotspots, and incrementally refactors legacy code
triggers: Idle time, explicit invocation, or large codebase detected
category: evolution
metadata: {"openclaw": {"always": false}}
---

## Purpose

Automatically analyse a codebase for structural health, surface the highest-priority
refactoring targets, and apply safe, incremental improvements — reverting any change
that breaks the test suite.

## Capabilities

1. **Dependency graph** — build a file-level import/dependency map for the workspace.
2. **Hotspot detection** — rank files by a composite score of line count, cyclomatic
   complexity proxy, import fan-in/fan-out, and recency of modification.
3. **Suggestion engine** — produce actionable refactoring suggestions (too-long files,
   circular imports, dead code, high coupling).
4. **Safe refactor step** — apply one atomic change, run tests, revert on failure.
5. **Background mode** — execute up to *N* safe steps unattended and return a summary.

## Constraints

- Every refactoring step is **atomic**: one change, one test run, auto-revert on failure.
- The engine never deletes files — it only proposes splits or extractions.
- Test commands are configurable; the default is `python -m pytest`.

## Runner

```
py skills/refactoring_engine/runner.py graph --workspace .
py skills/refactoring_engine/runner.py hotspots --workspace .
py skills/refactoring_engine/runner.py suggest --workspace .
py skills/refactoring_engine/runner.py refactor --workspace . --max-steps 3
```
