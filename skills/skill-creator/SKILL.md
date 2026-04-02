---
name: skill-creator
description: （工作区覆盖）创建/升级 SKILL.md 技能包；与 OpenClaw 的 meta-skill 同源思路，适配本仓库 DevClaw 工具名。
metadata: {"openclaw":{"always":true}}
---

## 何时触发

用户说：做个技能、写个 workflow、把某流程固化成 skill、OpenClaw 式技能 等。

## 步骤

1. **澄清**：要解决的 1–2 个具体用户原话示例；默认写到本仓库 `./skills/<name>/`（优先级最高，会覆盖捆绑包同名技能）。
2. **命名**：`name` 小写+连字符；目录名与 frontmatter `name` 一致。
3. **写 SKILL.md**：
   - 头部必须含：`name`、`description`（description 决定模型何时选用；要写清 **WHEN**）。
   - 可选一行 JSON：`metadata: {"openclaw":{"requires":{"bins":["git"]},"os":["win32","linux","darwin"]}}`（按需要）。
   - 正文用祈使句，写给**另一个 AI**执行；明确调用 `execute_terminal` / `edit_local_file` / `load_skill` / `web_fetch` / `append_typed_memory`。
4. **验证**：`edit_local_file` `mode=r` 读回；必要时跑一次最小终端命令证明路径存在。
5. **沉淀**：在 `skills.md` / `skills/skills.json` 增加一条「人类/机器可读」登记（若该技能还要给 Cursor 规则外的人类看）。

## 超越迷你教程的点

- 本运行时 **每个工具循环** 重扫技能目录（热重载），新建技能下一轮即可 `load_skill`。
- 工作区 `./skills` 永远赢过捆绑包与 `~/.openclaw/skills`（与 OpenClaw 文档一致的最高优先级思想）。
