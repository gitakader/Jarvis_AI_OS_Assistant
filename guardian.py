"""
guardian.py — the external watchdog that keeps JARVIS alive.

Self-healing foundation, piece 3 of 4. Run this instead of `python main.py`;
it launches JARVIS as a child process and supervises it:

  * Crash restart   — a nonzero exit with no intentional-shutdown marker is a
                      crash: the tail of the log is saved to a crash report,
                      then JARVIS is restarted with a small backoff.
  * Stall kill      — heartbeat stale for longer than the threshold (main thread
                      frozen, deadlock, GIL spin) while the process is still
                      alive: after a few confirming checks it is killed and
                      restarted. System sleep/wake is survived because both the
                      heartbeat and the guardian freeze together and resume
                      together — the repeated checks give the heartbeat time to
                      recover before any kill.
  * Clean exit      — exit code 0 (window closed, shutdown_jarvis tool) or a
                      fresh logs/intentional_shutdown.marker means "the user
                      wanted this"; the guardian exits quietly instead of
                      restarting.
  * Crash-loop stop — 5 crashes within 10 minutes and it gives up, leaving
                      logs/GUARDIAN_GAVE_UP.txt instead of burning CPU forever.
  * Single instance — a lock file keeps a second guardian from double-launching.
  * Already running — if JARVIS is already up (fresh heartbeat from another
                      pid), the guardian refuses to start a second copy.

Usage:
    python guardian.py                # supervise `python main.py` in this folder
    run_jarvis.bat                    # the one-line launcher for daily use

Diagnostics live in logs/guardian.log and logs/guardian_state.json (the latter
is also surfaced by the dashboard's /api/health).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOGS = ROOT / "logs"

try:
    from core import boot_sentry as _boot_sentry
except Exception:          # the sentry must never brick the watchdog
    _boot_sentry = None

_STATE_FILE = "guardian_state.json"
_GUARDIAN_LOG = "guardian.log"
_HEARTBEAT = "heartbeat.json"
_MARKER = "intentional_shutdown.marker"
_GAVE_UP = "GUARDIAN_GAVE_UP.txt"
_LOCK_FILE = "guardian.lock"
_CRASH_REPORT_LINES = 300
_CRASH_REPORT_AGE_DAYS = 30


def _log(msg: str) -> None:
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        with open(LOGS / _GUARDIAN_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _fixup_stdio() -> None:
    """Under pythonw there is no console: sys.stdout is None and print() raises."""
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            try:
                setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))
            except Exception:
                pass


# ── single-instance lock ─────────────────────────────────────────────────────

_lock_handle = None


def _acquire_lock() -> bool:
    global _lock_handle
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        lock = open(LOGS / _LOCK_FILE, "a+")
        import msvcrt
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        lock.seek(0)
        lock.truncate()
        lock.write(str(os.getpid()))
        lock.flush()
        _lock_handle = lock
        return True
    except Exception:
        return False


# ── state bookkeeping ────────────────────────────────────────────────────────

def _load_state() -> dict:
    try:
        return json.loads((LOGS / _STATE_FILE).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    try:
        LOGS.mkdir(parents=True, exist_ok=True)
        tmp = LOGS / (_STATE_FILE + ".tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        os.replace(tmp, LOGS / _STATE_FILE)
    except Exception:
        pass


def _read_heartbeat() -> dict | None:
    try:
        return json.loads((LOGS / _HEARTBEAT).read_text(encoding="utf-8"))
    except Exception:
        return None


def _marker_fresh(max_age: float = 60.0) -> bool:
    try:
        text = (LOGS / _MARKER).read_text(encoding="utf-8").strip()
        age = time.time() - float(text)
        return 0 <= age < max_age
    except Exception:
        return False


def _clear_marker() -> None:
    try:
        (LOGS / _MARKER).unlink(missing_ok=True)
    except Exception:
        pass


# ── crash report ─────────────────────────────────────────────────────────────

def _save_crash_report(exit_code: int) -> str | None:
    try:
        log_file = LOGS / "jarvis.log"
        if not log_file.exists():
            return None
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-_CRASH_REPORT_LINES:])
        stamp = time.strftime("%Y%m%d_%H%M%S")
        report = LOGS / f"crash_{stamp}.log"
        report.write_text(
            f"JARVIS exited unexpectedly (exit code {exit_code}) at "
            f"{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"{'=' * 60}\nLast {min(len(lines), _CRASH_REPORT_LINES)} lines of jarvis.log:\n"
            f"{tail}\n",
            encoding="utf-8",
        )
        return report.name
    except Exception:
        return None


def _prune_old_reports() -> None:
    try:
        cutoff = time.time() - _CRASH_REPORT_AGE_DAYS * 86400
        for p in LOGS.glob("crash_*.log"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except Exception:
        pass


# ── the supervisor loop ──────────────────────────────────────────────────────

class Guardian:
    def __init__(self, args):
        self.child_cmd = args.child
        self.poll_s = args.poll
        self.hb_stale = args.hb_stale
        self.hb_checks = args.hb_checks
        self.hb_gap = args.hb_gap
        self.max_crashes = args.max_crashes
        self.crash_window = args.crash_window
        self.watch_stalls = not args.no_stall_watch
        self._proc: subprocess.Popen | None = None
        self._crashes: list[float] = []     # timestamps of recent crashes
        self._restarts = 0
        self._warned_no_hb = False
        self._child_started_at: float = 0.0
        # Boot-crash history (CrashEvent), reloaded from state so a guardian
        # restart in the middle of a crash-loop keeps the signal.
        self._boot_events: list = []
        if _boot_sentry is not None:
            for e in _load_state().get("recent_crashes", []):
                try:
                    self._boot_events.append(_boot_sentry.CrashEvent(
                        e["crashed_at"], e["started_at"], e["exit_code"]))
                except Exception:
                    pass

    # -- launching / stopping ------------------------------------------------

    def launch(self) -> None:
        _clear_marker()
        self._child_started_at = time.time()
        self._proc = subprocess.Popen(self.child_cmd, cwd=str(ROOT))
        _log(f"Started JARVIS (pid {self._proc.pid}): {' '.join(self.child_cmd)}")

    def kill_child(self, why: str) -> None:
        if self._proc is None or self._proc.poll() is not None:
            return
        _log(f"{why} — terminating pid {self._proc.pid}")
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                pass
        except Exception:
            pass
        try:
            self._proc.kill()
            self._proc.wait(timeout=5)
        except Exception:
            pass
        _log("JARVIS terminated.")

    # -- crash bookkeeping ----------------------------------------------------

    def _record_crash(self, exit_code: int) -> tuple[bool, str | None]:
        """Register a crash. Returns (should_restart, report_name)."""
        now = time.time()
        self._crashes = [t for t in self._crashes if now - t < self.crash_window]
        self._crashes.append(now)
        self._restarts += 1
        report = _save_crash_report(exit_code)
        if report:
            _log(f"Crash report saved: logs/{report}")
        state = _load_state()
        state.update({
            "guardian_pid": os.getpid(),
            "started_at": state.get("started_at", time.strftime("%Y-%m-%d %H:%M:%S")),
            "crash_count": state.get("crash_count", 0) + 1,
            "restart_count": self._restarts,
            "last_crash": {
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "exit_code": exit_code,
                "report": report,
            },
        })
        _save_state(state)

        # ── boot sentry ───────────────────────────────────────────────────────
        # A crash-loop right after startup usually means the most recent edit
        # broke the boot. Two boot crashes in the window and the sentry rolls
        # that change back (.bak restore, else quarantine to logs/quarantine/).
        if _boot_sentry is not None:
            self._boot_events = [e for e in self._boot_events
                                 if now - e.crashed_at < self.crash_window]
            self._boot_events.append(_boot_sentry.CrashEvent(
                crashed_at=now,
                started_at=getattr(self, "_child_started_at", now),
                exit_code=exit_code))
            state["recent_crashes"] = [e.to_dict() for e in self._boot_events[-10:]]
            _save_state(state)
            try:
                last_ts = state.get("last_recovery", {}).get("ts")
                result = _boot_sentry.check_and_recover(
                    self._boot_events, now, ROOT, last_ts)
                if result.get("fired"):
                    state["last_recovery"] = {
                        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "ts": now,
                        "action": result.get("action"),
                        "file": result.get("file"),
                        "moved_to": result.get("moved_to"),
                        "backup": result.get("backup"),
                        "exit_codes": result.get("exit_codes"),
                    }
                    _save_state(state)
                    _log(f"[BootSentry] {result.get('action')}: "
                         f"{result.get('file')} -> "
                         f"{result.get('moved_to') or result.get('backup')}")
            except Exception as e:
                _log(f"[BootSentry] sentry pass failed: {e}")

        if len(self._crashes) >= self.max_crashes:
            return False, report
        return True, report

    def _backoff(self) -> int:
        """Small, capped exponential backoff between crash restarts."""
        return min(60, 5 * (2 ** min(len(self._crashes), 4)))

    # -- stall detection ------------------------------------------------------

    def _heartbeat_stale(self) -> bool:
        hb = _read_heartbeat()
        if hb is None:
            if not self._warned_no_hb:
                _log("No heartbeat file found — stall watch skipped (older JARVIS version?).")
                self._warned_no_hb = True
            return False
        try:
            return (time.time() - float(hb["ts"])) > self.hb_stale
        except Exception:
            return False

    def _confirm_stale(self) -> bool:
        """Stale on every check across several spaced reads — sleep/wake survives
        this because a recovering process refreshes the heartbeat between checks."""
        for _ in range(self.hb_checks):
            if self._proc is None or self._proc.poll() is not None:
                return False
            if not self._heartbeat_stale():
                return False
            time.sleep(self.hb_gap)
        return self._proc is not None and self._proc.poll() is None

    # -- main loop ------------------------------------------------------------

    def run(self) -> int:
        self.launch()
        last_hb_check = 0.0
        try:
            while True:
                rc = self._proc.poll() if self._proc else 0
                if rc is not None:
                    # Child exited. Intentional?
                    if rc == 0 or _marker_fresh():
                        _clear_marker()
                        _log(f"JARVIS exited cleanly (code {rc}) — user closed it. Guardian shutting down.")
                        return 0
                    _log(f"JARVIS exited unexpectedly (code {rc}).")
                    ok, report = self._record_crash(rc)
                    if not ok:
                        _log(f"{self.max_crashes} crashes in {int(self.crash_window)}s — giving up "
                             f"(see logs/{_GAVE_UP}).")
                        self._write_gave_up()
                        return 3
                    delay = self._backoff()
                    _log(f"Restarting JARVIS in {delay}s (crash {len(self._crashes)} "
                         f"of {self.max_crashes} in this window)...")
                    time.sleep(delay)
                    self.launch()
                    last_hb_check = 0.0
                    continue

                # Child alive — check for a stalled main thread.
                now = time.time()
                if self.watch_stalls and now - last_hb_check >= self.poll_s * 10:
                    last_hb_check = now
                    if self._heartbeat_stale() and self._confirm_stale():
                        self.kill_child(
                            f"Heartbeat stale >{self.hb_stale}s across {self.hb_checks} checks "
                            f"— main thread appears frozen")
                        _log("Treating the stall as a crash (see logs/stall_*.txt for the stack dump).")
                        ok, _ = self._record_crash(-999)  # -999 = stall kill
                        if not ok:
                            self._write_gave_up()
                            return 3
                        delay = self._backoff()
                        _log(f"Restarting JARVIS in {delay}s...")
                        time.sleep(delay)
                        self.launch()
                        last_hb_check = 0.0
                        continue

                time.sleep(self.poll_s)

        except KeyboardInterrupt:
            _log("Ctrl+C — guardian shutting down (JARVIS may keep running in this console).")
            return 130
        finally:
            _save_state(_load_state())

    def _write_gave_up(self) -> None:
        try:
            (LOGS / _GAVE_UP).write_text(
                "GUARDIAN GAVE UP\n"
                f"{time.strftime('%Y-%m-%d %H:%M:%S')}: JARVIS crashed {self.max_crashes} times "
                f"within {int(self.crash_window)} seconds, so the guardian stopped restarting it.\n\n"
                "Look at logs/crash_*.log (one per crash) and the tail of logs/jarvis.log "
                "for the cause. Fix the problem, then run run_jarvis.bat again.\n",
                encoding="utf-8",
            )
        except Exception:
            pass


# ── pre-flight: refuse to double-launch ──────────────────────────────────────

def _another_jarvis_running() -> int | None:
    """If JARVIS is already alive elsewhere (fresh heartbeat from a different
    pid), return that pid — a second instance would fight over the dashboard
    port and the microphone."""
    hb = _read_heartbeat()
    if not hb:
        return None
    try:
        if hb.get("state") == "stopped":
            return None                      # clean exit — no live JARVIS
        pid = int(hb.get("pid", 0))
        age = time.time() - float(hb.get("ts", 0))
        if pid and pid != os.getpid() and 0 <= age < 20:
            try:
                import psutil
                if not psutil.pid_exists(pid):
                    return None              # stale heartbeat from a dead process
            except Exception:
                pass                         # no psutil — fall back to freshness only
            return pid
    except Exception:
        pass
    return None


def _port_in_use(port: int) -> bool:
    """True when something is already listening on the port (e.g. an older
    JARVIS instance that predates the heartbeat file)."""
    import socket as _socket
    s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    try:
        s.bind(("0.0.0.0", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def main() -> int:
    _fixup_stdio()
    ap = argparse.ArgumentParser(description="Keep JARVIS alive.")
    ap.add_argument("--child", nargs=argparse.REMAINDER, default=None,
                    help="Command to run (default: same python, main.py). Everything after --child is the command.")
    ap.add_argument("--poll", type=float, default=1.0)
    ap.add_argument("--hb-stale", type=float, default=90.0, help="Heartbeat age (s) that counts as a stall.")
    ap.add_argument("--hb-checks", type=int, default=3, help="Confirming checks before a stall kill.")
    ap.add_argument("--hb-gap", type=float, default=20.0, help="Seconds between confirming checks.")
    ap.add_argument("--max-crashes", type=int, default=5)
    ap.add_argument("--crash-window", type=float, default=600.0)
    ap.add_argument("--no-stall-watch", action="store_true")
    ap.add_argument("--no-port-check", action="store_true",
                    help="Skip the dashboard-port pre-flight (offline tests only).")
    args = ap.parse_args()

    if args.child is None:
        args.child = [sys.executable, "main.py"]

    other = _another_jarvis_running()
    if other:
        print(f"JARVIS is already running (pid {other}).")
        print("The guardian will not start a second instance — close that one first,")
        print("or run this command from a folder without a live JARVIS.")
        return 2

    if not args.no_port_check:
        for port in (8000, 8001):
            if _port_in_use(port):
                print(f"Port {port} is already in use — an older JARVIS may still be running")
                print("(it predates the heartbeat file, so the guardian cannot see it).")
                print("Close that JARVIS window, then run run_jarvis.bat again.")
                return 2

    if not _acquire_lock():
        print("Another guardian is already watching this JARVIS.")
        return 2

    _log(f"Guardian started (pid {os.getpid()}).")
    _prune_old_reports()
    state = _load_state()
    state["guardian_pid"] = os.getpid()
    state.setdefault("started_at", time.strftime("%Y-%m-%d %H:%M:%S"))
    _save_state(state)

    try:
        return Guardian(args).run()
    except Exception as e:
        _log(f"Guardian crashed: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
