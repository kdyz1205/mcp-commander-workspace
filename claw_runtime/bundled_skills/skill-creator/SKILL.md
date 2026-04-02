---
name: skill-creator
description: Create or update AgentSkills-style SKILL.md under skills/. Use when the user wants a new workflow, meta-skill, or reusable agent procedure.
---

1. Ask for 1–2 example prompts and whether the skill is **workspace-only** (`skills/`) or personal (`~/.agents/skills/` on this machine).
2. Pick `name` (lowercase, hyphens). Create `skills/<name>/SKILL.md` (workspace) with frontmatter `name`, `description`, optional single-line `metadata` JSON for gates.
3. Body: imperative steps for another agent; reference tools: `execute_terminal`, `edit_local_file`, `load_skill`, `web_fetch`, `append_typed_memory`.
4. Read back the file to verify.
5. Next loop hot-reloads skills — no restart required in DevClaw.
