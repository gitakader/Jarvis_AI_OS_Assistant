"""
selfheal_tests/test_boot_sentry.py — offline verification of core/boot_sentry.py.

Phase-mission style: each check prints an evidence block; STOPS at the first
failure. Pure tests (evaluate / choose_candidate / recover / check_and_recover
on temp dirs) run by default and are safe while a live JARVIS is up.

The guardian integration test runs only with --guardian, because it drives a
real guardian + stub child through logs/ and actions/ — it needs the DOWN
WINDOW (live JARVIS stopped), exactly like the guardian tests in run_tests.py.

    venv\\Scripts\\python.exe -u selfheal_tests\\test_boot_sentry.py
    venv\\Scripts\\python.exe -u selfheal_tests\\test_boot_sentry.py --guardian
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = Path(__file__).resolve().parent
LOGS = ROOT / "logs"
PY = sys.executable

sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

import core.boot_sentry as BS

PASSED = 0


def ok(name, evidence):
    global PASSED
    PASSED += 1
    print(f"\nPASS {name}")
    for line in evidence:
        print(f"  {line}")


def fail(name, evidence):
    print(f"\nFAIL {name}")
    for line in evidence:
        print(f"  {line}")
    sys.exit(1)


def make_event(crashed_at, started_at, code=1):
    return BS.CrashEvent(crashed_at, started_at, code)


def fresh_root():
    tmp = Path(tempfile.mkdtemp(prefix="sentry_"))
    for d in ("actions", "plugins", "features", "mcp_servers", "core", "config",
              "logs"):
        (tmp / d).mkdir(parents=True, exist_ok=True)
    return tmp


# ── 1. trigger evaluation ────────────────────────────────────────────────────

def test_trigger():
    t0 = 1_000_000.0
    ev_empty = []
    ev_one = [make_event(t0 - 10, t0 - 20, 1)]
    ev_two_boot = [make_event(t0 - 40, t0 - 60, 1), make_event(t0 - 10, t0 - 25, 2)]
    ev_long = [make_event(t0 - 40, t0 - 400, 1), make_event(t0 - 10, t0 - 370, 1)]
    ev_stale = [make_event(t0 - 5000, t0 - 5020, 1), make_event(t0 - 4900, t0 - 4910, 1)]
    ev_mixed = [make_event(t0 - 40, t0 - 60, 1), make_event(t0 - 10, t0 - 300, 1)]

    evidence = []
    evidence.append(f"no events -> None: {BS.evaluate(ev_empty, t0) is None}")
    evidence.append(f"one boot crash -> None: {BS.evaluate(ev_one, t0) is None}")
    fired = BS.evaluate(ev_two_boot, t0)
    evidence.append(f"two boot crashes -> fired: {bool(fired)} "
                    f"(kind={fired.get('kind') if fired else None}, "
                    f"n={fired.get('recent_boot_crashes') if fired else None}, "
                    f"exit_codes={fired.get('exit_codes') if fired else None})")
    evidence.append(f"two long-uptime crashes -> None: {BS.evaluate(ev_long, t0) is None}")
    evidence.append(f"two old boot crashes -> None: {BS.evaluate(ev_stale, t0) is None}")
    evidence.append(f"one boot + one long -> None: {BS.evaluate(ev_mixed, t0) is None}")
    cool = BS.evaluate(ev_two_boot, t0, last_recovery_ts=t0 - 60)
    cool_old = BS.evaluate(ev_two_boot, t0, last_recovery_ts=t0 - 7200)
    evidence.append(f"recent recovery suppresses: {cool is None}")
    evidence.append(f"stale recovery does not: {cool_old is not None}")
    if not (BS.evaluate(ev_empty, t0) is None
            and BS.evaluate(ev_one, t0) is None
            and fired and fired.get("recent_boot_crashes") == 2
            and fired.get("exit_codes") == [1, 2]
            and BS.evaluate(ev_long, t0) is None
            and BS.evaluate(ev_stale, t0) is None
            and BS.evaluate(ev_mixed, t0) is None
            and cool is None and cool_old is not None):
        fail("trigger evaluation", evidence)
    ok("trigger evaluation", evidence)


# ── 2. candidate selection ───────────────────────────────────────────────────

def test_candidate():
    tmp = fresh_root()
    now = time.time()
    newest = tmp / "actions" / "newest.py"
    newest.write_text("# newest\n")
    (tmp / "plugins" / "older.py").write_text("# older\n")
    old = now - 10 * 86400
    os.utime(tmp / "plugins" / "older.py", (old, old))
    deny_newer = tmp / "core" / "sentry_core.py"
    deny_newer.write_text("# newest but denied\n")
    (tmp / "logs" / "state.json").write_text("{}")
    evidence = []
    cand = BS.choose_candidate(tmp, now)
    evidence.append(f"picks newest safe-surface file: {cand == newest} "
                    f"(got {cand.relative_to(tmp) if cand else None})")
    evidence.append(f"ignores newer deny-dir files: {cand != deny_newer}")
    newest.unlink()
    evidence.append(f"stale files ignored -> None: "
                    f"{BS.choose_candidate(tmp, now) is None}")
    if not (cand == newest and cand != deny_newer
            and BS.choose_candidate(tmp, now) is None):
        fail("candidate selection", evidence)
    ok("candidate selection", evidence)


# ── 3. recovery: .bak restore ────────────────────────────────────────────────

def test_restore_backup():
    tmp = fresh_root()
    f = tmp / "actions" / "broken.py"
    f.write_text("# broken\n")
    f.with_name("broken.py.bak").write_text("# good\n")
    result = BS.recover(f, tmp)
    evidence = []
    evidence.append(f"action=restore_backup: {result.get('action') == 'restore_backup'}")
    evidence.append(f"file restored from .bak: {f.read_text() == '# good\n'}")
    evidence.append(f"file still present: {f.exists()}")
    if not (result.get("action") == "restore_backup" and f.read_text() == "# good\n"
            and f.exists()):
        fail("recovery: restore .bak", evidence)
    ok("recovery: restore .bak", evidence)


# ── 4. recovery: quarantine ──────────────────────────────────────────────────

def test_quarantine():
    tmp = fresh_root()
    f = tmp / "plugins" / "culprit.py"
    f.write_text("raise SystemExit('boom')\n")
    result = BS.recover(f, tmp)
    q = list((tmp / "logs" / "quarantine").glob("*_culprit.py"))
    evidence = []
    evidence.append(f"action=quarantine: {result.get('action') == 'quarantine'}")
    evidence.append(f"original removed: {not f.exists()}")
    evidence.append(f"moved to logs/quarantine/: {bool(q)} ({q[0].name if q else 'none'})")
    evidence.append(f"content preserved: "
                    f"{q and q[0].read_text() == 'raise SystemExit(\'boom\')\n'}")
    if not (result.get("action") == "quarantine" and not f.exists() and q
            and q[0].read_text() == "raise SystemExit('boom')\n"):
        fail("recovery: quarantine", evidence)
    ok("recovery: quarantine", evidence)


# ── 5. recovery: deny defense ────────────────────────────────────────────────

def test_deny_defense():
    tmp = fresh_root()
    secrets = tmp / "config" / "api_keys.json"
    secrets.write_text('{"secret": 1}')
    entry = tmp / "main.py"
    entry.write_text("# entry\n")
    evidence = []
    for p in (secrets, entry):
        try:
            BS.recover(p, tmp)
            raised = False
        except ValueError:
            raised = True
        evidence.append(f"recover({p.name}) raises: {raised}")
    result = BS.check_and_recover(
        [make_event(time.time() - 10, time.time() - 20, 1),
         make_event(time.time() - 5, time.time() - 15, 1)],
        time.time(), tmp)
    evidence.append(f"check_and_recover stands down on deny: "
                    f"{result.get('fired') is False} (reason={result.get('reason')})")
    if not all("raises: True" in e for e in evidence[:2]) or result.get("fired"):
        fail("recovery: deny defense", evidence)
    ok("recovery: deny defense", evidence)


# ── 6. end-to-end orchestration ──────────────────────────────────────────────

def test_end_to_end():
    tmp = fresh_root()
    now = time.time()
    culprit = tmp / "actions" / "last_edit.py"
    culprit.write_text("def broken(): ...\n")
    events = [make_event(now - 40, now - 60, 1), make_event(now - 10, now - 25, 2)]
    result = BS.check_and_recover(events, now, tmp)
    evidence = []
    evidence.append(f"fires on boot loop + fresh safe edit: {result.get('fired') is True}")
    evidence.append(f"action={result.get('action')}, file={result.get('file')}")
    evidence.append(f"quarantined copy exists: "
                    f"{list((tmp / 'logs' / 'quarantine').glob('*_last_edit.py')) != []}")
    again = BS.check_and_recover(events, now, tmp, last_recovery_ts=now)
    evidence.append(f"cooldown stops a second pass: "
                    f"{again.get('fired') is False} (reason={again.get('reason')})")

    tmp2 = fresh_root()
    (tmp2 / "core" / "sentry_new.py").write_text("# newest, denied\n")
    (tmp2 / "actions" / "old.py").write_text("# old\n")
    old = now - 10 * 86400
    os.utime(tmp2 / "actions" / "old.py", (old, old))
    r2 = BS.check_and_recover(events, now, tmp2)
    evidence.append(f"newest edit in core/ -> stands down: "
                    f"{r2.get('fired') is False} (reason={r2.get('reason')})")
    if not (result.get("fired") is True and result.get("action") == "quarantine"
            and again.get("fired") is False
            and r2.get("fired") is False and r2.get("reason") == "no_safe_candidate"):
        fail("end-to-end orchestration", evidence)
    ok("end-to-end orchestration", evidence)


# ── 7. CrashEvent model ──────────────────────────────────────────────────────

def test_event_model():
    e = make_event(100.0, 70.0, 7)
    d = e.to_dict()
    evidence = []
    evidence.append(f"uptime = crashed - started: {e.uptime == 30.0}")
    evidence.append(f"negative uptime clamped: {make_event(50.0, 60.0, 1).uptime == 0.0}")
    evidence.append(f"to_dict fields: {sorted(d.keys())}")
    if not (e.uptime == 30.0 and make_event(50.0, 60.0, 1).uptime == 0.0
            and d["exit_code"] == 7):
        fail("CrashEvent model", evidence)
    ok("CrashEvent model", evidence)


# ── 8. guardian integration (--guardian, DOWN WINDOW ONLY) ───────────────────

def kill_stub():
    try:
        import psutil
        pid = int((TESTS / "stub.pid").read_text().strip())
        psutil.Process(pid).kill()
    except Exception:
        pass


def clean_boot_artifacts():
    for p in LOGS.glob("crash_*.log"):
        p.unlink(missing_ok=True)
    stub_file = ROOT / "actions" / "_sentry_test_stub.py"
    stub_file.unlink(missing_ok=True)
    qdir = LOGS / "quarantine"
    if qdir.is_dir():
        for p in qdir.glob("*_sentry_test_stub.py"):
            p.unlink(missing_ok=True)


def test_guardian_integration():
    clean_boot_artifacts()
    g = subprocess.Popen(
        [PY, "-u", str(ROOT / "guardian.py"), "--no-port-check",
         "--poll", "0.3", "--max-crashes", "5", "--crash-window", "120",
         "--child", PY, str(TESTS / "stub_bootcrash.py")],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    deadline = time.time() + 90
    quarantined = []
    while time.time() < deadline:
        quarantined = list((LOGS / "quarantine").glob("*_sentry_test_stub.py"))
        if quarantined:
            break
        time.sleep(0.5)

    try:
        g.terminate()
        g.wait(timeout=10)
    except Exception:
        g.kill()
    kill_stub()

    text = ""
    try:
        text = (LOGS / "guardian.log").read_text(encoding="utf-8", errors="replace")
    except Exception:
        pass
    state = {}
    try:
        state = json.loads((LOGS / "guardian_state.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    recovery = state.get("last_recovery", {})
    evidence = []
    evidence.append(f"guardian detected the crash loop: "
                    f"{'exited unexpectedly' in text} "
                    f"(crashes: {text.count('exited unexpectedly')})")
    evidence.append(f"sentry fired: {'[BootSentry]' in text}")
    evidence.append(f"quarantined the planted file: {[p.name for p in quarantined]}")
    evidence.append(f"state.last_recovery action={recovery.get('action')}, "
                    f"file={recovery.get('file')}")
    evidence.append(f"exactly one recovery (cooldown held): "
                    f"{recovery and len(quarantined) == 1}")
    planted_still_there = (ROOT / "actions" / "_sentry_test_stub.py").exists()
    evidence.append(f"planted file re-created by later launches "
                    f"(expected, cooldown must hold): {planted_still_there}")
    if not ("[BootSentry]" in text and quarantined
            and recovery.get("action") == "quarantine"
            and len(quarantined) == 1):
        clean_boot_artifacts()
        fail("guardian integration", evidence)
    clean_boot_artifacts()
    ok("guardian integration", evidence)


# ── run ──────────────────────────────────────────────────────────────────────

def main():
    print(f"boot_sentry verification suite — python {sys.version.split()[0]}")
    print(f"project: {ROOT}")
    test_trigger()
    test_candidate()
    test_restore_backup()
    test_quarantine()
    test_deny_defense()
    test_end_to_end()
    test_event_model()
    if "--guardian" in sys.argv:
        print("\n[guardian integration] requires JARVIS stopped (down window)")
        test_guardian_integration()
    print(f"\n=== ALL {PASSED}/{'8' if '--guardian' in sys.argv else '7'} TESTS PASSED ===")


if __name__ == "__main__":
    main()
