"""
active_context.py — Ambient Workspace & Active Application Sensor for MARK LV.

Provides instant ambient awareness of:
1. The currently focused application, window title, and active document.
2. Open top-level desktop windows and applications.
3. System clipboard content for immediate contextual explanation, translation, or debugging.
"""
from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_OS = platform.system()

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

if _OS == "Windows":
    try:
        import win32gui
        import win32process
        _WIN32 = True
    except ImportError:
        _WIN32 = False
else:
    _WIN32 = False


def get_active_window() -> dict[str, Any]:
    """Returns details of the currently focused/active window."""
    if _OS == "Windows" and _WIN32:
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return {"app": "Desktop", "title": "Desktop", "pid": 0}
                
            title = win32gui.GetWindowText(hwnd) or "Untitled Window"
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            app_name = "Unknown"
            
            if _PSUTIL and pid > 0:
                try:
                    proc = psutil.Process(pid)
                    app_name = proc.name()
                except Exception:
                    pass
                    
            return {
                "app": app_name,
                "title": title,
                "pid": pid,
                "hwnd": hwnd
            }
        except Exception as e:
            return {"error": str(e)}
            
    elif _OS == "Darwin":
        try:
            cmd = """osascript -e 'tell application "System Events" to get name of first process whose frontmost is true'"""
            app = subprocess.check_output(cmd, shell=True, text=True).strip()
            return {"app": app, "title": app, "pid": 0}
        except Exception as e:
            return {"error": str(e)}
            
    else:
        # Linux
        try:
            out = subprocess.check_output(["xdotool", "getactivewindow", "getwindowname"], text=True).strip()
            return {"app": "Linux Window", "title": out, "pid": 0}
        except Exception:
            return {"app": "Linux", "title": "Active Window", "pid": 0}


def get_open_windows() -> list[dict[str, str]]:
    """List visible desktop application windows."""
    windows = []
    if _OS == "Windows" and _WIN32:
        def enum_handler(hwnd, extra):
            if win32gui.IsWindowVisible(hwnd):
                title = win32gui.GetWindowText(hwnd).strip()
                if title and title != "Program Manager":
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    pname = "App"
                    if _PSUTIL and pid:
                        try:
                            pname = psutil.Process(pid).name()
                        except Exception:
                            pass
                    extra.append({"title": title, "process": pname})
        try:
            win32gui.EnumWindows(enum_handler, windows)
        except Exception as e:
            print(f"[ActiveContext] EnumWindows error: {e}")
            
    return windows[:25]


def read_clipboard_content() -> str:
    """Read plain text from system clipboard."""
    if not _PYPERCLIP:
        return "Clipboard access unavailable (pyperclip not installed)."
    try:
        content = pyperclip.paste()
        if not content or not content.strip():
            return "Clipboard is currently empty."
        if len(content) > 3000:
            return f"Clipboard Content ({len(content)} chars):\n{content[:3000]}...\n[Truncated]"
        return f"Clipboard Content:\n{content}"
    except Exception as e:
        return f"Failed to read clipboard: {e}"


def active_context(parameters: dict, player=None, session_memory=None) -> str:
    action = parameters.get("action", "active_app").lower().strip()
    
    if action in ("active_app", "focused_window", "current_window"):
        info = get_active_window()
        if "error" in info:
            return f"Could not determine active window: {info['error']}"
        return f"Active Application: {info.get('app')} | Window Title: \"{info.get('title')}\""
        
    elif action in ("list_windows", "open_windows", "running_apps"):
        wins = get_open_windows()
        if not wins:
            return "No open desktop windows detected."
        lines = [f"Open Windows ({len(wins)}):"]
        for w in wins:
            lines.append(f"• [{w['process']}] {w['title']}")
        return "\n".join(lines)
        
    elif action in ("clipboard", "read_clipboard", "paste"):
        return read_clipboard_content()
        
    else:
        return f"Unknown action: '{action}'. Available: active_app | open_windows | clipboard"


TOOL = {
    "name": "active_context",
    "description": "Ambient Workspace Context: inspect currently focused active application, window title, open desktop windows, or read clipboard text for instant assistance.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "active_app | open_windows | clipboard"
            }
        },
        "required": ["action"]
    },
    "handler": active_context,
}
