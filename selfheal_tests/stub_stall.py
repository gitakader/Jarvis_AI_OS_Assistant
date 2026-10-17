"""Stub child: simulates a frozen JARVIS — heartbeat forever stale, never dies."""
import json
import os
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "logs"
LOGS.mkdir(parents=True, exist_ok=True)
(Path(__file__).resolve().parent / "stub.pid").write_text(str(os.getpid()))
print("stub_stall: up (frozen)", flush=True)
while True:
    hb = {"v": 1, "pid": os.getpid(), "ts": time.time() - 1000,
          "started_at": time.time(), "state": "healthy", "main_age_s": 1000.0}
    (LOGS / "heartbeat.json").write_text(json.dumps(hb))
    time.sleep(20)
