"""task_status — check on or cancel background agent tasks.

Companion to run_task: quick lookup of the persisted task store (the
queue is saved to memory/agent_tasks.json once main.py wires it).
Blocking (the default) — the answer is immediate.
"""
from __future__ import annotations

from datetime import datetime

from core.agent.task_queue import get_queue


def _clip(text: str, limit: int = 400) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[:limit] + "..."


def _fmt_schedule(row: dict) -> str:
    parts = []
    if row.get("run_at"):
        parts.append(datetime.fromtimestamp(row["run_at"]).strftime("at %Y-%m-%d %H:%M"))
    if row.get("repeat_every"):
        parts.append(f"every {row['repeat_every']:g}s")
    return f" ({', '.join(parts)})" if parts else ""


def task_status(parameters: dict, player=None, **_kw) -> str:
    queue = get_queue()
    action = str(parameters.get("action") or "status").lower().strip()
    task_id = str(parameters.get("task_id") or "").strip()

    if action == "list":
        rows = queue.get_all_statuses()
        if not rows:
            return "No background tasks, sir."
        return "Background tasks:\n" + "\n".join(
            f"  {r['task_id']} — {r['status']} — {_clip(r['goal'], 60)}"
            f"{_fmt_schedule(r)}"
            for r in rows
        )

    if not task_id:
        return "I need a task id, sir. Use action 'list' to see the running tasks."

    if action == "cancel":
        if queue.cancel(task_id):
            return f"Task {task_id} cancelled, sir."
        st = queue.get_status(task_id)
        if st is None:
            return f"No task with id {task_id}, sir."
        return f"Task {task_id} is already {st['status']}, sir."

    st = queue.get_status(task_id)
    if st is None:
        return f"No task with id {task_id}, sir."
    out = f"Task {task_id}: {st['status']}.\nGoal: {_clip(st['goal'])}\n"
    sched = _fmt_schedule(st)
    if sched:
        out += f"Schedule:{sched}\n"
    if st.get("result"):
        out += f"Result: {_clip(st['result'])}\n"
    if st.get("error"):
        out += f"Error: {_clip(st['error'])}"
    return out


TOOL = {
    "name": "task_status",
    "description": (
        "Check on or cancel background tasks started with run_task. "
        "action 'status' reports one task (task_id required), 'list' shows all "
        "tasks, 'cancel' stops a pending or running task. "
        "Use when the user asks about the background task, whether it is done, "
        "or to stop/cancel it."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "status | list | cancel (default: status)",
            },
            "task_id": {
                "type": "STRING",
                "description": "The task id to check or cancel (not needed for list).",
            },
        },
    },
    "handler": task_status,
}
