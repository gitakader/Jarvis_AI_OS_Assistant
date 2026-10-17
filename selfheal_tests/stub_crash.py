"""Stub child: starts, reports its pid, exits with code 1 after 2s (a crash)."""
import os
import sys
import time
from pathlib import Path

(Path(__file__).resolve().parent / "stub.pid").write_text(str(os.getpid()))
print("stub_crash: up", flush=True)
time.sleep(2)
sys.exit(1)
