"""system_pack.py — System controls pack for JARVIS.

One tool with action sub-commands (same convention as computer_control and
computer_settings): processes, startup programs, apps via winget, battery,
notifications, Recycle Bin, and power (sleep / sign out).

Gates follow core/confirm.py: only genuinely irreversible things (empty the
Recycle Bin, sign out) park behind the on-screen HUD confirmation. Everything
reversible — killing an app, disabling a startup entry, installing a package —
runs immediately; the model is expected to have the user's explicit request.
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path

import psutil

from core import confirm

_OS = platform.system()

if _OS == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
    _DETACHED: dict = {
        "creationflags": subprocess.CREATE_NO_WINDOW
        | subprocess.DETACHED_PROCESS
        | subprocess.CREATE_NEW_PROCESS_GROUP,
    }
else:
    _WIN_HIDE: dict = {}
    _DETACHED: dict = {}


def _base_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def _powershell_exe() -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
               / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe")


def _run_powershell(script: str, timeout: int = 30):
    """Run PowerShell with UTF-8 output forced, falling back to the absolute
    exe path when the PATH is trimmed (e.g. some shells only expose System32)."""
    full_script = ("[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; " + script)
    for cmd in (["powershell", "-NoProfile", "-Command", full_script],
                [_powershell_exe(), "-NoProfile", "-Command", full_script]):
        try:
            return subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=timeout, **_WIN_HIDE)
        except FileNotFoundError:
            continue
    raise FileNotFoundError("powershell.exe not found")


# Processes JARVIS will never kill, by name. Explorer is here because a dead
# shell mid-conversation looks like a crash; python is here because JARVIS and
# its guardian run under python.exe, and killing "python.exe" by name would
# take the assistant down with it.
_PROTECTED = {
    "system idle process", "system", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe", "explorer.exe",
    "python.exe", "pythonw.exe",
}


def _own_pids() -> set[int]:
    pids: set[int] = set()
    try:
        p = psutil.Process(os.getpid())
        while p is not None:
            pids.add(p.pid)
            p = p.parent()
    except Exception:
        pass
    return pids


def _snapshot(prime_cpu: bool) -> list[dict]:
    procs = []
    for p in psutil.process_iter():
        try:
            procs.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if prime_cpu:
        for p in procs:
            try:
                p.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        time.sleep(0.3)
    rows = []
    for p in procs:
        try:
            rows.append({
                "pid": p.pid,
                "name": p.name() or "?",
                "cpu": p.cpu_percent(None) or 0.0,
                "mem": p.memory_percent() or 0.0,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return rows


def _process_list(sort_by: str, limit: int) -> str:
    limit = max(1, min(int(limit or 10), 25))
    by_mem = str(sort_by or "cpu").strip().lower() in ("mem", "memory", "ram")
    rows = _snapshot(prime_cpu=not by_mem)
    rows.sort(key=lambda r: r["mem" if by_mem else "cpu"], reverse=True)
    key = "RAM %" if by_mem else "CPU %"
    lines = [f"Top {min(limit, len(rows))} processes by {key}:"]
    for r in rows[:limit]:
        lines.append(f"  {r['pid']:>7}  {r['name'][:28]:<28} {key}: {r['mem' if by_mem else 'cpu']:>5.1f}")
    return "\n".join(lines)


def _terminate(p: psutil.Process) -> None:
    try:
        p.terminate()
        p.wait(timeout=3)
    except psutil.TimeoutExpired:
        p.kill()
    except psutil.NoSuchProcess:
        pass


def _process_kill(name: str, pid) -> str:
    own = _own_pids()
    if pid not in (None, ""):
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return f"Invalid pid: {pid!r}."
        if pid in own:
            return "I will not kill my own process, Sir."
        try:
            p = psutil.Process(pid)
            pname = p.name() or "?"
            if pname.lower() in _PROTECTED:
                return f"{pname} (pid {pid}) is a protected system process. I will not kill it."
            _terminate(p)
            return f"Killed {pname} (pid {pid})."
        except psutil.NoSuchProcess:
            return f"No process with pid {pid}."
        except psutil.AccessDenied:
            return f"Access denied killing pid {pid} — it may need administrator rights."

    if not name:
        return "Give me a process name (e.g. chrome.exe) or a pid to kill."
    low = str(name).strip().lower()
    if low in _PROTECTED:
        return f"{name} is a protected system process. I will not kill it."
    if not low.endswith(".exe"):
        low += ".exe"
    targets = []
    for p in psutil.process_iter():
        try:
            if (p.name() or "").lower() == low:
                targets.append(p)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    targets = [p for p in targets if p.pid not in own]
    if not targets:
        return f"No running process named '{name}'."
    killed, failed = [], []
    for p in targets:
        try:
            _terminate(p)
            killed.append(str(p.pid))
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            failed.append(str(p.pid))
    msg = f"Killed {len(killed)} process(es) named '{name}'"
    msg += f" (pids: {', '.join(killed)})." if killed else "."
    if failed:
        msg += f" {len(failed)} more refused access (may need administrator)."
    return msg


def _startup_folder() -> Path:
    base = Path(os.environ.get("APPDATA", ""))
    return base / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def _startup_entries() -> list[dict]:
    entries: list[dict] = []
    if _OS != "Windows":
        return entries
    import winreg
    for hive, scope in ((winreg.HKEY_CURRENT_USER, "user"),
                        (winreg.HKEY_LOCAL_MACHINE, "machine")):
        try:
            key = winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\Run",
                                 0, winreg.KEY_READ)
            i = 0
            while True:
                try:
                    vname, command, _ = winreg.EnumValue(key, i)
                    entries.append({
                        "name": vname,
                        "command": str(command),
                        "scope": scope,
                        "kind": "registry",
                        "disabled": vname.endswith(".disabled"),
                    })
                    i += 1
                except OSError:
                    break
            winreg.CloseKey(key)
        except OSError:
            continue
    folder = _startup_folder()
    if folder.is_dir():
        for sub in folder.iterdir():
            if sub.is_file():
                entries.append({"name": sub.name, "command": str(sub), "scope": "user",
                                "kind": "folder", "disabled": False})
        disabled_dir = folder / "_jarvis_disabled"
        if disabled_dir.is_dir():
            for sub in disabled_dir.iterdir():
                if sub.is_file():
                    entries.append({"name": sub.name, "command": str(sub), "scope": "user",
                                    "kind": "folder", "disabled": True})
    return entries


def _startup_list() -> str:
    entries = _startup_entries()
    if not entries:
        return "No startup programs found."
    lines = [f"{len(entries)} startup entr{'y' if len(entries) == 1 else 'ies'}:"]
    for e in sorted(entries, key=lambda e: (e["disabled"], e["name"].lower())):
        state = "  [disabled]" if e["disabled"] else ""
        scope = e["scope"] + (" registry" if e["kind"] == "registry" else " folder")
        lines.append(f"  {e['name']}{state} ({scope})")
    return "\n".join(lines)


def _normalise_startup(name: str) -> str:
    return str(name or "").strip().lower().removesuffix(".lnk").removesuffix(".disabled").strip()


def _find_startup(name: str) -> tuple[list[dict], str]:
    """Matching startup entries plus an error message ("" when fine)."""
    entries = _startup_entries()
    q = _normalise_startup(name)
    if not q:
        return [], "Give me the name of the startup entry."
    cands = [e for e in entries if _normalise_startup(e["name"]) == q]
    if len(cands) == 1:
        return cands, ""
    if not cands:
        sub = [e for e in entries if q in _normalise_startup(e["name"])]
        if len(sub) == 1:
            return sub, ""
        if len(sub) > 1:
            cands = sub
        else:
            return [], f"No startup entry matching '{name}'."
    # Multiple candidates — ambiguity is resolved by the user.
    lines = [f"Several startup entries match '{name}':"]
    for e in cands:
        lines.append(f"  {e['name']} ({e['scope']} {e['kind']})")
    lines.append("Tell me the exact name.")
    return [], "\n".join(lines)


def _startup_disable(name: str) -> str:
    entries, err = _find_startup(name)
    if err:
        return err
    e = entries[0]
    if e["disabled"]:
        return f"'{e['name']}' is already disabled."
    if e["kind"] == "folder":
        try:
            src = Path(e["command"])
            dst_dir = _startup_folder() / "_jarvis_disabled"
            dst_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst_dir / src.name))
            return f"Disabled startup entry '{e['name']}' (moved aside, nothing deleted)."
        except Exception as exc:
            return f"Could not disable '{e['name']}': {exc}"
    if e["scope"] == "machine":
        return f"'{e['name']}' is a machine-wide entry. Disabling it needs administrator rights — I did not touch it."
    import winreg
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        value, vtype = winreg.QueryValueEx(key, e["name"])
        winreg.SetValueEx(key, e["name"] + ".disabled", 0, vtype, value)
        winreg.DeleteValue(key, e["name"])
        winreg.CloseKey(key)
        return f"Disabled startup entry '{e['name']}' (renamed to '{e['name']}.disabled', nothing deleted)."
    except OSError as exc:
        return f"Could not disable '{e['name']}': {exc}"


def _startup_enable(name: str) -> str:
    entries, err = _find_startup(name)
    if err:
        return err
    e = entries[0]
    if not e["disabled"]:
        return f"'{e['name']}' is already enabled."
    if e["kind"] == "folder":
        try:
            src = Path(e["command"])
            dst = _startup_folder() / src.name
            if dst.exists():
                return f"Cannot enable '{e['name']}': an enabled entry with that name already exists."
            shutil.move(str(src), str(dst))
            return f"Enabled startup entry '{e['name']}'."
        except Exception as exc:
            return f"Could not enable '{e['name']}': {exc}"
    import winreg
    plain = e["name"].removesuffix(".disabled")
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
        try:
            winreg.QueryValueEx(key, plain)
            winreg.CloseKey(key)
            return f"Cannot enable '{plain}': an enabled entry with that name already exists."
        except FileNotFoundError:
            pass
        value, vtype = winreg.QueryValueEx(key, e["name"])
        winreg.SetValueEx(key, plain, 0, vtype, value)
        winreg.DeleteValue(key, e["name"])
        winreg.CloseKey(key)
        return f"Enabled startup entry '{plain}'."
    except OSError as exc:
        return f"Could not enable '{e['name']}': {exc}"


def _battery() -> str:
    try:
        b = psutil.sensors_battery()
    except Exception:
        b = None
    if b is None:
        return "No battery detected — this machine is probably a desktop."
    pct = round(b.percent)
    if b.power_plugged:
        state = "fully charged" if pct >= 100 else "charging"
        return f"Battery: {pct}% — plugged in ({state})."
    if b.secsleft == psutil.POWER_TIME_UNLIMITED:
        return f"Battery: {pct}% (on battery)."
    if b.secsleft in (psutil.POWER_TIME_UNKNOWN, None) or b.secsleft < 0:
        return f"Battery: {pct}% (on battery)."
    h, m = divmod(int(b.secsleft) // 60, 60)
    return f"Battery: {pct}% (on battery, about {h}h {m}m left)."


def _ps_quote(text: str) -> str:
    return str(text or "").replace("'", "''")


_TOAST_SCRIPT = """
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
$t = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$n = $t.GetElementsByTagName('text')
$n.Item(0).AppendChild($t.CreateTextNode('{title}')) | Out-Null
$n.Item(1).AppendChild($t.CreateTextNode('{message}')) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($t)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($toast)
"""


def _toast(title: str, message: str) -> str:
    if not title and not message:
        return "Give me a title and message for the notification."
    if _OS != "Windows":
        return "Notifications are Windows-only here."
    script = _TOAST_SCRIPT.format(title=_ps_quote(title or "JARVIS"),
                                  message=_ps_quote(message or ""))
    try:
        r = _run_powershell(script, timeout=20)
    except Exception as exc:
        return f"Could not show the notification: {exc}"
    if r.returncode == 0:
        return f"Notification shown: {title or message}"
    err = (r.stderr or r.stdout or "").strip().splitlines()
    tail = err[-1] if err else f"exit code {r.returncode}"
    return f"Could not show the notification: {tail}"


def _winget_run(args: list[str], timeout: int = 60) -> str:
    try:
        r = subprocess.run(["winget", *args], capture_output=True, text=True,
                           encoding="utf-8", errors="replace",
                           timeout=timeout, **_WIN_HIDE)
        return (r.stdout or "") + "\n" + (r.stderr or "")
    except FileNotFoundError:
        return "__MISSING__"
    except subprocess.TimeoutExpired:
        return "__TIMEOUT__"


_NO_RESULT_MARKERS = ("no installed package found", "no package found",
                      "no packages found")


def _empty_result(out: str) -> bool:
    low = out.lower()
    return any(m in low for m in _NO_RESULT_MARKERS)


def _table_rows(out: str) -> list[str]:
    lines = [ln.rstrip() for ln in out.splitlines()]
    for i, ln in enumerate(lines):
        if ln.strip().startswith("---"):
            return [l for l in lines[i + 1:] if l.strip()]
    return [l for l in lines if l.strip()]


def _app_search(query: str, limit: int) -> str:
    if _OS != "Windows":
        return "App management is Windows-only here."
    q = str(query or "").strip()
    if not q:
        return "Give me a search term, e.g. 'chrome'."
    out = _winget_run(["search", q, "--accept-source-agreements"], timeout=60)
    if out == "__MISSING__":
        return "winget is not installed. Windows 10/11 usually has it built in."
    if out == "__TIMEOUT__":
        return "The winget search timed out. Please try again."
    rows = _table_rows(out)
    if not rows or _empty_result(out):
        return f"No packages found for '{q}'."
    limit = max(1, min(int(limit or 8), 15))
    lines = [f"{len(rows)} package(s) found for '{q}' (name — id):"]
    for row in rows[:limit]:
        lines.append(f"  {row[:100]}")
    if len(rows) > limit:
        lines.append(f"  ... and {len(rows) - limit} more.")
    return "\n".join(lines)


def _winget_log_path(verb: str, package_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(package_id or "pkg"))[:60]
    return _base_dir() / "logs" / f"winget_{verb}_{safe}.log"


def _app_install(package_id: str) -> str:
    if _OS != "Windows":
        return "App management is Windows-only here."
    pid = str(package_id or "").strip()
    if not pid or re.search(r"\s", pid):
        return "Give me one winget package id (no spaces), e.g. Google.Chrome."
    check = _winget_run(["show", "--id", pid, "--accept-source-agreements"], timeout=60)
    if check == "__MISSING__":
        return "winget is not installed. Windows 10/11 usually has it built in."
    if check == "__TIMEOUT__":
        return "Checking the package timed out. Please try again."
    if "No package found" in check:
        return (f"winget does not know a package with id '{pid}'. "
                f"Use app_search first and give me the exact id.")
    log = _winget_log_path("install", pid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as f:
        subprocess.Popen(
            ["winget", "install", "--id", pid, "--silent",
             "--accept-source-agreements", "--accept-package-agreements",
             "--disable-interactivity"],
            stdout=f, stderr=subprocess.STDOUT, **_DETACHED,
        )
    return (f"Install of '{pid}' started in the background — this usually takes "
            f"a few minutes. I will not know automatically when it finishes; "
            f"when the user asks, call app_check to confirm it completed.")


def _app_uninstall(package_id: str) -> str:
    if _OS != "Windows":
        return "App management is Windows-only here."
    pid = str(package_id or "").strip()
    if not pid or re.search(r"\s", pid):
        return "Give me one winget package id (no spaces)."
    check = _winget_run(["list", "--id", pid], timeout=90)
    if check == "__MISSING__":
        return "winget is not installed. Windows 10/11 usually has it built in."
    if check == "__TIMEOUT__":
        return "Checking the installed list timed out. Please try again."
    rows = _table_rows(check)
    if not rows or _empty_result(check):
        return f"No installed package found with id '{pid}'. Nothing to uninstall."
    log = _winget_log_path("uninstall", pid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "wb") as f:
        subprocess.Popen(
            ["winget", "uninstall", "--id", pid, "--silent",
             "--accept-source-agreements", "--disable-interactivity"],
            stdout=f, stderr=subprocess.STDOUT, **_DETACHED,
        )
    return (f"Uninstall of '{pid}' started in the background. When the user "
            f"asks, call app_check to confirm it is gone.")


def _app_check(package_id: str) -> str:
    if _OS != "Windows":
        return "App management is Windows-only here."
    pid = str(package_id or "").strip()
    if not pid:
        return "Give me the winget package id to check."
    out = _winget_run(["list", "--id", pid], timeout=90)
    if out == "__MISSING__":
        return "winget is not installed. Windows 10/11 usually has it built in."
    if out == "__TIMEOUT__":
        return "Checking the installed list timed out. Please try again."
    rows = _table_rows(out)
    if rows and not _empty_result(out):
        return f"'{pid}' is installed: {rows[0][:100]}"
    lines = [f"'{pid}' is NOT in the installed list."]
    for verb in ("install", "uninstall"):
        log = _winget_log_path(verb, pid)
        if log.is_file():
            try:
                tail = log.read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
                lines.append(f"Last lines of the {verb} log:")
                lines.extend(f"  {ln[:110]}" for ln in tail)
            except OSError:
                pass
    return "\n".join(lines)


def _app_list(limit: int) -> str:
    if _OS != "Windows":
        return "App management is Windows-only here."
    out = _winget_run(["list"], timeout=90)
    if out == "__MISSING__":
        return "winget is not installed. Windows 10/11 usually has it built in."
    if out == "__TIMEOUT__":
        return "Listing installed apps timed out. Please try again."
    rows = _table_rows(out)
    if not rows or _empty_result(out):
        return "winget reports no installed packages."
    limit = max(1, min(int(limit or 15), 40))
    lines = [f"{len(rows)} installed package(s) (name — id):"]
    for row in rows[:limit]:
        lines.append(f"  {row[:100]}")
    if len(rows) > limit:
        lines.append(f"  ... and {len(rows) - limit} more. Ask with a larger limit for the rest.")
    return "\n".join(lines)


def _recycle_list() -> str:
    if _OS != "Windows":
        return "Recycle Bin listing is Windows-only here."
    script = (
        "$shell = New-Object -ComObject Shell.Application; "
        "$items = @($shell.NameSpace(0xA).Items()); "
        "if ($items.Count -eq 0) { Write-Output '__EMPTY__' } "
        "else { $items | Select-Object -First 25 | ForEach-Object { $_.Name } }"
    )
    try:
        r = _run_powershell(script, timeout=30)
    except Exception as exc:
        return f"Could not read the Recycle Bin: {exc}"
    lines = [ln.strip() for ln in (r.stdout or "").splitlines() if ln.strip()]
    if not lines:
        return f"Could not read the Recycle Bin: {((r.stderr or '').strip() or 'no output')}"
    if lines and lines[0] == "__EMPTY__":
        return "The Recycle Bin is empty."
    out = [f"{len(lines)} item(s) in the Recycle Bin (first 25):"]
    out.extend(f"  {ln[:80]}" for ln in lines)
    return "\n".join(out)


def _recycle_empty() -> str:
    if _OS != "Windows":
        return "Recycle Bin emptying is Windows-only here."
    if confirm.pending_title():
        return ("There is already a confirmation waiting on screen. "
                "Ask the user to answer that one first.")

    def run() -> str:
        try:
            r = _run_powershell("Clear-RecycleBin -Force -ErrorAction Stop",
                                timeout=60)
            if r.returncode == 0:
                return "The Recycle Bin has been emptied."
            return f"Could not empty the Recycle Bin: {(r.stderr or '').strip()}"
        except Exception as exc:
            return f"Could not empty the Recycle Bin: {exc}"

    return confirm.request(
        key="recycle_empty",
        title="Empty the Recycle Bin",
        detail="Everything in the Recycle Bin will be permanently deleted.",
        run=run,
    )


def _power(sub: str) -> str:
    sub = str(sub or "").strip().lower()
    if sub == "sleep":
        if _OS == "Windows":
            try:
                _run_powershell(
                    "Add-Type -AssemblyName System.Windows.Forms; "
                    "[System.Windows.Forms.Application]::SetSuspendState('Suspend', $false, $false)",
                    timeout=15)
            except Exception as exc:
                return f"Could not put the computer to sleep: {exc}"
            return "Putting the computer to sleep."
        return "Sleep is Windows-only here."
    if sub == "signout":
        if _OS != "Windows":
            return "Sign out is Windows-only here."
        if confirm.pending_title():
            return ("There is already a confirmation waiting on screen. "
                    "Ask the user to answer that one first.")

        def run() -> str:
            subprocess.run(["shutdown", "/l"], capture_output=True, **_WIN_HIDE)
            return "Signing out."

        return confirm.request(
            key="signout",
            title="Sign out of Windows",
            detail="All open programs will be closed. Anything unsaved will be lost.",
            run=run,
        )
    return "For 'power' give sub: sleep | signout."


_ACTIONS = {
    "process_list": lambda p: _process_list(p.get("sort_by"), p.get("limit")),
    "process_kill": lambda p: _process_kill(p.get("name"), p.get("pid")),
    "startup_list": lambda p: _startup_list(),
    "startup_disable": lambda p: _startup_disable(p.get("name") or ""),
    "startup_enable": lambda p: _startup_enable(p.get("name") or ""),
    "battery": lambda p: _battery(),
    "toast": lambda p: _toast(p.get("title") or "", p.get("message") or ""),
    "app_search": lambda p: _app_search(p.get("query"), p.get("limit")),
    "app_install": lambda p: _app_install(p.get("id") or ""),
    "app_uninstall": lambda p: _app_uninstall(p.get("id") or ""),
    "app_check": lambda p: _app_check(p.get("id") or ""),
    "app_list": lambda p: _app_list(p.get("limit")),
    "recycle_list": lambda p: _recycle_list(),
    "recycle_empty": lambda p: _recycle_empty(),
    "power": lambda p: _power(p.get("sub")),
}


def system_pack(parameters: dict = None, player=None) -> str:
    params = parameters or {}
    action = str(params.get("action", "")).strip().lower().replace(" ", "_").replace("-", "_")
    print(f"[Pack] Action: {action}")
    if player:
        player.write_log(f"[Pack] {action}")
    if action not in _ACTIONS:
        return ("Unknown action. Call system_pack again with `action` one of: "
                + ", ".join(sorted(_ACTIONS)) + ".")
    try:
        return _ACTIONS[action](params)
    except Exception as exc:
        return f"system_pack '{action}' failed: {exc}"


TOOL = {
    "name": "system_pack",
    "description": (
        "System administration for JARVIS. Use this tool for running processes, "
        "startup programs, installing/removing apps, battery, notifications, "
        "Recycle Bin and power. Sub-actions: process_list (what is running, top "
        "CPU or memory), process_kill (close a hung or unwanted program by name "
        "or pid — never for system processes), startup_list (programs that start "
        "with Windows), startup_disable / startup_enable (turn one off/on — "
        "reversible, nothing deleted), battery (charge level and time left), "
        "toast (show a Windows notification on screen), app_search (find a "
        "package, returns the exact id), app_install / app_uninstall (winget, "
        "only when the user explicitly asked, runs in background), app_check "
        "(verify an install/uninstall finished), app_list (installed packages), "
        "recycle_list (Recycle Bin contents), recycle_empty (needs the user's "
        "confirmation on the HUD), power (sub: sleep | signout — signout needs "
        "HUD confirmation)."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "process_list | process_kill | startup_list | startup_disable | "
                    "startup_enable | battery | toast | app_search | app_install | "
                    "app_uninstall | app_check | app_list | recycle_list | "
                    "recycle_empty | power"
                ),
            },
            "name": {
                "type": "STRING",
                "description": "Process name for process_kill (e.g. chrome.exe) or startup entry name for startup_disable/startup_enable",
            },
            "pid": {
                "type": "INTEGER",
                "description": "Process id for process_kill (alternative to name)",
            },
            "query": {
                "type": "STRING",
                "description": "Search term for app_search, e.g. 'chrome'",
            },
            "id": {
                "type": "STRING",
                "description": "Exact winget package id for app_install/app_uninstall/app_check, e.g. 'Google.Chrome'",
            },
            "title": {
                "type": "STRING",
                "description": "Notification title for toast",
            },
            "message": {
                "type": "STRING",
                "description": "Notification body text for toast",
            },
            "sort_by": {
                "type": "STRING",
                "description": "cpu | memory (process_list, default cpu)",
            },
            "limit": {
                "type": "INTEGER",
                "description": "Max results (process_list default 10, app_search default 8, app_list default 15)",
            },
            "sub": {
                "type": "STRING",
                "description": "For power: sleep | signout",
            },
        },
        "required": ["action"],
    },
    "handler": system_pack,
}
