"""
drive_manager.py — Full Drive & File System Access Plugin for JARVIS / MARK LV.

Provides system-wide disk, drive, and filesystem navigation across all drives (C:, D:, E:, etc.).
"""
from __future__ import annotations

import os
import shutil
import string
from pathlib import Path

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False

PLUGIN = {
    "name": "drive_manager",
    "description": (
        "Access, scan, and explore all system drives (C:, D:, E:, G:, etc.), "
        "root partitions, folders, and files across the entire operating system. "
        "Use this tool when asked to list drives, inspect drive storage, or "
        "access files and directories located on any drive outside the user's home directory."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "drives (list all system drives) | list (list files/folders in path) | info (path info) | usage (disk usage of drive)"
            },
            "path": {
                "type": "STRING",
                "description": "Target drive or directory path (e.g., 'E:\\', 'D:\\Projects', 'E:\\Recruitment')"
            },
            "show_hidden": {
                "type": "BOOLEAN",
                "description": "Whether to include hidden files and directories in listing"
            }
        },
        "required": ["action"]
    }
}

def _format_size(b: int) -> str:
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if b < 1024:
            return f"{b:.1f} {unit}"
        b /= 1024
    return f"{b:.1f} TB"

def get_accessible_drives() -> list[dict]:
    """Discover all mounted drives/partitions on the system."""
    drives = []
    seen = set()

    if _PSUTIL:
        try:
            for part in psutil.disk_partitions(all=True):
                mp = part.mountpoint
                if mp and mp.upper() not in seen:
                    seen.add(mp.upper())
                    try:
                        u = shutil.disk_usage(mp)
                        pct = (u.used / u.total) * 100 if u.total else 0
                        drives.append({
                            "drive": mp,
                            "total": _format_size(u.total),
                            "free": _format_size(u.free),
                            "used": _format_size(u.used),
                            "percent": f"{pct:.1f}%",
                            "fstype": getattr(part, "fstype", "")
                        })
                    except Exception:
                        drives.append({"drive": mp, "error": "Inaccessible"})
        except Exception:
            pass

    # Windows fallback for drives A through Z
    if os.name == "nt":
        for letter in string.ascii_uppercase:
            drive_str = f"{letter}:\\"
            if drive_str.upper() not in seen and os.path.exists(drive_str):
                seen.add(drive_str.upper())
                try:
                    u = shutil.disk_usage(drive_str)
                    pct = (u.used / u.total) * 100 if u.total else 0
                    drives.append({
                        "drive": drive_str,
                        "total": _format_size(u.total),
                        "free": _format_size(u.free),
                        "used": _format_size(u.used),
                        "percent": f"{pct:.1f}%",
                        "fstype": "NTFS/FAT"
                    })
                except Exception:
                    drives.append({"drive": drive_str, "error": "Inaccessible"})

    return sorted(drives, key=lambda x: x["drive"])

def list_directory(target_path: str, show_hidden: bool = False) -> str:
    raw = target_path.strip().strip('"').strip("'")
    p = Path(raw).resolve()
    if not p.exists():
        return f"Sir, the path does not exist: {p}"
    if not p.is_dir():
        return f"Sir, {p} is a file, not a directory."

    try:
        entries = sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        dirs, files = [], []
        for item in entries:
            if not show_hidden and item.name.startswith("."):
                continue
            if item.is_dir():
                dirs.append(f"📁 {item.name}/")
            else:
                try:
                    sz = _format_size(item.stat().st_size)
                    files.append(f"📄 {item.name} ({sz})")
                except Exception:
                    files.append(f"📄 {item.name}")

        summary = f"Contents of {p} ({len(dirs)} folders, {len(files)} files):\n"
        items_preview = dirs + files
        if not items_preview:
            return f"{summary}  (Directory is empty)"
        if len(items_preview) > 50:
            preview_str = "\n".join(f"  {it}" for it in items_preview[:50])
            return f"{summary}{preview_str}\n  ... and {len(items_preview) - 50} more items."
        return summary + "\n".join(f"  {it}" for it in items_preview)

    except PermissionError:
        return f"Sir, OS permission was denied for: {p}"
    except Exception as e:
        return f"Sir, could not read directory {p}: {e}"

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Plugin entry point for JARVIS."""
    action = (parameters.get("action") or "drives").lower().strip()
    path = parameters.get("path") or ""
    show_hidden = bool(parameters.get("show_hidden", False))

    if player:
        try:
            player.write_log(f"[drive_manager] {action} {path}")
        except Exception:
            pass

    if action in ("drives", "list_drives"):
        drives = get_accessible_drives()
        if not drives:
            return "Sir, no accessible drives could be found."
        lines = [f"Accessible System Drives ({len(drives)}):"]
        for d in drives:
            if "error" in d:
                lines.append(f"  💾 {d['drive']} (Inaccessible)")
            else:
                lines.append(f"  💾 {d['drive']} — Free: {d['free']} of {d['total']} ({d['percent']} used)")
        return "\n".join(lines)

    elif action in ("list", "ls", "dir"):
        if not path:
            return "Sir, please specify a path to list."
        return list_directory(path, show_hidden=show_hidden)

    elif action in ("usage", "disk_usage"):
        target = path if path else "C:\\"
        try:
            u = shutil.disk_usage(target)
            pct = (u.used / u.total) * 100 if u.total else 0
            return (
                f"Disk Usage for {target}:\n"
                f"  Total: {_format_size(u.total)}\n"
                f"  Used : {_format_size(u.used)} ({pct:.1f}%)\n"
                f"  Free : {_format_size(u.free)}"
            )
        except Exception as e:
            return f"Sir, could not get disk usage for {target}: {e}"

    elif action in ("info", "stat"):
        if not path:
            return "Sir, please specify a path to inspect."
        p = Path(path.strip().strip('"').strip("'")).resolve()
        if not p.exists():
            return f"Sir, {p} was not found."
        try:
            st = p.stat()
            ftype = "Folder" if p.is_dir() else "File"
            return (
                f"Item Details for {p.name}:\n"
                f"  Type: {ftype}\n"
                f"  Path: {p}\n"
                f"  Size: {_format_size(st.st_size)}"
            )
        except Exception as e:
            return f"Sir, could not retrieve stats for {p}: {e}"

    return f"Sir, unknown action '{action}'. Valid actions are 'drives', 'list', 'usage', 'info'."
