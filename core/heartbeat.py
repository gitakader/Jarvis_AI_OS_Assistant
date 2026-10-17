"""
core/heartbeat.py — liveness heartbeat + stall detection.

Self-healing foundation, piece 2 of 4. Two questions decide JARVIS's fate as an
always-on assistant: "is the process alive?" and "is the main thread still
thinking?". The guardian answers the first by watching the child process; this
module answers the second, and publishes both answers as a tiny JSON file the
guardian (and the dashboard health endpoint) can read.

How it works:
  * A 1-second QTimer on the Qt thread stamps `_last_main_tick`. If the Qt main
    loop freezes (blocked callback, driver deadlock, GIL spin), the stamp stops
    moving — while the watchdog thread below keeps running.
  * A daemon watchdog thread writes logs/heartbeat.json every few seconds:
    {"ts", "pid", "state": "healthy"|"stall", "main_age_s"}. Writes are atomic
    (tmp + replace) so a reader never sees a half-written file.
  * On the healthy→stall transition it dumps ALL thread stacks to
    logs/stall_<timestamp>.txt via faulthandler — the post-mortem evidence that
    shows WHERE it froze, not just that it froze.
  * This module never kills anything. Deciding to restart is the guardian's
    job; killing yourself from inside a broken process is how you lose the
    forensic evidence that explains the freeze.

Called from main.py's main(): heartbeat.start(ui._win) before mainloop, and
heartbeat.stop() + mark_intentional_shutdown() after mainloop returns.
"""
from __future__ import annotations

import faulthandler
import os
import threading
import time
from pathlib import Path

from core.logging_setup import logs_dir

_HEARTBEAT_FILE = "heartbeat.json"
_MARKER_FILE = "intentional_shutdown.marker"

_started_at: float = 0.0
_last_main_tick: float | None = None
_timer = None                 # QTimer reference — keep alive for the process lifetime
_stop_event = threading.Event()
_watchdog: threading.Thread | None = None


# ── heartbeat file (shared with guardian.py and dashboard health) ─────────────

def heartbeat_path() -> Path:
    return logs_dir() / _HEARTBEAT_FILE


def _write_heartbeat(state: str, main_age: float) -> None:
    import json
    payload = json.dumps({
        "v": 1,
        "pid": os.getpid(),
        "ts": time.time(),
        "started_at": _started_at,
        "state": state,
        "main_age_s": round(main_age, 2),
    })
    path = heartbeat_path()
    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except Exception:
        pass  # never let heartbeat I/O take anything down


def read_heartbeat() -> dict | None:
    """Current heartbeat as a dict, or None if missing/unreadable (used by
    dashboard health and by guardian.py)."""
    import json
    try:
        return json.loads(heartbeat_path().read_text(encoding="utf-8"))
    except Exception:
        return None


def mark_intentional_shutdown() -> None:
    """One-line marker proving the user closed JARVIS on purpose. The guardian
    checks this before restarting, so 'close the window' never becomes
    'crash'."""
    try:
        (logs_dir() / _MARKER_FILE).write_text(
            f"{time.time():.3f}\n", encoding="utf-8")
    except Exception:
        pass


def _intentional_marker_age() -> float | None:
    try:
        text = (logs_dir() / _MARKER_FILE).read_text(encoding="utf-8").strip()
        return time.time() - float(text)
    except Exception:
        return None


def was_intentional_exit() -> bool:
    """True when a shutdown marker exists and is less than 60 seconds old."""
    age = _intentional_marker_age()
    return age is not None and 0 <= age < 60


def clear_intentional_marker() -> None:
    try:
        (logs_dir() / _MARKER_FILE).unlink(missing_ok=True)
    except Exception:
        pass


# ── Qt-side tick ─────────────────────────────────────────────────────────────

def _tick() -> None:
    global _last_main_tick
    _last_main_tick = time.time()


def start(qt_parent=None, tick_interval_ms: int = 1000,
          heartbeat_interval_s: float = 3.0, stall_after_s: float = 45.0) -> bool:
    """Start the Qt ticker + watchdog thread. Returns False if Qt is missing
    (caller ignores it — the guardian still sees a live process via the pid,
    stall detection just stays off)."""
    global _started_at, _last_main_tick, _timer, _watchdog

    try:
        from PyQt6.QtCore import QTimer
    except Exception:
        print("[Heartbeat] PyQt6 unavailable — stall watchdog disabled.")
        return False

    _started_at = time.time()
    _last_main_tick = time.time()
    _stop_event.clear()

    timer = QTimer(qt_parent)
    timer.timeout.connect(_tick)
    timer.start(tick_interval_ms)
    _timer = timer

    def _loop() -> None:
        was_stall = False
        while not _stop_event.wait(heartbeat_interval_s):
            if _last_main_tick is None:
                continue
            age = time.time() - _last_main_tick
            state = "stall" if age > stall_after_s else "healthy"
            if state == "stall" and not was_stall:
                _dump_thread_stacks()
            was_stall = (state == "stall")
            _write_heartbeat(state, age)

    _watchdog = threading.Thread(target=_loop, name="heartbeat-watchdog", daemon=True)
    _watchdog.start()
    print(f"[Heartbeat] Watchdog active (stall threshold {stall_after_s:.0f}s).")
    return True


def _dump_thread_stacks() -> None:
    """All threads' current stacks → logs/stall_<ts>.txt. The 'where did it
    freeze' evidence, taken at the moment of the freeze."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = logs_dir() / f"stall_{stamp}.txt"
    try:
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write(f"Main-thread stall detected at {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 60 + "\n")
            faulthandler.dump_traceback(file=f, all_threads=True)
        print(f"[Heartbeat] MAIN THREAD STALL — stack dump: {path}")
    except Exception as e:
        print(f"[Heartbeat] could not dump stacks: {e}")


def stop() -> None:
    """Clean shutdown: cancel the ticker, stop the watchdog, publish the final
    'stopped' heartbeat so a slow guardian can see the exit was orderly."""
    try:
        if _timer is not None:
            _timer.stop()
    except Exception:
        pass
    _stop_event.set()
    if _watchdog is not None:
        _watchdog.join(timeout=2.0)
    if _started_at:
        _write_heartbeat("stopped", 0.0)
