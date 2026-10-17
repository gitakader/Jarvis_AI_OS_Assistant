"""
sensorium.py — Query the ambient telemetry daemon (core/sensorium.py).

Lets JARVIS answer "what am I doing", "how long have I been in this app",
or "how healthy is the system" from the passive sensorium singleton.
"""
from __future__ import annotations

import json


def _snapshot() -> dict:
    try:
        from core.sensorium import sensorium
        return sensorium.get_snapshot()
    except Exception as e:
        return {"error": str(e)}


def sensorium_action(parameters, **kwargs) -> str:
    action = (parameters.get("action") or "context").strip().lower()

    if action in ("context", "status", "what_am_i_doing"):
        snap = _snapshot()
        if "error" in snap:
            return f"Ambient sensing unavailable: {snap['error']}"
        return (
            f"Current app: {snap['process_name']} | "
            f"Window: \"{snap['window_title']}\" | "
            f"Focused for {snap['dwell_seconds']}s | "
            f"Idle for {snap['user_idle_seconds']}s | "
            f"User active: {'yes' if snap['is_user_active'] else 'no'}"
        )

    if action in ("vitals", "system", "health"):
        snap = _snapshot()
        if "error" in snap:
            return f"Ambient sensing unavailable: {snap['error']}"
        battery = (f"{snap['battery_percent']}%"
                   if snap["battery_percent"] is not None else "n/a")
        plugged = ("plugged in" if snap["power_plugged"] else "on battery") \
            if snap["battery_percent"] is not None else ""
        return (f"CPU: {snap['cpu_percent']}% | RAM: {snap['ram_percent']}% | "
                f"Battery: {battery} {plugged}".strip())

    if action in ("idle", "away"):
        snap = _snapshot()
        if "error" in snap:
            return f"Ambient sensing unavailable: {snap['error']}"
        return (f"User idle for {snap['user_idle_seconds']}s "
                f"({'active' if snap['is_user_active'] else 'away'}).")

    if action == "snapshot":
        snap = _snapshot()
        if "error" in snap:
            return f"Ambient sensing unavailable: {snap['error']}"
        return json.dumps(snap, indent=2)

    return (f"Unknown action '{action}'. Available: "
            "context | vitals | idle | snapshot")


TOOL = {
    "name": "sensorium",
    "description": "Ambient sensing: what app/window the user is focused on and for how long, "
                   "how long they have been idle, and system vitals (CPU/RAM/battery). "
                   "Use for questions like 'what am I doing' or 'how is the system'.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "context | vitals | idle | snapshot",
            }
        },
        "required": ["action"],
    },
    "handler": sensorium_action,
}
