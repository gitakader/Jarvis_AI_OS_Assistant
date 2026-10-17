#computer_control.py
import io
import json
import platform
import re
import string
import subprocess
import sys

if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}
import time
import random
from pathlib import Path

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except ImportError:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except ImportError:
    _PYPERCLIP = False

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE         = _base_dir()
_CONFIG_PATH  = _BASE / "config" / "api_keys.json"
_MEMORY_PATH  = _BASE / "memory" / "long_term.json"

def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _platform_os() -> str:
    return {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(
        platform.system(), "linux"
    )

def _get_os() -> str:
    return _load_config().get("os_system", _platform_os()).lower()


def _get_api_key() -> str:
    return _load_config().get("gemini_api_key", "")

_SAFE_SCREENSHOT_ROOTS = (
    Path.home(),
)

def _safe_screenshot_path(requested: str | None) -> Path:
    fallback = Path.home() / "Desktop" / "jarvis_screenshot.png"
    if not requested:
        return fallback
    try:
        p = Path(requested).expanduser().resolve()
        for root in _SAFE_SCREENSHOT_ROOTS:
            if p.is_relative_to(root.resolve()):
                p.parent.mkdir(parents=True, exist_ok=True)
                return p
    except Exception:
        pass
    return fallback

def _require_pyautogui():
    if not _PYAUTOGUI:
        raise RuntimeError("PyAutoGUI not installed. Run: pip install pyautogui")

_FIRST_NAMES = [
    "Alex", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Drew", "Quinn",
    "Avery", "Blake", "Cameron", "Dakota", "Emerson", "Finley", "Harper",
]
_LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Wilson", "Moore", "Taylor", "Anderson", "Thomas", "Jackson",
]
_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "proton.me", "mail.com"]


def _random_data(data_type: str) -> str:
    dt = data_type.lower().strip()

    if dt == "first_name":
        return random.choice(_FIRST_NAMES)

    if dt == "last_name":
        return random.choice(_LAST_NAMES)

    if dt == "name":
        return f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    if dt == "email":
        first = random.choice(_FIRST_NAMES).lower()
        last  = random.choice(_LAST_NAMES).lower()
        num   = random.randint(10, 999)
        return f"{first}.{last}{num}@{random.choice(_DOMAINS)}"

    if dt == "username":
        return f"{random.choice(_FIRST_NAMES).lower()}{random.randint(100, 9999)}"

    if dt == "password":
        chars = string.ascii_letters + string.digits + "!@#$%"
        raw   = (
            random.choice(string.ascii_uppercase)
            + random.choice(string.digits)
            + random.choice("!@#$%")
            + "".join(random.choices(chars, k=9))
        )
        return "".join(random.sample(raw, len(raw)))

    if dt == "phone":
        return f"+1{random.randint(200,999)}{random.randint(1_000_000, 9_999_999)}"

    if dt == "birthday":
        y = random.randint(1980, 2000)
        m = random.randint(1, 12)
        d = random.randint(1, 28)
        return f"{m:02d}/{d:02d}/{y}"

    if dt == "address":
        num    = random.randint(100, 9999)
        street = random.choice(["Main St", "Oak Ave", "Park Blvd", "Elm St", "Cedar Ln"])
        return f"{num} {street}"

    if dt == "zip_code":
        return str(random.randint(10000, 99999))

    if dt == "city":
        return random.choice(["New York", "Los Angeles", "Chicago", "Houston", "Phoenix"])

    return f"random_{data_type}_{random.randint(1000, 9999)}"

def _user_profile() -> dict:
    """Read identity fields from long-term memory."""
    try:
        if _MEMORY_PATH.exists():
            data     = json.loads(_MEMORY_PATH.read_text(encoding="utf-8"))
            identity = data.get("identity", {})
            return {k: v.get("value", "") for k, v in identity.items()}
    except Exception:
        pass
    return {}

def _type(text: str, interval: float = 0.03) -> str:
    _require_pyautogui()
    time.sleep(0.3)
    pyautogui.typewrite(text, interval=interval)
    return f"Typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _smart_type(text: str, clear_first: bool = True) -> str:
    _require_pyautogui()
    if clear_first:
        _clear_field()
        time.sleep(0.1)

    if len(text) > 20 and _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Smart-typed (clipboard): {text[:60]}{'…' if len(text) > 60 else ''}"

    pyautogui.typewrite(text, interval=0.04)
    return f"Smart-typed: {text[:60]}{'…' if len(text) > 60 else ''}"


def _click(x=None, y=None, button: str = "left", clicks: int = 1) -> str:
    _require_pyautogui()
    if x is not None and y is not None:
        pyautogui.click(x, y, button=button, clicks=clicks)
        return f"{'Double-c' if clicks == 2 else 'C'}licked ({x}, {y}) [{button}]"
    pyautogui.click(button=button, clicks=clicks)
    return f"Clicked at current position [{button}]"


def _hotkey(*keys) -> str:
    _require_pyautogui()
    pyautogui.hotkey(*keys)
    return f"Hotkey: {'+'.join(keys)}"


def _press(key: str) -> str:
    _require_pyautogui()
    pyautogui.press(key)
    return f"Pressed: {key}"


def _scroll(direction: str = "down", amount: int = 3) -> str:
    _require_pyautogui()
    vertical   = direction in ("up", "down")
    clicks     = amount if direction in ("up", "right") else -amount
    pyautogui.scroll(clicks) if vertical else pyautogui.hscroll(clicks)
    return f"Scrolled {direction} ×{amount}"


def _move(x: int, y: int, duration: float = 0.3) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x, y, duration=duration)
    return f"Mouse → ({x}, {y})"


def _drag(x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> str:
    _require_pyautogui()
    pyautogui.moveTo(x1, y1, duration=0.2)
    pyautogui.dragTo(x2, y2, duration=duration, button="left")
    return f"Dragged ({x1},{y1}) → ({x2},{y2})"


def _clipboard_get() -> str:
    if _PYPERCLIP:
        return pyperclip.paste()
    _hotkey("ctrl", "c")
    time.sleep(0.2)
    return "(copied — pyperclip unavailable for read)"


def _clipboard_paste(text: str) -> str:
    if _PYPERCLIP:
        pyperclip.copy(text)
        time.sleep(0.1)
        _require_pyautogui()
        paste_key = "command" if _get_os() == "mac" else "ctrl"
        pyautogui.hotkey(paste_key, "v")
        return f"Pasted: {text[:60]}{'…' if len(text) > 60 else ''}"
    return "pyperclip not available"


def _screenshot(save_path: str | None = None) -> str:
    _require_pyautogui()
    path = _safe_screenshot_path(save_path)
    img  = pyautogui.screenshot()
    img.save(str(path))
    return f"Screenshot saved: {path}"


def _clear_field() -> str:
    _require_pyautogui()
    select_key = "command" if _get_os() == "mac" else "ctrl"
    pyautogui.hotkey(select_key, "a")
    time.sleep(0.1)
    pyautogui.press("delete")
    return "Field cleared"

def _focus_window(title: str) -> str:
    os_name = _get_os()

    if os_name == "windows":
        try:
            script = f'(New-Object -ComObject WScript.Shell).AppActivate("{title}")'
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=5, **_WIN_HIDE,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (Windows) failed: {e}"

    if os_name == "mac":
        script = (
            f'tell application "System Events" to '
            f'set frontmost of (first process whose name contains "{title}") to true'
        )
        try:
            subprocess.run(
                ["osascript", "-e", script],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except Exception as e:
            return f"focus_window (macOS) failed: {e}"

    if os_name == "linux":
        try:
            result = subprocess.run(
                ["wmctrl", "-a", title],
                capture_output=True, timeout=5,
            )
            if result.returncode == 0:
                time.sleep(0.3)
                return f"Focused window: {title}"
        except FileNotFoundError:
            pass
        try:
            result = subprocess.run(
                ["xdotool", "search", "--name", title, "windowactivate"],
                capture_output=True, timeout=5,
            )
            time.sleep(0.3)
            return f"Focused window: {title}"
        except FileNotFoundError:
            return "focus_window (Linux) requires wmctrl or xdotool"
        except Exception as e:
            return f"focus_window (Linux) failed: {e}"

    return f"focus_window: unknown OS '{os_name}'"

def _screen_find(description: str) -> tuple[int, int] | None:
    api_key = _get_api_key()
    if not api_key:
        print("[ComputerControl] ⚠️ No API key for screen_find")
        return None

    try:
        from google import genai
        from google.genai import types as gtypes

        _require_pyautogui()
        w, h  = pyautogui.size()
        img   = pyautogui.screenshot()
        buf   = io.BytesIO()
        img.save(buf, format="PNG")
        image_bytes = buf.getvalue()

        prompt = (
            f"This is a screenshot of a {w}×{h} pixel screen. "
            f"Locate the UI element described as: '{description}'. "
            f"Reply with ONLY the center coordinates as: x,y "
            f"If the element is not visible, reply: NOT_FOUND"
        )

        from core import gemini
        response = gemini.call(
            [gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"), prompt],
            tier=gemini.FAST, timeout_ms=20_000,
        )
        if response is None:
            return None

        text = (response.text or "").strip()
        if "NOT_FOUND" in text.upper():
            return None

        match = re.search(r"(\d+)\s*,\s*(\d+)", text)
        if match:
            return int(match.group(1)), int(match.group(2))

    except Exception as e:
        print(f"[ComputerControl] ⚠️ screen_find failed: {e}")

    return None


# ── Screen observe / autonomous task loop ─────────────────────────────────────
#
# screen_observe : one structured look at the screen (summary + clickable
#                  elements with real-pixel coordinates).
# screen_task    : a bounded observe → decide → act → observe-again loop that
#                  drives the PC on its own until the goal is done (or the
#                  step/time cap hits). Only whitelisted actions are allowed,
#                  every step is logged, and pyautogui's corner-abort FAILSAFE
#                  remains the physical kill switch.

_SCREEN_MAX_W = 1024          # downscale cap for vision latency

# The ONLY actions the autonomous loop may take.
_TASK_ACTION_WHITELIST = (
    "click", "double_click", "right_click", "type", "hotkey", "scroll",
    "wait", "done",
)

# Hotkeys the loop may press. Deliberately no win+r / win+l / ctrl+shift+esc /
# alt+f4 — nothing that can launch a shell or lock/close things out from under
# the user.
_SAFE_HOTKEYS = {
    "enter", "tab", "esc", "space", "backspace", "delete",
    "up", "down", "left", "right", "home", "end",
    "ctrl+a", "ctrl+c", "ctrl+v", "ctrl+z", "ctrl+s", "alt+tab", "win",
}

_DEFAULT_MAX_STEPS   = 8
_DEFAULT_MAX_SECONDS = 90.0
_LOOP_SETTLE_SECONDS = 1.0


def _capture_frame(max_w: int = _SCREEN_MAX_W):
    """Return (png_bytes, scale) where scale = real_width / sent_width."""
    _require_pyautogui()
    img = pyautogui.screenshot()
    w, h = img.size
    scale = 1.0
    if w > max_w:
        scale = w / max_w
        img = img.resize((max_w, max(1, int(h / scale))))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue(), scale


def _parse_json(text: str):
    """Best-effort JSON extraction — the model may wrap it in fences or chatter."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```(?:json)?\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return None


def _vision_json(image_bytes: bytes, prompt: str, timeout_ms: int = 15_000, tier=None):
    """One vision call returning a parsed JSON dict, or None on failure.

    The agent loop needs better models than the default FAST ladder (flash-lite
    first) — it defaults to SMART (2.5-flash first) unless told otherwise."""
    try:
        from google.genai import types as gtypes
        from core import gemini
        response = gemini.call(
            [gtypes.Part.from_bytes(data=image_bytes, mime_type="image/png"), prompt],
            tier=tier or gemini.SMART, timeout_ms=timeout_ms,
        )
        if response is None:
            return None
        return _parse_json(response.text or "")
    except Exception as e:
        print(f"[ComputerControl] vision JSON call failed: {e}")
        return None


def _observe_screen() -> str:
    """One structured look: summary + main clickable elements, real-pixel coords."""
    _require_pyautogui()
    try:
        img_b, _scale = _capture_frame()
        w, h = pyautogui.size()
        prompt = (
            'You are looking at a screenshot of a desktop screen. Describe what '
            'is on screen in one sentence and list the main interactive elements. '
            'Give element positions as NORMALIZED coordinates: x from 0 (left '
            'edge) to 1000 (right edge), y from 0 (top edge) to 1000 (bottom '
            'edge) — e.g. the screen center is x=500, y=500. Reply with ONLY a '
            'JSON object (no markdown, no extra text) in this exact shape: '
            '{"summary": "one sentence", "elements": [{"label": "element name", '
            '"x": <0-1000>, "y": <0-1000>, "confidence": <0.0-1.0>}]}.'
        )
        data = _vision_json(img_b, prompt)
    except Exception as e:
        return f"Could not capture the screen: {e}"
    if not data:
        return "Could not read the screen (vision model unavailable)."

    summary = str(data.get("summary", "")).strip()
    lines = [f"Screen {w}x{h}: {summary or '(no summary)'}"]
    elems = data.get("elements") or []
    if elems:
        lines.append("Elements:")
        for i, e in enumerate(elems[:25], 1):
            try:
                nx, ny = float(e.get("x", -1)), float(e.get("y", -1))
                if not (0.0 <= nx <= 1000.0 and 0.0 <= ny <= 1000.0):
                    continue
                x = int(round(nx / 1000.0 * w))
                y = int(round(ny / 1000.0 * h))
                label = str(e.get("label", "unnamed")).strip()
                conf = e.get("confidence")
                conf_s = f"{float(conf):.2f}" if conf is not None else "?"
                lines.append(f"  {i}. {label} — ({x}, {y}) conf {conf_s}")
            except Exception:
                continue
    return "\n".join(lines)


def _norm_pt(v, span: int):
    """Normalized 0-1000 model coordinate → real pixel in [0, span-1]."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= v <= 1000.0):
        return None
    return max(0, min(span - 1, int(round(v / 1000.0 * span))))


def _execute_loop_step(step, sw: int, sh: int) -> str:
    """Validate + execute ONE model-chosen step. Never raises."""
    if not isinstance(step, dict):
        return "SKIPPED (bad step format)"
    action = str(step.get("action", "")).lower().strip()
    if action not in _TASK_ACTION_WHITELIST:
        return f"SKIPPED (action '{action}' not allowed)"

    try:
        if action == "done":
            return "DONE"

        if action in ("click", "double_click", "right_click"):
            x = _norm_pt(step.get("x"), sw)
            y = _norm_pt(step.get("y"), sh)
            if x is None or y is None:
                return f"SKIPPED (bad coordinates {step.get('x')},{step.get('y')})"
            button = "right" if action == "right_click" else "left"
            clicks = 2 if action == "double_click" else 1
            _click(x=x, y=y, button=button, clicks=clicks)
            return f"{action} ({x},{y})"

        if action == "type":
            text = str(step.get("text", ""))
            if not text:
                return "SKIPPED (empty text)"
            return _smart_type(text[:200], clear_first=True)

        if action == "hotkey":
            keys = "".join(str(step.get("keys", "")).split()).lower()
            if keys not in _SAFE_HOTKEYS:
                return f"SKIPPED (hotkey '{keys}' not in safe list)"
            parts = [k.strip() for k in keys.split("+")]
            return _hotkey(*parts)

        if action == "scroll":
            direction = str(step.get("direction", "down")).lower()
            if direction not in ("up", "down", "left", "right"):
                direction = "down"
            amount = min(max(int(step.get("amount", 3)), 1), 20)
            return _scroll(direction=direction, amount=amount)

        if action == "wait":
            secs = min(max(float(step.get("seconds", 1.0)), 0.1), 5.0)
            time.sleep(secs)
            return f"waited {secs:.1f}s"
    except Exception as e:
        return f"FAILED ({e})"
    return "SKIPPED"


def _task_result(goal: str, journal: list[str], reason: str) -> str:
    head = {
        "completed":      f"Task '{goal}' completed",
        "time limit":     f"Task '{goal}' stopped: time limit reached",
        "step limit":     f"Task '{goal}' stopped: step limit reached",
        "vision failure": f"Task '{goal}' stopped: vision model unavailable",
    }.get(reason, f"Task '{goal}' stopped: {reason}")
    if not journal:
        return head + " (no steps taken)."
    return head + f" after {len(journal)} step(s):\n" + "\n".join(journal)


def _run_screen_task(goal: str, max_steps=None, max_seconds=None,
                     player=None, hide_ui: bool = True) -> str:
    """Autonomous observe→decide→act loop for one goal.

    hide_ui=True hides the JARVIS HUD for the duration (via the thread-safe
    player facade) so the loop can see — and click — what is behind it. The
    HUD is restored in the finally block no matter how the loop ends."""
    _require_pyautogui()
    goal = (goal or "").strip()
    if not goal:
        return "No goal given for screen_task."
    max_steps   = min(max(int(max_steps or _DEFAULT_MAX_STEPS), 1), 15)
    max_seconds = min(max(float(max_seconds or _DEFAULT_MAX_SECONDS), 10.0), 180.0)

    hide_ui = bool(hide_ui)
    if hide_ui and player is not None and hasattr(player, "hide_ui"):
        try:
            player.hide_ui()
        except Exception as e:
            print(f"[ComputerControl] could not hide HUD: {e}")
        time.sleep(0.8)   # let the window actually disappear before the first frame
    try:
        return _screen_task_loop(goal, max_steps, max_seconds, player)
    finally:
        if hide_ui and player is not None and hasattr(player, "show_ui"):
            try:
                player.show_ui()
            except Exception as e:
                print(f"[ComputerControl] could not restore HUD: {e}")


def _screen_task_loop(goal: str, max_steps: int, max_seconds: float,
                      player=None) -> str:
    sw, sh = pyautogui.size()
    started   = time.time()
    journal: list[str] = []
    failures  = 0

    for step_no in range(1, max_steps + 1):
        if time.time() - started > max_seconds:
            return _task_result(goal, journal, "time limit")

        img_b, _scale = _capture_frame()
        history = ""
        if journal:
            recent = " | ".join(journal[-3:])
            history = (f"Your recent steps were: {recent}. Do NOT repeat a step "
                       "that did not change the screen — try a different "
                       "approach instead.\n")
        prompt = (
            f"You are controlling a Windows PC on behalf of the user. "
            f"Goal: {goal}\n"
            f"{history}"
            "Give element positions as NORMALIZED coordinates: x from 0 (left "
            "edge) to 1000 (right edge), y from 0 (top edge) to 1000 (bottom "
            "edge) — e.g. the screen center is x=500, y=500. Prefer keyboard "
            "actions over clicking (for example: hotkey win, then type, then "
            "hotkey enter to launch the search result). After typing into a "
            "search box, always press enter. Choose exactly ONE next action "
            'from this list: {"action":"click","x":0-1000,"y":0-1000} | '
            '{"action":"double_click","x":0-1000,"y":0-1000} | '
            '{"action":"right_click","x":0-1000,"y":0-1000} | '
            '{"action":"type","text":"..."} | '
            '{"action":"hotkey","keys":"one of: ' + ", ".join(sorted(_SAFE_HOTKEYS)) + '"} | '
            '{"action":"scroll","direction":"up|down","amount":int} | '
            '{"action":"wait","seconds":0.5-3} | '
            '{"action":"done"}\n'
            'Reply {"action":"done"} ONLY when the current screenshot already '
            'shows the goal achieved. '
            'Reply with ONLY a JSON object: {"thought": "short reason", '
            '"step": <one action object from the list above>}. Never pick an '
            'action outside this list.'
        )
        data = _vision_json(img_b, prompt, tier=None)
        if not data:
            failures += 1
            journal.append("(vision failed — retrying)")
            if failures >= 2:
                return _task_result(goal, journal, "vision failure")
            time.sleep(_LOOP_SETTLE_SECONDS)
            continue
        failures = 0

        step    = data.get("step") or data
        thought = str(data.get("thought", "")).strip()
        msg     = _execute_loop_step(step, sw, sh)
        line    = f"Step {step_no}: {msg}"
        if thought:
            line = f"Step {step_no} [{thought}]: {msg}"
        journal.append(line)
        if player:
            try:
                player.write_log(f"[Vision] {line}")
            except Exception:
                pass
        if msg.startswith("DONE"):
            return _task_result(goal, journal, "completed")
        time.sleep(_LOOP_SETTLE_SECONDS)   # let the UI settle before the next frame

    return _task_result(goal, journal, "step limit")

def computer_control(
    parameters: dict,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """
    Dispatch table for all computer control actions.

    parameters keys (all optional unless noted):
      action        : (required) one of the actions listed below
      text          : text to type or paste
      x, y          : screen coordinates
      button        : 'left' | 'right' (default: left)
      keys          : hotkey string, e.g. 'ctrl+c'
      key           : single key name, e.g. 'enter'
      direction     : 'up' | 'down' | 'left' | 'right'
      amount        : scroll amount (default: 3)
      seconds       : wait duration
      title         : window title fragment for focus_window
      description   : natural-language element description for screen_find/click
      type          : data type for random_data
      field         : memory field name for user_data
      clear_first   : bool, clear field before typing (default: true)
      path          : save path for screenshot (must be inside home dir)

    Actions:
      type          — type text at cursor
      smart_type    — clear field + type (clipboard-backed)
      click         — left click
      double_click  — double left click
      right_click   — right click
      move          — move mouse
      drag          — click-drag between two points
      hotkey        — key combination
      press         — single key
      scroll        — scroll the wheel
      copy          — read clipboard
      paste         — write + paste clipboard
      screenshot    — capture screen (safe path only)
      wait          — sleep N seconds
      clear_field   — select-all + delete
      focus_window  — bring window to foreground
      screen_find   — AI element finder (returns x,y)
      screen_click  — AI element finder + click
      screen_observe — one structured look (summary + elements with coords)
      screen_task   — autonomous bounded observe→act loop for a goal
      random_data   — generate fake form data
      user_data     — pull real data from memory
    """
    params = parameters or {}
    action = params.get("action", "").lower().strip()

    if not action:
        return "No action specified for computer_control."

    if player:
        player.write_log(f"[Computer] {action}")

    print(f"[ComputerControl] ▶ {action}  {params}")

    try:

        if action == "type":
            return _type(params.get("text", ""))

        if action == "smart_type":
            return _smart_type(
                params.get("text", ""),
                clear_first=params.get("clear_first", True),
            )

        if action in ("click", "left_click"):
            return _click(params.get("x"), params.get("y"), "left", 1)

        if action == "double_click":
            return _click(params.get("x"), params.get("y"), "left", 2)

        if action == "right_click":
            return _click(params.get("x"), params.get("y"), "right", 1)

        if action == "move":
            return _move(int(params.get("x", 0)), int(params.get("y", 0)))

        if action == "drag":
            return _drag(
                int(params.get("x1", 0)), int(params.get("y1", 0)),
                int(params.get("x2", 0)), int(params.get("y2", 0)),
            )

        if action == "hotkey":
            raw  = params.get("keys", "")
            keys = [k.strip() for k in raw.split("+")] if isinstance(raw, str) else raw
            return _hotkey(*keys)

        if action == "press":
            return _press(params.get("key", "enter"))

        if action == "scroll":
            return _scroll(
                direction=params.get("direction", "down"),
                amount=int(params.get("amount", 3)),
            )

        if action == "copy":
            return _clipboard_get()

        if action == "paste":
            return _clipboard_paste(params.get("text", ""))

        if action == "screenshot":
            return _screenshot(params.get("path"))

        if action == "screen_find":
            coords = _screen_find(params.get("description", ""))
            return f"{coords[0]},{coords[1]}" if coords else "NOT_FOUND"

        if action == "screen_click":
            desc   = params.get("description", "")
            coords = _screen_find(desc)
            if coords:
                time.sleep(0.2)
                _click(x=coords[0], y=coords[1])
                return f"Clicked '{desc}' at {coords}"
            return f"Element not found on screen: '{desc}'"

        if action == "screen_observe":
            return _observe_screen()

        if action == "screen_task":
            return _run_screen_task(
                goal=params.get("goal", ""),
                max_steps=params.get("max_steps"),
                max_seconds=params.get("max_seconds"),
                player=player,
                hide_ui=params.get("hide_ui", True),
            )

        if action == "wait":
            secs = float(params.get("seconds", 1.0))
            secs = min(secs, 30.0)
            time.sleep(secs)
            return f"Waited {secs}s"

        if action == "clear_field":
            return _clear_field()

        if action == "focus_window":
            return _focus_window(params.get("title", ""))

        if action == "random_data":
            dt     = params.get("type", "name")
            result = _random_data(dt)
            print(f"[ComputerControl] 🎲 random {dt} → {result}")
            return result

        if action == "user_data":
            field   = params.get("field", "name")
            profile = _user_profile()
            value   = profile.get(field, "")
            if not value:
                value = _random_data(field)
                print(f"[ComputerControl] ⚠️ No '{field}' in memory, using random: {value}")
            return value

        return f"Unknown action: '{action}'"

    except Exception as e:
        print(f"[ComputerControl] ❌ {action}: {e}")
        return f"computer_control '{action}' failed: {e}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "computer_control",
    "description": (
        "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, "
        "find elements on screen, and vision. Use action 'screen_observe' to take one "
        "structured look at the screen (summary + clickable elements with coordinates). "
        "Use action 'screen_task' with a 'goal' to run an autonomous bounded loop that "
        "keeps looking at the screen and acting (click/type/hotkey/scroll) until the goal "
        "is done or the step/time limit hits — prefer it when the task needs several "
        "screen-guided steps (e.g. 'open Settings and turn Bluetooth on')."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | screen_observe | screen_task | random_data | user_data"
            },
            "text": {
                "type": "STRING",
                "description": "Text to type or paste"
            },
            "x": {
                "type": "INTEGER",
                "description": "X coordinate"
            },
            "y": {
                "type": "INTEGER",
                "description": "Y coordinate"
            },
            "keys": {
                "type": "STRING",
                "description": "Key combination e.g. 'ctrl+c'"
            },
            "key": {
                "type": "STRING",
                "description": "Single key e.g. 'enter'"
            },
            "direction": {
                "type": "STRING",
                "description": "up | down | left | right"
            },
            "amount": {
                "type": "INTEGER",
                "description": "Scroll amount (default: 3)"
            },
            "seconds": {
                "type": "NUMBER",
                "description": "Seconds to wait"
            },
            "title": {
                "type": "STRING",
                "description": "Window title for focus_window"
            },
            "description": {
                "type": "STRING",
                "description": "Element description for screen_find/screen_click"
            },
            "goal": {
                "type": "STRING",
                "description": "Natural-language task for screen_task, e.g. 'open Settings and turn Bluetooth on'"
            },
            "max_steps": {
                "type": "INTEGER",
                "description": "Step cap for screen_task (default 8, max 15)"
            },
            "max_seconds": {
                "type": "NUMBER",
                "description": "Time cap for screen_task (default 90, max 180)"
            },
            "hide_ui": {
                "type": "BOOLEAN",
                "description": "Hide the JARVIS HUD while screen_task runs so the screen is fully visible (default: true)"
            },
            "type": {
                "type": "STRING",
                "description": "Data type for random_data"
            },
            "field": {
                "type": "STRING",
                "description": "Field for user_data: name|email|city"
            },
            "clear_first": {
                "type": "BOOLEAN",
                "description": "Clear field before typing (default: true)"
            },
            "path": {
                "type": "STRING",
                "description": "Save path for screenshot"
            }
        },
        "required": [
            "action"
        ]
    },
    "handler": computer_control,
}
