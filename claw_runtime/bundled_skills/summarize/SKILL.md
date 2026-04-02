---
name: summarize
description: Summarize long text, logs, or JSON into bullets with risks and next actions. Use when output is too large to read quickly.
---

1. Identify audience (human vs. another agent) and max bullet count (default 7).
2. Extract: goal, current state, blockers, files touched, commands run, open questions.
3. If input is JSON, prefer schema-level summary (keys, counts) before deep values.
4. End with **Next step** (one concrete command or file to open).
