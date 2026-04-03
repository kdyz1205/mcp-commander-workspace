"""
Persistent JSONL task queue for TG / DevClaw: survives process restarts.

v2.0 — Upgraded with:
- Task IDs, states (pending/running/done/failed), priorities
- Parent-child relationships for decomposed tasks
- Dependency tracking (blocked_by)
- Retry counts and cost estimation
- Smart queue: pull next runnable task (respects dependencies)
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_locks: dict[str, threading.Lock] = {}


def _lock_for(ws: Path) -> threading.Lock:
    key = str(ws.resolve())
    if key not in _locks:
        _locks[key] = threading.Lock()
    return _locks[key]


def persisted_tasks_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "persisted_tasks.jsonl"


def smart_tasks_path(workspace: Path) -> Path:
    return Path(workspace).resolve() / ".claw" / "smart_task_registry.jsonl"


# ---------------------------------------------------------------------------
# Legacy API (backward compatible)
# ---------------------------------------------------------------------------

def enqueue_persisted_task(
    workspace: Path,
    kind: str,
    text: str,
    meta: dict[str, Any] | None = None,
) -> None:
    ws = Path(workspace).resolve()
    p = persisted_tasks_path(ws)
    with _lock_for(ws):
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            rec = {
                "ts": time.time(),
                "kind": kind,
                "text": (text or "")[:8000],
                "meta": meta or {},
            }
            with p.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except OSError:
            pass


def pop_persisted_tasks(workspace: Path, max_n: int = 5) -> list[dict[str, Any]]:
    """Atomically remove up to max_n tasks from the head of the queue."""
    ws = Path(workspace).resolve()
    p = persisted_tasks_path(ws)
    with _lock_for(ws):
        if not p.is_file():
            return []
        try:
            lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        except OSError:
            return []
        if not lines:
            return []
        take = lines[:max_n]
        rest = lines[max_n:]
        out: list[dict[str, Any]] = []
        for ln in take:
            try:
                o = json.loads(ln)
                if isinstance(o, dict):
                    out.append(o)
            except json.JSONDecodeError:
                continue
        try:
            p.write_text("\n".join(rest) + ("\n" if rest else ""), encoding="utf-8")
        except OSError:
            pass
        return out


def persisted_queue_depth(workspace: Path) -> int:
    p = persisted_tasks_path(Path(workspace).resolve())
    if not p.is_file():
        return 0
    try:
        return sum(1 for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip())
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Smart Task Registry (v2.0)
# ---------------------------------------------------------------------------

@dataclass
class SmartTask:
    """A task with full lifecycle tracking."""
    task_id: str
    instruction: str
    status: str = "pending"          # pending | running | done | failed | cancelled
    priority: int = 5                # 1 (highest) - 10 (lowest)
    parent_task_id: str | None = None
    child_task_ids: list[str] = field(default_factory=list)
    blocked_by: list[str] = field(default_factory=list)  # task_ids that must complete first
    phase: int = 0                   # execution phase/stage number
    retries: int = 0
    max_retries: int = 3
    kind: str = "user"               # user | decomposed | autonomous | survival
    estimated_tokens: int = 0
    actual_tokens: int = 0
    created_at: float = 0.0
    started_at: float = 0.0
    completed_at: float = 0.0
    error_message: str = ""
    result_summary: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SmartTask":
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return cls(**filtered)


class SmartTaskRegistry:
    """
    Persistent task registry with dependency-aware scheduling.

    Stores tasks in .claw/smart_task_registry.jsonl.
    Supports parent-child decomposition, dependency blocking, and priority ordering.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self._path = smart_tasks_path(self.workspace)
        self._lock = _lock_for(self.workspace)

    def _load_all(self) -> list[SmartTask]:
        with self._lock:
            if not self._path.is_file():
                return []
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return []
            tasks = []
            for ln in lines:
                if not ln.strip():
                    continue
                try:
                    d = json.loads(ln)
                    if isinstance(d, dict):
                        tasks.append(SmartTask.from_dict(d))
                except (json.JSONDecodeError, TypeError):
                    continue
            return tasks

    def _save_all(self, tasks: list[SmartTask]) -> None:
        with self._lock:
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                content = "\n".join(
                    json.dumps(t.to_dict(), ensure_ascii=False) for t in tasks
                )
                self._path.write_text(content + "\n", encoding="utf-8")
            except OSError:
                pass

    def enqueue(
        self,
        instruction: str,
        *,
        kind: str = "user",
        priority: int = 5,
        parent_task_id: str | None = None,
        blocked_by: list[str] | None = None,
        phase: int = 0,
        estimated_tokens: int = 0,
        meta: dict[str, Any] | None = None,
    ) -> SmartTask:
        """Create and enqueue a new smart task."""
        task = SmartTask(
            task_id=uuid.uuid4().hex[:12],
            instruction=instruction[:8000],
            status="pending",
            priority=priority,
            parent_task_id=parent_task_id,
            blocked_by=blocked_by or [],
            phase=phase,
            kind=kind,
            estimated_tokens=estimated_tokens,
            created_at=time.time(),
            meta=meta or {},
        )
        tasks = self._load_all()
        tasks.append(task)

        # If this task has a parent, register it as child
        if parent_task_id:
            for t in tasks:
                if t.task_id == parent_task_id and task.task_id not in t.child_task_ids:
                    t.child_task_ids.append(task.task_id)

        self._save_all(tasks)
        return task

    def enqueue_batch(self, task_specs: list[dict[str, Any]]) -> list[SmartTask]:
        """Enqueue multiple tasks atomically (for decomposition results)."""
        tasks = self._load_all()
        new_tasks = []
        for spec in task_specs:
            t = SmartTask(
                task_id=uuid.uuid4().hex[:12],
                instruction=spec.get("instruction", "")[:8000],
                status="pending",
                priority=spec.get("priority", 5),
                parent_task_id=spec.get("parent_task_id"),
                blocked_by=spec.get("blocked_by", []),
                phase=spec.get("phase", 0),
                kind=spec.get("kind", "decomposed"),
                estimated_tokens=spec.get("estimated_tokens", 0),
                created_at=time.time(),
                meta=spec.get("meta", {}),
            )
            new_tasks.append(t)
            tasks.append(t)

        # Register children on parent
        for t in new_tasks:
            if t.parent_task_id:
                for parent in tasks:
                    if parent.task_id == t.parent_task_id and t.task_id not in parent.child_task_ids:
                        parent.child_task_ids.append(t.task_id)

        self._save_all(tasks)
        return new_tasks

    def next_runnable(self) -> SmartTask | None:
        """
        Get the highest-priority pending task whose dependencies are all satisfied.

        A task is runnable when:
        - status == "pending"
        - all tasks in blocked_by have status "done"
        """
        tasks = self._load_all()
        done_ids = {t.task_id for t in tasks if t.status == "done"}
        cancelled_ids = {t.task_id for t in tasks if t.status == "cancelled"}

        candidates = []
        for t in tasks:
            if t.status != "pending":
                continue
            # Check all blockers are done or cancelled
            blockers_clear = all(
                bid in done_ids or bid in cancelled_ids
                for bid in t.blocked_by
            )
            if blockers_clear:
                candidates.append(t)

        if not candidates:
            return None

        # Sort by: phase ASC, priority ASC (lower = higher priority), created_at ASC
        candidates.sort(key=lambda x: (x.phase, x.priority, x.created_at))
        return candidates[0]

    def mark_running(self, task_id: str) -> SmartTask | None:
        tasks = self._load_all()
        for t in tasks:
            if t.task_id == task_id:
                t.status = "running"
                t.started_at = time.time()
                self._save_all(tasks)
                return t
        return None

    def mark_done(self, task_id: str, *, result_summary: str = "", actual_tokens: int = 0) -> SmartTask | None:
        tasks = self._load_all()
        for t in tasks:
            if t.task_id == task_id:
                t.status = "done"
                t.completed_at = time.time()
                t.result_summary = result_summary[:2000]
                t.actual_tokens = actual_tokens
                self._save_all(tasks)
                # Check if parent task should also be marked done
                self._maybe_complete_parent(tasks, t.parent_task_id)
                return t
        return None

    def mark_failed(self, task_id: str, *, error: str = "") -> SmartTask | None:
        tasks = self._load_all()
        for t in tasks:
            if t.task_id == task_id:
                t.retries += 1
                if t.retries >= t.max_retries:
                    t.status = "failed"
                    t.error_message = error[:2000]
                else:
                    t.status = "pending"  # retry
                    t.error_message = f"retry {t.retries}/{t.max_retries}: {error[:1500]}"
                t.completed_at = time.time()
                self._save_all(tasks)
                return t
        return None

    def _maybe_complete_parent(self, tasks: list[SmartTask], parent_id: str | None) -> None:
        """If all children of a parent are done, mark parent done too."""
        if not parent_id:
            return
        parent = None
        for t in tasks:
            if t.task_id == parent_id:
                parent = t
                break
        if not parent or not parent.child_task_ids:
            return

        child_statuses = []
        for t in tasks:
            if t.task_id in parent.child_task_ids:
                child_statuses.append(t.status)

        if all(s == "done" for s in child_statuses):
            parent.status = "done"
            parent.completed_at = time.time()
            parent.result_summary = f"所有 {len(parent.child_task_ids)} 个子任务已完成"
            self._save_all(tasks)

    def get_task(self, task_id: str) -> SmartTask | None:
        for t in self._load_all():
            if t.task_id == task_id:
                return t
        return None

    def get_children(self, parent_task_id: str) -> list[SmartTask]:
        return [t for t in self._load_all() if t.parent_task_id == parent_task_id]

    def pending_count(self) -> int:
        return sum(1 for t in self._load_all() if t.status == "pending")

    def running_count(self) -> int:
        return sum(1 for t in self._load_all() if t.status == "running")

    def queue_summary(self) -> dict[str, int]:
        """Return count of tasks by status."""
        counts: dict[str, int] = {"pending": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
        for t in self._load_all():
            counts[t.status] = counts.get(t.status, 0) + 1
        return counts

    def format_queue_status(self) -> str:
        """Human-readable queue status for TG reporting."""
        tasks = self._load_all()
        if not tasks:
            return "📋 任务队列为空"

        summary = self.queue_summary()
        lines = [
            "📋 **任务队列状态**",
            f"  待执行: {summary['pending']} | 执行中: {summary['running']} | "
            f"已完成: {summary['done']} | 失败: {summary['failed']}",
            "",
        ]

        # Show active and pending tasks
        active = [t for t in tasks if t.status in ("running", "pending")]
        for t in active[:10]:
            icon = "🔄" if t.status == "running" else "⏳"
            dep_info = ""
            if t.blocked_by:
                unmet = [bid for bid in t.blocked_by if not any(
                    x.task_id == bid and x.status == "done" for x in tasks
                )]
                if unmet:
                    dep_info = f" [blocked by {len(unmet)} tasks]"
            parent_info = f" (子任务 of {t.parent_task_id[:8]})" if t.parent_task_id else ""
            lines.append(
                f"  {icon} [{t.task_id[:8]}] P{t.priority} "
                f"{t.instruction[:60]}{'...' if len(t.instruction) > 60 else ''}"
                f"{parent_info}{dep_info}"
            )

        if len(active) > 10:
            lines.append(f"  ... 还有 {len(active) - 10} 个任务")

        return "\n".join(lines)

    def cleanup_old(self, max_age_hours: float = 72) -> int:
        """Remove completed/failed tasks older than max_age_hours."""
        cutoff = time.time() - max_age_hours * 3600
        tasks = self._load_all()
        original_count = len(tasks)
        tasks = [
            t for t in tasks
            if t.status in ("pending", "running")
            or t.completed_at > cutoff
        ]
        self._save_all(tasks)
        return original_count - len(tasks)
