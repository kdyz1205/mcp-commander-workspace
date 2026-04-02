---
name: git-commit
description: Create conventional commits after reviewing git status and diff. Use when the user asks to commit, save snapshot, or land changes.
metadata: {"openclaw":{"requires":{"bins":["git"]}}}
---

1. Run `git status` and `git diff` (or `git diff --staged` if files are staged).
2. Infer scope and pick type: feat, fix, docs, chore, refactor, test, etc.
3. Stage with `git add` only the intended paths.
4. `git commit -m "type(scope): summary"` with an imperative subject ≤72 chars.
5. Show `git log --oneline -1`.

If not a git repo, report that and stop.
