"""
file_index.py — System-wide file index for MARK LV.

A background-built, in-memory index of every file the user can see (home
folders plus all mounted drives), with fuzzy name search and a persisted
snapshot so searches work instantly on the next boot while a fresh walk
refreshes in the background.

Design notes
------------
* The walk uses os.scandir (fast) with a system-directory skip list, an
  entry cap (default 300k) and a time cap (~150s) so startup never stalls.
* Entries are plain tuples (name, stem, parent, path); string scoring stays
  fast at that size and the running list is swapped atomically under the
  GIL, so search never blocks and never sees a half-built index.
* The snapshot (memory/file_index.json) is a compact JSON list of full
  paths, written atomically (tmp file + os.replace) after each build.
* Symlinked / junction directories are not followed, so walk cycles are
  impossible by construction.
* open_path() launches a file or folder with the OS default app.
"""

from __future__ import annotations

import difflib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

if getattr(sys, "frozen", False):
    _BASE = Path(sys.executable).parent
else:
    _BASE = Path(__file__).resolve().parent.parent

_DEFAULT_SNAPSHOT = _BASE / "memory" / "file_index.json"

_MAX_ENTRIES = 300_000
_MAX_WALK_SECONDS = 150.0

# Directories never worth descending into (lower-cased exact names).
_SKIP_DIRS = {
    "windows", "program files", "program files (x86)", "programdata",
    "appdata", "$recycle.bin", "system volume information", "node_modules",
    "__pycache__", ".git", ".svn", ".hg", ".venv", "venv", "site-packages",
    "onedrivetemp", "msocache", "recovery", "windows.old",
    "application data", "local settings", "documents and settings",
    "intel", "nvidia", "amd", "perflogs",
    "config.msi", "system32", "syswow64", "winsxs", "installer", "boot",
}

# Sub-string patterns that poison any directory name containing them.
_SKIP_SUBSTRINGS = (
    ".venv", "site-packages", "node_modules", "__pycache__", ".git",
    "windows.old", "$recycle", "system volume information",
)

# Virtual filesystems never worth walking on Linux.
_LINUX_VIRTUAL_FS = {
    "proc", "sysfs", "devtmpfs", "tmpfs", "devpts", "overlay", "squashfs",
    "cgroup", "cgroup2", "debugfs", "tracefs", "securityfs", "pstore",
    "bpf", "hugetlbfs", "mqueue", "configfs", "fusectl",
}

_TOKEN_RE = re.compile(r"[^a-z0-9]+")


def _stem_of(name: str) -> str:
    """Lower-cased file name without its extension ('.bashrc' → '', 'A.md' → 'a')."""
    dot = name.rfind(".")
    if dot <= 0:
        return name.lower()
    return name[:dot].lower()


def _default_roots() -> list[Path]:
    """Home folder + every accessible drive/partition, de-duplicated.

    Home is deliberately listed first so the user's own files are indexed
    before the drive-wide walk can time out."""
    roots: list[Path] = [Path.home()]
    try:
        import psutil
        for p in psutil.disk_partitions(all=True):
            if not p.mountpoint:
                continue
            if _OS == "Linux" and (p.fstype or "").lower() in _LINUX_VIRTUAL_FS:
                continue
            roots.append(Path(p.mountpoint))
    except Exception:
        pass
    if _OS == "Windows":
        import string
        for letter in string.ascii_uppercase:
            drive_root = Path(f"{letter}:\\")
            if drive_root.exists() and drive_root not in roots:
                roots.append(drive_root)

    seen: set[str] = set()
    out: list[Path] = []
    for r in roots:
        key = str(r).lower().rstrip("\\/")
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def _score(name: str, stem: str, q: str, tokens: list[str]) -> int:
    """Rank a candidate (name = full file name, stem = name minus extension).

    Deterministic ladder: exact name > exact stem > name-prefix > stem-prefix >
    stem-inside-query > query-substring > multi-token. Returns 0 for no match;
    difflib rescue for typos happens separately in search()."""
    name_l = name.lower()
    if name_l == q:
        return 100
    if stem == q:
        return 98
    if name_l.startswith(q):
        return 95
    if stem.startswith(q):
        return 90
    if len(stem) >= 3 and q.startswith(stem):
        return 80
    if q in stem:
        return 70
    if len(tokens) > 1:
        hits = sum(1 for t in tokens if t in stem)
        if hits == len(tokens):
            return 75
        if hits:
            return 50 + 10 * hits
    return 0


def _mtime_or_0(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


class FileIndex:
    """In-memory index of file paths with fuzzy name search.

    Instantiate with custom roots/snapshot for tests; the module-level
    singleton `file_index` uses the real system roots and
    memory/file_index.json. The constructor does no I/O — all disk work
    happens in rebuild(), which the background thread runs."""

    def __init__(
        self,
        roots: list[Path] | None = None,
        snapshot_path: Path | None = None,
        max_entries: int = _MAX_ENTRIES,
        max_seconds: float = _MAX_WALK_SECONDS,
    ) -> None:
        self._roots = [Path(r) for r in (roots if roots is not None else _default_roots())]
        self._snapshot_path = Path(snapshot_path) if snapshot_path is not None else _DEFAULT_SNAPSHOT
        self._max_entries = int(max_entries)
        self._max_seconds = float(max_seconds)
        # (name, stem, parent, path) — path = parent + sep + name
        self._entries: list[tuple[str, str, str, str]] = []
        self._state = "idle"        # idle | building | ready
        self._build_started = False

    # ── Build / persistence ─────────────────────────────────────────────────

    def start_background_build(self) -> None:
        """Kick off a daemon-thread rebuild. Safe to call more than once."""
        if self._build_started:
            return
        self._build_started = True
        threading.Thread(
            target=self._safe_rebuild, name="file-index-builder", daemon=True
        ).start()

    def _safe_rebuild(self) -> None:
        try:
            self.rebuild()
        except Exception as e:
            self._state = "idle"
            print(f"[FileIndex] background build failed: {e}")

    def rebuild(self) -> None:
        """Synchronous full rebuild: snapshot in, fresh walk, snapshot out.

        The snapshot is loaded first so searches already work while the walk
        runs; the fresh list is then swapped in atomically."""
        self._state = "building"
        self._load_snapshot()
        started = time.time()
        entries = self._walk_all()
        self._entries = entries          # atomic swap (GIL)
        self._built_at = time.time()
        self._state = "ready"
        self._save_snapshot()
        print(f"[FileIndex] Indexed {len(entries)} file(s) in {time.time() - started:.1f}s")

    def _walk_all(self) -> list[tuple[str, str, str, str]]:
        deadline = time.time() + self._max_seconds
        entries: list[tuple[str, str, str, str]] = []
        for root in self._roots:
            if len(entries) >= self._max_entries or time.time() >= deadline:
                break
            try:
                self._walk_root(root, entries, deadline)
            except Exception as e:
                print(f"[FileIndex] walk {root} failed: {e}")
        # Home is nested inside C:\ on Windows — de-duplicate by path,
        # keeping the first (home-first) occurrence.
        seen: dict[str, tuple[str, str, str, str]] = {}
        for e in entries:
            seen.setdefault(e[3], e)
        return list(seen.values())

    def _walk_root(
        self,
        root: Path,
        entries: list[tuple[str, str, str, str]],
        deadline: float,
    ) -> None:
        stack: list[str] = [str(root)]
        while stack:
            if len(entries) >= self._max_entries or time.time() >= deadline:
                return
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if len(entries) >= self._max_entries or time.time() >= deadline:
                            return
                        name = e.name
                        low = name.lower()
                        try:
                            if e.is_dir(follow_symlinks=False):
                                if low in _SKIP_DIRS or any(s in low for s in _SKIP_SUBSTRINGS):
                                    continue
                                stack.append(e.path)
                            elif e.is_file(follow_symlinks=False):
                                entries.append(
                                    (name, _stem_of(name), d, os.path.join(d, name))
                                )
                        except OSError:
                            continue
            except (OSError, PermissionError):
                continue

    def _load_snapshot(self) -> None:
        try:
            raw = self._snapshot_path.read_text(encoding="utf-8")
        except Exception:
            return
        try:
            data = json.loads(raw)
            paths = data.get("paths") if isinstance(data, dict) else \
                (data if isinstance(data, list) else None)
            if not paths:
                return
            entries: list[tuple[str, str, str, str]] = []
            for p in paths[: self._max_entries]:
                name = os.path.basename(p) or p
                entries.append((name, _stem_of(name), os.path.dirname(p), p))
            self._entries = entries
            print(f"[FileIndex] Loaded {len(entries)} cached path(s) from snapshot.")
        except Exception:
            pass

    def _save_snapshot(self) -> None:
        if not self._entries:
            return
        try:
            self._snapshot_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"v": 1, "paths": [e[3] for e in self._entries]}
            tmp = self._snapshot_path.with_name(self._snapshot_path.name + ".tmp")
            tmp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            os.replace(tmp, self._snapshot_path)
        except Exception as e:
            print(f"[FileIndex] snapshot save failed: {e}")

    # ── Live updates (files created/moved/deleted by the assistant) ─────────

    def add_path(self, path) -> None:
        p = str(path)
        name = os.path.basename(p) or p
        entry = (name, _stem_of(name), os.path.dirname(p), p)
        for i, e in enumerate(self._entries):
            if e[3] == p:
                self._entries[i] = entry
                return
        self._entries.append(entry)

    def remove_path(self, path) -> None:
        p = str(path)
        self._entries = [e for e in self._entries if e[3] != p]

    # ── Query ───────────────────────────────────────────────────────────────

    def search(self, query: str, limit: int = 10) -> list[dict]:
        """Fuzzy-ranked lookup. Returns [{'name','path','parent','score'}].

        Direct string matches always outrank difflib typo rescues, and ties
        are broken by most-recently-modified first."""
        q = (query or "").strip().lower()
        if not q:
            return []
        limit = max(1, min(int(limit), 50))
        tokens = [t for t in _TOKEN_RE.split(q) if t]
        entries = self._entries

        scored: list[tuple[float, str, str, str]] = []   # (score, name, parent, path)
        for name, stem, parent, path in entries:
            s = _score(name, stem, q, tokens)
            if s:
                scored.append((float(s), name, parent, path))

        # Typo rescue: only when direct hits are scarce. Pre-filtered by the
        # query's first letter and budget-capped so it stays fast on 300k
        # entries.
        if len(scored) < limit and q and q[0].isalnum():
            rescue: list[tuple[float, str, str, str]] = []
            budget = 50_000
            for name, stem, parent, path in entries:
                if budget <= 0:
                    break
                budget -= 1
                if not stem or q[0] not in stem:
                    continue
                ratio = difflib.SequenceMatcher(None, q, stem).ratio()
                if ratio >= 0.6:
                    rescue.append((40.0 + 40.0 * ratio, name, parent, path))
            if rescue:
                seen_paths = {t[3] for t in scored}
                for r in rescue:
                    if r[3] not in seen_paths:
                        scored.append(r)

        scored.sort(key=lambda t: (-t[0], len(t[1]), t[3]))
        top = scored[:limit]
        if len(top) > 1:
            try:
                top = sorted(top, key=lambda t: (-t[0], -_mtime_or_0(t[3])))
            except Exception:
                pass

        return [
            {"name": n, "path": p, "parent": pr, "score": int(s)}
            for s, n, pr, p in top
        ]

    def status(self) -> str:
        n = len(self._entries)
        if self._state == "building":
            return f"building ({n} file(s) so far)"
        if self._state == "ready":
            return f"ready ({n} file(s) indexed)"
        return f"idle ({n} file(s) loaded)"

    def count(self) -> int:
        return len(self._entries)

    # ── Open ────────────────────────────────────────────────────────────────

    def open_path(self, path) -> tuple[bool, str]:
        """Open a file or folder with the OS default app."""
        p = Path(str(path)).expanduser()
        if not p.exists():
            return False, f"Not found: {p}"
        try:
            if _OS == "Windows":
                os.startfile(str(p))                # type: ignore[attr-defined]
            elif _OS == "Darwin":
                subprocess.Popen(["open", str(p)])
            else:
                opener = next((c for c in ("xdg-open", "gio") if shutil.which(c)), None)
                if opener is None:
                    return False, "No default-app opener available on this system."
                subprocess.Popen([opener, str(p)])
            return True, f"Opened: {p.name}"
        except Exception as e:
            return False, f"Could not open '{p.name}': {e}"


# Module-level singleton used by actions/file_controller.py and main.py.
file_index = FileIndex()
