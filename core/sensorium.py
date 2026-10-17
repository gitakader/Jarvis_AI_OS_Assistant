"""
sensorium.py — Passive ambient telemetry daemon for MARK LV.

A background thread (3s poll) that continuously senses:
  1. Foreground window + how long the user has dwelled on it
  2. User idle time (seconds since last mouse/keyboard input)
  3. System vitals: CPU %, RAM %, battery % / plugged-in

Heuristics emit alerts (ram_critical, battery_critical, flow_strain,
welcome_back) through registered interjection callbacks with per-type
cooldowns. The module is policy-free: callbacks decide presentation
(log / dashboard feed / speech). The loop must never crash the host —
any telemetry error is swallowed and the poll continues.
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from ctypes import wintypes
from typing import Any, Callable, Dict, List, Optional

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

user32 = ctypes.windll.user32 if sys.platform == "win32" else None
kernel32 = ctypes.windll.kernel32 if sys.platform == "win32" else None


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("dwTime", wintypes.DWORD),
    ]


def get_idle_time_seconds() -> float:
    """Seconds since the user last touched mouse or keyboard."""
    if not user32 or not kernel32:
        return 0.0
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if user32.GetLastInputInfo(ctypes.byref(lii)):
        millis = kernel32.GetTickCount() - lii.dwTime
        return max(0.0, millis / 1000.0)
    return 0.0


def _get_foreground() -> Dict[str, Any]:
    """Foreground window info via the existing ambient-context action."""
    try:
        from actions.active_context import get_active_window
        return get_active_window()
    except Exception:
        return {"app": "Unknown", "title": "", "pid": 0}


def _get_vitals() -> Dict[str, Any]:
    """CPU/RAM/battery, best-effort."""
    if not _PSUTIL:
        return {"cpu": 0.0, "ram": 0.0, "battery": None, "plugged": None}
    try:
        ram = psutil.virtual_memory().percent
    except Exception:
        ram = 0.0
    try:
        cpu = psutil.cpu_percent(interval=None)
    except Exception:
        cpu = 0.0
    battery = None
    plugged = None
    try:
        b = psutil.sensors_battery()
        if b is not None:
            battery = b.percent
            plugged = b.power_plugged
    except Exception:
        pass
    return {"cpu": cpu, "ram": ram, "battery": battery, "plugged": plugged}


class PassiveSensorium:
    RAM_CRITICAL = 92.0        # sustained RAM pressure threshold (%)
    BATTERY_CRITICAL = 15.0    # low battery while unplugged (%)
    DWELL_STRAIN = 5400.0      # continuous work on one task (s)
    IDLE_AWAY = 300.0          # away for this long = "left the desk" (s)

    RAM_COOLDOWN = 300.0       # 5 min between ram_critical alerts
    BATTERY_COOLDOWN = 600.0   # 10 min between battery_critical alerts
    STRAIN_COOLDOWN = 3600.0   # 60 min between flow_strain alerts
    WELCOME_COOLDOWN = 600.0   # 10 min between welcome_back alerts

    def __init__(self, poll_interval: float = 3.0):
        self.poll_interval = poll_interval
        self._running = False
        self._thread: Optional[threading.Thread] = None

        self.current_window: Dict[str, Any] = {}
        self.current_process_name: str = "Unknown"
        self.dwell_start_time: float = time.time()
        self.current_dwell_seconds: float = 0.0
        self.user_idle_seconds: float = 0.0

        self.cpu_percent: float = 0.0
        self.ram_percent: float = 0.0
        self.battery_percent: Optional[float] = None
        self.power_plugged: Optional[bool] = None

        self._interjection_callbacks: List[Callable[[str, Dict[str, Any]], None]] = []
        self._last_alert_time: Dict[str, float] = {}
        self._was_away: bool = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    def register_interjection_handler(
            self, callback: Callable[[str, Dict[str, Any]], None]) -> None:
        """callback(alert_type, metadata) — called from the sensorium thread."""
        self._interjection_callbacks.append(callback)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, name="Sensorium", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def get_snapshot(self) -> Dict[str, Any]:
        return {
            "window_title": self.current_window.get("title", ""),
            "process_name": self.current_process_name,
            "dwell_seconds": round(self.current_dwell_seconds, 1),
            "user_idle_seconds": round(self.user_idle_seconds, 1),
            "cpu_percent": self.cpu_percent,
            "ram_percent": self.ram_percent,
            "battery_percent": self.battery_percent,
            "power_plugged": self.power_plugged,
            "is_user_active": self.user_idle_seconds < 60.0,
            "timestamp": time.time(),
        }

    # ── internals ────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while self._running:
            try:
                self._update_telemetry()
                self._evaluate_heuristics()
            except Exception:
                # Sensorium must never crash the host process.
                pass
            time.sleep(self.poll_interval)

    def _update_telemetry(self) -> None:
        win_info = _get_foreground()
        title = win_info.get("title", "") or ""
        proc_name = win_info.get("app") or "Unknown"

        # Dwell time: resets whenever the window or owning process changes.
        if title != self.current_window.get("title", "") or \
                proc_name != self.current_process_name:
            self.current_window = win_info
            self.current_process_name = proc_name
            self.dwell_start_time = time.time()
            self.current_dwell_seconds = 0.0
        else:
            self.current_dwell_seconds = time.time() - self.dwell_start_time

        self.user_idle_seconds = get_idle_time_seconds()

        vitals = _get_vitals()
        self.cpu_percent = vitals["cpu"]
        self.ram_percent = vitals["ram"]
        self.battery_percent = vitals["battery"]
        self.power_plugged = vitals["plugged"]

    def _evaluate_heuristics(self) -> None:
        now = time.time()

        if self.ram_percent > self.RAM_CRITICAL:
            if self._cooldown_ok("ram_critical", self.RAM_COOLDOWN):
                self._last_alert_time["ram_critical"] = now
                self._emit_alert(
                    "ram_critical",
                    {
                        "message": f"System memory at {self.ram_percent}% capacity.",
                        "speech": f"Warning: system memory has reached "
                                  f"{int(self.ram_percent)} percent capacity.",
                        "ram_percent": self.ram_percent,
                    },
                )

        if (self.battery_percent is not None and not self.power_plugged
                and self.battery_percent < self.BATTERY_CRITICAL):
            if self._cooldown_ok("battery_critical", self.BATTERY_COOLDOWN):
                self._last_alert_time["battery_critical"] = now
                self._emit_alert(
                    "battery_critical",
                    {
                        "message": f"Battery critically low at {self.battery_percent}%.",
                        "speech": f"Sir, your battery is down to "
                                  f"{int(self.battery_percent)} percent. "
                                  f"Please connect a power source.",
                        "battery_percent": self.battery_percent,
                    },
                )

        if (self.current_dwell_seconds > self.DWELL_STRAIN
                and self.user_idle_seconds < 120):
            if self._cooldown_ok("flow_strain", self.STRAIN_COOLDOWN):
                self._last_alert_time["flow_strain"] = now
                self._emit_alert(
                    "flow_strain",
                    {
                        "message": f"Focused on {self.current_process_name} "
                                   f"for over 90 minutes.",
                        "speech": f"Sir, you have been continuously working on "
                                  f"{self.current_process_name} for over 90 minutes. "
                                  f"I recommend saving your session and taking a short break.",
                        "task": self.current_process_name,
                        "dwell_seconds": self.current_dwell_seconds,
                    },
                )

        if self._was_away and self.user_idle_seconds < 2.0:
            self._was_away = False
            if self._cooldown_ok("welcome_back", self.WELCOME_COOLDOWN):
                self._last_alert_time["welcome_back"] = now
                self._emit_alert(
                    "welcome_back",
                    {
                        "message": f"User returned to workspace "
                                   f"({self.current_process_name}).",
                        "speech": "Welcome back, sir. Your workspace is active "
                                  "and standing by.",
                        "task": self.current_process_name,
                    },
                )
        elif self.user_idle_seconds > self.IDLE_AWAY:
            self._was_away = True

    def _cooldown_ok(self, key: str, cooldown: float) -> bool:
        """True when the alert type may fire: never fired, or cooldown elapsed.
        (Using 0 as the default 'never fired' timestamp breaks under a small
        test clock, where now-0 can still be inside the cooldown.)"""
        last = self._last_alert_time.get(key)
        return last is None or time.time() - last > cooldown

    def _emit_alert(self, alert_type: str, metadata: Dict[str, Any]) -> None:
        for cb in self._interjection_callbacks:
            try:
                cb(alert_type, metadata)
            except Exception:
                pass


# Global singleton daemon instance.
sensorium = PassiveSensorium()
