"""
Screen & webcam capture for JARVIS vision.

Screen capture is on-demand (`_capture_screen`). The webcam has exactly ONE
owner: the HUD stream thread (ui.py `_cam_loop`) publishes every frame to a
thread-safe hub here, and `_capture_camera()` waits for the next fresh frame.
No other code may open the camera — a second cv2.VideoCapture on the same
device, from another thread, corrupted the heap and killed the process with
0xC0000374 (crash_20261014_003446.log).
"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

import numpy as np

try:
    import cv2
    _CV2 = True
except ImportError:
    _CV2 = False

try:
    import mss
    import mss.tools
    _MSS = True
except ImportError:
    _MSS = False

try:
    import PIL.Image
    _PIL = True
except ImportError:
    _PIL = False


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


_BASE        = _base_dir()
_CONFIG_PATH = _BASE / "config" / "api_keys.json"


def _load_config() -> dict:
    try:
        return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config_key(key: str, value) -> None:
    try:
        cfg = _load_config()
        cfg[key] = value
        _CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
    except Exception as e:
        print(f"[Vision] ⚠️  Could not save config key '{key}': {e}")


def _get_os() -> str:
    return _load_config().get("os_system", "windows").lower()


_IMG_MAX_W = 1280
_IMG_MAX_H = 720
_JPEG_Q    = 82


def _compress(img_bytes: bytes, source_format: str = "PNG") -> tuple[bytes, str]:
    if not _PIL:
        return img_bytes, f"image/{source_format.lower()}"

    try:
        img = PIL.Image.open(io.BytesIO(img_bytes)).convert("RGB")
        img.thumbnail((_IMG_MAX_W, _IMG_MAX_H), PIL.Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=_JPEG_Q, optimize=False)
        return buf.getvalue(), "image/jpeg"
    except Exception as e:
        print(f"[Vision] ⚠️  Image compress failed: {e}")
        return img_bytes, f"image/{source_format.lower()}"


def _capture_screen() -> tuple[bytes, str]:

    if not _MSS:
        raise RuntimeError("mss is not installed. Run: pip install mss")

    with mss.mss() as sct:
        monitors = sct.monitors          # [0] = all combined, [1..n] = real screens
        target   = monitors[1] if len(monitors) > 1 else monitors[0]
        shot     = sct.grab(target)
        png      = mss.tools.to_png(shot.rgb, shot.size)

    return _compress(png, "PNG")


def _cv2_backend() -> int:
    """Return the best OpenCV camera backend for the current OS."""
    if not _CV2:
        return 0
    os_name = _get_os()
    if os_name == "windows":
        return cv2.CAP_DSHOW
    if os_name == "mac":
        return cv2.CAP_AVFOUNDATION
    return cv2.CAP_ANY


def _probe_camera(index: int, backend: int, warmup: int = 5) -> bool:

    if not _CV2:
        return False
    cap = cv2.VideoCapture(index, backend)
    if not cap.isOpened():
        cap.release()
        return False
    for _ in range(warmup):
        cap.read()
    ret, frame = cap.read()
    cap.release()
    if not ret or frame is None:
        return False
    return bool(np.mean(frame) > 8)


def _detect_camera_index() -> int:

    backend = _cv2_backend()
    print("[Vision] 🔍 Auto-detecting camera...")
    for idx in range(6):
        if _probe_camera(idx, backend):
            print(f"[Vision] ✅ Camera found at index {idx}")
            _save_config_key("camera_index", idx)
            return idx
        print(f"[Vision] ⚠️  Camera index {idx}: no usable frame")

    print("[Vision] ⚠️  No camera found — defaulting to index 0")
    _save_config_key("camera_index", 0)
    return 0


def get_camera_index() -> int:
    cfg = _load_config()
    if "camera_index" in cfg:
        return int(cfg["camera_index"])
    return _detect_camera_index()


# ── Live-frame hub ─────────────────────────────────────────────────────────
# The webcam has exactly ONE owner: the HUD stream thread (ui.py `_cam_loop`).
# It publishes every frame here; _capture_camera waits for the next fresh
# frame instead of opening a second VideoCapture on the same device.

_CAM_TIMEOUT = 8.0            # seconds to wait for a fresh stream frame

_cam_lock  = threading.Lock()
_cam_frame = None             # (jpeg_bytes, mime) or None
_cam_seq   = 0                # bumped on every publish and on stream stop
_cam_event = threading.Event()


def publish_camera_frame(jpeg: bytes, mime: str) -> None:
    """Called by the HUD stream thread for every captured frame."""
    global _cam_frame, _cam_seq
    with _cam_lock:
        _cam_frame = (jpeg, mime)
        _cam_seq += 1
    _cam_event.set()


def camera_stream_stopped() -> None:
    """Called by the HUD stream thread when its capture loop exits."""
    global _cam_frame, _cam_seq
    with _cam_lock:
        _cam_frame = None
        _cam_seq += 1
    _cam_event.set()           # wake any waiter so it sees the empty frame


def _capture_camera() -> tuple[bytes, str]:
    """Return the next fresh frame from the HUD camera stream.

    Blocks until the stream publishes a frame newer than this call — with a
    running stream that is tens of milliseconds; on a cold start it includes
    the camera's open and warm-up time. Never opens the camera itself.
    """
    if not _CV2:
        raise RuntimeError("OpenCV (cv2) is not installed. Run: pip install opencv-python")

    with _cam_lock:
        start = _cam_seq
    deadline = time.monotonic() + _CAM_TIMEOUT
    while True:
        with _cam_lock:
            if _cam_frame is not None and _cam_seq > start:
                return _cam_frame
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "Camera stream produced no frame within "
                f"{_CAM_TIMEOUT:.0f}s — is the camera in use by another "
                "app or unplugged?")
        _cam_event.wait(remaining)
        _cam_event.clear()
