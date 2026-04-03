"""
Dynamic Multi-Agent Topology — DevClaw's Liquid Neural Mesh.

Upgrades from fixed agent roles to a dynamic topology that reshapes
itself based on the task type. Instead of always running the same
planner-builder-auditor pipeline, the system selects the optimal
communication pattern for each task.

Topologies:
- RING:     A writes -> B reviews -> C tests -> loop (refactoring/review)
- STAR:     Central dispatcher + N parallel workers (search/scan tasks)
- PIPELINE: Sequential phases with handoff (build/implement tasks)
- SWARM:    Independent parallel work, merge at end (analysis/research)
- PAIR:     One worker + one reviewer (simple tasks)

Philosophy: "The shape of the team should match the shape of the problem."
"""

from __future__ import annotations

import logging
import os
import re
import time
from enum import Enum
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Topology types
# ---------------------------------------------------------------------------

class TopologyType(Enum):
    """Communication topology for a multi-agent mesh."""
    RING = "ring"
    STAR = "star"
    PIPELINE = "pipeline"
    SWARM = "swarm"
    PAIR = "pair"


# ---------------------------------------------------------------------------
# Topology selection heuristics
# ---------------------------------------------------------------------------

# Keyword -> topology mapping (checked in order; first match wins)
_TOPOLOGY_KEYWORDS: list[tuple[list[str], TopologyType]] = [
    (["refactor", "review", "lint", "clean", "rewrite"], TopologyType.RING),
    (["search", "scan", "find", "grep", "locate", "discover"], TopologyType.STAR),
    (["build", "implement", "create", "develop", "deploy", "ship"], TopologyType.PIPELINE),
    (["analyze", "research", "investigate", "explore", "study", "compare"], TopologyType.SWARM),
]


def select_topology(
    task_instruction: str,
    num_agents: int = 4,
) -> TopologyType:
    """
    Heuristic selection of the best topology for a task.

    Scans the task instruction for keywords and selects the topology that
    best matches the intent. Falls back to PAIR for simple tasks or when
    only 2 agents are available.

    Args:
        task_instruction: The task description to analyze.
        num_agents: Number of agents available.

    Returns:
        The recommended TopologyType.
    """
    if num_agents <= 2:
        return TopologyType.PAIR

    text_lower = task_instruction.lower()

    for keywords, topology in _TOPOLOGY_KEYWORDS:
        for kw in keywords:
            # Match whole words using word boundary
            if re.search(rf"\b{re.escape(kw)}\b", text_lower):
                logger.info(
                    "Topology selection: keyword %r -> %s",
                    kw, topology.value,
                )
                return topology

    # Default: PAIR for short/simple instructions, PIPELINE for longer ones
    word_count = len(text_lower.split())
    if word_count > 30:
        return TopologyType.PIPELINE
    return TopologyType.PAIR


# ---------------------------------------------------------------------------
# Topology builders
# ---------------------------------------------------------------------------

def build_topology(
    topology_type: TopologyType,
    task: str,
    num_agents: int = 4,
) -> list[dict[str, Any]]:
    """
    Generate agent specs for the selected topology.

    Each agent spec contains:
    - role: str — the agent's role name
    - instruction: str — what this agent should do
    - depends_on: list[str] — roles whose output this agent needs
    - communicates_with: list[str] — roles this agent can talk to

    Args:
        topology_type: The topology pattern to build.
        task: The task instruction to distribute.
        num_agents: Maximum number of agents to create.

    Returns:
        List of agent specification dicts.
    """
    builders = {
        TopologyType.RING: _build_ring,
        TopologyType.STAR: _build_star,
        TopologyType.PIPELINE: _build_pipeline,
        TopologyType.SWARM: _build_swarm,
        TopologyType.PAIR: _build_pair,
    }
    builder = builders.get(topology_type, _build_pair)
    return builder(task, num_agents)


def _build_ring(task: str, num_agents: int) -> list[dict[str, Any]]:
    """
    RING topology: each agent's output feeds into the next.

    A writes -> B reviews -> C tests -> (loop back to A if needed).
    Best for: refactoring, code review, iterative improvement.
    """
    n = max(3, min(num_agents, 5))
    ring_roles = [
        {
            "role": "writer",
            "instruction": (
                f"You are the WRITER in a ring topology. Your task:\n{task}\n\n"
                "Write or refactor the code. Pass your output to the reviewer. "
                "If you receive feedback from the tester, incorporate it."
            ),
            "depends_on": ["tester"],
            "communicates_with": ["reviewer"],
        },
        {
            "role": "reviewer",
            "instruction": (
                f"You are the REVIEWER in a ring topology.\n\n"
                f"Original task: {task}\n\n"
                "Review the writer's code for correctness, style, edge cases, "
                "and security. Provide specific, actionable feedback. "
                "Pass approved code to the tester."
            ),
            "depends_on": ["writer"],
            "communicates_with": ["tester"],
        },
        {
            "role": "tester",
            "instruction": (
                f"You are the TESTER in a ring topology.\n\n"
                f"Original task: {task}\n\n"
                "Write and run tests for the code. Report failures back to "
                "the writer for fixing. Confirm when all tests pass."
            ),
            "depends_on": ["reviewer"],
            "communicates_with": ["writer"],
        },
    ]

    # Add extra agents for larger rings
    if n >= 4:
        ring_roles.append({
            "role": "optimizer",
            "instruction": (
                f"You are the OPTIMIZER in a ring topology.\n\n"
                f"Original task: {task}\n\n"
                "After the tester confirms tests pass, optimize the code "
                "for performance and readability. Feed back into the ring."
            ),
            "depends_on": ["tester"],
            "communicates_with": ["writer"],
        })
    if n >= 5:
        ring_roles.append({
            "role": "documenter",
            "instruction": (
                f"You are the DOCUMENTER in a ring topology.\n\n"
                f"Original task: {task}\n\n"
                "Write docstrings, type hints, and inline comments. "
                "Update any relevant documentation files."
            ),
            "depends_on": ["optimizer"],
            "communicates_with": ["writer"],
        })

    return ring_roles[:n]


def _build_star(task: str, num_agents: int) -> list[dict[str, Any]]:
    """
    STAR topology: central dispatcher + N parallel workers.

    Best for: search, scanning, finding things across a codebase.
    """
    n_workers = max(1, min(num_agents - 1, 4))

    agents: list[dict[str, Any]] = [
        {
            "role": "dispatcher",
            "instruction": (
                f"You are the DISPATCHER in a star topology.\n\n"
                f"Task: {task}\n\n"
                "Break the task into {n_workers} independent sub-searches. "
                "Assign each worker a specific scope (e.g., different directories, "
                "file types, or patterns). Merge their results into a final report."
            ),
            "depends_on": [],
            "communicates_with": [f"worker_{i+1}" for i in range(n_workers)],
        },
    ]

    for i in range(n_workers):
        agents.append({
            "role": f"worker_{i+1}",
            "instruction": (
                f"You are WORKER {i+1} of {n_workers} in a star topology.\n\n"
                f"Task: {task}\n\n"
                "Execute your assigned sub-task from the dispatcher. "
                "Report findings back to the dispatcher. "
                "Focus only on your assigned scope."
            ),
            "depends_on": ["dispatcher"],
            "communicates_with": ["dispatcher"],
        })

    return agents


def _build_pipeline(task: str, num_agents: int) -> list[dict[str, Any]]:
    """
    PIPELINE topology: sequential phases with handoff.

    plan -> code -> test -> deploy. Best for: building new features.
    """
    phases = [
        {
            "role": "planner",
            "instruction": (
                f"You are the PLANNER in a build pipeline.\n\n"
                f"Task: {task}\n\n"
                "Create a detailed implementation plan. Identify files to "
                "create/modify, interfaces, dependencies, and edge cases. "
                "Output a structured plan for the coder."
            ),
            "depends_on": [],
            "communicates_with": ["coder"],
        },
        {
            "role": "coder",
            "instruction": (
                f"You are the CODER in a build pipeline.\n\n"
                f"Task: {task}\n\n"
                "Implement the planner's design. Write production-quality code "
                "with type hints and docstrings. Hand off to the tester."
            ),
            "depends_on": ["planner"],
            "communicates_with": ["tester"],
        },
        {
            "role": "tester",
            "instruction": (
                f"You are the TESTER in a build pipeline.\n\n"
                f"Task: {task}\n\n"
                "Write comprehensive tests. Run them. Fix any issues found "
                "or report them back. Confirm readiness for deployment."
            ),
            "depends_on": ["coder"],
            "communicates_with": ["deployer"],
        },
        {
            "role": "deployer",
            "instruction": (
                f"You are the DEPLOYER in a build pipeline.\n\n"
                f"Task: {task}\n\n"
                "Verify all tests pass. Handle integration: update imports, "
                "exports, __init__.py files. Run a final lint/type-check pass. "
                "Confirm deployment readiness."
            ),
            "depends_on": ["tester"],
            "communicates_with": [],
        },
    ]

    return phases[:max(2, min(num_agents, 4))]


def _build_swarm(task: str, num_agents: int) -> list[dict[str, Any]]:
    """
    SWARM topology: independent parallel work, merge at end.

    All agents research independently, then results are merged.
    Best for: analysis, research, investigation.
    """
    n = max(2, min(num_agents, 5))
    agents: list[dict[str, Any]] = []

    perspectives = [
        ("analyst_code", "code structure, architecture, and implementation patterns"),
        ("analyst_data", "data flow, state management, and dependencies"),
        ("analyst_security", "security vulnerabilities, input validation, and auth"),
        ("analyst_performance", "performance bottlenecks, memory usage, and scalability"),
        ("analyst_ux", "user experience, API ergonomics, and documentation"),
    ]

    for i in range(n):
        role, focus = perspectives[i]
        other_roles = [perspectives[j][0] for j in range(n) if j != i]
        agents.append({
            "role": role,
            "instruction": (
                f"You are {role.upper()} in a swarm analysis.\n\n"
                f"Task: {task}\n\n"
                f"Your focus area: {focus}.\n\n"
                "Work independently. Write your findings to the shared "
                "whiteboard. Do NOT duplicate other analysts' work."
            ),
            "depends_on": [],
            "communicates_with": other_roles,
        })

    return agents


def _build_pair(task: str, num_agents: int) -> list[dict[str, Any]]:
    """
    PAIR topology: one worker + one reviewer.

    The simplest topology. Best for: simple tasks.
    """
    return [
        {
            "role": "worker",
            "instruction": (
                f"You are the WORKER in a pair.\n\n"
                f"Task: {task}\n\n"
                "Implement the task. Write clean, tested code."
            ),
            "depends_on": [],
            "communicates_with": ["reviewer"],
        },
        {
            "role": "reviewer",
            "instruction": (
                f"You are the REVIEWER in a pair.\n\n"
                f"Task: {task}\n\n"
                "Review the worker's implementation for correctness, style, "
                "and completeness. Provide specific feedback."
            ),
            "depends_on": ["worker"],
            "communicates_with": ["worker"],
        },
    ]


# ---------------------------------------------------------------------------
# Liquid mesh execution
# ---------------------------------------------------------------------------

def run_liquid_mesh(
    workspace: str | Path,
    task: str,
    topology_type: TopologyType | None = None,
    max_agents: int = 4,
    timeout: float = 600,
    progress_hook: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """
    Run a dynamically-shaped multi-agent mesh on a task.

    Auto-selects topology if not specified, builds agent specs, executes
    using the subagent_mesh infrastructure, and merges results.

    Args:
        workspace: DevClaw workspace root path.
        task: The task instruction.
        topology_type: Override topology selection (auto if None).
        max_agents: Maximum agents to spawn.
        timeout: Total timeout in seconds.
        progress_hook: Optional callback for progress messages.

    Returns:
        Dict with keys: topology, agents, results, merged_output, elapsed_sec.
    """
    ws = Path(workspace).resolve()
    start_time = time.time()

    # 1. Select topology
    if topology_type is None:
        topology_type = select_topology(task, max_agents)

    if progress_hook:
        progress_hook(
            f"[Liquid Topology] Selected: {topology_type.value.upper()} "
            f"for {max_agents} agents"
        )

    # 2. Build agent specs
    agent_specs = build_topology(topology_type, task, max_agents)

    if progress_hook:
        roles = ", ".join(a["role"] for a in agent_specs)
        progress_hook(f"[Liquid Topology] Agents: {roles}")

    # 3. Execute using subagent_mesh infrastructure
    results = _execute_topology(
        ws, topology_type, agent_specs, timeout, progress_hook
    )

    # 4. Merge results
    merged = _merge_results(topology_type, agent_specs, results)

    elapsed = time.time() - start_time

    return {
        "topology": topology_type.value,
        "agents": agent_specs,
        "results": results,
        "merged_output": merged,
        "elapsed_sec": round(elapsed, 1),
    }


def _execute_topology(
    workspace: Path,
    topology_type: TopologyType,
    agent_specs: list[dict[str, Any]],
    timeout: float,
    progress_hook: Callable[[str], None] | None,
) -> list[dict[str, Any]]:
    """
    Execute agent specs using the subagent_mesh parallel task runner.

    For sequential topologies (RING, PIPELINE), agents are run in
    dependency order. For parallel topologies (STAR, SWARM, PAIR),
    independent agents run concurrently.
    """
    try:
        from claw_runtime.ultimate.subagent_mesh import (
            run_parallel_tasks,
        )
    except ImportError:
        logger.warning(
            "subagent_mesh not available — falling back to sequential "
            "execution via dev_claw_run"
        )

        def run_parallel_tasks(
            workspace: str,
            task_specs: list[dict[str, Any]],
            max_workers: int = 1,
            timeout_sec: float = 600,
            progress_hook: Callable[[str], None] | None = None,
        ) -> list[dict[str, Any]]:
            """Fallback: run tasks sequentially using dev_claw_run."""
            results: list[dict[str, Any]] = []
            for spec in task_specs:
                t0 = time.time()
                try:
                    from dev_claw.main import dev_claw_run
                    output = dev_claw_run(
                        workspace=workspace,
                        instruction=spec.get("instruction", ""),
                    )
                    results.append({
                        "task_id": spec.get("task_id", ""),
                        "role": spec.get("role", "unknown"),
                        "success": True,
                        "summary": str(output)[:500] if output else "",
                        "elapsed_sec": round(time.time() - t0, 1),
                    })
                except Exception as exc:
                    logger.warning("Fallback dev_claw_run failed for %s: %s", spec.get("role"), exc)
                    results.append({
                        "task_id": spec.get("task_id", ""),
                        "role": spec.get("role", "unknown"),
                        "success": False,
                        "summary": f"fallback error: {exc}",
                        "elapsed_sec": round(time.time() - t0, 1),
                    })
            return results

    ws_str = str(workspace)

    if topology_type in (TopologyType.SWARM, TopologyType.STAR):
        # Parallel execution: all agents run at once
        task_specs = [
            {
                "task_id": f"liquid_{spec['role']}",
                "role": spec["role"],
                "instruction": spec["instruction"],
            }
            for spec in agent_specs
        ]
        return run_parallel_tasks(
            workspace=ws_str,
            task_specs=task_specs,
            max_workers=len(agent_specs),
            timeout_sec=timeout,
            progress_hook=progress_hook,
        )

    elif topology_type in (TopologyType.PIPELINE, TopologyType.RING):
        # Sequential execution: each phase runs after its dependency
        all_results: list[dict[str, Any]] = []
        remaining_timeout = timeout

        for i, spec in enumerate(agent_specs):
            phase_start = time.time()

            if progress_hook:
                progress_hook(
                    f"[Liquid Topology] Phase {i+1}/{len(agent_specs)}: "
                    f"{spec['role']}"
                )

            task_specs = [{
                "task_id": f"liquid_{spec['role']}",
                "role": spec["role"],
                "instruction": spec["instruction"],
            }]

            phase_results = run_parallel_tasks(
                workspace=ws_str,
                task_specs=task_specs,
                max_workers=1,
                timeout_sec=remaining_timeout,
                progress_hook=progress_hook,
            )
            all_results.extend(phase_results)

            remaining_timeout -= (time.time() - phase_start)
            if remaining_timeout <= 0:
                logger.warning("Liquid mesh timeout — stopping at phase %d", i + 1)
                break

        return all_results

    else:
        # PAIR: run both, worker first then reviewer
        all_results = []
        half_timeout = timeout / 2

        for spec in agent_specs:
            task_specs = [{
                "task_id": f"liquid_{spec['role']}",
                "role": spec["role"],
                "instruction": spec["instruction"],
            }]
            phase_results = run_parallel_tasks(
                workspace=ws_str,
                task_specs=task_specs,
                max_workers=1,
                timeout_sec=half_timeout,
                progress_hook=progress_hook,
            )
            all_results.extend(phase_results)

        return all_results


def _merge_results(
    topology_type: TopologyType,
    agent_specs: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> str:
    """
    Merge agent results into a single output based on topology type.

    - RING: last agent's output is the refined final product
    - STAR: dispatcher merges all worker findings
    - PIPELINE: last phase output is the deliverable
    - SWARM: concatenate all findings with section headers
    - PAIR: worker output + reviewer commentary
    """
    if not results:
        return "(no results)"

    lines: list[str] = []

    if topology_type == TopologyType.SWARM:
        lines.append("=== SWARM ANALYSIS RESULTS ===\n")
        for r in results:
            role = r.get("role", "unknown")
            summary = r.get("summary", "no output")
            success = "OK" if r.get("success") else "FAILED"
            lines.append(f"--- {role} [{success}] ---")
            lines.append(summary)
            lines.append("")

    elif topology_type == TopologyType.STAR:
        # Dispatcher result comes first, then workers
        dispatcher = [r for r in results if r.get("role") == "dispatcher"]
        workers = [r for r in results if r.get("role") != "dispatcher"]
        lines.append("=== STAR TOPOLOGY RESULTS ===\n")
        if dispatcher:
            lines.append(f"[Dispatcher] {dispatcher[0].get('summary', '')}")
            lines.append("")
        for r in workers:
            status = "OK" if r.get("success") else "FAILED"
            lines.append(f"[{r.get('role', '?')}] [{status}] {r.get('summary', '')}")
        lines.append("")

    elif topology_type == TopologyType.PIPELINE:
        lines.append("=== PIPELINE RESULTS ===\n")
        for r in results:
            status = "OK" if r.get("success") else "FAILED"
            lines.append(
                f"Phase [{r.get('role', '?')}] [{status}]: "
                f"{r.get('summary', '')} ({r.get('elapsed_sec', 0):.0f}s)"
            )
        lines.append("")
        # Final phase summary
        if results:
            last = results[-1]
            lines.append(f"Final deliverable from: {last.get('role', '?')}")
            lines.append(last.get("summary", ""))

    elif topology_type == TopologyType.RING:
        lines.append("=== RING ITERATION RESULTS ===\n")
        for r in results:
            status = "OK" if r.get("success") else "FAILED"
            lines.append(
                f"[{r.get('role', '?')}] [{status}]: "
                f"{r.get('summary', '')} ({r.get('elapsed_sec', 0):.0f}s)"
            )

    else:  # PAIR
        lines.append("=== PAIR RESULTS ===\n")
        for r in results:
            label = r.get("role", "?").upper()
            status = "OK" if r.get("success") else "FAILED"
            lines.append(f"[{label}] [{status}]: {r.get('summary', '')}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Report formatting
# ---------------------------------------------------------------------------

def format_topology_report(results: dict[str, Any]) -> str:
    """
    Format a human-readable report of the liquid mesh execution.

    Args:
        results: The dict returned by run_liquid_mesh().

    Returns:
        A formatted multi-line string suitable for display or logging.
    """
    topo = results.get("topology", "unknown")
    elapsed = results.get("elapsed_sec", 0)
    agents = results.get("agents", [])
    agent_results = results.get("results", [])
    merged = results.get("merged_output", "")

    success_count = sum(1 for r in agent_results if r.get("success"))
    total_count = len(agent_results)

    lines = [
        f"Liquid Topology Report",
        f"{'=' * 50}",
        f"Topology:  {topo.upper()}",
        f"Agents:    {total_count} ({success_count} succeeded)",
        f"Elapsed:   {elapsed:.1f}s",
        f"",
        f"Agent Roles:",
    ]

    for spec in agents:
        deps = ", ".join(spec.get("depends_on", [])) or "(none)"
        comms = ", ".join(spec.get("communicates_with", [])) or "(none)"
        lines.append(f"  - {spec['role']:15s} depends_on=[{deps}] talks_to=[{comms}]")

    lines.append("")
    lines.append("Execution Results:")

    for r in agent_results:
        icon = "[OK]" if r.get("success") else "[FAIL]"
        lines.append(
            f"  {icon} {r.get('role', '?'):15s} "
            f"{r.get('summary', 'no summary'):50s} "
            f"({r.get('elapsed_sec', 0):.0f}s)"
        )

    lines.append("")
    lines.append("Merged Output:")
    lines.append("-" * 50)
    lines.append(merged)
    lines.append("-" * 50)

    return "\n".join(lines)
