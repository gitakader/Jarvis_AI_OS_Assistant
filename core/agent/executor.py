"""Run a plan step by step, with timeouts, retries, and replan recovery.

Ported from Brahma's agent/executor but reworked for JARVIS:

- Tool dispatch is INJECTED (run_tool) — main.py wires its registry path in
  track 3, so this module never imports the session or the UI.
- run_tool contract: sync, returns str, RAISES on failure. (ActionRegistry.run
  never raises, so the track-3 wrapper translates its failure strings.)
- Safety limits are code-enforced, not prompt-enforced: MAX_STEPS cap,
  per-step timeout, whole-task deadline, MAX_REPLANS, cancel checks between
  every step/attempt.
- Cross-step data flows via {step_N} placeholders substituted before each run.
- A step that exceeds its timeout is abandoned: the tool runs in a daemon
  thread, so the executor proceeds while the thread finishes in background.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Optional

from core.agent.error_handler import (
    ErrorDecision,
    _DECISION_MAP,
    analyze_error as _analyze_error,
)
from core.agent.planner import MAX_STEPS, create_plan as _create_plan, replan as _replan

MAX_ATTEMPTS_PER_STEP = 3
STEP_TIMEOUT_SECONDS  = 120
TASK_TIMEOUT_SECONDS  = 600
MAX_REPLANS           = 2

_PLACEHOLDER_RE = re.compile(r"\{step_(\d+)\}")

_CANCEL_MSG = "Task cancelled."


def _substitute(value: Any, step_results: dict) -> Any:
    """Replace {step_N} in string parameters with earlier step results."""
    if isinstance(value, str):
        def _rep(m: re.Match) -> str:
            return str(step_results.get(int(m.group(1)), ""))
        return _PLACEHOLDER_RE.sub(_rep, value)
    if isinstance(value, dict):
        return {k: _substitute(v, step_results) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, step_results) for v in value]
    return value


def _call_with_timeout(fn: Callable[[], str], timeout: float,
                       cancel_flag: Optional[threading.Event]) -> tuple[bool, str]:
    """Run fn in a daemon thread. Returns (ok, result_or_error).

    A timeout abandons the thread (it finishes in background); a set
    cancel_flag abandons it the same way and reports cancellation."""
    box: dict[str, str] = {}

    def target() -> None:
        try:
            box["result"] = str(fn())
        except Exception as e:
            box["error"] = str(e)

    thread = threading.Thread(target=target, daemon=True, name="AgentStep")
    thread.start()
    deadline = time.monotonic() + timeout
    while thread.is_alive():
        if cancel_flag is not None and cancel_flag.is_set():
            return False, _CANCEL_MSG
        if time.monotonic() >= deadline:
            return False, f"Step timed out after {timeout:g}s (tool still finishing in background)"
        time.sleep(0.1)
    if "error" in box:
        return False, box["error"]
    return True, box.get("result", "Done.")


def _summarize(goal: str, completed_steps: list[dict],
               speak: Callable | None = None) -> str:
    """One-sentence wrap-up through the FAST tier; honest fallback otherwise."""
    fallback = f"All done, sir. Completed {len(completed_steps)} steps for: {goal[:60]}."
    try:
        from core import gemini
        steps_str = "\n".join(f"- {s.get('description', '')}" for s in completed_steps)
        prompt = (
            f'User goal: "{goal}"\n'
            f"Completed steps:\n{steps_str}\n\n"
            "Write a single natural sentence summarizing what was accomplished. "
            "Address the user as 'sir'. Be direct and positive."
        )
        raw = gemini.text(prompt, tier=gemini.FAST)
        if raw:
            summary = raw.strip()
            if speak:
                speak(summary)
            return summary
    except Exception as e:
        print(f"[Executor] summary failed: {e}")
    if speak:
        speak(fallback)
    return fallback


class AgentExecutor:
    def __init__(
        self,
        run_tool: Optional[Callable[[str, dict], str]] = None,
        planner: Optional[Callable[[str, str], dict]] = None,
        replanner: Optional[Callable[..., dict]] = None,
        analyzer: Optional[Callable[..., dict]] = None,
        summarizer: Optional[Callable[..., str]] = None,
        tool_catalog: Optional[list[dict]] = None,
        step_timeout: Optional[float] = None,
        task_timeout: Optional[float] = None,
    ):
        self._run_tool = run_tool
        self._planner = planner
        self._replanner = replanner
        self._analyzer = analyzer or _analyze_error
        self._summarizer = summarizer or _summarize
        self._tool_catalog = tool_catalog or []
        self._step_timeout = step_timeout or STEP_TIMEOUT_SECONDS
        self._task_timeout = task_timeout or TASK_TIMEOUT_SECONDS

    def _plan(self, goal: str, context: str) -> dict:
        if self._planner is not None:
            return self._planner(goal, context)
        return _create_plan(goal, context, self._tool_catalog)

    def _do_replan(self, goal: str, completed: list[dict], failed: dict,
                   error: str) -> dict:
        if self._replanner is not None:
            return self._replanner(goal, completed, failed, error)
        return _replan(goal, completed, failed, error, self._tool_catalog)

    def execute(self, goal: str, speak: Callable | None = None,
                cancel_flag: Optional[threading.Event] = None,
                player: Any = None, context: str = "") -> str:
        """Run the whole task. Returns the final summary/status sentence."""
        run_tool = self._run_tool
        if run_tool is None:
            msg = "Agent executor has no tool dispatcher wired, sir."
            if speak:
                speak(msg)
            return msg

        def _cancelled() -> bool:
            return cancel_flag is not None and cancel_flag.is_set()

        started = time.monotonic()
        replan_count = 0
        completed_steps: list[dict] = []
        step_results: dict = {}

        print(f"[Executor] goal: {goal[:80]}")
        plan = self._plan(goal, context)

        while True:
            steps = (plan.get("steps") or [])[:MAX_STEPS]
            if not steps:
                msg = "I couldn't create a valid plan for this task, sir."
                if speak:
                    speak(msg)
                return msg

            success = True
            failed_step: Optional[dict] = None
            failed_error = ""
            last_error = ""

            for step in steps:
                if _cancelled():
                    if speak:
                        speak(_CANCEL_MSG)
                    return _CANCEL_MSG
                if time.monotonic() - started > self._task_timeout:
                    msg = "Task ran out of time, sir. Stopping before the deadline."
                    if speak:
                        speak(msg)
                    return msg

                step_num = step.get("step")
                tool = step.get("tool") or ""
                desc = str(step.get("description") or "")
                params = _substitute(step.get("parameters") or {}, step_results)

                print(f"[Executor] Step {step_num}: [{tool}] {desc[:80]}")
                if speak:
                    speak(f"Step {step_num}: {desc}")

                step_ok = False
                for attempt in range(1, MAX_ATTEMPTS_PER_STEP + 1):
                    if _cancelled():
                        break
                    ok, result = _call_with_timeout(
                        lambda: run_tool(tool, params),
                        self._step_timeout, cancel_flag)
                    if ok:
                        step_results[step_num] = result
                        completed_steps.append(step)
                        print(f"[Executor] Step {step_num} done: {result[:100]}")
                        step_ok = True
                        break

                    last_error = result
                    if result == _CANCEL_MSG:
                        break
                    print(f"[Executor] Step {step_num} attempt {attempt} failed: {last_error[:120]}")

                    recovery = self._analyzer(
                        step, last_error, attempt=attempt,
                        max_attempts=MAX_ATTEMPTS_PER_STEP)
                    decision = recovery.get("decision", ErrorDecision.REPLAN)
                    if isinstance(decision, str):
                        # Injected analyzers (tests, custom handlers) may return
                        # plain strings; the real one returns the enum.
                        decision = _DECISION_MAP.get(decision, ErrorDecision.REPLAN)
                    user_msg = str(recovery.get("user_message") or "")
                    if speak and user_msg:
                        speak(user_msg)

                    if decision == ErrorDecision.RETRY and attempt < MAX_ATTEMPTS_PER_STEP:
                        time.sleep(1)
                        continue
                    if decision == ErrorDecision.SKIP:
                        print(f"[Executor] skipping step {step_num}")
                        completed_steps.append(step)
                        step_ok = True
                        break
                    if decision == ErrorDecision.ABORT:
                        msg = f"Task aborted, sir. {recovery.get('reason', '')}".strip()
                        if speak:
                            speak(msg)
                        return msg
                    # REPLAN (or retries exhausted): fail this step and recover
                    # at the plan level.
                    failed_step = step
                    failed_error = last_error
                    success = False
                    break

                if not step_ok:
                    if last_error == _CANCEL_MSG or _cancelled():
                        if speak:
                            speak(_CANCEL_MSG)
                        return _CANCEL_MSG
                    if failed_step is None:
                        failed_step = step
                        failed_error = last_error or "Step failed"
                    success = False
                    break

            if success:
                return self._summarizer(goal, completed_steps, speak)

            if replan_count >= MAX_REPLANS:
                msg = (f"Task failed after {MAX_REPLANS} replan attempts, sir. "
                       f"Last error: {failed_error[:120]}")
                if speak:
                    speak(msg)
                return msg

            replan_count += 1
            if speak:
                speak("Adjusting my approach, sir.")
            plan = self._do_replan(goal, completed_steps,
                                   failed_step or {}, failed_error)
