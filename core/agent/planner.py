"""Goal → plan. Breaks a user goal into a bounded JSON step plan.

Unlike Brahma's original, the tool catalog is NOT hardcoded: JARVIS's toolset
is dynamic (action registry + forged skills + plugins + MCP), so callers pass
the catalog in and the prompt is assembled from it. LLM calls go through
core.gemini (model ladder + local fallback), and the output is validated and
capped in code — MAX_STEPS is enforced by truncation, never by the prompt
alone.
"""
from __future__ import annotations

import json
import re
from typing import Callable, Optional

MAX_STEPS = 6

_PLANNER_RULES = """You are the planning module of JARVIS, a Windows AI assistant.
Your job: break any user goal into a sequence of steps using ONLY the tools listed below.

ABSOLUTE RULES:
- NEVER use a tool called generated_code and never invent tools.
- Every step is independent: do NOT put the literal output of one step into another step's parameters.
  If a later step needs a result from an earlier step, use the placeholder {step_N} (N = the earlier
  step's number) in a string parameter, e.g. "content": "report based on {step_1}".
- Use web_search for ANY information retrieval, research, or current data.
- Use skill_forge ONLY when no listed tool covers the requested capability.
- A step's "critical" flag is true ONLY when the goal cannot succeed without that step.
- At most {max_steps} steps. Use the minimum steps needed; prefer one step for simple goals.

TOOL CATALOG:
{tool_catalog}

OUTPUT — return ONLY valid JSON, no markdown, no explanation, no code blocks:
{{
  "goal": "...",
  "steps": [
    {{
      "step": 1,
      "tool": "tool_name",
      "description": "what this step does",
      "parameters": {{}},
      "critical": true
    }}
  ]
}}
"""


def build_planner_prompt(tool_catalog: list[dict]) -> str:
    """Assemble the planner system prompt from a live tool catalog.

    Each entry: {"name": ..., "description": ..., "parameters": {...}} —
    the same shape ActionRegistry.get_tool_declarations() returns.
    """
    lines = []
    for tool in tool_catalog or []:
        name = (tool.get("name") or "").strip()
        if not name:
            continue
        desc = (tool.get("description") or "").strip().replace("\n", " ")
        params = tool.get("parameters") or {}
        lines.append(f"{name}\n  {desc}\n  Parameters: {json.dumps(params, ensure_ascii=False)}")
    catalog_text = "\n".join(lines) if lines else "(no tools available)"
    return _PLANNER_RULES.replace("{tool_catalog}", catalog_text).replace(
        "{max_steps}", str(MAX_STEPS))


def _llm_json(system: str, prompt: str) -> Optional[dict]:
    """One JSON-mode generation through the standard ladder (see
    SkillForge._llm_json): gemini.call walks the model ladder, then the
    local llm_client is the final fallback. Returns a parsed dict or None."""
    try:
        from core import gemini
        from google.genai import types
        resp = gemini.call(
            contents=prompt,
            tier=gemini.SMART,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.2,
                response_mime_type="application/json",
            ),
            timeout_ms=180_000,
        )
        if resp is not None:
            raw = (getattr(resp, "text", None) or "").strip()
            if raw:
                return _parse_json(raw)
    except Exception as e:
        print(f"[Planner] gemini.call failed: {e}")

    try:
        from core.llm_client import call_llm_text
        raw = call_llm_text(prompt, system=system, timeout=300).strip()
        return _parse_json(raw) if raw else None
    except Exception as e:
        print(f"[Planner] llm_client fallback failed: {e}")
    return None


def _parse_json(raw: str) -> Optional[dict]:
    """Parse model output, tolerating fences and surrounding prose."""
    if not raw:
        return None
    text = raw.strip()
    text = re.sub(r"```(?:json)?", "", text).strip().rstrip("`").strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        if "{" in text and "}" in text:
            text = text[text.find("{"): text.rfind("}") + 1]
            try:
                parsed = json.loads(text)
                return parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                pass
        print(f"[Planner] reply was not JSON: {text[:120]}")
        return None


def _rewrite_generated_step(step: dict, tool_names: set[str]) -> None:
    """The model is told never to emit generated_code; if it does anyway,
    degrade it to web_search (when available) instead of failing at runtime."""
    if step.get("tool") != "generated_code":
        return
    desc = step.get("description") or "Search the web"
    if "web_search" in tool_names:
        print(f"[Planner] generated_code in step {step.get('step')} — replacing with web_search")
        step["tool"] = "web_search"
        step["parameters"] = {"query": str(desc)[:200]}
    else:
        # No safe substitute — leave the tool unknown so the executor surfaces
        # it to the error handler instead of guessing.
        print(f"[Planner] generated_code in step {step.get('step')} — no web_search to fall back to")


def _normalize_plan(raw: dict, tool_names: set[str]) -> dict:
    """Validate, cap, and renumber a plan. Raises ValueError on a useless plan."""
    if not isinstance(raw, dict):
        raise ValueError("plan is not an object")
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise ValueError("plan has no steps")

    clean: list[dict] = []
    for s in steps[:MAX_STEPS]:
        if not isinstance(s, dict):
            continue
        tool = str(s.get("tool") or "").strip()
        if not tool:
            continue
        _rewrite_generated_step(s, tool_names)
        clean.append({
            "step": len(clean) + 1,
            "tool": str(s.get("tool") or "").strip(),
            "description": str(s.get("description") or "").strip(),
            "parameters": s.get("parameters") if isinstance(s.get("parameters"), dict) else {},
            "critical": bool(s.get("critical")),
        })
    if not clean:
        raise ValueError("no usable steps after validation")
    if len(steps) > MAX_STEPS:
        print(f"[Planner] plan had {len(steps)} steps — capped to {MAX_STEPS}")
    return {"goal": str(raw.get("goal") or "").strip(), "steps": clean}


def create_plan(
    goal: str,
    context: str = "",
    tool_catalog: Optional[list[dict]] = None,
    llm_json: Optional[Callable[[str, str], Optional[dict]]] = None,
) -> dict:
    """Plan a goal. `tool_catalog` and `llm_json` are injectable for tests;
    with both absent the real gemini ladder and an empty catalog are used."""
    system = build_planner_prompt(tool_catalog or [])
    tool_names = {t.get("name") for t in (tool_catalog or [])}
    user_input = f"Goal: {goal}"
    if context:
        user_input += f"\n\nContext: {context}"

    llm = llm_json or _llm_json
    try:
        raw = llm(system, user_input)
        if raw is None:
            raise ValueError("no LLM answer")
        plan = _normalize_plan(raw, tool_names)
        print(f"[Planner] plan: {len(plan['steps'])} steps for: {goal[:60]}")
        for s in plan["steps"]:
            print(f"  Step {s['step']}: [{s['tool']}] {s['description'][:70]}")
        return plan
    except Exception as e:
        print(f"[Planner] planning failed: {e} — using fallback plan")
        return fallback_plan(goal, tool_names)


def replan(
    goal: str,
    completed_steps: list[dict],
    failed_step: dict,
    error: str,
    tool_catalog: Optional[list[dict]] = None,
    llm_json: Optional[Callable[[str, str], Optional[dict]]] = None,
) -> dict:
    """Revised plan for the remaining work only. Never repeats completed steps."""
    system = build_planner_prompt(tool_catalog or [])
    tool_names = {t.get("name") for t in (tool_catalog or [])}

    completed_summary = "\n".join(
        f"  - Step {s['step']} ({s['tool']}): DONE" for s in completed_steps
    ) or "  (none)"
    prompt = (
        f"Goal: {goal}\n\n"
        f"Already completed:\n{completed_summary}\n\n"
        f"Failed step: [{failed_step.get('tool')}] {failed_step.get('description')}\n"
        f"Error: {error[:300]}\n\n"
        "Create a REVISED plan for the remaining work only. Do not repeat completed steps."
    )

    llm = llm_json or _llm_json
    try:
        raw = llm(system, prompt)
        if raw is None:
            raise ValueError("no LLM answer")
        plan = _normalize_plan(raw, tool_names)
        print(f"[Planner] revised plan: {len(plan['steps'])} steps")
        return plan
    except Exception as e:
        print(f"[Planner] replan failed: {e} — using fallback plan")
        return fallback_plan(goal, tool_names)


def fallback_plan(goal: str, tool_names: set[str]) -> dict:
    """Deterministic single-step plan when the LLM cannot produce one.

    Only ever picks web_search — the one tool that is safe to run on an
    arbitrary goal. Without it, returns an empty plan so the executor can
    say so honestly instead of guessing a destructive tool."""
    if "web_search" in tool_names:
        return {
            "goal": goal,
            "steps": [{
                "step": 1,
                "tool": "web_search",
                "description": f"Search for: {goal}",
                "parameters": {"query": goal},
                "critical": True,
            }],
        }
    return {"goal": goal, "steps": []}
