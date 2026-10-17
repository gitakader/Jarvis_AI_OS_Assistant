"""
selfheal_tests/run_tests.py — offline verification of the self-healing pack.

Phase-mission style: each test prints an evidence block; the suite STOPS at
the first failure. Run with the venv python from the project root:

    venv\\Scripts\\python.exe -u selfheal_tests\\run_tests.py

Tests:
  1  compile        — all new/changed files are syntactically valid
  2  file logging   — stdout+stderr land in logs/jarvis.log, timestamps, rotation
  3  heartbeat      — healthy→stall transition + thread-stack dump + stopped state
  4  health API     — 401 without token, 200 with token, payload fields, provider
  5  guardian crash — nonzero exit → crash report + restart
  6  guardian clean — exit 0 → no restart, guardian exits
  7  guardian marker— exit 1 + marker → treated as intentional, no restart
  8  guardian double-launch — refuses when a live JARVIS heartbeat exists
  9  guardian stall — stale heartbeat → kill + treat as crash
  10 guardian port guard — refuses when the dashboard port is already held
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
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


def clean_artifacts():
    for name in ("guardian.log", "guardian_state.json", "guardian.lock",
                 "heartbeat.json", "heartbeat.json.tmp",
                 "intentional_shutdown.marker", "GUARDIAN_GAVE_UP.txt",
                 "jarvis.log", "jarvis.log.1", "jarvis.log.2", "jarvis.log.3"):
        try:
            (LOGS / name).unlink()
        except Exception:
            pass
    for p in LOGS.glob("crash_*.log"):
        p.unlink(missing_ok=True)
    for p in LOGS.glob("stall_*.txt"):
        p.unlink(missing_ok=True)


def read_log():
    try:
        return (LOGS / "guardian.log").read_text(encoding="utf-8")
    except Exception:
        return ""


def start_guardian(extra_args, child):
    cmd = [PY, "-u", str(ROOT / "guardian.py")] + extra_args + ["--child"] + child
    return subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)


def stop_process(proc):
    try:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
    except Exception:
        pass


def kill_stub():
    try:
        import psutil
        pid = int((TESTS / "stub.pid").read_text().strip())
        psutil.Process(pid).kill()
    except Exception:
        pass


# ── 1. compile ───────────────────────────────────────────────────────────────

def test_compile():
    import py_compile
    files = ["core/logging_setup.py", "core/heartbeat.py", "guardian.py",
             "dashboard/server.py", "main.py"]
    compiled = 0
    for rel in files:
        py_compile.compile(str(ROOT / rel), doraise=True)
        compiled += 1
    ok("compile", [f"{compiled}/{compiled} files compile cleanly: " + ", ".join(files)])


# ── 2. file logging ──────────────────────────────────────────────────────────

def test_logging():
    clean_artifacts()
    import core.logging_setup as ls
    ls.install_file_logging()
    print("LOG TEST stdout line 1")
    sys.stderr.write("LOG TEST stderr line\n")
    sys.stderr.flush()

    # rotation: cap the log at 2000 bytes so ~100 lines trigger one or two
    # rolls while the 3 backups keep every line — nothing may be lost
    ls._MAX_BYTES = 2000
    for i in range(100):
        print(f"LOG TEST filler {i}")
    time.sleep(0.3)

    current = (LOGS / "jarvis.log").read_text(encoding="utf-8", errors="replace")
    backups = ""
    for suffix in ("1", "2", "3"):
        try:
            backups += (LOGS / f"jarvis.log.{suffix}").read_text(encoding="utf-8",
                                                                 errors="replace")
        except Exception:
            pass
    evidence = []
    evidence.append(f"jarvis.log created: {(LOGS / 'jarvis.log').exists()}")
    evidence.append(f"rotation happened (backups exist): {(LOGS / 'jarvis.log.1').exists()}")
    evidence.append(f"newest line in active log: {'LOG TEST filler 99' in current}")
    evidence.append(f"oldest line survived rotation (in a backup): {'LOG TEST stdout line 1' in backups}")
    evidence.append(f"stderr captured: {'LOG TEST stderr line' in backups or 'LOG TEST stderr line' in current}")
    import re
    stamps = re.findall(r"\[\d{2}:\d{2}:\d{2}\]", current)
    evidence.append(f"timestamps present on active log lines: {bool(stamps)} (sample: {stamps[0] if stamps else 'none'})")
    if not all([(LOGS / 'jarvis.log').exists(), (LOGS / 'jarvis.log.1').exists(),
                'LOG TEST filler 99' in current, 'LOG TEST stdout line 1' in backups,
                'LOG TEST stderr line' in (backups + current), bool(stamps)]):
        fail("file logging", evidence)
    ok("file logging", evidence)


# ── 3. heartbeat ─────────────────────────────────────────────────────────────

def test_heartbeat():
    clean_artifacts()
    from PyQt6.QtWidgets import QApplication
    import core.heartbeat as hb

    app = QApplication.instance() or QApplication([])
    hb.start(stall_after_s=2.0, heartbeat_interval_s=0.3, tick_interval_ms=200)

    def pump(seconds):
        deadline = time.time() + seconds
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.05)

    pump(1.5)
    healthy = hb.read_heartbeat()

    # simulate a frozen main loop: the Qt timer stops firing but the watchdog
    # thread keeps running — exactly what a blocked GUI thread looks like.
    hb._timer.stop()
    pump(4.0)
    stalled = hb.read_heartbeat()
    dumps = list(LOGS.glob("stall_*.txt"))

    hb.stop()
    stopped = hb.read_heartbeat()

    evidence = []
    evidence.append(f"healthy state published: {healthy and healthy.get('state') == 'healthy'} "
                    f"(main_age_s={healthy.get('main_age_s') if healthy else None})")
    evidence.append(f"frozen main loop detected: {stalled and stalled.get('state') == 'stall'} "
                    f"(main_age_s={stalled.get('main_age_s') if stalled else None})")
    evidence.append(f"thread-stack dump written: {bool(dumps)} ({dumps[0].name if dumps else 'none'})")
    if dumps:
        text = dumps[0].read_text(encoding="utf-8", errors="replace")
        evidence.append(f"dump contains per-thread stacks: {'Thread 0x' in text or 'Current thread' in text}")
    evidence.append(f"clean stop publishes 'stopped': {stopped and stopped.get('state') == 'stopped'}")
    if not (healthy and healthy.get("state") == "healthy"
            and stalled and stalled.get("state") == "stall"
            and dumps and stopped and stopped.get("state") == "stopped"):
        fail("heartbeat", evidence)
    ok("heartbeat", evidence)


# ── 4. health API ────────────────────────────────────────────────────────────

def test_health_api():
    import threading
    import uvicorn
    from dashboard import server as dsrv

    port = 18023
    srv = dsrv.DashboardServer()
    srv._tokens.add("testtoken123")
    srv.set_health_provider(lambda: {"plugins": {"discovered": 3, "valid": 3, "enabled": 3}})

    uvicorn_server = uvicorn.Server(uvicorn.Config(
        srv.app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=uvicorn_server.run, daemon=True)
    thread.start()
    time.sleep(2.5)

    evidence = []
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=5)
            unauth = 200
        except urllib.error.HTTPError as e:
            unauth = e.code
        evidence.append(f"no token -> {unauth} (expected 401): {unauth == 401}")

        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/health",
                                     headers={"Authorization": "Bearer testtoken123"})
        body = json.loads(urllib.request.urlopen(req, timeout=5).read().decode("utf-8"))
        evidence.append(f"with token -> 200, ok={body.get('ok')}")
        evidence.append(f"payload fields: {sorted(body.keys())}")
        evidence.append(f"uptime reported: {body.get('uptime_s')}s")
        evidence.append(f"heartbeat field present: {'heartbeat' in body}")
        evidence.append(f"guardian field present: {'guardian' in body}")
        evidence.append(f"system field present: {'system' in body and 'python' in body.get('system', {})}")
        evidence.append(f"provider extras merged: {body.get('plugins')}")
        if not (unauth == 401 and body.get("ok") is True
                and "plugins" in body and body["plugins"]["enabled"] == 3):
            fail("health API", evidence)
        ok("health API", evidence)
    finally:
        uvicorn_server.should_exit = True
        thread.join(timeout=5)


# ── 5. guardian: crash restart ───────────────────────────────────────────────

def test_guardian_crash_restart():
    clean_artifacts()
    g = start_guardian(["--no-port-check", "--poll", "0.3", "--max-crashes", "3", "--crash-window", "120"],
                       [PY, str(TESTS / "stub_crash.py")])
    deadline = time.time() + 45
    while time.time() < deadline and read_log().count("Started JARVIS") < 2:
        time.sleep(0.5)
    stop_process(g)
    kill_stub()

    text = read_log()
    state = json.loads((LOGS / "guardian_state.json").read_text(encoding="utf-8"))
    reports = list(LOGS.glob("crash_*.log"))
    evidence = []
    evidence.append(f"crash detected: {'exited unexpectedly (code 1)' in text}")
    evidence.append(f"restarted after crash: {text.count('Started JARVIS') >= 2} (launches: {text.count('Started JARVIS')})")
    evidence.append(f"crash report saved: {[r.name for r in reports]}")
    evidence.append(f"state crash_count={state.get('crash_count')}, restart_count={state.get('restart_count')}")
    if not ("exited unexpectedly (code 1)" in text and text.count("Started JARVIS") >= 2
            and reports and state.get("crash_count", 0) >= 1):
        fail("guardian crash restart", evidence)
    ok("guardian crash restart", evidence)


# ── 6. guardian: clean exit ──────────────────────────────────────────────────

def test_guardian_clean_exit():
    clean_artifacts()
    g = start_guardian(["--no-port-check", "--poll", "0.3"], [PY, str(TESTS / "stub_clean.py")])
    rc = g.wait(timeout=30)
    kill_stub()
    text = read_log()
    evidence = []
    evidence.append(f"guardian exit code: {rc} (expected 0)")
    evidence.append(f"recognised clean exit: {'exited cleanly (code 0)' in text}")
    evidence.append(f"no restart: {text.count('Started JARVIS') == 1}")
    evidence.append(f"no crash report: {not list(LOGS.glob('crash_*.log'))}")
    if not (rc == 0 and "exited cleanly (code 0)" in text
            and text.count("Started JARVIS") == 1):
        fail("guardian clean exit", evidence)
    ok("guardian clean exit", evidence)


# ── 7. guardian: marker exit ─────────────────────────────────────────────────

def test_guardian_marker_exit():
    clean_artifacts()
    g = start_guardian(["--no-port-check", "--poll", "0.3"], [PY, str(TESTS / "stub_marker.py")])
    rc = g.wait(timeout=30)
    kill_stub()
    text = read_log()
    evidence = []
    evidence.append(f"guardian exit code: {rc} (expected 0)")
    evidence.append(f"marker + code 1 treated as intentional: {'exited cleanly (code 1)' in text}")
    evidence.append(f"no restart: {text.count('Started JARVIS') == 1}")
    evidence.append(f"no crash report: {not list(LOGS.glob('crash_*.log'))}")
    if not (rc == 0 and "exited cleanly (code 1)" in text
            and text.count("Started JARVIS") == 1):
        fail("guardian marker exit", evidence)
    ok("guardian marker exit", evidence)


# ── 8. guardian: double-launch refusal ───────────────────────────────────────

def test_guardian_double_launch():
    clean_artifacts()
    # a live JARVIS elsewhere: fresh heartbeat from this (alive) test process
    (LOGS / "heartbeat.json").write_text(json.dumps({
        "v": 1, "pid": os.getpid(), "ts": time.time(),
        "started_at": time.time(), "state": "healthy", "main_age_s": 0.1}))
    g = start_guardian(["--no-port-check", "--poll", "0.3"], [PY, str(TESTS / "stub_clean.py")])
    rc = g.wait(timeout=30)
    out = g.stdout.read() if g.stdout else ""
    text = read_log()
    evidence = []
    evidence.append(f"guardian exit code: {rc} (expected 2)")
    evidence.append(f"refusal message printed: {'already running' in out}")
    evidence.append(f"child never launched: {'Started JARVIS' not in text}")
    if not (rc == 2 and "already running" in out and "Started JARVIS" not in text):
        fail("guardian double-launch", evidence)
    ok("guardian double-launch", evidence)


# ── 9. guardian: stall kill ──────────────────────────────────────────────────

def test_guardian_stall_kill():
    clean_artifacts()
    g = start_guardian(["--no-port-check", "--poll", "0.2", "--hb-stale", "3", "--hb-gap", "0.5",
                        "--hb-checks", "2", "--max-crashes", "3", "--crash-window", "120"],
                       [PY, str(TESTS / "stub_stall.py")])
    deadline = time.time() + 30
    while time.time() < deadline and "Treating the stall" not in read_log():
        time.sleep(0.3)
    stop_process(g)
    kill_stub()

    text = read_log()
    state = json.loads((LOGS / "guardian_state.json").read_text(encoding="utf-8"))
    reports = list(LOGS.glob("crash_*.log"))
    report_text = reports[0].read_text(encoding="utf-8", errors="replace") if reports else ""
    evidence = []
    evidence.append(f"stale heartbeat detected: {'Heartbeat stale' in text}")
    evidence.append(f"frozen child killed: {'terminating pid' in text}")
    evidence.append(f"treated as crash: {'Treating the stall' in text}")
    evidence.append(f"stall crash report (exit -999): {'exit code -999' in report_text}")
    evidence.append(f"state last_crash exit_code={state.get('last_crash', {}).get('exit_code')}")
    if not ("Heartbeat stale" in text and "terminating pid" in text
            and "Treating the stall" in text and "exit code -999" in report_text):
        fail("guardian stall kill", evidence)
    ok("guardian stall kill", evidence)


# ── 10. guardian: port guard ─────────────────────────────────────────────────

def test_guardian_port_guard():
    clean_artifacts()
    import socket
    # Hold port 8000 in THIS process (or fail if an older live JARVIS already
    # holds it — either way the guardian must refuse to launch).
    holder = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held_by_us = True
    try:
        holder.bind(("0.0.0.0", 8000))
        holder.listen(1)
    except OSError:
        held_by_us = False

    g = start_guardian(["--poll", "0.3"], [PY, str(TESTS / "stub_clean.py")])
    rc = g.wait(timeout=30)
    out = g.stdout.read() if g.stdout else ""
    text = read_log()
    if held_by_us:
        holder.close()

    evidence = []
    evidence.append(f"port 8000 held by {'test socket' if held_by_us else 'a live process'} "
                    f"— guardian must refuse")
    evidence.append(f"guardian exit code: {rc} (expected 2)")
    evidence.append(f"refusal explains the port: {'already in use' in out}")
    evidence.append(f"child never launched: {'Started JARVIS' not in text}")
    if not (rc == 2 and "already in use" in out and "Started JARVIS" not in text):
        fail("guardian port guard", evidence)
    ok("guardian port guard", evidence)


# ── run ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Self-heal verification suite — python {sys.version.split()[0]}")
    print(f"project: {ROOT}")
    test_compile()
    test_logging()
    test_heartbeat()
    test_health_api()
    test_guardian_crash_restart()
    test_guardian_clean_exit()
    test_guardian_marker_exit()
    test_guardian_double_launch()
    test_guardian_stall_kill()
    test_guardian_port_guard()
    print(f"\n=== ALL {PASSED}/10 TESTS PASSED ===")


if __name__ == "__main__":
    main()
