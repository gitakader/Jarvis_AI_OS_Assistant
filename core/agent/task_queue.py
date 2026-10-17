"""Background task queue for agent goals, persisted to disk.

Clean port of Brahma's task_queue: one worker thread, max_concurrent=1,
priority + FIFO ordering, cancel via threading.Event, plus scheduling
(run_at / repeat_every) and a save-on-change JSON store. The executor is
injected (executor_factory) so this module never imports main.py or the
session; the default factory lazily imports the core.agent executor.

Persistence contract: every mutation is written atomically (tmp file +
os.replace) as it happens — the app has no shutdown hook, so state must
never live only in RAM. On restore, a task that was RUNNING when the
process died becomes FAILED ("interrupted by shutdown") rather than being
re-run, so a crash can never restart a side-effecting goal.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Optional


class TaskStatus(Enum):
    PENDING   = "pending"
    RUNNING   = "running"
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    FAILED    = "failed"
    CANCELLED = "cancelled"


class TaskPriority(Enum):
    LOW    = 3
    NORMAL = 2
    HIGH   = 1


_TERMINAL = (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
_HISTORY_MAX = 50          # terminal tasks kept per store
_STORE_VERSION = 1


@dataclass(order=True)
class Task:
    priority:    int
    created_at:  float = field(compare=False)
    task_id:     str   = field(compare=False)
    goal:        str   = field(compare=False)
    status:      TaskStatus = field(compare=False, default=TaskStatus.PENDING)
    result:      Any        = field(compare=False, default=None)
    error:       str        = field(compare=False, default="")
    speak:       Any        = field(compare=False, default=None)
    on_complete: Any        = field(compare=False, default=None)
    cancel_flag: threading.Event = field(compare=False, default_factory=threading.Event)
    player:      Any        = field(compare=False, default=None)
    run_at:      Optional[float] = field(compare=False, default=None)
    repeat_every: Optional[float] = field(compare=False, default=None)


def _default_executor():
    """Lazy import so importing this module never drags in the executor."""
    from core.agent.executor import AgentExecutor
    return AgentExecutor()


class TaskQueue:
    def __init__(self, max_concurrent: int = 1,
                 executor_factory: Optional[Callable[[], Any]] = None):
        self._queue:        list[Task] = []
        self._lock:         threading.Lock = threading.Lock()
        self._condition:    threading.Condition = threading.Condition(self._lock)
        self._tasks:        dict[str, Task] = {}
        self._running:      bool = False
        self._worker_thread: Optional[threading.Thread] = None
        self._max_concurrent = max_concurrent
        self._active_count   = 0
        self._executor       = None
        self._executor_factory = executor_factory or _default_executor
        self._store_path: Optional[Path] = None
        self._store_lock:  threading.Lock = threading.Lock()

    def _get_executor(self):
        if self._executor is None:
            self._executor = self._executor_factory()
        return self._executor

    def set_executor(self, executor) -> None:
        """Wire the live executor in after boot (main.py owns the tool
        dispatcher, so the default factory cannot build a useful one)."""
        self._executor = executor

    def set_store(self, store_path) -> None:
        """Enable persistence and restore saved tasks.

        Call once, after set_executor, so restored work runs against the
        real dispatcher. PENDING tasks are re-queued; SCHEDULED ones wait
        for their run_at; a task that was RUNNING at shutdown becomes
        FAILED instead of being re-run.
        """
        if self._store_path is not None:
            return
        self._store_path = Path(store_path)
        if self._restore():
            self._persist()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="AgentTaskQueue")
        self._worker_thread.start()
        print("[TaskQueue] started")

    def stop(self) -> None:
        self._running = False
        with self._condition:
            self._condition.notify_all()
        worker = self._worker_thread
        if (worker is not None and worker.is_alive()
                and worker is not threading.current_thread()):
            worker.join(timeout=3.0)
        print("[TaskQueue] stopped")

    def submit(self, goal: str, priority: TaskPriority = TaskPriority.NORMAL,
               speak: Callable | None = None, on_complete: Callable | None = None,
               player: Any = None, run_at: float | None = None,
               repeat_every: float | None = None) -> str:
        """Queue a goal; returns the task_id (8 hex chars).

        run_at is epoch seconds; if it is in the future the task is created
        SCHEDULED and the worker promotes it when due (1s granularity).
        repeat_every is seconds between runs of a repeating task.
        """
        task_id = str(uuid.uuid4())[:8]
        now = time.time()
        scheduled = run_at is not None and run_at > now
        task = Task(
            priority=priority.value,
            created_at=now,
            task_id=task_id,
            goal=goal,
            status=TaskStatus.SCHEDULED if scheduled else TaskStatus.PENDING,
            speak=speak,
            on_complete=on_complete,
            player=player,
            run_at=run_at,
            repeat_every=repeat_every,
        )
        with self._condition:
            if not scheduled:
                self._queue.append(task)
                self._queue.sort(key=lambda t: (t.priority, t.created_at))
            self._tasks[task_id] = task
            self._condition.notify()
        self._persist()
        print(f"[TaskQueue] queued: [{task_id}] {goal[:60]}"
              + (f" (at {run_at:.0f})" if scheduled else ""))
        return task_id

    def cancel(self, task_id: str) -> bool:
        """Cancel a pending, scheduled or running task. Terminal → False."""
        with self._condition:
            task = self._tasks.get(task_id)
            if task is None or task.status in _TERMINAL:
                return False
            task.cancel_flag.set()
            if task.status == TaskStatus.PENDING:
                task.status = TaskStatus.CANCELLED
                try:
                    self._queue.remove(task)
                except ValueError:
                    pass
            elif task.status == TaskStatus.SCHEDULED:
                task.status = TaskStatus.CANCELLED
            # A RUNNING task is flagged; the runner marks it CANCELLED when it
            # next checks the flag.
            print(f"[TaskQueue] cancelled: [{task_id}]")
        self._persist()
        return True

    def get_status(self, task_id: str) -> dict | None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            return {
                "task_id": task.task_id,
                "goal":    task.goal,
                "status":  task.status.value,
                "result":  task.result,
                "error":   task.error,
                "run_at":  task.run_at,
                "repeat_every": task.repeat_every,
            }

    def get_all_statuses(self) -> list[dict]:
        with self._lock:
            return [
                {"task_id": t.task_id, "goal": t.goal[:50],
                 "status": t.status.value, "run_at": t.run_at,
                 "repeat_every": t.repeat_every}
                for t in self._tasks.values()
            ]

    def pending_count(self) -> int:
        with self._lock:
            return sum(1 for t in self._queue if t.status == TaskStatus.PENDING)

    # ── persistence ────────────────────────────────────────────────────────

    def _task_to_dict(self, task: Task) -> dict:
        return {
            "task_id":  task.task_id,
            "goal":     task.goal,
            "priority": task.priority,
            "created_at": task.created_at,
            "status":   task.status.value,
            "result":   task.result,
            "error":    task.error,
            "run_at":   task.run_at,
            "repeat_every": task.repeat_every,
        }

    def _task_from_dict(self, d: dict) -> Task | None:
        try:
            run_at = d.get("run_at")
            repeat = d.get("repeat_every")
            return Task(
                priority=int(d.get("priority", TaskPriority.NORMAL.value)),
                created_at=float(d.get("created_at") or time.time()),
                task_id=str(d.get("task_id") or str(uuid.uuid4())[:8]),
                goal=str(d.get("goal") or ""),
                status=TaskStatus(str(d.get("status") or "pending")),
                result=d.get("result"),
                error=str(d.get("error") or ""),
                run_at=float(run_at) if run_at is not None else None,
                repeat_every=float(repeat) if repeat is not None else None,
            )
        except (TypeError, ValueError):
            return None

    def _persist(self) -> None:
        """Write current state atomically; caps terminal history.

        Never call while holding _condition/_lock: it takes _store_lock,
        then _lock briefly for the snapshot, and does file I/O.
        """
        if self._store_path is None:
            return
        with self._store_lock:
            with self._lock:
                terminal = sorted(
                    (t for t in self._tasks.values() if t.status in _TERMINAL),
                    key=lambda t: t.created_at)
                for old in terminal[: max(0, len(terminal) - _HISTORY_MAX)]:
                    self._tasks.pop(old.task_id, None)
                payload = {"version": _STORE_VERSION,
                           "tasks": [self._task_to_dict(t)
                                     for t in self._tasks.values()]}
            try:
                text = json.dumps(payload, ensure_ascii=False, indent=2)
            except TypeError:
                for entry in payload["tasks"]:
                    entry["result"] = str(entry["result"])
                text = json.dumps(payload, ensure_ascii=False, indent=2)
            tmp = self._store_path.with_name(
                f"{self._store_path.name}.{uuid.uuid4().hex[:6]}.tmp")
            last_err: OSError | None = None
            for attempt in range(3):
                try:
                    tmp.write_text(text, encoding="utf-8")
                    os.replace(tmp, self._store_path)
                    last_err = None
                    break
                except OSError as e:
                    # Windows can briefly deny a replace while an AV scan or
                    # another reader has the file; retry before giving up.
                    last_err = e
                    time.sleep(0.05 * (attempt + 1))
            if last_err is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
                print(f"[TaskQueue] persist failed: {last_err}")

    def _restore(self) -> bool:
        """Load saved tasks into the live structures. True if any loaded.

        RUNNING on disk means the process died mid-task: mark it FAILED
        rather than re-running (a crash must not repeat side effects).
        PENDING tasks go back into the queue; SCHEDULED wait for run_at.
        """
        if not self._store_path.exists():
            return False
        try:
            data = json.loads(self._store_path.read_text(encoding="utf-8"))
            raw = data.get("tasks", []) if isinstance(data, dict) else []
        except (OSError, ValueError) as e:
            print(f"[TaskQueue] store unreadable ({e}); starting empty")
            return False
        restored = 0
        with self._condition:
            for item in raw:
                task = self._task_from_dict(item)
                if task is None:
                    continue
                if task.status == TaskStatus.RUNNING:
                    task.status = TaskStatus.FAILED
                    task.error = "Interrupted by shutdown."
                elif task.status == TaskStatus.PENDING:
                    self._queue.append(task)
                self._tasks[task.task_id] = task
                restored += 1
            if self._queue:
                self._queue.sort(key=lambda t: (t.priority, t.created_at))
                self._condition.notify()
        if restored:
            print(f"[TaskQueue] restored {restored} task(s) "
                  f"from {self._store_path.name}")
        return restored > 0

    # ── worker ──────────────────────────────────────────────────────────────

    def _next_task(self) -> Task | None:
        if self._active_count >= self._max_concurrent:
            return None
        for task in self._queue:
            if task.status == TaskStatus.PENDING and not task.cancel_flag.is_set():
                return task
        return None

    def _promote_due_scheduled_locked(self) -> bool:
        """Move SCHEDULED tasks whose run_at has passed into the queue.

        Must be called with self._condition held. Returns True if anything
        was promoted (caller persists the status change).
        """
        now = time.time()
        promoted = False
        for task in list(self._tasks.values()):
            if (task.status == TaskStatus.SCHEDULED
                    and task.run_at is not None
                    and task.run_at <= now
                    and not task.cancel_flag.is_set()):
                task.status = TaskStatus.PENDING
                self._queue.append(task)
                promoted = True
        if promoted:
            self._queue.sort(key=lambda t: (t.priority, t.created_at))
        return promoted

    def _worker_loop(self) -> None:
        while self._running:
            promoted = False
            task = None
            with self._condition:
                while self._running and self._next_task() is None:
                    self._condition.wait(timeout=1.0)
                    if self._promote_due_scheduled_locked():
                        promoted = True
                if not self._running:
                    return
                task = self._next_task()
                if task is not None:
                    task.status = TaskStatus.RUNNING
                    self._active_count += 1
                    try:
                        self._queue.remove(task)
                    except ValueError:
                        pass
            if promoted or task is not None:
                # RUNNING goes to disk too: restore converts it to FAILED
                # instead of silently re-running an interrupted goal.
                self._persist()
            if task is not None:
                threading.Thread(target=self._run_task, args=(task,),
                                 daemon=True, name=f"AgentTask-{task.task_id}").start()

    def _run_task(self, task: Task) -> None:
        print(f"[TaskQueue] running: [{task.task_id}] {task.goal[:60]}")
        try:
            executor = self._get_executor()
            result = executor.execute(
                goal=task.goal, speak=task.speak,
                cancel_flag=task.cancel_flag, player=task.player)
            with self._lock:
                if task.cancel_flag.is_set():
                    task.status = TaskStatus.CANCELLED
                else:
                    task.status = TaskStatus.COMPLETED
                    task.result = result
                self._active_count -= 1
            if task.on_complete and not task.cancel_flag.is_set():
                try:
                    task.on_complete(task.task_id, result)
                except Exception as e:
                    print(f"[TaskQueue] on_complete callback error: {e}")
            print(f"[TaskQueue] completed: [{task.task_id}]")
        except Exception as e:
            with self._lock:
                task.status = TaskStatus.FAILED
                task.error = str(e)
                self._active_count -= 1
            print(f"[TaskQueue] failed: [{task.task_id}] {e}")
        self._persist()
        self._rearm_repeat(task)
        with self._condition:
            self._condition.notify()

    def _rearm_repeat(self, task: Task) -> None:
        """A repeating task finished: queue its next run as SCHEDULED.

        Anchored to the original run_at so the cadence does not drift with
        execution time; a cancelled repeat is not re-armed.
        """
        if not task.repeat_every or task.status == TaskStatus.CANCELLED:
            return
        base = task.run_at if task.run_at else time.time()
        next_run = base + task.repeat_every
        while next_run <= time.time():
            next_run += task.repeat_every
        new_task = Task(
            priority=task.priority,
            created_at=time.time(),
            task_id=str(uuid.uuid4())[:8],
            goal=task.goal,
            status=TaskStatus.SCHEDULED,
            speak=task.speak,
            on_complete=task.on_complete,
            player=task.player,
            run_at=next_run,
            repeat_every=task.repeat_every,
        )
        with self._condition:
            self._tasks[new_task.task_id] = new_task
        self._persist()
        print(f"[TaskQueue] repeat armed: [{new_task.task_id}] "
              f"next at {next_run:.0f}")


_queue: TaskQueue | None = None
_queue_lock = threading.Lock()


def get_queue() -> TaskQueue:
    """Process-wide singleton; starts its worker on first call."""
    global _queue
    with _queue_lock:
        if _queue is None:
            _queue = TaskQueue()
            _queue.start()
        return _queue
