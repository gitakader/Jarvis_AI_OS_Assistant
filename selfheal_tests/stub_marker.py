"""Stub child: writes an intentional-shutdown marker, exits with code 1."""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(ROOT / "logs").mkdir(parents=True, exist_ok=True)
(ROOT / "logs" / "intentional_shutdown.marker").write_text(f"{time.time()}\n")
(Path(__file__).resolve().parent / "stub.pid").write_text(str(os.getpid()))
print("stub_marker: up", flush=True)
time.sleep(0.5)
sys.exit(1)
