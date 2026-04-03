"""
Task Complexity Detector — intercept grand/ambiguous tasks, auto-decompose into atom tasks,
enqueue them into bot_task_queue, and optionally dispatch via subagent_mesh.

Wired into dev_claw/main.py's core loop as a pre-execution gate.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from claw_runtime.bot_task_queue import enqueue_persisted_task, persisted_queue_depth
from claw_runtime.reasoning_episode import append_reasoning_episode


# ---------------------------------------------------------------------------
# Complexity signal keywords
# ---------------------------------------------------------------------------

_GRAND_TASK_KEYWORDS: list[tuple[str, int]] = [
    # Chinese
    ("重构", 4), ("重写", 4), ("整个项目", 5), ("全部代码", 5),
    ("屎山", 4), ("从零开始", 5), ("新系统", 4), ("架构升级", 4),
    ("全面重构", 5), ("大改", 3), ("重新设计", 4), ("迁移", 3),
    # English
    ("refactor", 3), ("rewrite", 4), ("entire project", 5),
    ("whole codebase", 5), ("from scratch", 5), ("new system", 4),
    ("architecture", 3), ("migration", 3), ("overhaul", 4),
    ("redesign", 4), ("rebuild", 4), ("full rewrite", 5),
    ("multi-module", 3), ("cross-file", 3), ("large-scale", 4),
]

_SIMPLE_TASK_KEYWORDS: list[str] = [
    "fix typo", "rename", "add comment", "format", "lint",
    "修个错", "改名", "加注释", "格式化",
]


@dataclass
class ComplexityVerdict:
    """Result of complexity analysis."""
    score: int                      # 0-20 scale
    level: str                      # trivial | moderate | grand
    file_count: int                 # workspace file count (estimated)
    keyword_hits: list[str]         # which grand keywords matched
    should_intercept: bool          # True → must decompose before execution
    rationale: str                  # human-readable explanation


@dataclass
class AtomTask:
    """A single decomposed sub-task."""
    phase: int
    title: str
    description: str
    depends_on: list[int]           # phase numbers this depends on


@dataclass
class DecompositionPlan:
    """Full decomposition result."""
    original_task: str
    verdict: ComplexityVerdict
    atom_tasks: list[AtomTask]
    plan_path: Path                 # path to task_plan.md
    queued_count: int               # how many tasks were enqueued


# ---------------------------------------------------------------------------
# File counting
# ---------------------------------------------------------------------------

def _estimate_file_count(workspace: Path) -> int:
    """Fast estimate of meaningful files in workspace (skip .git, node_modules, __pycache__, .claw)."""
    skip_dirs = {".git", "node_modules", "__pycache__", ".claw", ".venv", "venv", ".tox", "dist", "build"}
    count = 0
    try:
        for root, dirs, files in os.walk(workspace):
            dirs[:] = [d for d in dirs if d not in skip_dirs]
            for f in files:
                if f.endswith((".py", ".js", ".ts", ".jsx", ".tsx", ".go", ".rs",
                               ".java", ".c", ".cpp", ".h", ".vue", ".svelte",
                               ".rb", ".php", ".md", ".yaml", ".yml", ".toml",
                               ".json", ".sh", ".bat")):
                    count += 1
                    if count > 500:  # cap scan for performance
                        return count
        return count
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Complexity scoring
# ---------------------------------------------------------------------------

def analyze_complexity(workspace: Path, task_text: str) -> ComplexityVerdict:
    """Score a task's complexity and decide whether to intercept it."""
    text_lower = task_text.lower()
    score = 0
    hits: list[str] = []

    # Check for simple task signals first
    for simple_kw in _SIMPLE_TASK_KEYWORDS:
        if simple_kw in text_lower:
            return ComplexityVerdict(
                score=1,
                level="trivial",
                file_count=0,
                keyword_hits=[],
                should_intercept=False,
                rationale=f"任务包含简单信号词「{simple_kw}」，无需拦截。",
            )

    # Grand keyword scoring
    for kw, weight in _GRAND_TASK_KEYWORDS:
        if kw in text_lower:
            score += weight
            hits.append(kw)

    # Task text length signal
    if len(task_text) > 300:
        score += 2
    elif len(task_text) > 150:
        score += 1

    # File count signal
    file_count = _estimate_file_count(workspace)
    if file_count > 100:
        score += 3
    elif file_count > 50:
        score += 2
    elif file_count > 20:
        score += 1

    # Vague/ambiguous signals (no specific file/function mentioned)
    has_specific_target = bool(re.search(r"\b\w+\.(py|js|ts|go|rs|java|cpp|c|h)\b", text_lower))
    if not has_specific_target and score >= 3:
        score += 2

    # Determine level
    if score <= 3:
        level = "trivial"
    elif score <= 7:
        level = "moderate"
    else:
        level = "grand"

    should_intercept = level == "grand"

    # Build rationale
    parts: list[str] = []
    if hits:
        parts.append(f"命中宏大任务关键词: {', '.join(hits)}")
    if file_count > 20:
        parts.append(f"项目含 {file_count}+ 个源文件")
    if not has_specific_target and score >= 3:
        parts.append("任务缺少具体文件/函数目标，属于模糊宏大指令")
    if len(task_text) > 150:
        parts.append(f"任务描述较长({len(task_text)} 字符)")
    rationale = "；".join(parts) if parts else "复杂度较低，可直接执行。"

    if should_intercept:
        rationale = (
            f"⚠️ 智力觉醒：检测到宏大任务（得分 {score}）。"
            f"{'；'.join(parts)}。"
            f"警告：直接执行可能导致 Token 爆炸或逻辑崩溃。系统将强制拆解后排入工作队列。"
        )

    return ComplexityVerdict(
        score=score,
        level=level,
        file_count=file_count,
        keyword_hits=hits,
        should_intercept=should_intercept,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Auto-decomposition
# ---------------------------------------------------------------------------

# Default decomposition templates keyed by detected intent
_DECOMPOSITION_TEMPLATES: dict[str, list[dict[str, str]]] = {
    "refactor": [
        {"title": "梳理入口文件与依赖关系", "desc": "扫描项目结构，绘制模块依赖图，标记核心入口与高耦合节点。"},
        {"title": "提取并隔离数据层逻辑", "desc": "将数据库操作、ORM 模型、数据访问层从业务逻辑中剥离，建立清晰的 Data Layer 接口。"},
        {"title": "剥离 API/网络请求层", "desc": "将外部 API 调用（HTTP、WebSocket、RPC）封装到独立模块，统一错误处理与重试逻辑。"},
        {"title": "重构核心业务逻辑", "desc": "在数据层和 API 层已隔离后，重构核心业务流程，消除重复代码，建立单元测试。"},
        {"title": "集成测试与回归验证", "desc": "运行全量测试，检查所有重构是否引入回归，修复失败用例。"},
    ],
    "new_system": [
        {"title": "需求分析与架构设计", "desc": "明确系统边界、核心实体、接口契约，输出架构设计文档。"},
        {"title": "搭建项目骨架与基础设施", "desc": "初始化项目结构、配置管理、日志、CI/CD 基础。"},
        {"title": "实现核心模块", "desc": "按优先级实现最核心的业务模块，保持最小可运行状态。"},
        {"title": "实现辅助模块与集成", "desc": "实现周边功能模块，与核心模块集成联调。"},
        {"title": "测试、优化与文档", "desc": "补全测试覆盖，性能优化，编写用户/开发文档。"},
    ],
    "migration": [
        {"title": "评估现有系统与目标差异", "desc": "对比源系统与目标系统的数据模型、API、依赖差异。"},
        {"title": "建立兼容层/适配器", "desc": "编写数据转换、API 适配代码，确保迁移过程可回滚。"},
        {"title": "分批迁移与验证", "desc": "按模块/数据分批迁移，每批后运行验证脚本确认正确性。"},
        {"title": "切换流量与清理旧代码", "desc": "完成全量迁移后切换入口，移除兼容层与旧代码。"},
    ],
    "generic": [
        {"title": "分析任务范围与约束", "desc": "扫描项目结构，理解当前状态，明确任务边界与风险点。"},
        {"title": "制定分步执行计划", "desc": "将宏大目标拆解为可独立验证的里程碑。"},
        {"title": "执行核心变更", "desc": "按计划逐步实施核心变更，每步后验证。"},
        {"title": "收尾验证与文档更新", "desc": "运行全量测试，确认无回归，更新相关文档。"},
    ],
}


def _detect_intent(task_text: str) -> str:
    """Detect the primary intent of a grand task to select decomposition template."""
    text = task_text.lower()
    if any(kw in text for kw in ("重构", "refactor", "rewrite", "重写", "屎山", "overhaul")):
        return "refactor"
    if any(kw in text for kw in ("新系统", "new system", "从零", "from scratch", "rebuild")):
        return "new_system"
    if any(kw in text for kw in ("迁移", "migration", "migrate", "移植")):
        return "migration"
    return "generic"


def decompose_grand_task(
    workspace: Path,
    task_text: str,
    verdict: ComplexityVerdict,
) -> DecompositionPlan:
    """
    Decompose a grand task into atom tasks, write plan to task_plan.md, and enqueue into bot_task_queue.

    This is the "自动拆解" (auto-decomposition) + "自我派发" (self-dispatch) core.
    """
    workspace = Path(workspace).resolve()
    intent = _detect_intent(task_text)
    template = _DECOMPOSITION_TEMPLATES.get(intent, _DECOMPOSITION_TEMPLATES["generic"])

    # Build atom tasks
    atom_tasks: list[AtomTask] = []
    for i, step in enumerate(template):
        atom_tasks.append(AtomTask(
            phase=i + 1,
            title=step["title"],
            description=step["desc"],
            depends_on=[i] if i > 0 else [],
        ))

    # --- Write reasoning episode to task_plan.md ---
    verify_plan = [
        f"阶段 {at.phase}: {at.title}" for at in atom_tasks
    ]
    plan_path = append_reasoning_episode(
        workspace,
        trigger=f"complexity_detector:grand(score={verdict.score})",
        hypothesis=(
            f"原始任务: 「{task_text[:200]}」\n"
            f"复杂度分析: {verdict.rationale}\n"
            f"检测意图: {intent}\n"
            f"项目规模: ~{verdict.file_count} 个源文件\n\n"
            f"系统判定此任务过于宏大（得分 {verdict.score}/20），必须先拆解后执行。\n"
            f"已自动生成 {len(atom_tasks)} 个阶段性里程碑并压入工作队列。"
        ),
        verify_plan=verify_plan,
        revise_hint="若某阶段执行失败，系统将触发 reasoning_episode 进行错误推理，而非盲目重试。",
    )

    # --- Enqueue atom tasks into bot_task_queue ---
    queued = 0
    for at in atom_tasks:
        enqueue_persisted_task(
            workspace,
            kind="atom_task",
            text=f"[阶段 {at.phase}/{len(atom_tasks)}] {at.title}: {at.description}\n\n原始宏大任务: {task_text[:500]}",
            meta={
                "phase": at.phase,
                "total_phases": len(atom_tasks),
                "title": at.title,
                "intent": intent,
                "depends_on": at.depends_on,
                "original_task_hash": hash(task_text) & 0xFFFFFFFF,
            },
        )
        queued += 1

    return DecompositionPlan(
        original_task=task_text,
        verdict=verdict,
        atom_tasks=atom_tasks,
        plan_path=plan_path,
        queued_count=queued,
    )


# ---------------------------------------------------------------------------
# Queue consumer — pop next atom task
# ---------------------------------------------------------------------------

def pop_next_atom_task(workspace: Path) -> dict[str, Any] | None:
    """Pop the next atom_task from the persisted queue (FIFO). Returns None if empty."""
    from claw_runtime.bot_task_queue import pop_persisted_tasks

    tasks = pop_persisted_tasks(workspace, max_n=1)
    for t in tasks:
        if t.get("kind") == "atom_task":
            return t
    # If popped a non-atom task, put it back? For simplicity, return any task.
    return tasks[0] if tasks else None


# ---------------------------------------------------------------------------
# Failure-triggered reasoning + research
# ---------------------------------------------------------------------------

def on_consecutive_failures(
    workspace: Path,
    task_text: str,
    failure_count: int,
    last_error: str = "",
) -> Path | None:
    """
    Called when a task fails consecutively (>=2 times).
    Forces a reasoning episode and optionally triggers research_lab.

    Returns path to task_plan.md if written, else None.
    """
    workspace = Path(workspace).resolve()

    if failure_count < 2:
        return None

    # --- Force reasoning episode (>= 50 chars analysis) ---
    analysis = (
        f"任务「{task_text[:120]}」已连续失败 {failure_count} 次。\n"
        f"最近错误: {last_error[:300]}\n\n"
        "可能原因分析:\n"
        "1. 任务范围过大，单次迭代无法完成，需进一步拆解为更小的原子操作。\n"
        "2. 依赖环境或外部服务异常（API 不可达、依赖缺失、权限不足）。\n"
        "3. 代码逻辑错误需要先读取相关源文件再做修改，而非盲目尝试。\n"
        "建议: 先用只读工具（execute_terminal: ls/cat/grep）确认当前状态，再制定精确修复方案。"
    )

    plan_path = append_reasoning_episode(
        workspace,
        trigger=f"consecutive_failure:{failure_count}",
        hypothesis=analysis,
        verify_plan=[
            "用只读命令检查最近错误日志与环境状态",
            "确认失败是由环境问题还是逻辑错误导致",
            "若为知识盲区：调用 research_lab 搜索解决方案",
            "将结论写入 append_typed_memory(category=lesson)",
        ],
        revise_hint="若推理发现自身知识盲区，强制调用 research_lab.py 搜索解决方案。",
    )

    # --- If failure count >= 3, trigger research_lab ---
    if failure_count >= 3:
        try:
            from claw_runtime.research_lab import run_public_research_scout

            run_public_research_scout(
                workspace,
                f"troubleshooting: {task_text[:200]} error: {last_error[:200]}",
            )
        except Exception:
            pass  # research_lab is best-effort

        # --- Try skill synthesis for knowledge gaps ---
        if failure_count >= 4:
            try:
                from claw_runtime.ultimate.skill_synthesis import synthesize_two_skills
                from claw_runtime.skill_registry import SkillRegistry

                reg = SkillRegistry(workspace)
                names = sorted(reg.refresh().keys())
                if len(names) >= 2:
                    synthesize_two_skills(workspace, names[0], names[1])
            except Exception:
                pass  # skill synthesis is best-effort

    return plan_path


# ---------------------------------------------------------------------------
# Subagent dispatch (optional parallel execution)
# ---------------------------------------------------------------------------

def dispatch_to_subagents(
    workspace: Path,
    atom_tasks: list[AtomTask],
    *,
    max_parallel: int = 3,
) -> list[dict[str, Any]]:
    """
    Optionally dispatch atom tasks to subagent_mesh for parallel analysis.
    Returns mesh results. This is a lightweight pre-analysis, not full execution.
    """
    try:
        from claw_runtime.ultimate.subagent_mesh import run_subagent_mesh

        # Build roles from atom task titles
        roles = tuple(f"phase_{at.phase}" for at in atom_tasks[:max_parallel])
        task_desc = "\n".join(
            f"Phase {at.phase}: {at.title} — {at.description}"
            for at in atom_tasks[:max_parallel]
        )
        return run_subagent_mesh(task_desc, roles=roles)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main entry point for main.py integration
# ---------------------------------------------------------------------------

def intercept_if_grand(
    workspace: Path,
    task_text: str,
    *,
    emit: Any | None = None,
) -> DecompositionPlan | None:
    """
    Main integration point: analyze task complexity. If grand, decompose and enqueue.
    Returns DecompositionPlan if intercepted, None if task should proceed normally.

    Called from dev_claw/main.py before entering the tool loop.
    """
    workspace = Path(workspace).resolve()

    # Skip if explicitly disabled
    if os.environ.get("DEVCLAW_COMPLEXITY_DETECTOR", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None

    verdict = analyze_complexity(workspace, task_text)

    if not verdict.should_intercept:
        return None

    # --- Grand task intercepted ---
    if callable(emit):
        emit(
            f"🧠 智力觉醒：检测到宏大任务（复杂度 {verdict.score}/20）\n"
            f"{verdict.rationale}\n"
            "系统禁止直接执行，正在自动拆解..."
        )

    plan = decompose_grand_task(workspace, task_text, verdict)

    if callable(emit):
        task_list = "\n".join(
            f"  阶段 {at.phase}: {at.title}" for at in plan.atom_tasks
        )
        emit(
            f"📋 自动拆解完成，已生成 {plan.queued_count} 个原子任务并排入工作队列:\n"
            f"{task_list}\n\n"
            f"📝 详细计划已写入 task_plan.md\n"
            f"🚀 现在开始执行阶段一: {plan.atom_tasks[0].title}"
        )

    # Optionally do subagent pre-analysis
    if os.environ.get("DEVCLAW_SUBAGENT_PREANALYSIS", "").strip().lower() in {"1", "true", "yes"}:
        mesh_results = dispatch_to_subagents(workspace, plan.atom_tasks)
        if mesh_results and callable(emit):
            emit(f"🔀 子代理预分析完成: {len(mesh_results)} 个角色已返回初步分析。")

    return plan
