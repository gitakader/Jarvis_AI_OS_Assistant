"""
Offline tests for the core/agent stack (planner, error_handler, task_queue,
executor).

Everything is headless and deterministic: fake LLM callables, fake tool
dispatchers, fake executors, and injectable timeouts — no real Gemini call,
no registry, no UI, nothing written to disk.

Run:  venv/Scripts/python.exe selfheal_tests/test_agent_stack.py
"""
from __future__ import annotations

import copy
import pathlib
import sys
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


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — planner
# ─────────────────────────────────────────────────────────────────────────────

from core.agent import planner as P

CATALOG = [
    {"name": "web_search", "description": "Search the web.",
     "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING"}}}},
    {"name": "weather_report", "description": "Get weather for a city.",
     "parameters": {"type": "OBJECT", "properties": {"city": {"type": "STRING"}}}},
]

GOOD_PLAN = {
    "goal": "test goal",
    "steps": [
        {"step": 1, "tool": "web_search", "description": "search it",
         "parameters": {"query": "x"}, "critical": True},
        {"step": 2, "tool": "weather_report", "description": "weather",
         "parameters": {"city": "Dhaka"}, "critical": False},
    ],
}


def section1() -> None:
    print("SECTION 1 — planner")

    prompt = P.build_planner_prompt(CATALOG)
    check("1.1 prompt lists every tool", all(t["name"] in prompt for t in CATALOG))
    check("1.2 prompt states the step cap", str(P.MAX_STEPS) in prompt)

    check("1.3 _parse_json strips fences",
          P._parse_json("```json\n{\"a\": 1}\n```") == {"a": 1})
    check("1.4 _parse_json tolerates prose",
          P._parse_json("here you go: {\"a\": 1} thanks") == {"a": 1})
    check("1.5 _parse_json rejects non-object", P._parse_json("[1, 2]") is None)
    check("1.6 _parse_json rejects garbage", P._parse_json("no json here") is None)

    plan = P.create_plan("test goal", tool_catalog=CATALOG,
                         llm_json=lambda s, u: dict(GOOD_PLAN))
    check("1.7 create_plan parses", plan["goal"] == "test goal" and len(plan["steps"]) == 2)
    check("1.8 steps renumbered in order",
          [s["step"] for s in plan["steps"]] == [1, 2])

    big = {"goal": "g", "steps": [
        {"step": i, "tool": "web_search", "description": f"s{i}",
         "parameters": {}, "critical": False} for i in range(1, 9)]}
    plan = P.create_plan("g", tool_catalog=CATALOG, llm_json=lambda s, u: dict(big))
    check("1.9 plan capped to MAX_STEPS", len(plan["steps"]) == P.MAX_STEPS)
    check("1.10 cap keeps the first steps",
          plan["steps"][0]["description"] == "s1"
          and plan["steps"][-1]["description"] == f"s{P.MAX_STEPS}")

    gen = {"goal": "g", "steps": [
        {"step": 1, "tool": "generated_code", "description": "make me code",
         "parameters": {}, "critical": True}]}
    plan = P.create_plan("g", tool_catalog=CATALOG,
                         llm_json=lambda s, u: copy.deepcopy(gen))
    check("1.11 generated_code rewritten to web_search",
          plan["steps"][0]["tool"] == "web_search")
    plan = P.create_plan("g", tool_catalog=[CATALOG[1]],
                         llm_json=lambda s, u: copy.deepcopy(gen))
    check("1.12 generated_code left alone without web_search",
          plan["steps"][0]["tool"] == "generated_code")

    plan = P.create_plan("g", tool_catalog=CATALOG, llm_json=lambda s, u: None)
    check("1.13 LLM None → web_search fallback",
          plan["steps"][0]["tool"] == "web_search")
    plan = P.create_plan("g", tool_catalog=CATALOG, llm_json=lambda s, u: {"steps": []})
    check("1.14 empty steps → fallback", plan["steps"][0]["tool"] == "web_search")
    plan = P.create_plan("g", tool_catalog=CATALOG,
                         llm_json=lambda s, u: (_ for _ in ()).throw(RuntimeError("boom")))
    check("1.15 LLM crash → fallback", plan["steps"][0]["tool"] == "web_search")
    plan = P.fallback_plan("g", {"weather_report"})
    check("1.16 fallback without web_search has no steps", plan["steps"] == [])

    captured = {}

    def fake_replan_llm(system, user):
        captured["system"] = system
        captured["user"] = user
        return dict(GOOD_PLAN)

    plan = P.replan("test goal", [GOOD_PLAN["steps"][0]], GOOD_PLAN["steps"][1],
                    "it broke", tool_catalog=CATALOG, llm_json=fake_replan_llm)
    check("1.17 replan returns a plan", len(plan["steps"]) == 2)
    check("1.18 replan prompt mentions completed steps",
          "web_search" in captured["user"] and "DONE" in captured["user"])
    check("1.19 replan prompt carries the error", "it broke" in captured["user"])

    seen_ctx = {}

    def fake_ctx_llm(system, user):
        seen_ctx["user"] = user
        return dict(GOOD_PLAN)

    P.create_plan("the goal", context="extra context", tool_catalog=CATALOG,
                  llm_json=fake_ctx_llm)
    check("1.20 context reaches the LLM", "extra context" in seen_ctx["user"])


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — error_handler
# ─────────────────────────────────────────────────────────────────────────────

from core.agent.error_handler import ErrorDecision, analyze_error

STEP = {"step": 1, "tool": "web_search", "description": "search",
        "parameters": {"query": "x"}, "critical": False}


def _dec(decision: str, **extra):
    base = {"decision": decision, "reason": "why", "fix_suggestion": "try x",
            "max_retries": 1, "user_message": "msg"}
    base.update(extra)
    return base


def section2() -> None:
    print("SECTION 2 — error_handler")

    r = analyze_error(STEP, "network timeout", attempt=1, max_attempts=3,
                      llm_json=lambda s, u: _dec("retry"))
    check("2.1 retry decision", r["decision"] == ErrorDecision.RETRY)

    r = analyze_error(STEP, "not needed", attempt=1, max_attempts=3,
                      llm_json=lambda s, u: _dec("skip"))
    check("2.2 skip decision", r["decision"] == ErrorDecision.SKIP)

    r = analyze_error(STEP, "unsafe", attempt=1, max_attempts=3,
                      llm_json=lambda s, u: _dec("abort"))
    check("2.3 abort decision", r["decision"] == ErrorDecision.ABORT)

    called = []

    def boom(s, u):
        called.append(True)
        raise RuntimeError("LLM down")

    r = analyze_error(STEP, "x", attempt=3, max_attempts=3, llm_json=boom)
    check("2.4 max attempts forces replan without LLM",
          r["decision"] == ErrorDecision.REPLAN and not called)

    crit = dict(STEP, critical=True)
    r = analyze_error(crit, "x", attempt=1, max_attempts=3,
                      llm_json=lambda s, u: _dec("skip"))
    check("2.5 critical step cannot be skipped",
          r["decision"] == ErrorDecision.REPLAN and "critical" in r["user_message"])

    r = analyze_error(STEP, "x", attempt=1, max_attempts=3, llm_json=boom)
    check("2.6 LLM crash defaults to replan", r["decision"] == ErrorDecision.REPLAN)

    r = analyze_error(STEP, "x", attempt=1, max_attempts=3,
                      llm_json=lambda s, u: _dec("banana"))
    check("2.7 unknown decision defaults to replan",
          r["decision"] == ErrorDecision.REPLAN)

    r = analyze_error(STEP, "x", attempt=1, max_attempts=3,
                      llm_json=lambda s, u: None)
    check("2.8 LLM None defaults to replan", r["decision"] == ErrorDecision.REPLAN)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — task_queue
# ─────────────────────────────────────────────────────────────────────────────

from core.agent.task_queue import TaskPriority, TaskQueue, TaskStatus, get_queue


class FakeExec:
    def __init__(self, delay: float = 0.0, fail: bool = False,
                 wait_flag: bool = False):
        self.delay = delay
        self.fail = fail
        self.wait_flag = wait_flag
        self.goals: list[str] = []
        self.cancel_seen: list[bool] = []

    def execute(self, goal="", speak=None, cancel_flag=None, player=None):
        self.goals.append(goal)
        if self.wait_flag and cancel_flag is not None:
            cancel_flag.wait(timeout=5.0)
            self.cancel_seen.append(cancel_flag.is_set())
        elif self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise RuntimeError("fake failure")
        return f"done: {goal}"


def section3() -> None:
    print("SECTION 3 — task_queue")

    fake = FakeExec()
    q = TaskQueue(executor_factory=lambda: fake)
    tid = q.submit("hello goal")
    check("3.1 submit returns id and queues", tid and q.pending_count() == 1)
    q.start()
    check("3.2 task completes", wait_until(lambda: q.get_status(tid)["status"] == "completed"))
    st = q.get_status(tid)
    check("3.3 result recorded", st["result"] == "done: hello goal")

    done = {}
    tid2 = q.submit("cb goal", on_complete=lambda t, r: done.update({t: r}))
    check("3.4 on_complete fired", wait_until(lambda: tid2 in done)
          and wait_until(lambda: done.get(tid2) == "done: cb goal"))

    fake.fail = True
    tid3 = q.submit("bad goal")
    check("3.5 failing task marked FAILED",
          wait_until(lambda: q.get_status(tid3)["status"] == "failed"))
    check("3.6 failure recorded", "fake failure" in q.get_status(tid3)["error"])
    fake.fail = False

    check("3.7 unknown id → None", q.get_status("nope") is None)
    check("3.8 statuses listed", any(s["task_id"] == tid for s in q.get_all_statuses()))

    cancelled_runs = []

    def slow_exec_factory():
        e = FakeExec(delay=0.0)
        cancelled_runs.append(e)
        return e

    q2 = TaskQueue(executor_factory=slow_exec_factory)
    q2.start()
    tid4 = q2.submit("pending cancel")
    check("3.9 cancel pending works", q2.cancel(tid4))
    check("3.10 pending cancel → CANCELLED",
          q2.get_status(tid4)["status"] == "cancelled"
          and q2.pending_count() == 0)
    tid4b = q2.submit("runs instead")
    check("3.11 cancelled task never runs",
          wait_until(lambda: q2.get_status(tid4b)["status"] == "completed")
          and "pending cancel" not in cancelled_runs[-1].goals)

    blocker = FakeExec(wait_flag=True)
    q3 = TaskQueue(executor_factory=lambda: blocker)
    q3.start()
    tid5 = q3.submit("running cancel")
    check("3.12 running task picked up", wait_until(lambda: blocker.goals))
    check("3.13 cancel running returns True", q3.cancel(tid5))
    check("3.14 running cancel ends CANCELLED",
          wait_until(lambda: q3.get_status(tid5)["status"] == "cancelled"))
    check("3.15 runner observed the flag", blocker.cancel_seen == [True])
    check("3.16 cancel terminal task → False", not q3.cancel(tid5))

    order = []

    def order_exec_factory():
        e = FakeExec(delay=0.05)
        e.goals = order
        return e

    q4 = TaskQueue(executor_factory=order_exec_factory)
    q4.submit("low first", priority=TaskPriority.LOW)
    q4.submit("high second", priority=TaskPriority.HIGH)
    q4.start()
    check("3.17 priority beats FIFO",
          wait_until(lambda: len(order) == 2) and order == ["high second", "low first"])

    q.stop(); q2.stop(); q3.stop(); q4.stop()
    check("3.18 queue singleton", get_queue() is get_queue())
    gq = get_queue()
    gtid = gq.submit("singleton goal")
    check("3.19 singleton auto-starts",
          wait_until(lambda: gq.get_status(gtid)["status"] == "completed"))


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — executor
# ─────────────────────────────────────────────────────────────────────────────

from core.agent.executor import (AgentExecutor, MAX_ATTEMPTS_PER_STEP,
                                 MAX_REPLANS)

TWO_STEPS = {
    "goal": "g",
    "steps": [
        {"step": 1, "tool": "web_search", "description": "search",
         "parameters": {"query": "x"}, "critical": True},
        {"step": 2, "tool": "file_controller", "description": "save",
         "parameters": {"action": "write", "content": "notes from {step_1}"},
         "critical": False},
    ],
}

TWO_STEPS_NONCRIT = {
    "goal": "g",
    "steps": [
        {"step": 1, "tool": "web_search", "description": "search",
         "parameters": {"query": "x"}, "critical": False},
        {"step": 2, "tool": "file_controller", "description": "save",
         "parameters": {"action": "write"}, "critical": False},
    ],
}

# Hermetic replanner for every test that is not about replanning itself —
# guarantees no test can ever fall through to the real LLM ladder.
HERMETIC_REPLANNER = lambda g, c, f, e: {"goal": g, "steps": []}


def section4() -> None:
    print("SECTION 4 — executor")

    spoken: list[str] = []
    calls: list[tuple] = []

    def rt(tool, params):
        calls.append((tool, dict(params)))
        return {"web_search": "SEARCH RESULT", "file_controller": "saved"}[tool]

    ex = AgentExecutor(run_tool=rt, planner=lambda g, c: dict(TWO_STEPS),
                       analyzer=lambda *a, **k: _dec("retry"),
                       summarizer=lambda g, s, sp: "summary!", )
    out = ex.execute("g", speak=spoken.append)
    check("4.1 happy path returns summary", out == "summary!")
    check("4.2 both steps ran in order",
          [c[0] for c in calls] == ["web_search", "file_controller"])
    check("4.3 {step_1} substituted with prior result",
          calls[1][1]["content"] == "notes from SEARCH RESULT")
    check("4.4 progress spoken", spoken and any("Step 1" in s for s in spoken))

    flaky = {"calls": 0}

    def rt_flaky(tool, params):
        flaky["calls"] += 1
        if flaky["calls"] == 1:
            raise RuntimeError("transient timeout")
        return "ok"

    errs: list[str] = []

    def analyzer_flaky(step, error, attempt=None, max_attempts=None):
        errs.append(error)
        return _dec("retry")

    ex = AgentExecutor(run_tool=rt_flaky,
                       planner=lambda g, c: {"goal": "g", "steps": [
                           {"step": 1, "tool": "web_search", "description": "s",
                            "parameters": {}, "critical": False}]},
                       analyzer=analyzer_flaky,
                       replanner=HERMETIC_REPLANNER,
                       summarizer=lambda g, s, sp: "recovered")
    out = ex.execute("g")
    check("4.5 retry-then-success", out == "recovered" and flaky["calls"] == 2)
    check("4.6 analyzer saw the real error", errs and "transient" in errs[0])

    ex = AgentExecutor(run_tool=lambda t, p: (_ for _ in ()).throw(RuntimeError("boom")),
                       planner=lambda g, c: copy.deepcopy(TWO_STEPS_NONCRIT),
                       analyzer=lambda *a, **k: _dec("skip"),
                       replanner=HERMETIC_REPLANNER,
                       summarizer=lambda g, s, sp: f"done {len(s)}")
    out = ex.execute("g")
    check("4.7 skip keeps the task alive", out == "done 2")

    ex = AgentExecutor(run_tool=lambda t, p: (_ for _ in ()).throw(RuntimeError("unsafe")),
                       planner=lambda g, c: copy.deepcopy(TWO_STEPS),
                       analyzer=lambda *a, **k: _dec("abort", reason="cannot do"),
                       replanner=HERMETIC_REPLANNER,
                       summarizer=lambda g, s, sp: "unreachable")
    out = ex.execute("g")
    check("4.8 abort short-circuits", out.startswith("Task aborted"))

    replanner_calls: list[dict] = []
    attempts = {"n": 0}

    def rt_replan(tool, params):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("wrong approach")
        return "better result"

    def replanner(goal, completed, failed, error):
        replanner_calls.append({"completed": completed, "failed": failed, "error": error})
        return {"goal": goal, "steps": [
            {"step": 1, "tool": "web_search", "description": "alt approach",
             "parameters": {}, "critical": False}]}

    ex = AgentExecutor(run_tool=rt_replan,
                       planner=lambda g, c: {"goal": "g", "steps": [
                           {"step": 1, "tool": "web_search", "description": "bad plan",
                            "parameters": {}, "critical": False}]},
                       analyzer=lambda *a, **k: _dec("replan"),
                       replanner=replanner,
                       summarizer=lambda g, s, sp: "plan B worked")
    out = ex.execute("g")
    check("4.9 replan recovers", out == "plan B worked")
    check("4.10 replanner got the failed step",
          replanner_calls and replanner_calls[0]["failed"]["description"] == "bad plan"
          and "wrong approach" in replanner_calls[0]["error"])

    replan_count = {"n": 0}

    def replanner_always_fail(goal, completed, failed, error):
        replan_count["n"] += 1
        return {"goal": goal, "steps": [
            {"step": 1, "tool": "web_search", "description": "still bad",
             "parameters": {}, "critical": False}]}

    ex = AgentExecutor(run_tool=lambda t, p: (_ for _ in ()).throw(RuntimeError("nope")),
                       planner=lambda g, c: {"goal": "g", "steps": [
                           {"step": 1, "tool": "web_search", "description": "bad",
                            "parameters": {}, "critical": False}]},
                       analyzer=lambda *a, **k: _dec("replan"),
                       replanner=replanner_always_fail,
                       summarizer=lambda g, s, sp: "unreachable")
    out = ex.execute("g")
    check("4.11 replan budget exhausts", "replan attempts" in out
          and replan_count["n"] == MAX_REPLANS)

    flag = threading.Event()

    def rt_cancel_after_first(tool, params):
        if tool == "web_search":
            flag.set()
            return "r"
        return "should not run"

    ex = AgentExecutor(run_tool=rt_cancel_after_first,
                       planner=lambda g, c: copy.deepcopy(TWO_STEPS),
                       analyzer=lambda *a, **k: _dec("retry"),
                       replanner=HERMETIC_REPLANNER,
                       summarizer=lambda g, s, sp: "unreachable")
    out = ex.execute("g", cancel_flag=flag)
    check("4.12 cancel between steps", out == "Task cancelled.")

    blocking_flag = threading.Event()
    cancel_flag = threading.Event()

    def rt_blocking(tool, params):
        blocking_flag.set()
        time.sleep(5)
        return "late"

    ex = AgentExecutor(run_tool=rt_blocking,
                       planner=lambda g, c: copy.deepcopy(TWO_STEPS),
                       analyzer=lambda *a, **k: _dec("retry"),
                       replanner=HERMETIC_REPLANNER,
                       summarizer=lambda g, s, sp: "unreachable",
                       step_timeout=10.0)
    done = {}

    def runner():
        done["out"] = ex.execute("g", cancel_flag=cancel_flag)

    t = threading.Thread(target=runner, daemon=True)
    t.start()
    check("4.13 tool started", wait_until(blocking_flag.is_set))
    cancel_flag.set()
    check("4.14 cancel while running abandons the step",
          wait_until(lambda: "out" in done, timeout=3.0) and done["out"] == "Task cancelled.")

    timeout_errs: list[str] = []

    def analyzer_timeout(step, error, attempt=None, max_attempts=None):
        timeout_errs.append(error)
        return _dec("abort", reason="too slow")

    ex = AgentExecutor(run_tool=lambda t, p: time.sleep(1.0),
                       planner=lambda g, c: {"goal": "g", "steps": [
                           {"step": 1, "tool": "web_search", "description": "slow",
                            "parameters": {}, "critical": False}]},
                       analyzer=analyzer_timeout,
                       replanner=HERMETIC_REPLANNER,
                       summarizer=lambda g, s, sp: "unreachable",
                       step_timeout=0.2)
    out = ex.execute("g")
    check("4.15 step timeout treated as failure",
          out.startswith("Task aborted") and any("timed out" in e for e in timeout_errs))

    ex = AgentExecutor(run_tool=None,
                       planner=lambda g, c: dict(TWO_STEPS),
                       summarizer=lambda g, s, sp: "unreachable")
    out = ex.execute("g")
    check("4.16 no dispatcher wired → honest message", "dispatcher" in out)

    ex = AgentExecutor(run_tool=rt,
                       planner=lambda g, c: {"goal": "g", "steps": []},
                       summarizer=lambda g, s, sp: "unreachable")
    out = ex.execute("g")
    check("4.17 empty plan → honest message", "valid plan" in out)

    ran = []

    def rt_slow(tool, params):
        ran.append(tool)
        time.sleep(0.25)
        return "ok"

    big_plan = {"goal": "g", "steps": [
        {"step": i, "tool": "web_search", "description": f"s{i}",
         "parameters": {}, "critical": False} for i in range(1, 9)]}
    ex = AgentExecutor(run_tool=rt_slow,
                       planner=lambda g, c: dict(big_plan),
                       analyzer=lambda *a, **k: _dec("retry"),
                       summarizer=lambda g, s, sp: "capped",
                       task_timeout=0.3, step_timeout=1.0)
    out = ex.execute("g")
    check("4.18 task deadline stops a long plan",
          "ran out of time" in out and len(ran) < 8)

    ex = AgentExecutor(run_tool=rt,
                       planner=lambda g, c: dict(TWO_STEPS),
                       analyzer=lambda *a, **k: _dec("retry"),
                       summarizer=lambda g, s, sp: "capped")
    out = ex.execute("g")
    check("4.19 MAX_ATTEMPTS_PER_STEP exported", MAX_ATTEMPTS_PER_STEP == 3)


def main() -> None:
    section1()
    section2()
    section3()
    section4()
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    if FAILURES:
        print("Failures:")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)


if __name__ == "__main__":
    main()
