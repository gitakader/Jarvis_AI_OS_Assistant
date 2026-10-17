"""Agent task stack: plan → execute → recover, ported from Brahma Evo.

Planner turns a user goal into a bounded JSON plan using JARVIS's live tool
catalog; Executor runs the plan through an injected run_tool callable (the
main.py dispatch, wired in track 3) with per-step timeouts, retries, and
replanning; TaskQueue serializes background tasks. Every LLM call goes
through core.gemini so the model ladder and local fallback apply, and every
dependency that main.py owns (registry dispatch, speak, confirm gates) is
injected — this package never imports main.py or touches the session.
"""
from __future__ import annotations

from core.agent.planner import (
    MAX_STEPS,
    create_plan,
    fallback_plan,
    replan,
)
from core.agent.error_handler import (
    ErrorDecision,
    analyze_error,
)
from core.agent.task_queue import (
    TaskPriority,
    TaskQueue,
    TaskStatus,
    get_queue,
)
from core.agent.executor import (
    MAX_ATTEMPTS_PER_STEP,
    MAX_REPLANS,
    STEP_TIMEOUT_SECONDS,
    TASK_TIMEOUT_SECONDS,
    AgentExecutor,
)

__all__ = [
    "MAX_STEPS",
    "MAX_ATTEMPTS_PER_STEP",
    "MAX_REPLANS",
    "STEP_TIMEOUT_SECONDS",
    "TASK_TIMEOUT_SECONDS",
    "AgentExecutor",
    "ErrorDecision",
    "TaskPriority",
    "TaskQueue",
    "TaskStatus",
    "analyze_error",
    "create_plan",
    "fallback_plan",
    "get_queue",
    "replan",
]
