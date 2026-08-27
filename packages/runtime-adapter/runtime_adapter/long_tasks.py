"""Long-running Task decomposition & lifecycle — §16D.2

Features:
- Kanban-style decomposition: a high-level task → ordered sub-tasks
- Checkpoint / resume: persist progress so a 30min+ task survives restart
- Timeout per sub-task + global timeout
- Progress events stream (SSE-friendly dicts)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_id(prefix: str = "ltask") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class LongTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class SubTaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class SubTask:
    """One Kanban card within a long task."""

    id: str
    title: str
    order: int
    status: SubTaskStatus = SubTaskStatus.PENDING
    result: Any | None = None
    error: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    timeout_seconds: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Checkpoint:
    """Persisted snapshot after a sub-task completes."""

    task_id: str
    completed_step: int
    completed_subtask_id: str
    snapshot: dict[str, Any]
    created_at: str = field(default_factory=lambda: _now().isoformat())
    progress_pct: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProgressEvent:
    """SSE-friendly progress event."""

    task_id: str
    event: str  # started | subtask_started | subtask_done | checkpoint | timeout | completed | failed
    step: int
    total: int
    progress_pct: float
    detail: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: _now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LongTask:
    task_id: str
    title: str
    description: str
    status: LongTaskStatus = LongTaskStatus.PENDING
    sub_tasks: list[SubTask] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    progress_events: list[ProgressEvent] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: _now().isoformat())
    updated_at: str = field(default_factory=lambda: _now().isoformat())
    started_at: str | None = None
    finished_at: str | None = None
    timeout_seconds: float | None = None  # global
    deadline_epoch: float | None = None
    context: dict[str, Any] = field(default_factory=dict)  # tenant/user/session
    error: str | None = None
    resume_count: int = 0

    @property
    def total_steps(self) -> int:
        return len(self.sub_tasks)

    @property
    def completed_steps(self) -> int:
        return sum(1 for s in self.sub_tasks if s.status == SubTaskStatus.DONE)

    @property
    def progress_pct(self) -> float:
        if not self.total_steps:
            return 0.0
        return round(self.completed_steps / self.total_steps * 100, 1)

    @property
    def current_step(self) -> int:
        return self.completed_steps

    def is_expired(self) -> bool:
        if self.deadline_epoch is None:
            return False
        return time.time() > self.deadline_epoch

    def to_dict(self) -> dict:
        d = asdict(self)
        d["progress_pct"] = self.progress_pct
        d["completed_steps"] = self.completed_steps
        d["total_steps"] = self.total_steps
        return d


# ── Decomposition helpers ──────────────────────────────────────────

def decompose_task(
    title: str,
    description: str = "",
    steps: list[str] | None = None,
    *,
    max_steps: int = 20,
) -> list[SubTask]:
    """Decompose a high-level task into Kanban sub-tasks.

    If *steps* is provided, use it directly.  Otherwise split *description*
    by lines / sentences into at most *max_steps* cards.
    """
    if steps is not None:
        titles = steps[:max_steps]
    else:
        raw = (description or title).strip()
        # naive split: by newline then by period
        candidates: list[str] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            # split long lines by ". "
            parts = [p.strip() for p in line.split(". ") if p.strip()]
            candidates.extend(parts)
        if not candidates:
            candidates = [title]
        titles = candidates[:max_steps]

    sub_tasks: list[SubTask] = []
    for idx, t in enumerate(titles):
        sub_tasks.append(SubTask(id=_new_id("sub"), title=t, order=idx))
    return sub_tasks


# ── Manager ─────────────────────────────────────────────────────────

class LongTaskManager:
    """In-memory manager for long-running tasks.

    Production would persist to Postgres/Redis; this implementation keeps
    everything in-memory but exposes checkpoint/resume semantics so tests
    and callers can verify the contract without external deps.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, LongTask] = {}
        # persisted checkpoints store (simulates DB)
        self._checkpoint_store: dict[str, list[Checkpoint]] = {}

    # -- lifecycle ---------------------------------------------------

    def create_task(
        self,
        title: str,
        description: str = "",
        steps: list[str] | None = None,
        *,
        timeout_seconds: float | None = None,
        subtask_timeout: float | None = None,
        context: dict[str, Any] | None = None,
    ) -> LongTask:
        sub_tasks = decompose_task(title, description, steps)
        if subtask_timeout is not None:
            for s in sub_tasks:
                s.timeout_seconds = subtask_timeout
        task_id = _new_id("ltask")
        deadline = time.time() + timeout_seconds if timeout_seconds else None
        task = LongTask(
            task_id=task_id,
            title=title,
            description=description,
            sub_tasks=sub_tasks,
            timeout_seconds=timeout_seconds,
            deadline_epoch=deadline,
            context=context or {},
        )
        self._tasks[task_id] = task
        return task

    def get_task(self, task_id: str) -> LongTask:
        task = self._tasks.get(task_id)
        if not task:
            raise KeyError(f"long task not found: {task_id}")
        return task

    def list_tasks(self, status: LongTaskStatus | None = None) -> list[LongTask]:
        if status is None:
            return list(self._tasks.values())
        return [t for t in self._tasks.values() if t.status == status]

    def start(self, task_id: str) -> LongTask:
        task = self.get_task(task_id)
        if task.status not in (LongTaskStatus.PENDING, LongTaskStatus.PAUSED):
            raise ValueError(f"cannot start task in status {task.status}")
        if task.is_expired():
            task.status = LongTaskStatus.TIMEOUT
            task.error = "global timeout expired before start"
            self._emit(task, "timeout", detail={"reason": task.error})
            return task
        task.status = LongTaskStatus.RUNNING
        task.started_at = task.started_at or _now().isoformat()
        task.updated_at = _now().isoformat()
        self._emit(task, "started")
        return task

    def complete_subtask(
        self,
        task_id: str,
        subtask_id: str,
        result: Any | None = None,
        *,
        snapshot: dict[str, Any] | None = None,
    ) -> LongTask:
        task = self.get_task(task_id)
        if task.status != LongTaskStatus.RUNNING:
            raise ValueError(f"task not running: {task.status}")
        if task.is_expired():
            task.status = LongTaskStatus.TIMEOUT
            task.error = "global timeout expired"
            self._emit(task, "timeout", detail={"reason": task.error})
            return task
        sub = next((s for s in task.sub_tasks if s.id == subtask_id), None)
        if not sub:
            raise KeyError(f"subtask not found: {subtask_id}")
        if sub.status == SubTaskStatus.DONE:
            return task  # idempotent
        sub.status = SubTaskStatus.DONE
        sub.result = result
        sub.finished_at = _now().isoformat()
        task.updated_at = _now().isoformat()
        self._emit(task, "subtask_done", detail={"subtask_id": subtask_id, "title": sub.title})
        # auto-checkpoint
        cp = Checkpoint(
            task_id=task_id,
            completed_step=task.completed_steps,
            completed_subtask_id=subtask_id,
            snapshot=snapshot or {"result": result},
            progress_pct=task.progress_pct,
        )
        task.checkpoints.append(cp)
        self._checkpoint_store.setdefault(task_id, []).append(cp)
        self._emit(task, "checkpoint", detail={"checkpoint": cp.to_dict()})
        # auto-complete if all done
        if task.completed_steps == task.total_steps:
            task.status = LongTaskStatus.COMPLETED
            task.finished_at = _now().isoformat()
            self._emit(task, "completed")
        return task

    def fail_subtask(self, task_id: str, subtask_id: str, error: str) -> LongTask:
        task = self.get_task(task_id)
        sub = next((s for s in task.sub_tasks if s.id == subtask_id), None)
        if not sub:
            raise KeyError(f"subtask not found: {subtask_id}")
        sub.status = SubTaskStatus.FAILED
        sub.error = error
        sub.finished_at = _now().isoformat()
        task.status = LongTaskStatus.FAILED
        task.error = error
        task.updated_at = _now().isoformat()
        self._emit(task, "failed", detail={"subtask_id": subtask_id, "error": error})
        return task

    def cancel(self, task_id: str) -> LongTask:
        task = self.get_task(task_id)
        task.status = LongTaskStatus.CANCELLED
        task.finished_at = _now().isoformat()
        self._emit(task, "cancelled")
        return task

    # timeout check — caller can poll or call explicitly
    def check_timeout(self, task_id: str) -> LongTask:
        task = self.get_task(task_id)
        if task.status == LongTaskStatus.RUNNING and task.is_expired():
            task.status = LongTaskStatus.TIMEOUT
            task.error = "global timeout expired"
            task.finished_at = _now().isoformat()
            self._emit(task, "timeout", detail={"reason": task.error})
        return task

    # -- checkpoint / resume -----------------------------------------

    def checkpoint(self, task_id: str, snapshot: dict[str, Any] | None = None) -> Checkpoint:
        task = self.get_task(task_id)
        cp = Checkpoint(
            task_id=task_id,
            completed_step=task.completed_steps,
            completed_subtask_id=task.sub_tasks[task.completed_steps - 1].id if task.completed_steps > 0 else "",
            snapshot=snapshot or {},
            progress_pct=task.progress_pct,
        )
        task.checkpoints.append(cp)
        self._checkpoint_store.setdefault(task_id, []).append(cp)
        self._emit(task, "checkpoint", detail={"checkpoint": cp.to_dict()})
        return cp

    def get_checkpoints(self, task_id: str) -> list[Checkpoint]:
        return list(self._checkpoint_store.get(task_id, []))

    def resume(self, task_id: str) -> LongTask:
        task = self.get_task(task_id)
        if task.status not in (LongTaskStatus.PAUSED, LongTaskStatus.FAILED, LongTaskStatus.TIMEOUT):
            # also allow resuming a running task that was evicted (idempotent)
            if task.status == LongTaskStatus.RUNNING:
                return task
            raise ValueError(f"cannot resume task in status {task.status}")
        # verify deadline not permanently expired unless caller extends it
        if task.is_expired():
            raise TimeoutError("cannot resume: global deadline already expired")
        task.status = LongTaskStatus.RUNNING
        task.resume_count += 1
        task.updated_at = _now().isoformat()
        self._emit(task, "resumed", detail={"resume_count": task.resume_count})
        return task

    def pause(self, task_id: str) -> LongTask:
        task = self.get_task(task_id)
        if task.status != LongTaskStatus.RUNNING:
            raise ValueError(f"cannot pause task in status {task.status}")
        task.status = LongTaskStatus.PAUSED
        task.updated_at = _now().isoformat()
        self.checkpoint(task_id, snapshot={"reason": "paused"})
        self._emit(task, "paused")
        return task

    def extend_timeout(self, task_id: str, extra_seconds: float) -> LongTask:
        task = self.get_task(task_id)
        if task.deadline_epoch is None:
            task.deadline_epoch = time.time() + extra_seconds
        else:
            task.deadline_epoch += extra_seconds
        if task.timeout_seconds is not None:
            task.timeout_seconds += extra_seconds
        task.updated_at = _now().isoformat()
        return task

    # -- progress ----------------------------------------------------

    def get_progress(self, task_id: str) -> dict:
        task = self.get_task(task_id)
        return {
            "task_id": task.task_id,
            "status": task.status.value,
            "progress_pct": task.progress_pct,
            "completed_steps": task.completed_steps,
            "total_steps": task.total_steps,
            "current_step": task.current_step,
            "resume_count": task.resume_count,
        }

    def progress_events(self, task_id: str) -> list[dict]:
        task = self.get_task(task_id)
        return [e.to_dict() for e in task.progress_events]

    # internal
    def _emit(self, task: LongTask, event: str, detail: dict[str, Any] | None = None) -> None:
        ev = ProgressEvent(
            task_id=task.task_id,
            event=event,
            step=task.completed_steps,
            total=task.total_steps,
            progress_pct=task.progress_pct,
            detail=detail or {},
        )
        task.progress_events.append(ev)


# singleton for convenience
default_manager = LongTaskManager()
