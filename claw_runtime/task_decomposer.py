"""
Task Decomposer — DevClaw's "Project Manager Brain".

When a grand task is detected, this engine:
1. Calls the LLM to break it into atomic sub-tasks
2. Builds a dependency DAG
3. Writes milestones to task_plan.md
4. Enqueues sub-tasks into SmartTaskRegistry
5. Reports decomposition to the user (via TG hook)

This is the core intelligence that transforms DevClaw from a
"single-step command executor" into a "self-scheduling project manager".
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from claw_runtime.bot_task_queue import SmartTaskRegistry
from claw_runtime.reasoning_episode import append_reasoning_episode
from claw_runtime.task_complexity_detector import (
    ComplexityAssessment,
    assess_complexity,
    format_assessment_report,
)


# ---------------------------------------------------------------------------
# Decomposition prompt template
# ---------------------------------------------------------------------------

_DECOMPOSE_SYSTEM = """\
你是一个顶级项目经理AI。你的任务是将一个宏大模糊的指令拆解为可独立执行的原子任务。

规则：
1. 每个原子任务必须能在 16 次工具调用内完成
2. 每个原子任务只涉及 1-3 个文件
3. 明确标注任务之间的依赖关系（哪些必须先完成）
4. 按执行阶段(phase)分组：阶段1的任务全部完成后才能开始阶段2
5. 给每个任务估算优先级(1=最高, 10=最低)

输出格式 — 严格JSON数组，不要包含任何其他文字：
[
  {
    "instruction": "具体的原子任务描述",
    "phase": 1,
    "priority": 3,
    "depends_on": [],
    "estimated_files": 2,
    "estimated_tokens": 3000
  },
  {
    "instruction": "第二个任务...",
    "phase": 1,
    "priority": 5,
    "depends_on": [0],
    "estimated_files": 1,
    "estimated_tokens": 2000
  }
]

其中 depends_on 是本数组中其他任务的索引(从0开始)。
"""

_DECOMPOSE_USER = """\
请将以下宏大任务拆解为原子任务：

任务指令：{instruction}

工作区信息：
- 项目路径：{workspace}
- 预估涉及文件数：~{estimated_files}
- 复杂度评分：{score}/10

{context}

请输出JSON数组（不要markdown代码块，纯JSON）：
"""


def _try_parse_json_array(text: str) -> list[dict[str, Any]] | None:
    """Try to extract a JSON array from LLM response (tolerant of markdown fences)."""
    text = text.strip()

    # Strip markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines).strip()

    # Try direct parse
    try:
        result = json.loads(text)
        if isinstance(result, list):
            return result
    except json.JSONDecodeError:
        pass

    # Try to find JSON array within text
    import re
    match = re.search(r"\[[\s\S]*\]", text)
    if match:
        try:
            result = json.loads(match.group())
            if isinstance(result, list):
                return result
        except json.JSONDecodeError:
            pass

    return None


def decompose_with_llm(
    instruction: str,
    assessment: ComplexityAssessment,
    workspace: Path,
    *,
    context: str = "",
    progress_hook: Callable[[str], None] | None = None,
) -> list[dict[str, Any]] | None:
    """
    Call LLM to decompose a grand task into atomic sub-tasks.

    Returns list of task specs or None if decomposition fails.
    """
    try:
        from openai import OpenAI
    except ImportError:
        return None

    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        # Try parasite mode
        base_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
        if not base_url:
            return None
        client = OpenAI(
            base_url=base_url,
            api_key=os.environ.get("OLLAMA_API_KEY", "ollama"),
        )
        model = os.environ.get("OLLAMA_MODEL", "llama3.2")
    else:
        client = OpenAI(api_key=api_key)
        model = os.environ.get("OPENAI_MODEL", "gpt-4o")

    user_prompt = _DECOMPOSE_USER.format(
        instruction=instruction[:3000],
        workspace=str(workspace),
        estimated_files=assessment.estimated_files,
        score=assessment.score,
        context=context[:2000] if context else "(无额外上下文)",
    )

    if progress_hook:
        progress_hook("[🧠 前额叶] 正在分析任务结构，拆解为原子任务...")

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _DECOMPOSE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=4000,
            timeout=60,
        )
        content = response.choices[0].message.content or ""
        return _try_parse_json_array(content)
    except Exception as e:
        if progress_hook:
            progress_hook(f"[⚠️ 拆解失败] LLM调用出错: {e!s}")
        return None


def decompose_heuristic(
    instruction: str,
    assessment: ComplexityAssessment,
    workspace: Path,
) -> list[dict[str, Any]]:
    """
    Heuristic decomposition when LLM is unavailable.

    Produces a generic 3-phase plan: analyze → implement → verify.
    """
    return [
        {
            "instruction": f"分析阶段：阅读项目结构，理解现有代码。扫描所有相关文件，在 task_plan.md 中记录发现。原始任务：{instruction[:500]}",
            "phase": 1,
            "priority": 1,
            "depends_on": [],
            "estimated_files": min(assessment.estimated_files, 10),
            "estimated_tokens": 3000,
        },
        {
            "instruction": f"实施阶段：根据分析结果，执行核心修改。每次只改 1-3 个文件，改完立即测试。原始任务：{instruction[:500]}",
            "phase": 2,
            "priority": 3,
            "depends_on": [0],
            "estimated_files": assessment.estimated_files,
            "estimated_tokens": assessment.estimated_tokens,
        },
        {
            "instruction": f"验证阶段：运行所有测试，检查代码质量，确保无回归。整理 task_plan.md 记录成果。原始任务：{instruction[:500]}",
            "phase": 3,
            "priority": 5,
            "depends_on": [1],
            "estimated_files": 3,
            "estimated_tokens": 2000,
        },
    ]


def decompose_and_enqueue(
    instruction: str,
    workspace: Path,
    *,
    progress_hook: Callable[[str], None] | None = None,
    context: str = "",
    force: bool = False,
) -> tuple[bool, str, list[str]]:
    """
    Main entry point: assess complexity, decompose if needed, enqueue sub-tasks.

    Args:
        instruction: The user's grand task
        workspace: Project workspace path
        progress_hook: Optional callback for progress updates (e.g., TG)
        context: Optional additional context
        force: Force decomposition even for simple tasks

    Returns:
        (was_decomposed, report, task_ids)
    """
    workspace = Path(workspace).resolve()

    # Step 1: Assess complexity
    assessment = assess_complexity(instruction, workspace=workspace)
    report = format_assessment_report(assessment)

    if not assessment.should_decompose and not force:
        return False, report, []

    if progress_hook:
        progress_hook(
            f"[🧠 任务嗅探器] 检测到宏大任务 (复杂度: {assessment.score}/10)\n"
            f"预估涉及 ~{assessment.estimated_files} 个文件，~{assessment.estimated_steps} 个步骤\n"
            f"正在自动拆解..."
        )

    # Step 2: Decompose (try LLM first, fallback to heuristic)
    sub_tasks = decompose_with_llm(
        instruction, assessment, workspace,
        context=context,
        progress_hook=progress_hook,
    )

    if not sub_tasks:
        sub_tasks = decompose_heuristic(instruction, assessment, workspace)
        if progress_hook:
            progress_hook("[📋 启发式拆解] LLM不可用，使用通用3阶段拆解方案")

    # Step 3: Write milestones to task_plan.md
    _write_milestones_to_plan(workspace, instruction, assessment, sub_tasks)

    # Step 4: Write reasoning episode
    append_reasoning_episode(
        workspace,
        trigger=f"task_decomposition: score={assessment.score}",
        hypothesis=f"任务「{instruction[:100]}」过于复杂，需要拆解为 {len(sub_tasks)} 个原子任务依次执行",
        verify_plan=[
            f"子任务 {i+1}: {t['instruction'][:80]}" for i, t in enumerate(sub_tasks[:5])
        ] + (["...等"] if len(sub_tasks) > 5 else []),
        revise_hint="如果某个子任务失败，分析原因后进一步拆解或调整策略",
    )

    # Step 5: Enqueue to SmartTaskRegistry
    registry = SmartTaskRegistry(workspace)

    # Create parent task
    parent = registry.enqueue(
        instruction=instruction,
        kind="decomposed",
        priority=3,
        estimated_tokens=assessment.estimated_tokens,
        meta={"complexity_score": assessment.score, "sub_task_count": len(sub_tasks)},
    )

    # Build task specs with proper dependency mapping
    task_specs = []
    index_to_id: dict[int, str] = {}  # will be filled after enqueue

    # First pass: create specs without dependencies (we'll set them after)
    for i, st in enumerate(sub_tasks):
        task_specs.append({
            "instruction": st["instruction"],
            "phase": st.get("phase", 1),
            "priority": st.get("priority", 5),
            "parent_task_id": parent.task_id,
            "blocked_by": [],  # filled in second pass
            "kind": "decomposed",
            "estimated_tokens": st.get("estimated_tokens", 2000),
            "meta": {
                "original_index": i,
                "depends_on_indices": st.get("depends_on", []),
            },
        })

    # Enqueue all sub-tasks
    created_tasks = registry.enqueue_batch(task_specs)

    # Second pass: set up dependency references
    for i, ct in enumerate(created_tasks):
        index_to_id[i] = ct.task_id

    # Update dependencies using actual task IDs
    tasks_all = registry._load_all()
    changed = False
    for ct in created_tasks:
        dep_indices = ct.meta.get("depends_on_indices", [])
        for idx in dep_indices:
            if idx in index_to_id:
                dep_id = index_to_id[idx]
                if dep_id not in ct.blocked_by:
                    ct.blocked_by.append(dep_id)
                    changed = True
        # Find and update in full list
        for t in tasks_all:
            if t.task_id == ct.task_id:
                t.blocked_by = ct.blocked_by
                break
    if changed:
        registry._save_all(tasks_all)

    task_ids = [ct.task_id for ct in created_tasks]

    # Step 6: Build user-facing report
    decompose_report = _build_decomposition_report(
        instruction, assessment, sub_tasks, parent.task_id, task_ids
    )

    if progress_hook:
        progress_hook(decompose_report)

    return True, decompose_report, task_ids


def _write_milestones_to_plan(
    workspace: Path,
    instruction: str,
    assessment: ComplexityAssessment,
    sub_tasks: list[dict[str, Any]],
) -> None:
    """Write decomposition milestones to task_plan.md."""
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    plan_path = workspace / "task_plan.md"

    lines = [
        f"\n\n## 🧠 自动拆解计划 [{ts}]",
        f"",
        f"**原始任务**: {instruction[:300]}",
        f"**复杂度**: {assessment.score}/10 ({assessment.level})",
        f"**拆解为**: {len(sub_tasks)} 个原子任务",
        f"",
    ]

    # Group by phase
    phases: dict[int, list[dict[str, Any]]] = {}
    for st in sub_tasks:
        phase = st.get("phase", 1)
        phases.setdefault(phase, []).append(st)

    for phase_num in sorted(phases.keys()):
        lines.append(f"### 阶段 {phase_num}")
        for i, st in enumerate(phases[phase_num]):
            deps = st.get("depends_on", [])
            dep_str = f" (依赖: {deps})" if deps else ""
            lines.append(f"- [ ] {st['instruction'][:200]}{dep_str}")
        lines.append("")

    lines.append("---\n")
    content = "\n".join(lines)

    try:
        if plan_path.is_file():
            existing = plan_path.read_text(encoding="utf-8", errors="replace")
            plan_path.write_text(existing + content, encoding="utf-8")
        else:
            plan_path.write_text(
                "# Task Plan\n\n> 由 DevClaw 任务拆解引擎自动生成\n" + content,
                encoding="utf-8",
            )
    except OSError:
        pass


def _build_decomposition_report(
    instruction: str,
    assessment: ComplexityAssessment,
    sub_tasks: list[dict[str, Any]],
    parent_id: str,
    task_ids: list[str],
) -> str:
    """Build user-facing decomposition report."""
    lines = [
        f"🧠 **任务已自动拆解**",
        f"",
        f"老板，任务太大了，我已经自动将其拆分为 **{len(sub_tasks)} 个阶段性子任务**并排入工作队列。",
        f"",
        f"📊 复杂度: {assessment.score}/10 | 预估文件: ~{assessment.estimated_files} | "
        f"预估Token: ~{assessment.estimated_tokens:,}",
        f"🔗 父任务ID: {parent_id[:8]}",
        f"",
    ]

    phases: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for tid, st in zip(task_ids, sub_tasks):
        phase = st.get("phase", 1)
        phases.setdefault(phase, []).append((tid, st))

    for phase_num in sorted(phases.keys()):
        lines.append(f"**阶段 {phase_num}:**")
        for tid, st in phases[phase_num]:
            lines.append(f"  [{tid[:8]}] {st['instruction'][:80]}{'...' if len(st['instruction']) > 80 else ''}")
        lines.append("")

    lines.append("我现在开始执行阶段一。完成后会自动推进到下一阶段。")
    return "\n".join(lines)
