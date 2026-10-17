"""
Offline tests for the agent wiring: queue set_executor, the run_task and
task_status actions, and their action discovery records.

Headless and deterministic: fake players, fake executors set onto the
process-local queue singleton (a separate process from any live JARVIS),
no LLM, no UI, nothing written to disk.

Run:  venv/Scripts/python.exe selfheal_tests/test_agent_wiring.py
"""
from __future__ import annotations

import pathlib
import sys
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


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — queue set_executor + run_task/task_status actions
# ─────────────────────────────────────────────────────────────────────────────

from core.agent.task_queue import TaskQueue, get_queue
from actions.agent_task import run_task
from actions.task_status import task_status


class FakePlayer:
    def __init__(self):
        self.log: list[str] = []

    def write_log(self, msg: str) -> None:
        self.log.append(msg)


class WireExec:
    """Records goals and the speak callbacks it was handed."""

    def __init__(self):
        self.goals: list[str] = []
        self.spoke: list[str] = []

    def execute(self, goal="", speak=None, cancel_flag=None, player=None):
        self.goals.append(goal)
        if speak is not None:
            speak(f"progress: {goal}")
            self.spoke.append(goal)
        return f"finished: {goal}"


class BlockExec:
    """Blocks until its cancel flag is set — models a long-running task."""

    def __init__(self):
        self.started: list[str] = []
        self.cancelled: list[bool] = []

    def execute(self, goal="", speak=None, cancel_flag=None, player=None):
        self.started.append(goal)
        if cancel_flag is not None:
            cancel_flag.wait(timeout=5.0)
            self.cancelled.append(cancel_flag.is_set())
        return "done"


def section1() -> None:
    print("SECTION 1 — wiring")

    q = TaskQueue()
    fake = WireExec()
    q.set_executor(fake)
    check("1.1 set_executor takes effect", q._get_executor() is fake)

    player = FakePlayer()
    out = run_task({"goal": "   "}, player=player, speak=None)
    check("1.2 empty goal refused", "need a goal" in out)

    gq = get_queue()
    wire = WireExec()
    gq.set_executor(wire)

    out = run_task({"goal": "research topic"}, player=player,
                   speak=lambda t: wire.spoke.append(t))
    check("1.3 submit announces task id",
          "running in the background" in out and "Task " in out)
    task_id = out.split()[1]
    check("1.4 task executed in background",
          wait_until(lambda: wire.goals == ["research topic"]))
    check("1.5 executor receives speak callback",
          wait_until(lambda: len(wire.spoke) >= 1)
          and any("progress" in s or s == "research topic" for s in wire.spoke))
    check("1.6 start logged to player",
          wait_until(lambda: any("started" in m for m in player.log)))
    check("1.7 completion logged to player",
          wait_until(lambda: any("finished" in m for m in player.log)))
    check("1.8 status shows completed",
          wait_until(lambda: task_status(
              {"action": "status", "task_id": task_id}).startswith(
              f"Task {task_id}: completed")))

    out = task_status({"action": "list"})
    check("1.9 list includes the task", task_id in out and "completed" in out)

    out = task_status({"action": "status", "task_id": "zzzzzzzz"})
    check("1.10 unknown id reported", "No task" in out)

    out = task_status({"action": "status", "task_id": ""})
    check("1.11 missing id asks for one", "task id" in out)

    blocker = BlockExec()
    gq.set_executor(blocker)
    out = run_task({"goal": "long job"}, player=player, speak=None)
    tid = out.split()[1]
    check("1.12 running task picked up", wait_until(lambda: blocker.started == ["long job"]))
    out = task_status({"action": "cancel", "task_id": tid})
    check("1.13 cancel reports success", "cancelled" in out)
    check("1.14 runner observed the cancel flag",
          wait_until(lambda: blocker.cancelled == [True]))
    check("1.15 status ends cancelled",
          wait_until(lambda: task_status(
              {"action": "status", "task_id": tid}).startswith(
              f"Task {tid}: cancelled")))
    out = task_status({"action": "cancel", "task_id": tid})
    check("1.16 cancelling a terminal task says so", "already" in out)

    gq.set_executor(WireExec())  # restore a harmless executor


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — action discovery records
# ─────────────────────────────────────────────────────────────────────────────

from core.action_loader import discover_actions


def section2() -> None:
    print("SECTION 2 — discovery")

    reg = discover_actions(BASE / "actions", reserved_names=set())
    recs = {r.name: r for r in reg._all_records}

    check("2.1 run_task discovered", "run_task" in recs and recs["run_task"].valid,
          recs.get("run_task").error if recs.get("run_task") else "missing")
    check("2.2 task_status discovered",
          "task_status" in recs and recs["task_status"].valid,
          recs.get("task_status").error if recs.get("task_status") else "missing")

    rt = recs.get("run_task")
    if rt is not None and rt.valid:
        check("2.3 run_task is NON_BLOCKING + SILENT",
              rt.behavior == "NON_BLOCKING" and rt.scheduling == "SILENT",
              f"{rt.behavior}/{rt.scheduling}")
    ts = recs.get("task_status")
    if ts is not None and ts.valid:
        check("2.4 task_status is default blocking",
              ts.behavior is None and ts.scheduling is None,
              f"{ts.behavior}/{ts.scheduling}")


def main() -> None:
    section1()
    section2()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
