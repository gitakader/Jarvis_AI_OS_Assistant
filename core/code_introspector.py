import json
import os
from pathlib import Path
from typing import Dict, List

# Build a lightweight metadata registry of available actions and their parameters.
# This scans the actions directory for TOOL definitions.

ACTIONS_DIR = Path(__file__).resolve().parent.parent / "actions"

def _load_action_tool(module_name: str):
    try:
        module = __import__(module_name, fromlist=["TOOL"])
        return getattr(module, "TOOL", None)
    except Exception:
        return None

def build_introspection() -> Dict[str, Dict]:
    """Return a dict mapping action name to its description and expected parameters.
    Example:
        {
            "file_controller": {
                "description": "Filesystem operations",
                "parameters": {"action": "list", "path": "..."}
            },
            ...
        }
    """
    introspection: Dict[str, Dict] = {}
    for py_file in ACTIONS_DIR.glob("*.py"):
        if py_file.name.startswith("_"):
            continue
        module_name = f"actions.{py_file.stem}"
        tool = _load_action_tool(module_name)
        if isinstance(tool, dict) and "name" in tool:
            name = tool["name"]
            introspection[name] = {
                "description": tool.get("description", ""),
                "parameters_schema": tool.get("parameters", {}),
                "module": module_name,
            }
    return introspection

# Cache the introspection on first import
INTROSPECTION_CACHE = build_introspection()

def describe_action(action_name: str) -> str:
    info = INTROSPECTION_CACHE.get(action_name)
    if not info:
        return f"Action '{action_name}' not found."
    desc = info.get("description", "")
    params = info.get("parameters_schema", {})
    return f"{action_name}: {desc}\nExpected parameters schema: {json.dumps(params, indent=2)}"
