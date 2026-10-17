"""Stub child for the boot-sentry integration test.

Plants a fresh, harmless file on a hot-loadable surface (actions/) so the
sentry has a candidate, reports its pid, then exits 1 after 1s — a boot
crash by definition (uptime < 60s). It re-plants the file on every launch,
which proves the recovery cooldown holds across a multi-crash loop.
"""
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
(Path(__file__).resolve().parent / "stub.pid").write_text(str(os.getpid()))

plant = ROOT / "actions" / "_sentry_test_stub.py"
plant.write_text(
    'TOOL = {\n'
    '    "name": "_sentry_test_stub",\n'
    '    "description": "boot-sentry test artifact - safe to delete",\n'
    '    "parameters": {"type": "OBJECT", "properties": {}},\n'
    '    "handler": lambda params, **kw: "ok",\n'
    '}\n'
)
print("stub_bootcrash: up", flush=True)
time.sleep(1)
sys.exit(1)
