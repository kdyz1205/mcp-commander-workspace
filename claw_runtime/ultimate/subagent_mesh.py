"""
Dimension 3 — Real sub-agent mesh: parallel task execution with shared memory whiteboard.

Upgrades from demo stub to production parallel executor:
- Fork multiple DevClaw instances for independent sub-tasks
- Shared results via file-based whiteboard (.claw/mesh_whiteboard.jsonl)
- Configurable concurrency (default: 3 workers)
- Per-agent timeout and error handling
- Results merged and reported to parent
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable


def _whiteboard_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "mesh_whiteboard.jsonl"


def _append_whiteboard(workspace: Path, entry: dict[str, Any]) -> None:
    """Thread/process-safe append to shared whiteboard."""
    p = _whiteboard_path(workspace)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    except OSError:
        pass


def read_whiteboard(workspace: Path | str) -> list[dict[str, Any]]:
    """Read all entries from the mesh whiteboard."""
    p = _whiteboard_path(Path(workspace))
    if not p.is_file():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
        entries = []
        for ln in lines:
            if ln.strip():
                try:
                    entries.append(json.loads(ln))
                except json.JSONDecodeError:
                    continue
        return entries
    except OSError:
        return []


def clear_whiteboard(workspace: Path | str) -> None:
    """Clear the whiteboard for a new mesh run."""
    p = _whiteboard_path(Path(workspace))
    try:
        if p.is_file():
            p.write_text("", encoding="utf-8")
    except OSError:
        pass


def _agent_worker(
    workspace: str,
    role: str,
    task_instruction: str,
    task_id: str,
    max_iterations: int,
    result_queue: mp.Queue,
) -> None:
    """
    Worker process: runs a DevClaw instance for one sub-task.

    Writes results to the shared whiteboard and the result queue.
    """
    ws_path = Path(workspace)
    start_time = time.time()
    success = False
    summary = ""

    try:
        # Add repo root to path
        repo_root = str(ws_path.resolve())
        if repo_root not in sys.path:
            sys.path.insert(0, repo_root)

        from dev_claw.main import dev_claw_run

        # Run DevClaw with the sub-task instruction
        augmented_instruction = (
            f"[SubAgent:{role}|TaskID:{task_id}] {task_instruction}\n\n"
            f"重要：你是并行子代理之一，角色={role}。"
            f"完成后将结果写入 .claw/mesh_whiteboard.jsonl 以便主代理合并。"
            f"只专注于你的分配任务，不要尝试执行其他子代理的工作。"
        )

        success = dev_claw_run(
            augmented_instruction,
            max_iterations=max_iterations,
        )
        summary = f"{role}: completed {'successfully' if success else 'with issues'}"

    except Exception as e:
        summary = f"{role}: failed with error: {e!s}"
        success = False

    elapsed = time.time() - start_time

    result = {
        "role": role,
        "task_id": task_id,
        "success": success,
        "summary": summary,
        "elapsed_sec": round(elapsed, 1),
        "timestamp": time.time(),
    }

    # Write to shared whiteboard
    _append_whiteboard(ws_path, result)

    # Send back via queue
    result_queue.put(result)


def run_subagent_mesh(
    task: str,
    roles: tuple[str, ...] | None = None,
    *,
    workspace: str | None = None,
    max_iterations: int = 10,
    timeout_sec: float = 300,
) -> list[dict[str, Any]]:
    """
    Legacy API: run multiple agents with role-based task distribution.

    For new code, prefer run_parallel_tasks() instead.
    """
    roles = roles or ("watch", "intel", "coder")
    ws = workspace or os.environ.get("DEVCLAW_WORKSPACE", os.getcwd())

    ctx = mp.get_context("spawn")
    result_queue: mp.Queue = ctx.Queue()
    procs: list[mp.Process] = []

    for role in roles:
        p = ctx.Process(
            target=_agent_worker,
            args=(ws, role, task, f"mesh_{role}", max_iterations, result_queue),
        )
        p.start()
        procs.append(p)

    results: list[dict[str, Any]] = []
    deadline = time.time() + timeout_sec

    for _ in roles:
        remaining = max(0.1, deadline - time.time())
        try:
            results.append(result_queue.get(timeout=remaining))
        except Exception:
            results.append({"role": "unknown", "success": False, "summary": "timeout"})

    for p in procs:
        p.join(timeout=5)
        if p.is_alive():
            p.terminate()

    return results


def run_parallel_tasks(
    workspace: str | Path,
    task_specs: list[dict[str, Any]],
    *,
    max_workers: int = 3,
    max_iterations_per_task: int = 10,
    timeout_sec: float = 600,
    progress_hook: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """
    Run multiple independent sub-tasks in parallel using DevClaw instances.

    Args:
        workspace: Project workspace path
        task_specs: List of dicts with {task_id, instruction, role}
        max_workers: Max concurrent agents
        max_iterations_per_task: Max tool iterations per agent
        timeout_sec: Total timeout for all tasks
        progress_hook: Optional callback for progress updates

    Returns:
        List of result dicts with {task_id, role, success, summary, elapsed_sec}
    """
    ws = str(Path(workspace).resolve())

    # Clear whiteboard for this run
    clear_whiteboard(Path(ws))

    if progress_hook:
        progress_hook(
            f"[🔀 分身术] 启动 {min(len(task_specs), max_workers)} 个并行子代理...\n"
            + "\n".join(
                f"  Agent-{i+1} [{s.get('role', 'worker')}]: {s.get('instruction', '')[:60]}"
                for i, s in enumerate(task_specs[:max_workers])
            )
        )

    ctx = mp.get_context("spawn")
    result_queue: mp.Queue = ctx.Queue()
    all_results: list[dict[str, Any]] = []

    # Process in batches of max_workers
    for batch_start in range(0, len(task_specs), max_workers):
        batch = task_specs[batch_start : batch_start + max_workers]
        procs: list[mp.Process] = []

        for spec in batch:
            p = ctx.Process(
                target=_agent_worker,
                args=(
                    ws,
                    spec.get("role", "worker"),
                    spec.get("instruction", ""),
                    spec.get("task_id", f"parallel_{batch_start}"),
                    max_iterations_per_task,
                    result_queue,
                ),
            )
            p.start()
            procs.append(p)

        deadline = time.time() + timeout_sec
        batch_results: list[dict[str, Any]] = []

        for _ in batch:
            remaining = max(0.1, deadline - time.time())
            try:
                batch_results.append(result_queue.get(timeout=remaining))
            except Exception:
                batch_results.append({
                    "role": "unknown",
                    "success": False,
                    "summary": "timeout or error",
                })

        for p in procs:
            p.join(timeout=5)
            if p.is_alive():
                p.terminate()

        all_results.extend(batch_results)

        if progress_hook:
            done = sum(1 for r in all_results if r.get("success"))
            failed = len(all_results) - done
            progress_hook(
                f"[🔀 分身术] 批次完成: {done} 成功, {failed} 失败 "
                f"({len(all_results)}/{len(task_specs)} total)"
            )

    return all_results


def format_mesh_results(results: list[dict[str, Any]]) -> str:
    """Format mesh results for human/TG display."""
    if not results:
        return "🔀 分身术：无结果"

    success = sum(1 for r in results if r.get("success"))
    total = len(results)
    total_time = sum(r.get("elapsed_sec", 0) for r in results)

    lines = [
        f"🔀 **分身术执行报告**",
        f"成功: {success}/{total} | 总耗时: {total_time:.0f}s",
        "",
    ]

    for r in results:
        icon = "✅" if r.get("success") else "❌"
        lines.append(
            f"  {icon} [{r.get('role', '?')}] {r.get('summary', 'no summary')}"
            f" ({r.get('elapsed_sec', 0):.0f}s)"
        )

    return "\n".join(lines)
