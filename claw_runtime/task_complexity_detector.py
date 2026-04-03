"""
Task Complexity Detector — DevClaw's "Prefrontal Cortex".

Intercepts incoming instructions and determines whether they are:
- Atomic (safe to execute directly)
- Grand (must be decomposed into sub-tasks before execution)

Uses heuristic keyword analysis + optional LLM scoring.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ComplexityAssessment:
    """Result of analyzing a task's complexity."""
    instruction: str
    score: int                      # 1-10
    level: str                      # "atomic" | "moderate" | "grand"
    should_decompose: bool          # True if score >= threshold
    reasons: list[str] = field(default_factory=list)
    estimated_files: int = 0        # rough file count estimate
    estimated_steps: int = 1        # rough step count
    estimated_tokens: int = 0       # rough token budget estimate
    keywords_matched: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Keyword banks for heuristic scoring
# ---------------------------------------------------------------------------

_GRAND_KEYWORDS_ZH = [
    "重构", "重写", "整个", "所有", "全部", "系统", "架构",
    "从零", "从头", "大改", "全面", "迁移", "升级整个",
    "开发一个", "搭建", "构建", "设计并实现", "屎山",
]

_GRAND_KEYWORDS_EN = [
    "refactor", "rewrite", "entire", "whole", "all files",
    "from scratch", "redesign", "architecture", "migrate",
    "build a", "create a system", "overhaul", "restructure",
    "implement a full", "develop a new", "legacy code",
]

_MODERATE_KEYWORDS_ZH = [
    "修改多个", "跨文件", "集成", "对接", "联调", "多模块",
    "优化", "增加功能", "新增", "扩展",
]

_MODERATE_KEYWORDS_EN = [
    "cross-file", "integration", "multi-module", "optimize",
    "add feature", "extend", "connect", "wire up",
    "multiple files", "several components",
]

_ATOMIC_KEYWORDS_ZH = [
    "修复", "修bug", "改个", "加个注释", "格式化", "lint",
    "单个文件", "这个函数", "这行", "打印",
]

_ATOMIC_KEYWORDS_EN = [
    "fix bug", "typo", "rename", "format", "lint", "single file",
    "this function", "add comment", "log", "print",
]


def _count_keyword_hits(text: str, bank: list[str]) -> list[str]:
    lower = text.lower()
    return [kw for kw in bank if kw in lower]


def _estimate_file_scope(instruction: str, workspace: Path | None = None) -> int:
    """Rough estimate of how many files the task might touch."""
    lower = instruction.lower()

    # Explicit file counts mentioned
    m = re.search(r"(\d+)\s*(?:个文件|files?)", lower)
    if m:
        return int(m.group(1))

    # "Entire project" / "all files" type signals
    if any(kw in lower for kw in ("整个项目", "所有文件", "entire project", "all files", "whole codebase")):
        if workspace and workspace.is_dir():
            try:
                count = sum(1 for f in workspace.rglob("*.py") if ".claw" not in str(f))
                return min(count, 200)
            except OSError:
                pass
        return 50  # conservative default

    # Cross-file signals
    if any(kw in lower for kw in ("跨文件", "多个文件", "cross-file", "multiple files")):
        return 10

    return 1


def assess_complexity(
    instruction: str,
    *,
    workspace: Path | None = None,
    threshold: int | None = None,
) -> ComplexityAssessment:
    """
    Assess task complexity and decide whether decomposition is needed.

    Args:
        instruction: The user's task instruction
        workspace: Optional workspace path for file counting
        threshold: Score threshold for decomposition (default from env or 5)

    Returns:
        ComplexityAssessment with score, level, and decomposition decision
    """
    if threshold is None:
        threshold = int(os.environ.get("DEVCLAW_DECOMPOSE_THRESHOLD", "5") or "5")

    score = 0
    reasons: list[str] = []
    all_keywords: list[str] = []

    # --- Keyword heuristics ---
    grand_zh = _count_keyword_hits(instruction, _GRAND_KEYWORDS_ZH)
    grand_en = _count_keyword_hits(instruction, _GRAND_KEYWORDS_EN)
    moderate_zh = _count_keyword_hits(instruction, _MODERATE_KEYWORDS_ZH)
    moderate_en = _count_keyword_hits(instruction, _MODERATE_KEYWORDS_EN)
    atomic_zh = _count_keyword_hits(instruction, _ATOMIC_KEYWORDS_ZH)
    atomic_en = _count_keyword_hits(instruction, _ATOMIC_KEYWORDS_EN)

    grand_hits = grand_zh + grand_en
    moderate_hits = moderate_zh + moderate_en
    atomic_hits = atomic_zh + atomic_en

    if grand_hits:
        score += min(len(grand_hits) * 2, 6)
        reasons.append(f"Grand keywords: {', '.join(grand_hits[:5])}")
        all_keywords.extend(grand_hits)

    if moderate_hits:
        score += min(len(moderate_hits), 3)
        reasons.append(f"Moderate keywords: {', '.join(moderate_hits[:5])}")
        all_keywords.extend(moderate_hits)

    if atomic_hits:
        score -= min(len(atomic_hits), 2)
        reasons.append(f"Atomic keywords (reduces score): {', '.join(atomic_hits[:3])}")
        all_keywords.extend(atomic_hits)

    # --- Instruction length ---
    char_len = len(instruction)
    if char_len > 500:
        score += 2
        reasons.append(f"Long instruction ({char_len} chars)")
    elif char_len > 200:
        score += 1
        reasons.append(f"Medium instruction ({char_len} chars)")

    # --- File scope ---
    estimated_files = _estimate_file_scope(instruction, workspace)
    if estimated_files > 20:
        score += 3
        reasons.append(f"Large file scope (~{estimated_files} files)")
    elif estimated_files > 5:
        score += 2
        reasons.append(f"Multi-file scope (~{estimated_files} files)")
    elif estimated_files > 2:
        score += 1
        reasons.append(f"Few files (~{estimated_files} files)")

    # --- Clamp ---
    score = max(1, min(10, score))

    # --- Level classification ---
    if score <= 3:
        level = "atomic"
        estimated_steps = 1
    elif score <= 5:
        level = "moderate"
        estimated_steps = max(2, estimated_files // 3)
    else:
        level = "grand"
        estimated_steps = max(3, estimated_files // 2)

    # --- Token estimate (rough) ---
    # ~2000 tokens per file interaction, ~500 per step overhead
    estimated_tokens = estimated_files * 2000 + estimated_steps * 500

    should_decompose = score >= threshold

    return ComplexityAssessment(
        instruction=instruction,
        score=score,
        level=level,
        should_decompose=should_decompose,
        reasons=reasons,
        estimated_files=estimated_files,
        estimated_steps=estimated_steps,
        estimated_tokens=estimated_tokens,
        keywords_matched=all_keywords,
    )


def format_assessment_report(assessment: ComplexityAssessment) -> str:
    """Human-readable report of a complexity assessment."""
    lines = [
        f"## 任务复杂度评估 (Task Complexity Assessment)",
        f"",
        f"**指令**: {assessment.instruction[:200]}{'...' if len(assessment.instruction) > 200 else ''}",
        f"**复杂度得分**: {assessment.score}/10 ({assessment.level})",
        f"**是否需要拆解**: {'✅ 是 — 任务过大，必须拆解' if assessment.should_decompose else '❌ 否 — 可直接执行'}",
        f"**预估文件数**: ~{assessment.estimated_files}",
        f"**预估步骤数**: ~{assessment.estimated_steps}",
        f"**预估Token消耗**: ~{assessment.estimated_tokens:,}",
        f"",
        f"### 判断依据",
    ]
    for r in assessment.reasons:
        lines.append(f"- {r}")
    return "\n".join(lines)
