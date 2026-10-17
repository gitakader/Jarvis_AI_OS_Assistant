"""
skill_forge action — teach JARVIS a brand-new ability by voice.

Sub-actions:
    forge   — synthesize a Python skill from a plain-language goal, verify it
              in the Crucible sandbox, and hot-register it. Long-running, so
              the tool is declared NON_BLOCKING with SILENT scheduling: the
              handler announces completion through the speak channel itself
              and the model is never held up or prompted twice.
    list    — what skills exist and their state.
    toggle  — enable/disable a skill (reversible; persists).
    delete  — permanently remove a skill. Irreversible, so it goes behind
              the on-screen confirmation gate (core/confirm.py).

The registry instance arrives via the tool context ("skills"), which main.py
injects at dispatch time.
"""
from __future__ import annotations

from core import confirm
from core.skill_forge import SkillForge

TOOL = {
    "name": "skill_forge",
    "description": (
        "The Skill Forge: create a brand-new reusable tool from a natural-language "
        "request — the skill is synthesized as Python, verified in a sandbox, and "
        "activated. Also list forged skills, enable/disable one, or delete one. "
        "Use 'forge' when the user asks to teach JARVIS a new ability (for example "
        "\"build a skill that shows cricket scores\"). Forging takes a minute or two."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "forge | list | toggle | delete (default forge)",
            },
            "goal": {
                "type": "STRING",
                "description": "For forge: what the new skill should do, in plain words.",
            },
            "name": {
                "type": "STRING",
                "description": "For forge: an optional snake_case name hint. "
                               "For toggle/delete: the skill's name (see list).",
            },
            "enable": {
                "type": "BOOLEAN",
                "description": "For toggle: true to enable, false to disable. Omit to flip.",
            },
        },
    },
    "handler": None,  # filled below; the dict is read at import time
    "behavior": "NON_BLOCKING",
    "scheduling": "SILENT",
}


def skill_forge(parameters: dict, player=None, speak=None, skills=None) -> str:
    action = str(parameters.get("action") or "forge").strip().lower()

    if action == "list":
        return _list_skills(skills)
    if action == "toggle":
        return _toggle(parameters, skills)
    if action == "delete":
        return _delete(parameters, skills)
    return _forge(parameters, speak, skills)


# ── forge ──────────────────────────────────────────────────────────────────

def _forge(parameters: dict, speak, skills) -> str:
    if skills is None:
        return "The Skill Forge is unavailable — the skill registry is not running."

    goal = str(parameters.get("goal") or "").strip()
    if not goal:
        return ("Tell me what the skill should do, for example: "
                "\"forge a skill that converts currencies using a public API\".")

    name_hint = str(parameters.get("name") or "").strip()
    result = SkillForge.forge(goal=goal, registry=skills, name_hint=name_hint)

    if not result.get("success"):
        msg = result.get("message", "the forge failed")
        _announce(speak, f"Sir, the Skill Forge could not build that skill. {msg}")
        return f"Skill forge failed: {msg}"

    name = result["name"]
    message = result.get("message", "")
    _announce(
        speak,
        f"Skill Forge complete. The new skill '{name}' is now active: "
        f"{result.get('description', '')[:140]}",
    )
    return (f"{message} "
            f"It joins my tool set on the next reconnect.")


# ── list / toggle / delete ─────────────────────────────────────────────────

def _list_skills(skills) -> str:
    if skills is None:
        return "The skill registry is unavailable."
    entries = skills.list()
    if not entries:
        return "There are no forged skills yet. Ask me to forge one!"
    lines = [f"- {s['name']} ({'enabled' if s['active'] else 'disabled'}"
             f"{f', {s['invocations']} runs' if s['invocations'] else ''}): {s['description']}"
             for s in entries]
    return "Forged skills:\n" + "\n".join(lines)


def _toggle(parameters: dict, skills) -> str:
    if skills is None:
        return "The skill registry is unavailable."
    name = str(parameters.get("name") or "").strip()
    if not name:
        return "Give me the skill's name to toggle (try action=list first)."
    if not skills.has(name):
        return (f"I don't have a forged skill named '{name}'. "
                f"Use action=list to see the names.")
    enable = parameters.get("enable")
    enable = None if enable is None else bool(enable)
    if not skills.toggle(name, enable):
        return f"Could not toggle skill '{name}' — the state may not have been saved."
    state = "enabled" if skills.get(name).active else "disabled"
    return f"Skill '{name}' is now {state}."


def _delete(parameters: dict, skills) -> str:
    if skills is None:
        return "The skill registry is unavailable."
    name = str(parameters.get("name") or "").strip()
    if not name:
        return "Give me the skill's name to delete (try action=list first)."
    rec = skills.get(name)
    if rec is None:
        return (f"I don't have a forged skill named '{name}'. "
                f"Use action=list to see the names.")

    detail = (f"Skill: {name}\n{rec.description}\n\n"
              f"This permanently deletes the skill file. It cannot be undone.")

    def _do_delete() -> str:
        if skills.delete(name):
            return f"Deleted skill '{name}'."
        return f"Could not delete skill '{name}'."

    return confirm.request(
        key="skill_delete",
        title=f"Delete skill '{name}'?",
        detail=detail,
        run=_do_delete,
    )


def _announce(speak, text: str) -> None:
    if speak is None:
        return
    try:
        speak(text)
    except Exception:
        pass


TOOL["handler"] = skill_forge
