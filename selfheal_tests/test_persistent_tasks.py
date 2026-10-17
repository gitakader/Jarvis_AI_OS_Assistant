"""
Offline tests for task-queue persistence and scheduling
(core/agent/task_queue.py + actions/agent_task.py).

Everything is hermetic: a temp-directory JSON store, a fake executor that
never touches the network or the LLM, and short timeouts. No singleton
queue is used — each test builds its own TaskQueue, so the worker threads
are test-local and deterministic.

Run:  venv/Scripts/python.exe selfheal_tests/test_persistent_tasks.py
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import threading
import time

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{label} — {detail}" if detail else label)
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


def wait_until(cond, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return cond()


class FakeExecutor:
    """Records execute() calls; returns a fixed string.

    With block set, execute() waits on it before returning, so tests can
    hold a task in RUNNING (cancel mid-flight); started is set on entry.
    """

    def __init__(self, result: str = "fake result",
                 block: threading.Event | None = None):
        self.result = result
        self.calls: list[str] = []
        self.cancel_flags: list = []
        self.block = block
        self.started = threading.Event()

    def execute(self, goal, speak=None, cancel_flag=None, player=None) -> str:
        self.started.set()
        self.calls.append(goal)
        self.cancel_flags.append(cancel_flag)
        if self.block is not None:
            self.block.wait(timeout=5.0)
        return self.result


def new_queue(store, executor, start: bool = True):
    from core.agent.task_queue import TaskQueue
    q = TaskQueue(max_concurrent=1, executor_factory=lambda: executor)
    q.set_store(store)
    if start:
        q.start()
    return q


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — persistence round-trip
# ─────────────────────────────────────────────────────────────────────────────

def test_persistence_roundtrip(tmpdir: pathlib.Path) -> None:
    from core.agent.task_queue import TaskQueue

    store = tmpdir / "agent_tasks.json"
    ex1 = FakeExecutor()
    q1 = new_queue(store, ex1, start=False)
    tid = q1.submit("build a report", on_complete=None)

    check("roundtrip: store file written on submit", store.exists())
    data = json.loads(store.read_text(encoding="utf-8"))
    check("roundtrip: version field", data.get("version") == 1)
    saved = {t["task_id"]: t for t in data["tasks"]}
    check("roundtrip: task present", tid in saved)
    check("roundtrip: status pending on disk",
          saved[tid]["status"] == "pending")

    # "Restart": a fresh queue over the same store picks the task up and runs it.
    q1.stop()
    ex2 = FakeExecutor()
    q2 = new_queue(store, ex2, start=True)
    try:
        st = wait_until(lambda: q2.get_status(tid)["status"] == "completed")
        check("roundtrip: restored task executed after restart", st)
        check("roundtrip: executor saw the goal",
              ex2.calls and ex2.calls[0] == "build a report")
        data = json.loads(store.read_text(encoding="utf-8"))
        saved = {t["task_id"]: t for t in data["tasks"]}
        check("roundtrip: completion persisted",
              saved[tid]["status"] == "completed")
        check("roundtrip: result persisted",
              saved[tid]["result"] == "fake result")
        check("roundtrip: no runtime fields on disk",
              not {"speak", "on_complete", "cancel_flag", "player"} & set(saved[tid]))
        check("roundtrip: no tmp file left behind",
              not store.with_name(store.name + ".tmp").exists())
    finally:
        q2.stop()


def test_running_becomes_failed_on_restore(tmpdir: pathlib.Path) -> None:
    from core.agent.task_queue import TaskStatus

    store = tmpdir / "agent_tasks.json"
    payload = {"version": 1, "tasks": [{
        "task_id": "abc12345", "goal": "risky side effect", "priority": 2,
        "created_at": time.time(), "status": "running", "result": None,
        "error": "", "run_at": None, "repeat_every": None,
    }]}
    store.write_text(json.dumps(payload), encoding="utf-8")

    ex = FakeExecutor()
    q = new_queue(store, ex, start=True)
    try:
        st = q.get_status("abc12345")
        check("interrupt: status is failed, not re-run",
              st and st["status"] == TaskStatus.FAILED.value)
        check("interrupt: error explains shutdown",
              st and "shutdown" in (st["error"] or "").lower())
        time.sleep(0.3)
        check("interrupt: executor never called", ex.calls == [])
        data = json.loads(store.read_text(encoding="utf-8"))
        saved = {t["task_id"]: t for t in data["tasks"]}
        check("interrupt: failed state persisted",
              saved["abc12345"]["status"] == "failed")
    finally:
        q.stop()


def test_scheduled_and_terminal_restore(tmpdir: pathlib.Path) -> None:
    store = tmpdir / "agent_tasks.json"
    future = time.time() + 3600
    payload = {"version": 1, "tasks": [
        {"task_id": "sched1111", "goal": "later goal", "priority": 2,
         "created_at": time.time(), "status": "scheduled", "result": None,
         "error": "", "run_at": future, "repeat_every": None},
        {"task_id": "done2222", "goal": "old goal", "priority": 2,
         "created_at": time.time() - 10, "status": "completed",
         "result": "old result", "error": "", "run_at": None,
         "repeat_every": None},
    ]}
    store.write_text(json.dumps(payload), encoding="utf-8")

    ex = FakeExecutor()
    q = new_queue(store, ex, start=True)
    try:
        st = q.get_status("sched1111")
        check("restore: scheduled kept as scheduled",
              st and st["status"] == "scheduled" and st["run_at"] == future)
        check("restore: scheduled not in ready queue",
              q.pending_count() == 0)
        st2 = q.get_status("done2222")
        check("restore: terminal history loaded",
              st2 and st2["status"] == "completed")
        time.sleep(0.3)
        check("restore: nothing executed yet", ex.calls == [])
    finally:
        q.stop()


def test_history_cap(tmpdir: pathlib.Path) -> None:
    from core.agent.task_queue import Task, TaskQueue, TaskStatus

    store = tmpdir / "agent_tasks.json"
    q = TaskQueue(max_concurrent=1, executor_factory=lambda: FakeExecutor())
    q.set_store(store)
    now = time.time()
    for i in range(60):
        q._tasks[f"old{i:04d}"] = Task(
            priority=2, created_at=now - 60 + i, task_id=f"old{i:04d}",
            goal=f"task {i}", status=TaskStatus.COMPLETED, result=f"r{i}")
    q._persist()

    data = json.loads(store.read_text(encoding="utf-8"))
    check("cap: store holds at most 50 terminal tasks",
          len(data["tasks"]) == 50)
    ids = {t["task_id"] for t in data["tasks"]}
    check("cap: oldest dropped", "old0000" not in ids)
    check("cap: newest kept", "old0059" in ids)
    check("cap: queue dict pruned too", len(q._tasks) == 50)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — scheduling
# ─────────────────────────────────────────────────────────────────────────────

def test_scheduled_task_fires_when_due(tmpdir: pathlib.Path) -> None:
    store = tmpdir / "agent_tasks.json"
    ex = FakeExecutor()
    q = new_queue(store, ex, start=True)
    try:
        tid = q.submit("due soon", run_at=time.time() + 0.4)
        st = q.get_status(tid)
        check("fire: created as scheduled", st["status"] == "scheduled")
        time.sleep(0.2)
        check("fire: not run before due time", ex.calls == [])
        done = wait_until(lambda: q.get_status(tid)["status"] == "completed",
                          timeout=6.0)
        check("fire: ran once due", done)
        check("fire: executor got the goal",
              ex.calls == ["due soon"])
        check("fire: persisted as completed",
              q.get_status(tid)["status"] == "completed")
    finally:
        q.stop()


def test_scheduled_past_runs_immediately(tmpdir: pathlib.Path) -> None:
    store = tmpdir / "agent_tasks.json"
    ex = FakeExecutor()
    q = new_queue(store, ex, start=False)
    tid = q.submit("late goal", run_at=time.time() - 60)
    check("past: created pending",
          q.get_status(tid)["status"] == "pending")
    q.start()
    try:
        done = wait_until(lambda: q.get_status(tid)["status"] == "completed")
        check("past: executed", done and ex.calls == ["late goal"])
    finally:
        q.stop()


def test_repeat_rearms(tmpdir: pathlib.Path) -> None:
    store = tmpdir / "agent_tasks.json"
    ex = FakeExecutor()
    q = new_queue(store, ex, start=True)
    try:
        tid = q.submit("heartbeat", repeat_every=0.6)
        done1 = wait_until(lambda: q.get_status(tid)["status"] == "completed")
        check("repeat: first run completes", done1)
        rows = q.get_all_statuses()
        sched = [r for r in rows if r["status"] == "scheduled"]
        check("repeat: next run armed as scheduled", len(sched) == 1)
        if sched:
            check("repeat: run_at roughly one interval out",
                  sched[0]["run_at"] is not None
                  and 0.3 < sched[0]["run_at"] - time.time() < 1.5)
        done2 = wait_until(lambda: len(ex.calls) >= 2, timeout=6.0)
        check("repeat: second run executed", done2)
        check("repeat: same goal each run",
              ex.calls == ["heartbeat", "heartbeat"])
    finally:
        q.stop()


def test_cancel_scheduled(tmpdir: pathlib.Path) -> None:
    store = tmpdir / "agent_tasks.json"
    ex = FakeExecutor()
    q = new_queue(store, ex, start=True)
    try:
        tid = q.submit("never", run_at=time.time() + 0.5)
        check("cancel-sched: scheduled first",
              q.get_status(tid)["status"] == "scheduled")
        check("cancel-sched: cancel returns True", q.cancel(tid) is True)
        check("cancel-sched: status cancelled",
              q.get_status(tid)["status"] == "cancelled")
        time.sleep(0.8)
        check("cancel-sched: never executed", ex.calls == [])
        data = json.loads(store.read_text(encoding="utf-8"))
        saved = {t["task_id"]: t for t in data["tasks"]}
        check("cancel-sched: persisted",
              saved[tid]["status"] == "cancelled")
    finally:
        q.stop()


def test_cancel_running_no_rearm(tmpdir: pathlib.Path) -> None:
    store = tmpdir / "agent_tasks.json"
    block = threading.Event()
    ex = FakeExecutor(block=block)
    q = new_queue(store, ex, start=True)
    try:
        tid = q.submit("cancel me", repeat_every=0.5)
        check("cancel-run: executor started", ex.started.wait(timeout=2.0))
        check("cancel-run: cancel accepted while running",
              q.cancel(tid) is True)
        block.set()
        done = wait_until(lambda: q.get_status(tid)["status"] == "cancelled")
        check("cancel-run: marked cancelled", done)
        time.sleep(0.8)
        sched = [r for r in q.get_all_statuses()
                 if r["status"] == "scheduled"]
        check("cancel-run: no re-arm after cancel", sched == [])
    finally:
        block.set()
        q.stop()


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — run_task action parameter parsing
# ─────────────────────────────────────────────────────────────────────────────

def test_run_at_parsing() -> None:
    from actions.agent_task import _parse_run_at

    ts, err = _parse_run_at("2099-01-01T00:00")
    check("parse: ISO future parses", err is None and ts > time.time())
    ts, err = _parse_run_at("2000-01-01T00:00")
    check("parse: ISO past parses", err is None and ts < time.time())
    ts, err = _parse_run_at(" 2099-01-01T00:00 ")
    check("parse: whitespace tolerated", err is None)
    ts, err = _parse_run_at(None)
    check("parse: None is no-op", err is None and ts is None)
    ts, err = _parse_run_at("tomorrow morning")
    check("parse: garbage rejected with message",
          ts is None and err is not None and "ISO-8601" in err)


def test_repeat_parsing() -> None:
    from actions.agent_task import _parse_repeat

    sec, err = _parse_repeat("hourly")
    check("repeat: hourly", err is None and sec == 3600)
    sec, err = _parse_repeat("daily")
    check("repeat: daily", err is None and sec == 86400)
    sec, err = _parse_repeat("weekly")
    check("repeat: weekly", err is None and sec == 604800)
    sec, err = _parse_repeat(" 3600 ")
    check("repeat: numeric seconds", err is None and sec == 3600)
    sec, err = _parse_repeat("1.5")
    check("repeat: fractional seconds", err is None and sec == 1.5)
    sec, err = _parse_repeat(None)
    check("repeat: None is no-op", err is None and sec is None)
    sec, err = _parse_repeat("0")
    check("repeat: zero rejected", sec is None and err is not None)
    sec, err = _parse_repeat("-5")
    check("repeat: negative rejected", sec is None and err is not None)
    sec, err = _parse_repeat("every tuesday")
    check("repeat: garbage rejected with message",
          sec is None and err is not None)


def test_run_task_rejects_invalid_values_without_creating_tasks() -> None:
    import actions.agent_task as A

    out = A.run_task({"goal": "x", "run_at": "banana"})
    check("action: bad run_at → error, no task", "ISO-8601" in out)

    out = A.run_task({"goal": "x", "repeat_every": "banana"})
    check("action: bad repeat → error, no task",
          "repeat interval" in out)

    out = A.run_task({})
    check("action: empty goal → error", "goal" in out.lower())


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — task_status rendering
# ─────────────────────────────────────────────────────────────────────────────

def test_task_status_schedule_format() -> None:
    from actions.task_status import _fmt_schedule

    check("fmt: empty schedule is empty string",
          _fmt_schedule({}) == "")
    out = _fmt_schedule({"run_at": time.time() + 3600,
                         "repeat_every": 86400})
    check("fmt: shows time and interval",
          "at " in out and "every 86400s" in out)


def test_store_survives_corruption(tmpdir: pathlib.Path) -> None:
    store = tmpdir / "agent_tasks.json"
    store.write_text("{not json", encoding="utf-8")
    ex = FakeExecutor()
    q = new_queue(store, ex, start=True)
    try:
        tid = q.submit("fresh start")
        done = wait_until(lambda: q.get_status(tid)["status"] == "completed")
        check("corrupt: queue still works", done)
        check("corrupt: file rewritten valid",
              isinstance(json.loads(store.read_text(encoding="utf-8")), dict))
    finally:
        q.stop()


# ─────────────────────────────────────────────────────────────────────────────

def subdir(tmpdir: pathlib.Path, name: str) -> pathlib.Path:
    d = tmpdir / name
    d.mkdir()
    return d


def main() -> int:
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="jarvis_persist_"))
    try:
        test_persistence_roundtrip(subdir(tmpdir, "roundtrip"))
        test_running_becomes_failed_on_restore(subdir(tmpdir, "interrupt"))
        test_scheduled_and_terminal_restore(subdir(tmpdir, "restore"))
        test_history_cap(subdir(tmpdir, "cap"))
        test_scheduled_task_fires_when_due(subdir(tmpdir, "due"))
        test_scheduled_past_runs_immediately(subdir(tmpdir, "past"))
        test_repeat_rearms(subdir(tmpdir, "repeat"))
        test_cancel_scheduled(subdir(tmpdir, "cancel_sched"))
        test_cancel_running_no_rearm(subdir(tmpdir, "cancel_run"))
        test_run_at_parsing()
        test_repeat_parsing()
        test_run_task_rejects_invalid_values_without_creating_tasks()
        test_task_status_schedule_format()
        test_store_survives_corruption(subdir(tmpdir, "corrupt"))
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
