"""
selfheal_tests/test_camera_hub.py — offline verification of the camera
frame hub (the 0xC0000374 single-owner webcam fix).

No camera, no UI: the hub is pure Python. Each test prints an evidence
block; the suite STOPS at the first failure. Run with the venv python
from the project root:

    venv\\Scripts\\python.exe -u selfheal_tests\\test_camera_hub.py

Tests:
  1  freshness      — capture returns only a frame NEWER than the call
  2  stale frame    — a frame published before the call is not returned
  3  invalidation   — camera_stream_stopped() empties the hub
  4  restart cycle  — publish after a stop serves a new capture
  5  timeout        — no stream → clear RuntimeError after _CAM_TIMEOUT
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from actions import screen_processor as sp

passed, failed = 0, 0


def check(name, cond, detail=""):
    global passed, failed
    tag = "PASS" if cond else "FAIL"
    if cond:
        passed += 1
    else:
        failed += 1
    print(f"[{tag}] {name}{(' — ' + str(detail)) if detail else ''}")
    return cond


def _publish_after(delay: float, *frames):
    def run():
        for f in frames:
            time.sleep(delay)
            sp.publish_camera_frame(*f)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def main() -> None:
    sp._cam_event.clear()
    sp._cam_lock.acquire()
    sp._cam_frame = None
    sp._cam_seq = 0
    sp._cam_lock.release()

    # 1. freshness — capture() must return a frame NEWER than the call
    sp.publish_camera_frame(b"old", "image/jpeg")
    pub = _publish_after(0.2, (b"new", "image/jpeg"))
    got = sp._capture_camera()
    pub.join(5)
    check("freshness: returns post-call frame",
          got == (b"new", "image/jpeg"), got)
    if not check("freshness: old frame not returned", got[0] != b"old"):
        sys.exit(1)

    # 2. stale frame — published before the call must NOT satisfy it
    pub = _publish_after(0.2, (b"after", "image/jpeg"))
    got = sp._capture_camera()
    pub.join(5)
    check("stale frame skipped", got == (b"after", "image/jpeg"), got)

    # 3. invalidation — a stream stop empties the hub
    sp.publish_camera_frame(b"live", "image/jpeg")
    sp.camera_stream_stopped()
    old_timeout, sp._CAM_TIMEOUT = sp._CAM_TIMEOUT, 0.5
    try:
        t0 = time.monotonic()
        try:
            sp._capture_camera()
            check("invalidation: capture after stop", False,
                  "expected RuntimeError")
            sys.exit(1)
        except RuntimeError as e:
            check("invalidation: capture after stop raises",
                  "no frame" in str(e), e)
        check("invalidation: waited, did not hang",
              time.monotonic() - t0 < 2.0, f"{time.monotonic() - t0:.1f}s")

        # 4. restart cycle — publish after the stop serves the next capture
        pub = _publish_after(0.2, (b"restarted", "image/jpeg"))
        got = sp._capture_camera()
        pub.join(5)
        check("restart cycle: new stream frame served",
              got == (b"restarted", "image/jpeg"), got)
    finally:
        sp._CAM_TIMEOUT = old_timeout

    # 5. timeout — nothing publishing, hub empty
    sp.camera_stream_stopped()
    old_timeout, sp._CAM_TIMEOUT = sp._CAM_TIMEOUT, 0.4
    try:
        t0 = time.monotonic()
        try:
            sp._capture_camera()
            check("timeout: capture with no stream", False,
                  "expected RuntimeError")
            sys.exit(1)
        except RuntimeError as e:
            check("timeout: clear error, not a hang",
                  "no frame" in str(e) and "8s" not in str(e)
                  and "0s" in str(e), e)
        check("timeout: raised near _CAM_TIMEOUT",
              0.2 < time.monotonic() - t0 < 2.0, f"{time.monotonic() - t0:.1f}s")
    finally:
        sp._CAM_TIMEOUT = old_timeout

    print(f"\nRESULT: {passed} passed, {failed} failed")
    sys.exit(0 if failed == 0 else 1)


main()
