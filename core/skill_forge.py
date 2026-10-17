"""
Skill Forge — turns a natural-language request into a verified, reusable skill.

Pipeline (each stage must pass before the next begins):

  1. Synthesis     — the LLM writes a complete package: manifest, code,
                     test cases. Done through core/gemini.call so the forge
                     inherits the whole model ladder + local-router fallback.
  2. Crucible AST  — ban-list and entry-point check (core/skill_crucible).
  3. Dependencies  — missing third-party imports are installed.
  4. Sandbox test  — code runs in an isolated subprocess with a timeout.
  5. Repair loop   — an AST or sandbox failure asks the LLM to fix the code
                     (bounded attempts), then re-enters the pipeline.
  6. Commit        — the verified skill is written to features/ and
                     hot-registered via the SkillRegistry.

The forge deliberately never deletes anything and never touches config: it
creates one new file set under features/ (reversible by deleting the skill).
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.skill_crucible import SkillCrucible
from core.skill_registry import SkillRegistry

# Where generated skills may save visual deliverables. Fixed (no env lookup)
# so generated code has one simple, guaranteed-to-exist place to write.
DELIVERABLES_DIR = str(Path.home() / "Downloads" / "JARVIS")


class SkillForge:
    @classmethod
    def forge(
        cls,
        goal: str,
        registry: SkillRegistry,
        name_hint: str = "",
        context_hints: str = "",
        max_repair_attempts: int = 2,
        logger=lambda m: print(m),
    ) -> Dict[str, Any]:
        """Synthesize, verify, and hot-register a skill. Never raises.

        Returns a dict with "success" and either "name"/"message" (plus
        "description") or "message" explaining the failure."""
        if not goal or not goal.strip():
            return {"success": False, "message": "No goal given — describe what the skill should do."}
        if registry is None:
            return {"success": False, "message": "Skill registry is unavailable."}

        name_hint_norm = re.sub(r"[^a-zA-Z0-9_]", "_", (name_hint or "").lower()).strip("_")
        logger(f"[Forge] Synthesizing skill for goal: {goal[:80]}")

        synthesis = cls._call_synthesizer(goal, name_hint_norm, context_hints)
        if not synthesis.get("success"):
            return {"success": False,
                    "message": f"Skill synthesis failed: {synthesis.get('error')}"}

        manifest = synthesis.get("manifest") or {}
        code = synthesis.get("code") or ""
        test_cases = synthesis.get("test_cases") or [{"input": {}}]
        actual_name = str(manifest.get("name") or name_hint_norm or "custom_skill")

        # A taken name is settled up front (cheap) rather than after three
        # verification stages (expensive and embarrassing).
        ok, reason = registry.check_name(actual_name)
        if not ok:
            candidate = f"{actual_name}_skill"
            ok2, _ = registry.check_name(candidate)
            actual_name = candidate if ok2 else ""
            if not actual_name:
                return {"success": False, "message": f"Cannot use name: {reason}"}

        # 2-5. Verify with repair loop.
        for attempt in range(max_repair_attempts + 1):
            logger(f"[Forge] Crucible verification attempt {attempt + 1}/{max_repair_attempts + 1} for '{actual_name}'")

            ast_ok, ast_err = SkillCrucible.validate_ast(code)
            if not ast_ok:
                if attempt < max_repair_attempts:
                    logger(f"[Forge] AST rejected ({ast_err}) — requesting repair.")
                    repair = cls._repair_code(code, ast_err, goal)
                    if repair.get("success"):
                        code = repair["code"]
                        continue
                return {"success": False,
                        "message": f"Crucible AST rejected the skill: {ast_err}"}

            deps = SkillCrucible.extract_dependencies(code)
            deps_ok, deps_msg = SkillCrucible.resolve_dependencies(deps)
            if not deps_ok:
                return {"success": False, "message": f"Dependency resolution failed: {deps_msg}"}

            test_ok, test_msg, telemetry = SkillCrucible.run_sandbox_test(code, test_cases)
            if not test_ok:
                if attempt < max_repair_attempts:
                    logger(f"[Forge] Sandbox failed ({test_msg}) — requesting repair.")
                    repair = cls._repair_code(code, test_msg, goal)
                    if repair.get("success"):
                        code = repair["code"]
                        continue
                return {"success": False, "message": f"Crucible sandbox tests failed: {test_msg}",
                        "telemetry": telemetry}

            break

        # 6. Commit + hot-register.
        description = str(manifest.get("description") or "").strip() or \
            f"Generated skill for: {goal.strip()}"
        try:
            registry.register(actual_name, description,
                              manifest.get("parameters") or {"type": "OBJECT", "properties": {}},
                              code)
        except ValueError as e:
            return {"success": False, "message": str(e)}

        logger(f"[Forge] Skill '{actual_name}' forged: {test_msg}")
        return {
            "success": True,
            "name": actual_name,
            "description": description,
            "message": f"Forged and activated skill '{actual_name}'. {test_msg}.",
        }

    # ── LLM calls ──────────────────────────────────────────────────────────

    @classmethod
    def _call_synthesizer(cls, goal: str, name_hint: str, context_hints: str) -> Dict[str, Any]:
        prompt = (
            f"Synthesize a new skill for the following user request:\n"
            f"Goal: {goal}\n"
            f"Preferred Name: {name_hint or 'auto_generate'}\n"
            f"Additional Context: {context_hints}\n"
        )
        raw = cls._llm_json(_SYNTHESIZER_SYSTEM, prompt, temperature=0.2)
        if raw is None:
            return {"success": False, "error": "every LLM on the ladder failed to answer"}
        try:
            data = cls._parse_json_response(raw)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        if not isinstance(data.get("manifest"), dict) or not isinstance(data.get("code"), str) or not data["code"].strip():
            return {"success": False, "error": "model reply must contain a manifest object and a code string"}
        data["success"] = True
        return data

    @classmethod
    def _repair_code(cls, broken_code: str, error_msg: str, goal: str) -> Dict[str, Any]:
        prompt = (
            f"A Python skill generated for JARVIS failed verification.\n"
            f"User Goal: {goal}\n"
            f"Verification Error: {error_msg}\n\n"
            f"Broken Code:\n```python\n{broken_code}\n```\n\n"
            f"Repair it so it passes every check in the JARVIS skill rules "
            f"(self-contained, no os/subprocess/eval, execute(**kwargs) with "
            f"safe defaults, graceful fallbacks — never return an 'error' dict).\n"
            f"Return ONLY a JSON object: {{\"code\": \"the fully corrected Python code\"}}"
        )
        raw = cls._llm_json(_SYNTHESIZER_SYSTEM, prompt, temperature=0.1)
        if raw is None:
            return {"success": False, "error": "repair LLM call failed"}
        try:
            data = cls._parse_json_response(raw)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        if not isinstance(data.get("code"), str) or not data["code"].strip():
            return {"success": False, "error": "repair reply had no code"}
        data["success"] = True
        return data

    @staticmethod
    def _llm_json(system: str, prompt: str, temperature: float) -> Optional[str]:
        """One JSON-mode generation through the standard ladder.

        core.gemini.call walks the model ladder and falls back to the smart
        provider router; if even that is down, a final attempt is made via
        the local llm_client (Ollama). Returns the raw text or None."""
        try:
            from core import gemini
            from google.genai import types
            resp = gemini.call(
                contents=prompt,
                tier=gemini.SMART,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=temperature,
                    response_mime_type="application/json",
                ),
                timeout_ms=180_000,
            )
            if resp is not None:
                raw = (getattr(resp, "text", None) or "").strip()
                if raw:
                    return raw
        except Exception as e:
            print(f"[Forge] gemini.call failed: {e}")

        try:
            from core.llm_client import call_llm_text
            raw = call_llm_text(prompt, system=system, timeout=300).strip()
            return raw or None
        except Exception as e:
            print(f"[Forge] local LLM fallback failed: {e}")
        return None

    @staticmethod
    def _parse_json_response(text: str) -> Dict[str, Any]:
        """Robust JSON extraction from model output (fences, trailing prose)."""
        clean = (text or "").strip()
        if clean.startswith("```"):
            clean = re.sub(r"^```(?:json)?\s*\n?", "", clean, flags=re.IGNORECASE)
            clean = re.sub(r"\n?```\s*$", "", clean).strip()

        try:
            data = json.loads(clean)
            if isinstance(data, dict):
                return data
        except Exception:
            pass

        start_idx = clean.find("{")
        if start_idx != -1:
            try:
                decoder = json.JSONDecoder()
                obj, _ = decoder.raw_decode(clean[start_idx:])
                if isinstance(obj, dict):
                    return obj
            except Exception:
                pass
            for end_idx in range(len(clean) - 1, start_idx, -1):
                if clean[end_idx] == "}":
                    try:
                        data = json.loads(clean[start_idx:end_idx + 1])
                        if isinstance(data, dict):
                            return data
                    except Exception:
                        continue

        raise ValueError("Could not extract a valid JSON object from the model output.")


_SYNTHESIZER_SYSTEM = f"""You are the JARVIS Skill Architect. You invent and write complete, standalone, production-ready Python skills.

HARD RULES (the sandbox enforces every one of these — violating any of them fails verification):
1. Entry point MUST be `def execute(**kwargs)` or `async def execute(**kwargs)`.
2. Self-contained: import ONLY the Python standard library (using pathlib instead of os, json, urllib, etc.) and third-party pip packages. NEVER import os, subprocess, ctypes, socket, pickle, marshal, multiprocessing, or any project module (core, actions, plugins, features, memory, dashboard).
3. NEVER use eval, exec, compile, __import__, or anything that runs system commands. No shell, no process spawning, ever.
4. Safe defaults: in execute(**kwargs), assign fallback defaults to every expected parameter. Called with empty kwargs (as in sandbox verification), the skill MUST run cleanly without KeyError or TypeError.
5. Resilient network: for HTTP use `requests` with `timeout=8, verify=False`, or urllib with `ssl._create_unverified_context()` (Windows Python often fails certificate verification). NEVER assume API keys exist. Use only public open APIs or local Python logic.
6. If any network call fails, catch the exception and return a graceful fallback. NEVER return {{'error': ...}} — the test runner treats that as failure.
7. Visual deliverables (images, GIFs, charts, cards): produce an actual file saved to `{DELIVERABLES_DIR}` (create it with `Path(...).mkdir(parents=True, exist_ok=True)` — no os module). Generate natively with PIL (Pillow) or matplotlib (use `matplotlib.use('Agg')` before pyplot). Cyberpunk dark theme: background #0B0F19, glowing cyan #00F0FF, emerald #10B981, white #FFFFFF. If a web image download fails, fall back to drawing the visual with PIL/matplotlib. Return {{'image_path': ..., 'title': ..., 'summary': ...}}.
8. Keep skills fast and small: no heavy scraping, no selenium/playwright, no long loops.

OUTPUT FORMAT — return ONLY this JSON object (no markdown fences):
{{
    "manifest": {{
        "name": "snake_case_name",
        "description": "One or two sentences on what this skill does and when JARVIS should call it",
        "parameters": {{
            "type": "OBJECT",
            "properties": {{
                "param_name": {{"type": "STRING", "description": "what it is"}}
            }},
            "required": []
        }}
    }},
    "code": "the complete Python source including imports and def execute(**kwargs)",
    "test_cases": [{{"input": {{}}}}]
}}"""
