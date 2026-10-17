"""Error recovery decisions for a failed plan step.

The LLM suggests a decision (retry/skip/replan/abort); the safety rules are
enforced in code, never left to the model: max-attempts always forces REPLAN,
a critical step can never be SKIPped, and an unusable model answer degrades
to REPLAN. Brahma's generate_fix (a claude_code swap) is deliberately dropped:
JARVIS recovers through the plan-level replan path instead.
"""
from __future__ import annotations

import json
from enum import Enum
from typing import Callable, Optional

from core.agent.planner import _llm_json


class ErrorDecision(Enum):
    RETRY  = "retry"
    SKIP   = "skip"
    REPLAN = "replan"
    ABORT  = "abort"


_ERROR_ANALYST_PROMPT = """You are the error recovery module of JARVIS, a Windows AI assistant.

A task step has failed. Analyze the error and decide what to do.

DECISIONS:
- retry   : Transient error (network timeout, temporary file lock, race condition).
            The same step can succeed if tried again.
- skip    : This step is not critical and the task can succeed without it.
- replan  : The approach was wrong. A different tool or method should be tried.
- abort   : The task is fundamentally impossible or unsafe to continue.

Also provide:
- A brief explanation of WHY it failed (1 sentence)
- A fix suggestion if decision is replan (what to try instead)
- Max retries: how many times to retry if decision is retry (1 or 2)

Return ONLY valid JSON:
{
  "decision": "retry|skip|replan|abort",
  "reason": "why it failed",
  "fix_suggestion": "what to try instead (for replan)",
  "max_retries": 1,
  "user_message": "Short message to tell the user (max 15 words)"
}
"""

_DECISION_MAP = {
    "retry":  ErrorDecision.RETRY,
    "skip":   ErrorDecision.SKIP,
    "replan": ErrorDecision.REPLAN,
    "abort":  ErrorDecision.ABORT,
}


def analyze_error(
    step: dict,
    error: str,
    attempt: int = 1,
    max_attempts: int = 2,
    llm_json: Optional[Callable[[str, str], Optional[dict]]] = None,
) -> dict:
    """Decide how to recover from a failed step.

    Returns {"decision": ErrorDecision, "reason", "fix_suggestion",
    "max_retries", "user_message"}. Never raises; the LLM is optional
    (injectable for tests) and every rule below is enforced in code.
    """
    if attempt >= max_attempts:
        print(f"[ErrorHandler] step {step.get('step')} failed {attempt} times — forcing replan")
        return {
            "decision":       ErrorDecision.REPLAN,
            "reason":         f"Failed {attempt} times: {error[:100]}",
            "fix_suggestion": "Try a completely different approach or tool",
            "max_retries":    0,
            "user_message":   "Trying a different approach, sir.",
        }

    prompt = (
        f"Failed step:\n"
        f"Tool: {step.get('tool')}\n"
        f"Description: {step.get('description')}\n"
        f"Parameters: {json.dumps(step.get('parameters', {}), ensure_ascii=False)}\n"
        f"Critical: {step.get('critical', False)}\n\n"
        f"Error:\n{error[:500]}\n\n"
        f"Attempt number: {attempt}"
    )

    llm = llm_json or _llm_json
    try:
        raw = llm(_ERROR_ANALYST_PROMPT, prompt)
        if raw is None:
            raise ValueError("no LLM answer")
        decision = _DECISION_MAP.get(str(raw.get("decision", "")).lower(),
                                     ErrorDecision.REPLAN)
    except Exception as e:
        print(f"[ErrorHandler] analysis failed: {e} — defaulting to replan")
        return {
            "decision":       ErrorDecision.REPLAN,
            "reason":         str(e),
            "fix_suggestion": "Try alternative approach",
            "max_retries":    1,
            "user_message":   "Encountered an issue, adjusting approach, sir.",
        }

    if step.get("critical") and decision == ErrorDecision.SKIP:
        print("[ErrorHandler] critical step cannot be skipped — forcing replan")
        decision = ErrorDecision.REPLAN
        raw["user_message"] = "This step is critical — finding an alternative approach, sir."

    result = {
        "decision":       decision,
        "reason":         str(raw.get("reason") or ""),
        "fix_suggestion": str(raw.get("fix_suggestion") or ""),
        "max_retries":    int(raw.get("max_retries") or 1),
        "user_message":   str(raw.get("user_message") or ""),
    }
    print(f"[ErrorHandler] decision: {decision.value} — {result['reason'][:80]}")
    return result
