"""Stub child: starts, reports its pid, exits cleanly with code 0 after 1s."""
import os
import sys
import time
from pathlib import Path

(Path(__file__).resolve().parent / "stub.pid").write_text(str(os.getpid()))
print("stub_clean: up", flush=True)
time.sleep(1)
sys.exit(0)
