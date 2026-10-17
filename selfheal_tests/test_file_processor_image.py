"""Offline checks for the image path in file_processor and core.gemini.

Root cause being guarded: _process_image passed a bare PIL Image into
gemini.call; core/gemini._to_live_parts silently dropped it, so the live
model answered text-only prompts from imagination (2026-10-15: NID card
OCR came back as a motivational quote). The contract now: images travel
as inline_data Parts / live parts, never as bare PIL objects."""
import base64
import io
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import actions.file_processor as FP
import core.gemini as G
from PIL import Image

PASS = 0
FAIL = 0

def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")

class _FakeModel:
    def __init__(self, text="462 259 7393"):
        self.calls = []
        self.text = text

    def generate_content(self, contents):
        self.calls.append(contents)
        return _FakeReply(self.text)

class _FakeReply:
    def __init__(self, text):
        self.text = text

def _png_bytes() -> bytes:
    img = Image.new("RGB", (8, 8), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

def main():
    tmp = Path(tempfile.mkdtemp(prefix="fp_image_"))
    png_path = tmp / "nid.png"
    png_path.write_bytes(_png_bytes())

    # ── _process_image must send a Part with PNG bytes, not a PIL image ─────
    fake = _FakeModel()
    orig_client = FP._gemini_client
    FP._gemini_client = lambda: fake
    try:
        out = FP.file_processor({"file_path": str(png_path), "action": "ocr"})
    finally:
        FP._gemini_client = orig_client
    check("image: one call", len(fake.calls) == 1, str(len(fake.calls)))
    contents = fake.calls[0]
    check("image: two items", len(contents) == 2, repr([type(c).__name__ for c in contents]))
    part = contents[0]
    check("image: not a PIL image", not isinstance(part, Image.Image),
          type(part).__name__)
    blob = getattr(part, "inline_data", None)
    check("image: has inline_data", blob is not None, repr(part)[:120])
    if blob is not None:
        data = getattr(blob, "data", None)
        check("image: png bytes", isinstance(data, bytes) and data[:4] == b"\x89PNG",
              repr(data[:16]) if data else "None")
        check("image: mime image/png", getattr(blob, "mime_type", "") == "image/png",
              str(getattr(blob, "mime_type", None)))
    check("image: prompt is text", isinstance(contents[1], str) and
          "text" in contents[1].lower(), repr(contents[1])[:80])
    check("image: result returned", out == "462 259 7393", repr(out)[:80])

    # ── instruction override still works alongside the Part ─────────────────
    fake2 = _FakeModel()
    FP._gemini_client = lambda: fake2
    try:
        FP.file_processor({"file_path": str(png_path), "action": "read",
                           "instruction": "read the NID number only"})
    finally:
        FP._gemini_client = orig_client
    contents2 = fake2.calls[0]
    check("image: instruction replaces prompt", contents2[1] == "read the NID number only",
          repr(contents2[1])[:80])
    check("image: part still first with instruction",
          getattr(contents2[0], "inline_data", None) is not None)

    # ── long OCR stays inline up to _EXTRACT_INLINE, then saves ─────────────
    fake3 = _FakeModel(text="NID No. 462 259 7393 " + ("filler " * 400))
    FP._gemini_client = lambda: fake3
    try:
        out_long = FP.file_processor({"file_path": str(png_path), "action": "ocr"})
    finally:
        FP._gemini_client = orig_client
    check("image: long ocr inline", "NID No. 462 259 7393" in out_long,
          out_long[:120])
    saved_long = tmp / "nid_result.txt"
    check("image: long ocr not saved below cap", not saved_long.exists())

    huge = "x" * 9000
    fake4 = _FakeModel(text=huge)
    FP._gemini_client = lambda: fake4
    old_cap = FP._EXTRACT_INLINE
    FP._EXTRACT_INLINE = 100
    try:
        out_huge = FP.file_processor({"file_path": str(png_path), "action": "ocr"})
    finally:
        FP._gemini_client = orig_client
        FP._EXTRACT_INLINE = old_cap
    check("image: huge ocr truncates", "...[truncated]" in out_huge, out_huge[:150])
    check("image: huge ocr saves", (tmp / "nid_result.txt").exists())

    # ── _to_live_parts converts PIL -> inline_data, keeps str/Part ──────────
    img = Image.new("RGB", (4, 4), (0, 255, 0))
    parts = G._to_live_parts([img, "read this"])
    check("live: two parts", len(parts) == 2, repr(parts)[:160])
    first = parts[0]
    ok_shape = (isinstance(first, dict) and
                first.get("inline_data", {}).get("mime_type") == "image/png")
    check("live: pil -> inline_data dict", ok_shape, repr(first)[:160])
    if ok_shape:
        raw = base64.b64decode(first["inline_data"]["data"])
        check("live: decodes to png", raw[:4] == b"\x89PNG", repr(raw[:8]))
    check("live: str stays text", parts[1] == {"text": "read this"}, repr(parts[1]))

    from google.genai import types as gtypes
    spart = gtypes.Part.from_bytes(data=_png_bytes(), mime_type="image/png")
    sparts = G._to_live_parts([spart])
    check("live: Part inline_data passthrough",
          len(sparts) == 1 and sparts[0]["inline_data"]["mime_type"] == "image/png",
          repr(sparts)[:160])

    print(f"\n{PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0

if __name__ == "__main__":
    sys.exit(main())
