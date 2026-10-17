"""Real-time Windows toast listener.

JARVIS reads other apps' toast notifications through the WinRT
UserNotificationListener API (winsdk package). One background thread owns
the listener and pushes parsed toasts into a small hub, the same pattern
as the camera frame hub in screen_processor.py — a pending queue plus a
bounded history, both guarded by one lock.

Privacy: full toast text lives only in this module's memory structures.
Callers must log the app name only, never the text.
"""

import asyncio
import json
import sys
import threading
import time
from collections import deque
from pathlib import Path

from core import confirm

_ROOT = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _ROOT / "config" / "api_keys.json"

try:
    from winsdk.windows.ui.notifications.management import (
        UserNotificationListener,
        UserNotificationListenerAccessStatus,
    )
    from winsdk.windows.ui.notifications import (
        KnownNotificationBindings,
        NotificationKinds,
        UserNotificationChangedKind,
    )
    WINSDK_AVAILABLE = True
except Exception:  # environment without winsdk
    WINSDK_AVAILABLE = False

# Stable AUMID: Windows ties the "read notifications" permission grant to
# this string. Changing it silently resets the user's grant.
NOTIF_AUMID = "MarkLV.JARVIS.Notifications"

_PENDING_CAP = 50
_RECENT_CAP = 200

_lock = threading.Lock()
_recent = deque(maxlen=_RECENT_CAP)    # newest first: {"app", "text", "time"}
_pending = deque(maxlen=_PENDING_CAP)  # not yet drained by the session
_seq = 0                               # bumped on every push
_status = "off"                        # off|starting|allowed|waiting|denied|error|unavailable
_thread = None
_stop = threading.Event()
_loop = None                           # listener thread's asyncio loop


def _load_config(path: Path | None = None) -> dict:
    p = path or _CONFIG_PATH
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(cfg: dict, path: Path | None = None) -> bool:
    p = path or _CONFIG_PATH
    try:
        p.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        return True
    except Exception:
        return False


def notifications_enabled(path: Path | None = None) -> bool:
    """Whether the user has switched toast reading on (config key)."""
    return bool(_load_config(path).get("notifications", {}).get("enabled", False))


def set_enabled(on: bool, path: Path | None = None) -> str:
    """Persist the enabled flag inside config (notifications section) — "ok" or "error"."""
    cfg = _load_config(path)
    ncfg = dict(cfg.get("notifications") or {})
    ncfg["enabled"] = bool(on)
    ncfg.setdefault("mode", "collect")
    cfg["notifications"] = ncfg
    return "ok" if _save_config(cfg, path) else "error"


def _set_status(s: str) -> None:
    global _status
    with _lock:
        _status = s


def get_status() -> str:
    with _lock:
        return _status


def _set_explicit_aumid() -> None:
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(NOTIF_AUMID)
    except Exception:
        pass


def extract_text_lines(binding) -> list:
    """All non-empty text lines from a toast binding (pure helper, stub-testable)."""
    out = []
    for t in binding.get_text_elements() or []:
        s = (t.text or "").strip()
        if s:
            out.append(s)
    return out


def push(app: str, lines: list, ts: float | None = None, nid=None) -> None:
    """Record one toast in the hub. Called from the listener thread."""
    global _seq
    entry = {"app": app, "text": "\n".join(lines), "time": ts or time.time(),
             "nid": nid}
    with _lock:
        _recent.appendleft(entry)
        _pending.append(entry)
        _seq += 1


def drain() -> list:
    """Return and remove toasts not yet seen by the session (newest last)."""
    with _lock:
        out = list(_pending)
        _pending.clear()
    return out


def get_recent(n: int = 10) -> list:
    with _lock:
        return list(_recent)[:n]


def _parse_un(un) -> dict:
    """(app, lines) from one UserNotification; never raises."""
    app = "Unknown app"
    lines: list = []
    try:
        app = un.app_info.display_info.display_name or app
        binding = un.notification.visual.get_binding(
            KnownNotificationBindings.toast_generic)
        if binding is not None:
            lines = extract_text_lines(binding)
    except Exception:
        lines = []
    if not lines:
        lines = ["(no text)"]
    return {"app": app, "lines": lines}


def _ingest_un(un) -> None:
    try:
        e = _parse_un(un)
        push(e["app"], e["lines"], nid=getattr(un, "id", None))
    except Exception as e:
        print(f"[Notif] ingest error: {e}")


async def _ingest_async(listener, nid) -> None:
    """Event path: fetch one toast by id and push it. Runs on the listener loop."""
    try:
        un = listener.get_notification(nid)  # sync in this winsdk build
        if un is not None:
            _ingest_un(un)
    except Exception as e:
        print(f"[Notif] ingest error: {e}")


async def _listen_async() -> None:
    """The listener thread body. Exits when _stop is set."""
    import asyncio
    global _loop
    listener = UserNotificationListener.current
    status = await listener.request_access_async()
    print(f"[Notif] Access status: {status}")
    if status == UserNotificationListenerAccessStatus.DENIED:
        _set_status("denied")
        _stop.wait()  # denied is final; nothing to do but wait for shutdown
        return
    # The consent prompt may still be unanswered (UNSPECIFIED). Registering a
    # change handler before access is ALLOWED fails with 0x80070490, so poll
    # the live status until the user answers, re-fetching the listener in case
    # the consent flow invalidated the cached instance.
    while status != UserNotificationListenerAccessStatus.ALLOWED:
        if _stop.is_set():
            return
        await asyncio.sleep(1.0)
        listener = UserNotificationListener.current
        status = listener.get_access_status()
        if status == UserNotificationListenerAccessStatus.DENIED:
            _set_status("denied")
            _stop.wait()
            return
    _set_status("allowed")
    try:
        loop = asyncio.get_running_loop()
        _loop = loop
        tid = threading.get_ident()

        def on_changed(sender, args) -> None:
            if args.change_kind != UserNotificationChangedKind.ADDED:
                return
            if threading.get_ident() == tid:
                loop.create_task(_ingest_async(listener, args.user_notification_id))
            else:
                asyncio.run_coroutine_threadsafe(
                    _ingest_async(listener, args.user_notification_id), loop)

        registered = False
        for _attempt in range(2):
            try:
                listener.add_notification_changed(on_changed)
                registered = True
                print("[Notif] Event handler registered.")
                break
            except OSError:
                await asyncio.sleep(1.0)
                listener = UserNotificationListener.current
        if not registered:
            # winsdk beta.10 fails change-handler registration with 0x80070490
            # even when access is ALLOWED — fall back to a poll-and-diff loop,
            # which suits collect-on-demand mode anyway.
            print("[Notif] Event registration unavailable — polling every 2s.")

        if not registered:
            seen: deque = deque(maxlen=2000)
            for un in await listener.get_notifications_async(NotificationKinds.TOAST):
                seen.append(un.id)  # baseline: only NEW arrivals count
            print(f"[Notif] Poll baseline: {len(seen)} toast(s) already present.")

        try:
            while not _stop.wait(2.0 if not registered else 1.0):
                if registered:
                    continue
                try:
                    current = await listener.get_notifications_async(
                        NotificationKinds.TOAST)
                except Exception as e:
                    print(f"[Notif] poll error: {e}")
                    continue
                for un in current:
                    if un.id in seen:
                        continue
                    seen.append(un.id)
                    _ingest_un(un)
        finally:
            if registered:
                listener.remove_notification_changed(on_changed)
            _loop = None
    except Exception as e:
        _set_status("error")
        print(f"[Notif] Listener loop error: {e}")


def _listen_loop() -> None:
    import asyncio
    global _thread
    try:
        asyncio.run(_listen_async())
    except Exception as e:
        _set_status("error")
        print(f"[Notif] Listener error: {e}")
    finally:
        if _thread is threading.current_thread():
            _thread = None
        _set_status("off")


def start_listener() -> str:
    """Start the single listener thread (idempotent). Returns the status."""
    global _thread
    if not WINSDK_AVAILABLE:
        _set_status("unavailable")
        return "unavailable"
    with _lock:
        if _thread is not None and _thread.is_alive():
            return _status
        _set_explicit_aumid()
        _stop.clear()
        t = threading.Thread(target=_listen_loop, daemon=True, name="notif-listener")
        _thread = t
    t.start()
    return "starting"


def stop_listener() -> None:
    _stop.set()


def _run_on_loop(coro, timeout: float = 10.0):
    """Run a coroutine on the listener thread's loop; None on any failure."""
    if not WINSDK_AVAILABLE or _loop is None:
        return None
    import asyncio
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        return fut.result(timeout)
    except Exception as e:
        print(f"[Notif] loop call failed: {e}")
        return None


def current_notifications(timeout: float = 10.0) -> list:
    """On-demand read of the newest toasts in the notification center."""

    async def _collect():
        out = []
        listener = UserNotificationListener.current
        notifications = await listener.get_notifications_async(NotificationKinds.TOAST)
        for un in list(notifications)[-20:]:  # newest 20; the view is oldest-first
            e = _parse_un(un)
            ts = None
            if hasattr(un.creation_time, "timestamp"):
                ts = un.creation_time.timestamp()
            out.append({"app": e["app"], "text": "\n".join(e["lines"]), "time": ts,
                        "nid": getattr(un, "id", None)})
            await asyncio.sleep(0)
        return out

    result = _run_on_loop(_collect(), timeout)
    return result if result is not None else []


async def _remove_async(nid) -> bool:
    """Remove one toast from the center. Runs on the listener loop.

    In winsdk beta.10 this is a fast synchronous call (~90 ms measured)."""
    try:
        UserNotificationListener.current.remove_notification(nid)
        return True
    except Exception as e:
        print(f"[Notif] remove error: {e}")
        return False


async def _clear_async() -> bool:
    try:
        UserNotificationListener.current.clear_notifications()
        return True
    except Exception as e:
        print(f"[Notif] clear error: {e}")
        return False


def _open_action_center() -> str:
    """Open the Action Center (Win+N Win11 >= 22000, Win+A Win10). '' on success."""
    try:
        import pyautogui
        build = sys.getwindowsversion().build
        pyautogui.hotkey("win", "n" if build >= 22000 else "a")
        return ""
    except Exception as e:
        return str(e)


def _close_action_center() -> None:
    try:
        import pyautogui
        pyautogui.press("esc")
    except Exception:
        pass


def _open_click(app: str, text: str, timeout: float = 8.0) -> tuple:
    """Click the Action Center toast whose row matches app/text.

    Returns (status, detail) with status in ok|nomatch|unavailable. Never
    raises — the center renders asynchronously, so this polls the UIA tree
    until the deadline."""
    try:
        from pywinauto import Desktop
    except Exception as e:
        return ("unavailable", f"pywinauto missing: {e}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            desktop = Desktop(backend="uia")
            wins = list(desktop.windows(class_name="Windows.UI.Core.CoreWindow"))
        except Exception as e:
            return ("unavailable", str(e))
        if not wins:
            time.sleep(0.5)
            continue
        for w in wins:
            try:
                items = list(w.descendants(control_type="ListItem"))
            except Exception:
                continue
            for item in items:
                try:
                    item_text = item.window_text() or ""
                except Exception:
                    continue
                if not _matches({"app": item_text, "text": item_text}, app, text):
                    continue
                try:
                    item.click_input()
                except Exception as e:
                    return ("unavailable", str(e))
                return ("ok", "")
        time.sleep(0.7)
    return ("nomatch", "")


# ── Collect-on-demand tool ─────────────────────────────────────────────────
#
# The user chose collect-on-demand (2026-10-14): toasts are never announced
# spontaneously; JARVIS reports them when asked. Logs (jarvis.log and the HUD
# activity log) carry app names only — toast text exists solely in the hub
# memory and in the tool reply to the user.


def notification_mode() -> str:
    mode = str(_load_config().get("notifications", {}).get("mode", "collect")).strip().lower()
    return mode if mode in ("collect",) else "collect"


def _format_events(events: list, limit: int | None = None) -> str:
    out = []
    for e in (events[:limit] if limit else events):
        stamp = ""
        if e.get("time"):
            stamp = time.strftime("%H:%M", time.localtime(e["time"])) + " "
        out.append(f"{stamp}{e['app']}: {e['text']}")
    return "\n".join(out)


def _log_apps_only(prefix: str, events: list, player=None) -> None:
    apps = sorted({e["app"] for e in events})
    note = f"{prefix} {len(events)} toast(s) from {', '.join(apps) if apps else 'nobody'}"
    print(f"[Notif] {note}")
    if player:
        player.write_log(f"[Notif] {note}")


def _matches(e: dict, app: str, text: str) -> bool:
    """Case-insensitive substring match on app and text ('' matches anything)."""
    a, t = app.strip().lower(), text.strip().lower()
    ea = str(e.get("app") or "").lower()
    et = str(e.get("text") or "").lower()
    return (not a or a in ea) and (not t or t in et)


def _find_entry(events: list, app: str, text: str) -> dict | None:
    """First (newest) entry matching app/text, or None."""
    for e in events:
        if _matches(e, app, text):
            return e
    return None


def _drop_from_hub(nid) -> None:
    """Remove hub entries carrying this id (recent + pending)."""
    global _recent, _pending
    if not nid:
        return
    with _lock:
        _recent = deque([e for e in _recent if e.get("nid") is None or e.get("nid") != nid],
                        maxlen=_RECENT_CAP)
        _pending = deque([e for e in _pending if e.get("nid") is None or e.get("nid") != nid],
                         maxlen=_PENDING_CAP)


_PERMISSION_GUIDANCE = (
    "If a Windows prompt appears asking to allow JARVIS to read notifications, "
    "the user must click Allow/Yes. If the prompt was missed or previously "
    "denied, open Settings > System > Notifications and turn ON 'JARVIS' under "
    "'Notifications from apps and other senders'."
)


def notifications(parameters: dict = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).strip().lower().replace(" ", "_")

    if action == "enable":
        if set_enabled(True) != "ok":
            return "Could not save the notifications setting (config write failed)."
        st = start_listener()
        print("[Notif] Toast reading enabled by user request.")
        if player:
            player.write_log("[Notif] Toast reading enabled")
        return (f"Toast reading is now ON (collect-on-demand — nothing is "
                f"announced unless asked). {_PERMISSION_GUIDANCE} "
                f"Listener status: {st}.")

    if action == "disable":
        if set_enabled(False) != "ok":
            return "Could not save the notifications setting (config write failed)."
        stop_listener()
        print("[Notif] Toast reading disabled by user request.")
        if player:
            player.write_log("[Notif] Toast reading disabled")
        return "Toast reading is now OFF. JARVIS will no longer capture notifications."

    if action in ("", "new", "unread"):
        events = drain()
        _log_apps_only("query new:", events, player)
        if events:
            return _format_events(events)
        status = get_status()
        if status == "unavailable":
            return "Toast reading is not available in this install (winsdk missing)."
        if status == "denied":
            return ("No notifications captured. The Windows permission to read "
                    "notifications was denied — the user can re-enable it under "
                    "Settings > Privacy > Notifications.")
        if status == "off":
            return ("No notifications captured — toast reading is switched off "
                    "(settings > notifications).")
        if status == "waiting":
            return ("No notifications captured yet — waiting for the user to allow "
                    "notification access (a Windows prompt may be showing).")
        return "No new notifications."

    if action == "recent":
        limit = int(params.get("limit") or 5)
        events = get_recent(limit)
        _log_apps_only("query recent:", events, player)
        if not events:
            return "No notifications captured yet."
        return _format_events(events)

    if action == "current":
        events = current_notifications()
        _log_apps_only("query current:", events, player)
        if not events:
            return "No notifications currently in the notification center."
        return _format_events(events)

    if action == "dismiss":
        app = str(params.get("app") or "").strip()
        text = str(params.get("text") or "").strip()
        if not app and not text:
            return ("Tell me which notification to dismiss — give the app name "
                    "and/or part of the message text.")
        if get_status() != "allowed":
            return (f"Cannot dismiss right now — listener status is "
                    f"'{get_status()}' (needs 'allowed').")
        entry = _find_entry(get_recent(50), app, text)
        if entry is None:
            entry = _find_entry(list(reversed(current_notifications())), app, text)
        if entry is None:
            return (f"No matching notification found for app '{app or '?'}' / "
                    f"text '{text or '?'}'.")
        if not entry.get("nid"):
            return ("I found it, but I have no id for it, so I can't dismiss it "
                    "individually.")
        if not _run_on_loop(_remove_async(entry["nid"])):
            return "Found the notification, but Windows refused to remove it."
        _drop_from_hub(entry["nid"])
        _log_apps_only("dismiss:", [entry], player)
        return f"Dismissed the notification from {entry['app']}."

    if action == "clear":
        if get_status() != "allowed":
            return (f"Cannot clear right now — listener status is "
                    f"'{get_status()}' (needs 'allowed').")

        def _do_clear():
            if _run_on_loop(_clear_async()):
                with _lock:
                    _recent.clear()
                    _pending.clear()
                return "The notification center was cleared."
            return "Windows refused to clear the notification center."

        return confirm.request(
            "notif_clear",
            "Clear all notifications",
            "JARVIS wants to remove every toast from the Windows notification center.",
            _do_clear,
        )

    if action == "open":
        app = str(params.get("app") or "").strip()
        text = str(params.get("text") or "").strip()
        if not app and not text:
            return ("Tell me which notification to open — give the app name "
                    "and/or part of the message text.")
        entry = _find_entry(get_recent(50), app, text)  # label only; click is UIA
        err = _open_action_center()
        if err:
            return f"Could not open the notification center: {err}"
        status, detail = _open_click(app, text)
        if status == "ok":
            if entry is not None and entry.get("app"):
                _log_apps_only("open:", [entry], player)
                return (f"Opened the notification from {entry['app']} — its app "
                        f"should be on screen now.")
            _log_apps_only("open:", [{"app": "unknown app"}], player)
            return "Opened that notification — its app should be on screen now."
        _close_action_center()
        if status == "nomatch":
            return ("I opened the notification center but could not find that "
                    "toast in it to click.")
        return f"Could not click the notification: {detail}"

    if action == "status":
        return (f"Notification listener status: {get_status()} "
                f"(mode: {notification_mode()}).")

    return ("Unknown action. Call notifications again with `action` one of: "
            "new, recent, current, status, enable, disable, dismiss, clear, open.")


TOOL = {
    "name": "notifications",
    "description": (
        "Read the Windows toast notifications JARVIS has captured in real time, "
        "switch the feature on/off, and manage the notification center. "
        "Collect-on-demand mode: toasts are never announced spontaneously — use "
        "this tool when the user asks things like 'any notifications?', 'what "
        "did I miss?' or 'check my messages'. Sub-actions: new (toasts arrived "
        "since the last check, default), recent (the latest few, limit default "
        "5), current (toasts still in the Windows notification center), status "
        "(listener and permission state), enable (turn toast reading on — a "
        "Windows permission prompt appears for the user the first time; tell "
        "the user to click Allow), disable (turn it off), dismiss (remove ONE "
        "notification from the center — say which one by app name and/or part "
        "of its text via app= and text=), clear (remove ALL notifications from "
        "the center — a confirmation banner appears on the HUD which the user "
        "must press CONFIRM on before anything is removed), open (click a "
        "notification in the Windows notification center so its source app "
        "opens — say which one via app= and/or text=; the Action Center "
        "briefly appears on screen and closes again). Report the text "
        "only in your reply — do not read it aloud unless the user asks."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "new | recent | current | status | enable | disable | dismiss | clear | open (default new)",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Max toasts for recent (default 5)",
            },
            "app": {
                "type": "STRING",
                "description": "For dismiss/open: app name to match (case-insensitive substring)",
            },
            "text": {
                "type": "STRING",
                "description": "For dismiss/open: part of the toast text to match (case-insensitive substring)",
            },
        },
    },
    "handler": notifications,
    "redact_log": True,
}
