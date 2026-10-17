"""run_task — delegate a goal to the background agent stack.

Planner → executor → queue: the goal is planned against the live tool
catalog and executed step by step in the queue's worker thread, with
progress and the final summary spoken as they happen. NON_BLOCKING +
SILENT, the same pattern as skill_forge: the turn is not held up and the
result does not trigger a model reply (the executor already announced).

run_at (ISO-8601, local time if no offset) schedules the task for later;
repeat_every ('hourly' | 'daily' | 'weekly' | seconds) re-arms it after
each run. Invalid values create no task and return a clear error.
"""
from __future__ import annotations

import time
from datetime import datetime

from core.agent.task_queue import TaskPriority, get_queue

_PRIORITY = {
    "high":   TaskPriority.HIGH,
    "normal": TaskPriority.NORMAL,
    "low":    TaskPriority.LOW,
}

_REPEAT_UNITS = {
    "hourly": 3600.0,
    "daily":  86400.0,
    "weekly": 604800.0,
}


def _make_on_complete(player):
    """Log the finished task; voice is the executor's job (it already
    speaks progress and the summary), so this never double-announces."""
    def on_complete(task_id: str, result) -> None:
        try:
            text = str(result or "")
            if len(text) > 600:
                text = text[:600] + "..."
            if player is not None and hasattr(player, "write_log"):
                player.write_log(f"SYS: Background task {task_id} finished.\n{text}")
        except Exception as e:
            print(f"[AgentTask] on_complete log failed: {e}")
    return on_complete


def _parse_run_at(value) -> tuple[float | None, str | None]:
    """ISO-8601 date/time → epoch seconds, or (None, error message)."""
    if value is None:
        return None, None
    text = str(value).strip().strip("\"'")
    if not text:
        return None, None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None, (
            f"I could not understand '{text}' as a time, sir. "
            f"Please use ISO-8601 like '2026-10-15T09:00' (local time) "
            f"or '2026-10-15T09:00+06:00'."
        )
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt.timestamp(), None


def _parse_repeat(value) -> tuple[float | None, str | None]:
    """'hourly'/'daily'/'weekly' or seconds → float, or (None, error)."""
    if value is None:
        return None, None
    text = str(value).strip().lower().strip("\"'")
    if not text:
        return None, None
    if text in _REPEAT_UNITS:
        return _REPEAT_UNITS[text], None
    try:
        seconds = float(text)
    except ValueError:
        return None, (
            f"I could not understand '{value}' as a repeat interval, sir. "
            f"Use 'hourly', 'daily', 'weekly', or a number of seconds."
        )
    if seconds <= 0:
        return None, "The repeat interval must be positive, sir."
    return seconds, None


def run_task(parameters: dict, player=None, speak=None, **_kw) -> str:
    goal = str(parameters.get("goal") or "").strip()
    if not goal:
        return "I need a goal to work on, sir."

    run_at, err = _parse_run_at(parameters.get("run_at"))
    if err:
        return err
    repeat_every, err = _parse_repeat(parameters.get("repeat_every"))
    if err:
        return err

    priority = _PRIORITY.get(
        str(parameters.get("priority") or "normal").lower(),
        TaskPriority.NORMAL,
    )
    queue = get_queue()
    task_id = queue.submit(
        goal, priority=priority, speak=speak, player=player,
        on_complete=_make_on_complete(player),
        run_at=run_at, repeat_every=repeat_every,
    )
    if player is not None and hasattr(player, "write_log"):
        player.write_log(f"SYS: Background task {task_id} started: {goal}")
    if run_at is not None and run_at > time.time():
        when = datetime.fromtimestamp(run_at).strftime("%Y-%m-%d %H:%M")
        msg = f"Task {task_id} is scheduled for {when}, sir."
        if repeat_every:
            msg += f" It repeats {str(parameters.get('repeat_every')).strip()}."
        return msg
    return (f"Task {task_id} is running in the background, sir. "
            f"I will tell you when it finishes.")


TOOL = {
    "name": "run_task",
    "description": (
        "Delegate a goal to the background agent. It plans the goal into steps, "
        "executes them autonomously (web searches, files, system tasks, reports), "
        "retries or re-plans on failure, and announces the result when done. "
        "Use when the user asks for something that needs several steps or takes a "
        "while — e.g. research a topic and write it to a file, compare several "
        "products, or gather today's news and save a summary. Do NOT use for a "
        "single quick tool call you can make yourself right now."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "goal": {
                "type": "STRING",
                "description": "The complete goal for the background task, in the user's language.",
            },
            "priority": {
                "type": "STRING",
                "description": "'high', 'normal', or 'low' (default: normal).",
            },
            "run_at": {
                "type": "STRING",
                "description": (
                    "Optional. When to run the task, as an ISO-8601 date/time "
                    "like '2026-10-15T09:00' (local time if no offset). "
                    "If omitted the task runs now."
                ),
            },
            "repeat_every": {
                "type": "STRING",
                "description": (
                    "Optional. Re-run the task on a schedule: 'hourly', 'daily', "
                    "'weekly', or a number of seconds (e.g. '21600' = every 6 hours)."
                ),
            },
        },
        "required": ["goal"],
    },
    "behavior": "NON_BLOCKING",
    "scheduling": "SILENT",
    "handler": run_task,
}
