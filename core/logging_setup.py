"""
core/logging_setup.py — everything JARVIS prints is also written to a file.

Self-healing foundation, piece 1 of 4. The console is ephemeral: when the app
crashes the terminal history dies with it (and under pythonw there is no console
at all). A rotating on-disk log means the guardian and any post-mortem have the
full transcript — timestamps, tracebacks, tool activity — no matter how the
process ended.

Design:
  * Tees sys.stdout / sys.stderr at the Python level. JARVIS prints everywhere
    (main.py, actions, plugins, dashboard); wrapping the stream objects catches
    all of it with zero edits to those files.
  * Installed by main.py right after its UTF-8 reconfigure, so the file gets
    the same replacement-safe UTF-8 text the console does.
  * Rotation by size (5 MB, 3 backups) done manually in write(): the tee needs
    a raw file object, and logging.RotatingFileHandler only consumes LogRecords.
  * BOTH tees share ONE file handle via _Sink. Two handles to the same file
    would block the rename on Windows (an open handle without FILE_SHARE_DELETE
    makes Path.replace raise PermissionError) — observed in testing.
  * Every line is prefixed with HH:MM:SS — crash forensics without extra cost.
  * Never raises, never blocks startup: any failure falls back to the console.

The one thing this module deliberately does NOT do is decide anything about
restarts or recovery — that is the guardian's job. Logging only records.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

_MAX_BYTES = 5 * 1024 * 1024
_BACKUPS = 3
_LOCK = threading.Lock()
_installed = False


def logs_dir() -> Path:
    """logs/ under the project root; created on first use, never fails."""
    d = _PROJECT_ROOT / "logs"
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return d


def log_path() -> Path:
    return logs_dir() / "jarvis.log"


def _roll(path: Path) -> None:
    """jarvis.log -> jarvis.log.1 -> ... -> jarvis.log.3. Caller must have
    closed every handle to the file first (Windows cannot rename open files)."""
    try:
        for i in range(_BACKUPS - 1, 0, -1):
            src = Path(f"{path}.{i}")
            dst = Path(f"{path}.{i + 1}")
            if src.exists():
                if dst.exists():
                    dst.unlink()
                src.replace(dst)
        backup = Path(f"{path}.1")
        if backup.exists():
            backup.unlink()
        path.replace(backup)
    except Exception:
        pass  # rotation is best-effort; the write below proceeds regardless


class _Sink:
    """The single shared file handle behind both tee streams."""

    def __init__(self):
        self._fh = None
        self._path: Path | None = None

    def _open(self) -> None:
        path = log_path()
        if self._fh is not None and self._path == path:
            return
        try:
            logs_dir().mkdir(parents=True, exist_ok=True)
            self._fh = open(path, "a", encoding="utf-8", errors="replace")
            self._path = path
        except Exception:
            self._fh = None
            self._path = None

    def write_line(self, text: str) -> None:
        if not text:
            return
        try:
            self._open()
            if self._fh is None:
                return
            path = self._path
            if (path is not None and path.exists()
                    and path.stat().st_size >= _MAX_BYTES):
                self._fh.close()
                self._fh = None
                self._path = None
                _roll(path)
                self._open()
            if self._fh is None:
                return
            self._fh.write(text)
            self._fh.flush()
        except Exception:
            pass  # a logging failure must never take the app down


_SINK = _Sink()


class _Tee:
    """Writes to the original console stream AND the shared rotating log.

    Buffers until a newline so each line can be timestamped; flushes the file
    on every line so a hard crash loses at most the final partial line."""

    def __init__(self, orig, is_stderr: bool):
        self._orig = orig
        self._tag = "E" if is_stderr else " "
        self._buf = ""

    def write(self, data) -> int:
        with _LOCK:
            try:
                n = self._orig.write(data) if self._orig is not None else len(data)
            except Exception:
                n = len(data)

            self._buf += str(data)
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                if line:
                    ts = time.strftime("%H:%M:%S")
                    _SINK.write_line(f"[{ts}][{self._tag}] {line}\n")
            return n

    def flush(self) -> None:
        try:
            if self._orig is not None:
                self._orig.flush()
        except Exception:
            pass
        with _LOCK:
            if self._buf:
                ts = time.strftime("%H:%M:%S")
                _SINK.write_line(f"[{ts}][{self._tag}] {self._buf}\n")
                self._buf = ""

    def isatty(self) -> bool:
        try:
            return bool(self._orig is not None and self._orig.isatty())
        except Exception:
            return False


def install_file_logging() -> bool:
    """Tee stdout/stderr into logs/jarvis.log. Idempotent, never raises.

    Returns True when the file log is active, False otherwise (the console
    keeps working either way)."""
    global _installed
    if _installed:
        return True
    try:
        for name in ("stdout", "stderr"):
            stream = getattr(sys, name, None)
            # Wrap None too: under pythonw there is no console, sys.stdout/
            # sys.stderr are None, and print() silently no-ops (CPython returns
            # early when the stream is None). Teeing even a None stream gives
            # the file log every print and every traceback, and downstream
            # code (uvicorn logging etc.) a real stream with isatty()==False.
            if not isinstance(stream, _Tee):
                setattr(sys, name, _Tee(stream, is_stderr=(name == "stderr")))
        _installed = True
    except Exception:
        pass
    if _installed:
        try:
            print("[Logging] File log active: logs/jarvis.log")
        except Exception:
            pass
    return _installed
