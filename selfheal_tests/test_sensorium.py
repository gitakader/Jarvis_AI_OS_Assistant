"""Offline checks for the sensorium ambient telemetry daemon (Track A).

Guarded invariants: idle-time math (incl. tick wraparound clamp), dwell
reset on window change, all four alert heuristics + cooldowns, the daemon
loop surviving telemetry exceptions, interjection callback isolation, and
the query TOOL contract."""
import ctypes
import json
import sys
import time as _time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import core.sensorium as SM
import actions.sensorium as AS
from core.action_loader import _validate

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


class _FakeClock:
    """Mutable stand-in for time.time, so dwell/cooldown tests are instant."""
    def __init__(self, t=1000.0):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, dt):
        self.t += dt


class _FakeWin:
    """Fakes user32/kernel32 for GetLastInputInfo / GetTickCount."""
    def __init__(self, now=100_000, last_input=100_000):
        self.now = now
        self.last_input = last_input
    def GetTickCount(self):
        return self.now
    def GetLastInputInfo(self, ref):
        ptr = ctypes.cast(ref, ctypes.POINTER(SM.LASTINPUTINFO))
        ptr.contents.dwTime = self.last_input
        return 1


def _fresh(clock):
    s = SM.PassiveSensorium()
    SM._get_foreground = lambda: {"app": "chrome.exe", "title": "Google", "pid": 1}
    SM.get_idle_time_seconds = lambda: 10.0
    SM._get_vitals = lambda: {"cpu": 12.0, "ram": 45.0,
                              "battery": 80.0, "plugged": True}
    s._was_away = False
    return s


def main():
    orig_user32, orig_kernel32 = SM.user32, SM.kernel32
    orig_time = SM.time.time
    orig_fg = SM._get_foreground
    orig_idle = SM.get_idle_time_seconds
    orig_vitals = SM._get_vitals
    clock = _FakeClock()
    SM.time.time = clock
    try:
        # ── idle math ────────────────────────────────────────────────────────
        fw = _FakeWin()
        SM.user32, SM.kernel32 = fw, fw
        fw.last_input = fw.now - 5000
        check("idle: 5s since last input", SM.get_idle_time_seconds() == 5.0,
              SM.get_idle_time_seconds())
        fw.last_input = fw.now + 1000   # tick wraparound
        check("idle: negative clamped to 0", SM.get_idle_time_seconds() == 0.0,
              SM.get_idle_time_seconds())
        SM.user32, SM.kernel32 = None, None
        check("idle: non-win32 -> 0", SM.get_idle_time_seconds() == 0.0)
        SM.user32, SM.kernel32 = orig_user32, orig_kernel32

        # ── dwell tracking + snapshot ────────────────────────────────────────
        s = _fresh(clock)
        s._update_telemetry()
        check("dwell: first poll starts at 0", s.current_dwell_seconds == 0.0,
              s.current_dwell_seconds)
        check("dwell: process resolved", s.current_process_name == "chrome.exe",
              s.current_process_name)
        clock.advance(30)
        s._update_telemetry()
        check("dwell: accumulates while same window", 29.5 <= s.current_dwell_seconds <= 30.5,
              s.current_dwell_seconds)
        SM._get_foreground = lambda: {"app": "notepad.exe", "title": "Untitled", "pid": 2}
        clock.advance(5)
        s._update_telemetry()
        check("dwell: resets on window change", s.current_dwell_seconds == 0.0,
              s.current_dwell_seconds)
        check("dwell: new process tracked", s.current_process_name == "notepad.exe",
              s.current_process_name)
        snap = s.get_snapshot()
        check("snapshot: window/process fields",
              snap["window_title"] == "Untitled" and snap["process_name"] == "notepad.exe")
        check("snapshot: vitals pass through",
              snap["cpu_percent"] == 12.0 and snap["ram_percent"] == 45.0
              and snap["battery_percent"] == 80.0 and snap["power_plugged"] is True)
        check("snapshot: active at idle 10s", snap["is_user_active"] is True)
        SM.get_idle_time_seconds = lambda: 61.0
        s._update_telemetry()
        check("snapshot: away at idle 61s", s.get_snapshot()["is_user_active"] is False)

        # ── alert heuristics + cooldowns ─────────────────────────────────────
        got = []
        s.register_interjection_handler(lambda t, m: got.append(t))

        s.ram_percent = 95.0
        s._evaluate_heuristics()
        s._evaluate_heuristics()
        check("ram_critical: fires once within cooldown", got.count("ram_critical") == 1,
              got)
        clock.advance(301)
        s._evaluate_heuristics()
        check("ram_critical: re-fires after cooldown", got.count("ram_critical") == 2, got)
        s.ram_percent = 45.0
        clock.advance(301)
        s._evaluate_heuristics()
        check("ram_critical: silent below threshold", got.count("ram_critical") == 2, got)

        s.battery_percent, s.power_plugged = 10.0, False
        s._evaluate_heuristics()
        check("battery_critical: fires unplugged + low",
              got.count("battery_critical") == 1, got)
        s.power_plugged = True
        clock.advance(601)
        s._evaluate_heuristics()
        check("battery_critical: silent while plugged",
              got.count("battery_critical") == 1, got)

        s.current_dwell_seconds, s.user_idle_seconds = 6000.0, 30.0
        s._evaluate_heuristics()
        check("flow_strain: fires on 90min+ dwell while active",
              got.count("flow_strain") == 1, got)
        s.user_idle_seconds = 200.0
        clock.advance(3601)
        s._evaluate_heuristics()
        check("flow_strain: silent while away", got.count("flow_strain") == 1, got)

        s.user_idle_seconds = 400.0
        s._evaluate_heuristics()
        check("welcome_back: no alert while still away",
              got.count("welcome_back") == 0, got)
        s.user_idle_seconds = 1.0
        s._evaluate_heuristics()
        s._evaluate_heuristics()
        check("welcome_back: fires once on return", got.count("welcome_back") == 1, got)
        s.user_idle_seconds = 400.0
        clock.advance(601)
        s._evaluate_heuristics()
        s.user_idle_seconds = 0.5
        s._evaluate_heuristics()
        check("welcome_back: re-fires after cooldown", got.count("welcome_back") == 2, got)

        # ── alert metadata carries speech+message for the presenter ─────────
        meta = {}
        s2 = _fresh(clock)
        s2.register_interjection_handler(lambda t, m: meta.update({t: m}))
        s2.ram_percent = 96.0
        s2._evaluate_heuristics()
        m = meta.get("ram_critical")
        check("alert: metadata has message + speech + value",
              m and "message" in m and "speech" in m and m.get("ram_percent") == 96.0,
              m)

        # ── loop crash-safety ────────────────────────────────────────────────
        s3 = SM.PassiveSensorium(poll_interval=0.02)
        SM._get_foreground = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        s3.start()
        _time.sleep(0.12)
        check("loop: survives telemetry exception", s3._running and s3._thread.is_alive())
        s3.stop()
        check("loop: stop joins the thread", not s3._thread.is_alive())

        # ── callback isolation ───────────────────────────────────────────────
        hits = []
        def _bad(t, m):
            raise ValueError("callback bug")
        s4 = SM.PassiveSensorium()
        s4.register_interjection_handler(_bad)
        s4.register_interjection_handler(lambda t, m: hits.append(t))
        s4._emit_alert("test_alert", {"message": "x"})
        check("emit: raising callback doesn't block others", hits == ["test_alert"], hits)

        # ── singleton exists and is lazy (not running) ───────────────────────
        check("singleton: instance of PassiveSensorium",
              isinstance(SM.sensorium, SM.PassiveSensorium))

        # ── query TOOL ───────────────────────────────────────────────────────
        fake_snap = {"window_title": "Google", "process_name": "chrome.exe",
                     "dwell_seconds": 12.3, "user_idle_seconds": 4.0,
                     "cpu_percent": 11.0, "ram_percent": 33.0,
                     "battery_percent": 77.0, "power_plugged": False,
                     "is_user_active": True, "timestamp": 1.0}
        AS._snapshot = lambda: dict(fake_snap)
        out_ctx = AS.sensorium_action({"action": "context"})
        check("tool context: names app/window/dwell/idle",
              "chrome.exe" in out_ctx and "Google" in out_ctx
              and "12.3s" in out_ctx and "4.0s" in out_ctx, out_ctx)
        out_vit = AS.sensorium_action({"action": "vitals"})
        check("tool vitals: cpu/ram/battery present",
              "11.0" in out_vit and "33.0" in out_vit and "77.0%" in out_vit
              and "on battery" in out_vit, out_vit)
        out_idle = AS.sensorium_action({"action": "idle"})
        check("tool idle: seconds + active", "4.0s" in out_idle and "active" in out_idle,
              out_idle)
        out_json = AS.sensorium_action({"action": "snapshot"})
        check("tool snapshot: valid JSON with fields",
              json.loads(out_json).get("process_name") == "chrome.exe", out_json[:80])
        out_bad = AS.sensorium_action({"action": "nonsense"})
        check("tool: unknown action message", "Unknown action" in out_bad, out_bad)
        AS._snapshot = lambda: {"error": "sensor down"}
        out_err = AS.sensorium_action({"action": "context"})
        check("tool: unavailable path", "Ambient sensing unavailable" in out_err, out_err)

        # ── TOOL dict validates for discovery ────────────────────────────────
        rec = _validate(AS, "sensorium.py")
        check("tool: valid", rec.valid and rec.error == "", rec.error)
        check("tool: name", rec.name == "sensorium")

    finally:
        SM.user32, SM.kernel32 = orig_user32, orig_kernel32
        SM.time.time = orig_time
        SM._get_foreground = orig_fg
        SM.get_idle_time_seconds = orig_idle
        SM._get_vitals = orig_vitals

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
