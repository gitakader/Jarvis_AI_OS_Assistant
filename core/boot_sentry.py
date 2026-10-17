"""
core/boot_sentry.py — boot-crash rollback sentry for JARVIS.

When JARVIS dies right after starting, over and over (a boot crash-loop),
the usual suspect is the most recent edit. This module detects that loop
and rolls the change back:

  * trigger  — two or more crashes, each within 60 s of that launch's start,
               inside the crash window (600 s);
  * recovery — the most recently modified file on a hot-loadable surface
               (actions/, plugins/, features/, mcp_servers/) gets its `.bak`
               restored if one exists, otherwise it is quarantined into
               logs/quarantine/ so the next boot can succeed without it.

Boot-critical code (main.py, guardian.py, ui.py, core/, dashboard/), secrets
(config/), brain state (memory/), logs and the venv are never touched — there
is nothing to safely roll them back to, and quarantining them would brick or
corrupt JARVIS. If the newest edit isn't a safe candidate, the sentry stays
silent rather than guess.

Policy-free, like the rest of the self-heal pack: `evaluate()` decides,
`recover()` acts, and the caller (guardian.py) owns logging and state. The
module must never raise into its caller — `check_and_recover()` swallows
everything and reports through its result dict.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any, Optional

# ── tunables ─────────────────────────────────────────────────────────────────

BOOT_CRASH_MAX_UPTIME_S = 60.0    # a crash this soon after launch = boot crash
TRIGGER_WINDOW_S = 600.0          # lookback window for the crash-loop signal
TRIGGER_MIN_BOOT_CRASHES = 2      # how many boot crashes arm the sentry
RECOVERY_COOLDOWN_S = 1800.0      # don't act twice within 30 min
CANDIDATE_MAX_AGE_S = 7 * 86400   # only recent edits are candidates

# Hot-loadable surfaces: modules JARVIS discovers with skip-on-error, or
# imports behind try/except — removing one lets the app boot again.
SAFE_DIRS = ("actions", "plugins", "features", "mcp_servers")

# Never touch, in any recovery: secrets, brain state, logs, venv, tests, and
# boot-critical files whose absence would brick startup.
DENY_DIRS = {"logs", "venv", "memory", "config", "__pycache__", "selfheal_tests",
             "core", "dashboard", "mcp_tests"}
DENY_FILES = {"main.py", "guardian.py", "ui.py", "run_jarvis.bat",
              "setup.py", "API_hf.py", "google_setup.py", "requirements.txt"}

QUARANTINE_DIR = "logs/quarantine"


# ── crash event model ────────────────────────────────────────────────────────

class CrashEvent:
    """One observed child crash. `started_at` is when that child launch began."""

    __slots__ = ("crashed_at", "started_at", "exit_code")

    def __init__(self, crashed_at: float, started_at: float, exit_code: int):
        self.crashed_at = float(crashed_at)
        self.started_at = float(started_at)
        self.exit_code = int(exit_code)

    @property
    def uptime(self) -> float:
        return max(0.0, self.crashed_at - self.started_at)

    def to_dict(self) -> dict:
        return {
            "crashed_at": self.crashed_at,
            "started_at": self.started_at,
            "uptime": round(self.uptime, 1),
            "exit_code": self.exit_code,
        }


# ── decision (pure) ──────────────────────────────────────────────────────────

def evaluate(
    crash_events: list,
    now: float,
    last_recovery_ts: Optional[float] = None,
    max_uptime: float = BOOT_CRASH_MAX_UPTIME_S,
    window: float = TRIGGER_WINDOW_S,
    min_crashes: int = TRIGGER_MIN_BOOT_CRASHES,
    cooldown: float = RECOVERY_COOLDOWN_S,
) -> Optional[dict]:
    """Return a trigger dict when a boot crash-loop is underway, else None.

    Pure: no file or clock access beyond the values passed in. A recent
    recovery suppresses the trigger so a stubborn loop can't quarantine the
    whole project one file at a time."""
    if last_recovery_ts is not None and (now - last_recovery_ts) < cooldown:
        return None

    recent = [e for e in crash_events if now - e.crashed_at < window]
    boot_crashes = [e for e in recent if e.uptime < max_uptime]
    if len(boot_crashes) < min_crashes:
        return None

    return {
        "kind": "boot_crash_loop",
        "recent_boot_crashes": len(boot_crashes),
        "window_s": window,
        "max_uptime_s": max_uptime,
        "exit_codes": [e.exit_code for e in boot_crashes],
    }


# ── candidate selection ──────────────────────────────────────────────────────

def _denied(rel: Path) -> bool:
    parts = rel.parts
    if parts[0] in DENY_DIRS:
        return True
    if len(parts) == 1 and parts[0] in DENY_FILES:
        return True
    return False


def choose_candidate(
    root: Path,
    now: float,
    max_age: float = CANDIDATE_MAX_AGE_S,
) -> Optional[Path]:
    """The most recently modified safe-surface file, or None.

    Only files under SAFE_DIRS qualify — never the deny-list. If the newest
    edit is in core/ or main.py, there is nothing safe to remove, and None
    tells the caller to stand down."""
    best: Optional[Path] = None
    best_mtime = 0.0
    cutoff = now - max_age
    for directory in SAFE_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if _denied(rel):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if cutoff <= mtime and mtime > best_mtime:
                best = path
                best_mtime = mtime
    return best


# ── recovery (acts) ──────────────────────────────────────────────────────────

def recover(candidate: Path, root: Path) -> dict:
    """Roll back `candidate`: restore its .bak if present, else quarantine it.

    Raises ValueError for deny-listed paths (defense in depth); the caller's
    check_and_recover() converts that into a stand-down result."""
    rel = candidate.relative_to(root)
    if _denied(rel):
        raise ValueError(f"refusing to touch deny-listed path: {rel}")

    backup = candidate.with_name(candidate.name + ".bak")
    if backup.exists():
        shutil.copy2(str(backup), str(candidate))
        return {
            "action": "restore_backup",
            "file": str(rel),
            "backup": str(backup),
        }

    qdir = root / QUARANTINE_DIR
    qdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    dest = qdir / f"{stamp}_{candidate.name}"
    shutil.move(str(candidate), str(dest))
    return {
        "action": "quarantine",
        "file": str(rel),
        "moved_to": str(dest.relative_to(root)),
    }


# ── one-call orchestration (used by guardian.py) ─────────────────────────────

def check_and_recover(
    crash_events: list,
    now: float,
    root: Path,
    last_recovery_ts: Optional[float] = None,
) -> dict:
    """Full sentry pass. Returns {"fired": bool, ...} — never raises."""
    try:
        decision = evaluate(crash_events, now, last_recovery_ts)
        if decision is None:
            return {"fired": False, "reason": "trigger_not_met"}
        candidate = choose_candidate(root, now)
        if candidate is None:
            return {"fired": False, "reason": "no_safe_candidate", **decision}
        result = recover(candidate, root)
        return {"fired": True, **decision, **result}
    except Exception as e:  # the sentry must never crash the guardian
        return {"fired": False, "reason": f"error: {e}"}
