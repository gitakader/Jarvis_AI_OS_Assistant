"""Offline tests for actions/notifications.py (toast listener hub).

Never starts the real listener thread — that would trigger the Windows
permission prompt. The winsdk-dependent path is exercised through the
monkeypatched "unavailable" branch and pure helpers only.
"""
import io
import sys
from contextlib import redirect_stdout

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

import actions.notifications as n

_checks = 0
_fails = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global _checks, _fails
    _checks += 1
    if ok:
        print(f"[PASS] {name}")
    else:
        _fails += 1
        print(f"[FAIL] {name} {detail}")


# --- 1. no-winsdk fallback: start_listener must degrade, not crash ---
real_flag = n.WINSDK_AVAILABLE
n.WINSDK_AVAILABLE = False
st = n.start_listener()
check("fallback: start_listener returns unavailable", st == "unavailable", f"got {st!r}")
check("fallback: status set to unavailable", n.get_status() == "unavailable",
      f"got {n.get_status()!r}")
check("fallback: no thread started", n._thread is None)
st2 = n.start_listener()
check("fallback: idempotent", st2 == "unavailable", f"got {st2!r}")
n.WINSDK_AVAILABLE = real_flag
n._set_status("off")

# --- 2. extract_text_lines on a stub binding ---
class _T:
    def __init__(self, text):
        self.text = text


class _B:
    def __init__(self, texts):
        self.text_elements = [_T(t) for t in texts]

    def get_text_elements(self):
        return self.text_elements


lines = n.extract_text_lines(_B(["  hello  ", "", None, "world", "  "]))
check("text extraction: non-empty trimmed lines", lines == ["hello", "world"], f"got {lines!r}")
check("text extraction: empty binding", n.extract_text_lines(_B([])) == [])
check("text extraction: None elements", n.extract_text_lines(_B([None, "", None])) == [])

# --- 3. push/drain freshness ---
n._recent.clear()
n._pending.clear()
n.push("AppA", ["one"])
d = n.drain()
check("drain: returns pushed toast", len(d) == 1 and d[0]["app"] == "AppA"
      and d[0]["text"] == "one", f"got {d!r}")
check("drain: second drain is empty", n.drain() == [])
n.push("AppB", ["two"])
n.push("AppC", ["three"])
d = n.drain()
check("drain: oldest first", [e["app"] for e in d] == ["AppB", "AppC"],
      f"got {[e['app'] for e in d]}")

# --- 4. pending cap ---
n._recent.clear()
n._pending.clear()
for i in range(60):
    n.push("Flood", [f"n{i}"])
d = n.drain()
check("pending cap: bounded to 50", len(d) == 50, f"got {len(d)}")
check("pending cap: keeps newest", d[-1]["text"] == "n59" and d[0]["text"] == "n10",
      f"first={d[0]['text']!r} last={d[-1]['text']!r}")

# --- 5. recent order (newest first) ---
n._recent.clear()
n._pending.clear()
n.push("R1", ["a"])
n.push("R2", ["b"])
r = n.get_recent(1)
check("recent: newest first", len(r) == 1 and r[0]["app"] == "R2", f"got {r!r}")
check("recent: limit respected", len(n.get_recent(10)) == 2)

# --- 6. status set/get ---
n._set_status("allowed")
check("status: set/get round-trip", n.get_status() == "allowed")
n._set_status("off")
check("status: reset to off", n.get_status() == "off")

# --- 7. notifications_enabled returns a bool (real config, may lack key) ---
check("enabled: returns bool", isinstance(n.notifications_enabled(), bool))

# --- 8. hub operations print nothing (no full-text leak to logs) ---
n._recent.clear()
n._pending.clear()
buf = io.StringIO()
with redirect_stdout(buf):
    n.push("Secret", ["private-message-body"])
    n.drain()
    n.get_recent(5)
check("hub: push/drain/get_recent print nothing", buf.getvalue() == "",
      f"printed {buf.getvalue()!r}")

# --- 9. track 2: collect-on-demand handler ---
class _FakePlayer:
    def __init__(self):
        self.log = []

    def write_log(self, s):
        self.log.append(s)


n._recent.clear()
n._pending.clear()
n._set_status("allowed")
n.push("WhatsApp", ["Rahim: are you coming?"])
n.push("Mail", ["Meeting at 3 PM"])

player = _FakePlayer()
buf = io.StringIO()
with redirect_stdout(buf):
    out = n.notifications({"action": "new"}, player=player)
check("handler new: returns both toasts", "WhatsApp" in out and "Rahim: are you coming?" in out
      and "Mail" in out and "Meeting at 3 PM" in out, f"got {out!r}")
check("handler new: drains the queue", n.drain() == [])
check("handler log: app names only, no text", "WhatsApp" in player.log[0]
      and "Mail" in player.log[0] and "Rahim" not in player.log[0]
      and "Meeting" not in player.log[0], f"got {player.log!r}")
check("handler stdout: app names only, no text", "WhatsApp" in buf.getvalue()
      and "Rahim" not in buf.getvalue() and "Meeting" not in buf.getvalue(),
      f"got {buf.getvalue()!r}")

n._set_status("off")
out = n.notifications({}, player=None)
check("handler new empty: honest off message", "switched off" in out, f"got {out!r}")
n._set_status("denied")
out = n.notifications({}, player=None)
check("handler new empty: denied message", "denied" in out, f"got {out!r}")
n._set_status("waiting")
out = n.notifications({}, player=None)
check("handler new empty: waiting message", "waiting" in out, f"got {out!r}")
n._set_status("allowed")

n._recent.clear()
n._pending.clear()
n.push("Telegram", ["t1"])
n.push("Discord", ["t2"])
n.push("Slack", ["t3"])
n.drain()
out = n.notifications({"action": "recent", "limit": 2}, player=None)
check("handler recent: limit respected", out.count("\n") == 1 and "Slack" in out
      and "t3" in out and "t1" not in out, f"got {out!r}")
out = n.notifications({"action": "status"}, player=None)
check("handler status: reports state", "listener status: allowed" in out, f"got {out!r}")
out = n.notifications({"action": "bogus"}, player=None)
check("handler unknown action: guidance", "Unknown action" in out and "new" in out,
      f"got {out!r}")
check("mode: default collect", n.notification_mode() == "collect")

# --- 10. track 3: settings toggle (temp config path, real file untouched) ---
import json
import tempfile
from pathlib import Path

_tmpdir = tempfile.mkdtemp(prefix="notif_test_")
_tmpcfg = Path(_tmpdir) / "api_keys.json"
_tmpcfg.write_text(json.dumps({"providers": {"keep": "me"}, "other": 1}),
                   encoding="utf-8")

check("toggle: default disabled (temp cfg)", n.notifications_enabled(_tmpcfg) is False)
check("toggle: enable persists", n.set_enabled(True, _tmpcfg) == "ok"
      and n.notifications_enabled(_tmpcfg) is True)
check("toggle: mode defaulted to collect", "collect" ==
      json.loads(_tmpcfg.read_text(encoding="utf-8"))["notifications"]["mode"])
check("toggle: other keys preserved", "providers" in
      json.loads(_tmpcfg.read_text(encoding="utf-8"))
      and "keep" in json.loads(_tmpcfg.read_text(encoding="utf-8"))["providers"])
check("toggle: disable persists", n.set_enabled(False, _tmpcfg) == "ok"
      and n.notifications_enabled(_tmpcfg) is False)

# handler enable/disable against the temp config
real_path = n._CONFIG_PATH
n._CONFIG_PATH = _tmpcfg
real_flag = n.WINSDK_AVAILABLE
n.WINSDK_AVAILABLE = False  # keep the real listener from starting
out = n.notifications({"action": "enable"}, player=None)
check("handler enable: on + guidance + status", "ON" in out and "Windows prompt" in out
      and "Listener status:" in out, f"got {out!r}")
check("handler enable: persisted", n.notifications_enabled() is True)
out = n.notifications({"action": "disable"}, player=None)
check("handler disable: off message", "OFF" in out, f"got {out!r}")
check("handler disable: persisted", n.notifications_enabled() is False)
n._CONFIG_PATH = real_path
n.WINSDK_AVAILABLE = real_flag
n._set_status("off")

# --- 11. track 5 pre-flight: redact_log flag keeps results out of logs ---
from core.action_loader import discover_actions

check("redact: TOOL flag set", n.TOOL.get("redact_log") is True)
_tmpact = Path(tempfile.mkdtemp(prefix="notif_act_"))
(_tmpact / "t_red.py").write_text(
    'TOOL = {"name": "t_red", "description": "d", "handler": lambda: "secret", "redact_log": True}',
    encoding="utf-8")
(_tmpact / "t_plain.py").write_text(
    'TOOL = {"name": "t_plain", "description": "d", "handler": lambda: "open"}',
    encoding="utf-8")
_reg = discover_actions(_tmpact, logger=lambda *a: None)
check("redact: registry honors flag", _reg.redact_log("t_red") is True
      and _reg.redact_log("t_plain") is False
      and _reg.redact_log("missing") is False)

# --- 12. track 6: dismiss/clear helpers + handler (offline, fakes only) ---
n._recent.clear()
n._pending.clear()

e = {"app": "WhatsApp", "text": "Rahim: are you coming?"}
check("matches: app substring case-insensitive", n._matches(e, "whats", "") is True)
check("matches: text substring case-insensitive", n._matches(e, "", "COMING") is True)
check("matches: both fields", n._matches(e, "App", "rahim") is True)
check("matches: wrong app", n._matches(e, "Telegram", "") is False)
check("matches: wrong text", n._matches(e, "", "xyzzy") is False)
check("matches: empty app+text matches all", n._matches(e, "", "") is True)

ev1 = {"app": "A", "text": "old", "nid": 1}
ev2 = {"app": "A", "text": "new", "nid": 2}
check("find: newest first", n._find_entry([ev1, ev2], "a", "") is ev1)
check("find: no match", n._find_entry([ev1, ev2], "zzz", "") is None)

n.push("AppN", ["body"], nid=77)
r = n.get_recent(1)
check("push: stores nid", r[0].get("nid") == 77, f"got {r!r}")

n.push("AppX", ["x"], nid=1)
n.push("AppY", ["y"], nid=2)
n.push("AppOld", ["no nid"])  # legacy entry without nid
n._drop_from_hub(1)
ids = {e.get("nid") for e in n.get_recent(50)}
check("drop: removes matching nid", 1 not in ids, f"got {ids!r}")
check("drop: keeps other nid", 2 in ids)
check("drop: keeps no-nid entries", None in ids)
n._recent.clear()
n._pending.clear()

class _Recorder:
    def __init__(self):
        self.calls = []
        self.result = True

    def __call__(self, coro, timeout=10.0):
        self.calls.append(coro)
        try:
            coro.close()  # fake never awaits; silence the RuntimeWarning
        except Exception:
            pass
        return self.result


fake_loop = _Recorder()
real_run_on_loop = n._run_on_loop
real_current = n.current_notifications
live_center = []


def fake_current(timeout=10.0):
    return live_center


n._run_on_loop = fake_loop
n.current_notifications = fake_current

n._set_status("allowed")
out = n.notifications({"action": "dismiss"}, player=None)
check("dismiss: no app/text guidance", "which notification" in out, f"got {out!r}")

n._set_status("off")
out = n.notifications({"action": "dismiss", "app": "X"}, player=None)
check("dismiss: honest non-allowed message", "Cannot dismiss" in out and "off" in out,
      f"got {out!r}")
n._set_status("allowed")

out = n.notifications({"action": "dismiss", "app": "zzznope"}, player=None)
check("dismiss: honest no-match message", "No matching notification" in out, f"got {out!r}")

n.push("WhatsApp", ["secret-message-body"], nid=99)
player = _FakePlayer()
buf = io.StringIO()
with redirect_stdout(buf):
    out = n.notifications({"action": "dismiss", "app": "whatsapp", "text": "secret"},
                          player=player)
check("dismiss: success message", out == "Dismissed the notification from WhatsApp.",
      f"got {out!r}")
check("dismiss: remove called on loop", len(fake_loop.calls) == 1,
      f"got {len(fake_loop.calls)}")
check("dismiss: hub entry dropped", n.get_recent(5) == [], f"got {n.get_recent(5)!r}")
check("dismiss: reply has no toast text", "secret" not in out)
check("dismiss: log app-only, no text",
      "WhatsApp" in player.log[0] and "secret" not in player.log[0]
      and "secret" not in buf.getvalue(),
      f"log={player.log!r} stdout={buf.getvalue()!r}")

n.push("LegacyApp", ["old body"], nid=None)
out = n.notifications({"action": "dismiss", "app": "legacy"}, player=None)
check("dismiss: no-nid honest message", "no id" in out, f"got {out!r}")
n._recent.clear()
n._pending.clear()

live_center[:] = [{"app": "Mail", "text": "live body", "time": None, "nid": 55}]
fake_loop.calls.clear()
out = n.notifications({"action": "dismiss", "app": "mail"}, player=None)
check("dismiss: live-center fallback", out == "Dismissed the notification from Mail.",
      f"got {out!r}")
check("dismiss: remove called via fallback", len(fake_loop.calls) == 1)

fake_loop.result = False
n.push("Mail", ["live body"], nid=56)
out = n.notifications({"action": "dismiss", "app": "mail"}, player=None)
check("dismiss: Windows-refused message", "refused" in out, f"got {out!r}")
fake_loop.result = True
n._recent.clear()
n._pending.clear()

class _FakeConfirm:
    def __init__(self):
        self.calls = []

    def request(self, key, title, detail, run):
        self.calls.append((key, title, detail, run))
        return "[CONFIRMATION_PENDING] Clear all notifications"


fake_confirm = _FakeConfirm()
real_confirm_request = n.confirm.request
n.confirm.request = fake_confirm.request

n._set_status("off")
out = n.notifications({"action": "clear"}, player=None)
check("clear: honest non-allowed message", "Cannot clear" in out and "off" in out,
      f"got {out!r}")

n._set_status("allowed")
n.push("AppA", ["a-body"], nid=1)
n.push("AppB", ["b-body"], nid=2)
fake_loop.calls.clear()
out = n.notifications({"action": "clear"}, player=None)
check("clear: gate requested", len(fake_confirm.calls) == 1
      and fake_confirm.calls[0][0] == "notif_clear"
      and bool(fake_confirm.calls[0][2]), f"got {fake_confirm.calls!r}")
check("clear: pending sentence returned", "CONFIRMATION_PENDING" in out, f"got {out!r}")
check("clear: nothing removed yet", len(n.get_recent(5)) == 2)

key, title, detail, run = fake_confirm.calls[0]
res = run()
check("clear: worker success", "cleared" in res, f"got {res!r}")
check("clear: remove called on loop", len(fake_loop.calls) == 1)
check("clear: hub emptied", n.get_recent(5) == [] and n.drain() == [],
      f"got {n.get_recent(5)!r}")

fake_loop.result = False
n.push("AppC", ["c-body"], nid=3)
fake_confirm.calls.clear()
n.notifications({"action": "clear"}, player=None)
key, title, detail, run = fake_confirm.calls[0]
res = run()
check("clear: worker refusal", "refused" in res, f"got {res!r}")
check("clear: hub kept on failure", len(n.get_recent(5)) == 1)
fake_loop.result = True

n.confirm.request = real_confirm_request
n._run_on_loop = real_run_on_loop
n.current_notifications = real_current
n._recent.clear()
n._pending.clear()
n._set_status("off")

# --- 13. track 6.2: open sub-action (UIA fakes only, never real UI) ---
n._recent.clear()
n._pending.clear()

class _FakeUI:
    def __init__(self):
        self.hotkey_calls = 0
        self.hotkey_err = ""
        self.click_result = ("ok", "")
        self.click_calls = []
        self.close_calls = 0

    def hotkey(self):
        self.hotkey_calls += 1
        return self.hotkey_err

    def click(self, app, text, timeout=8.0):
        self.click_calls.append((app, text))
        return self.click_result

    def close(self):
        self.close_calls += 1


fake_ui = _FakeUI()
real_hotkey = n._open_action_center
real_click = n._open_click
real_close = n._close_action_center
n._open_action_center = fake_ui.hotkey
n._open_click = fake_ui.click
n._close_action_center = fake_ui.close

n._set_status("off")  # open must not depend on the listener at all
out = n.notifications({"action": "open"}, player=None)
check("open: no app/text guidance", "which notification" in out, f"got {out!r}")
check("open: no hotkey attempted for guidance", fake_ui.hotkey_calls == 0)

fake_ui.hotkey_err = "hotkey failed"
out = n.notifications({"action": "open", "app": "X"}, player=None)
check("open: hotkey failure honest", "Could not open the notification center" in out
      and "hotkey failed" in out, f"got {out!r}")
check("open: no click on hotkey failure", fake_ui.click_calls == [])
fake_ui.hotkey_err = ""

n.push("WhatsApp", ["secret-open-body"], nid=10)
player = _FakePlayer()
buf = io.StringIO()
with redirect_stdout(buf):
    out = n.notifications({"action": "open", "app": "whatsapp", "text": "secret"},
                          player=player)
check("open: success names hub app",
      out == "Opened the notification from WhatsApp — its app should be on screen now.",
      f"got {out!r}")
check("open: click targeted correctly", fake_ui.click_calls == [("whatsapp", "secret")],
      f"got {fake_ui.click_calls!r}")
check("open: reply has no toast text", "secret-open-body" not in out)
check("open: log app-only, no text",
      "WhatsApp" in player.log[0] and "secret" not in player.log[0]
      and "secret-open-body" not in buf.getvalue(),
      f"log={player.log!r} stdout={buf.getvalue()!r}")
check("open: center not closed after success", fake_ui.close_calls == 0)

n._recent.clear()
n._pending.clear()
fake_ui.click_calls.clear()
out = n.notifications({"action": "open", "app": "telegram"}, player=None)
check("open: no hub label -> generic reply", "Opened that notification" in out,
      f"got {out!r}")

fake_ui.click_result = ("nomatch", "")
out = n.notifications({"action": "open", "app": "telegram"}, player=None)
check("open: nomatch honest", "could not find that toast" in out, f"got {out!r}")
check("open: center closed on nomatch", fake_ui.close_calls == 1)

fake_ui.click_result = ("unavailable", "uia boom")
out = n.notifications({"action": "open", "app": "telegram"}, player=None)
check("open: unavailable honest", "Could not click" in out and "uia boom" in out,
      f"got {out!r}")
check("open: center closed on failure", fake_ui.close_calls == 2)
fake_ui.click_result = ("ok", "")

n._open_action_center = real_hotkey
n._open_click = real_click
n._close_action_center = real_close
n._recent.clear()
n._pending.clear()
n._set_status("off")

print(f"\nRESULT: {_checks - _fails} passed, {_fails} failed")
sys.exit(1 if _fails else 0)
