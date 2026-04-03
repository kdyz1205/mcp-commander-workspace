---
name: gene_splicer
description: Auto-detect complex needs, merge existing skills into new hybrid skills, hot-reload into registry
triggers: Task requires capabilities spanning multiple existing skills
category: evolution
metadata: {"openclaw": {"always": false}}
---

## Purpose

When a task demands capabilities that span two or more existing skills, **gene_splicer**
automatically identifies the best candidates, merges their logic into a new hybrid skill,
and registers it for immediate use — no manual authoring required.

## Capabilities

1. **Registry scan** — enumerate every skill in the workspace with its name, description,
   and file inventory.
2. **Gap analysis** — given a task description and the current skill set, determine what
   composite capability is missing.
3. **Template-based splicing** — read the bodies of two parent skills and merge them into
   a coherent new `SKILL.md` + `runner.py` under `skills/<new_name>/`.
4. **Auto-splice** — end-to-end: analyse a task, pick the two best-matching parents,
   splice, register, return the new skill name.

## Constraints

- Merging is **template-based** (deterministic string composition), not LLM-dependent.
- The new skill is always written to disk before being returned so it can be loaded
  immediately.
- Parent skills are never modified.

## Runner

```
py skills/gene_splicer/runner.py --task "describe the task here"
py skills/gene_splicer/runner.py --splice skill_a skill_b new_name --instruction "merge guidance"
```
