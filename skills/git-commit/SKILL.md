---
name: git-commit
description: （工作区覆盖）中文/英文提交说明均可；按约定式提交生成 message 并执行 git。用户说要提交、commit、保存版本时触发。
metadata: {"openclaw":{"requires":{"bins":["git"]}}}
---

1. `git status -sb`，必要时 `git diff` 与 `git diff --staged`。
2. 用约定式提交：`type(scope): subject`，subject 用祈使句，≤72 字符。
3. 只 `git add` 相关路径，避免误加密钥。
4. `git commit -m "..."`，输出 `git log --oneline -1`。
5. 若 pre-commit 失败，读 stderr，修复或建议用户处理后再试。
