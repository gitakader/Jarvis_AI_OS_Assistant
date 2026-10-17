"""
selfheal_tests/test_aggregate_ocr_skill.py — offline verification of
features/aggregate_ocr.py (the Forge skill that OCRs a whole directory).

Phase-mission style: evidence blocks, STOP on first failure.
One REAL Gemini OCR call on a single copied image = positive control.

    venv\\Scripts\\python.exe -u selfheal_tests\\test_aggregate_ocr_skill.py
"""
import importlib.util
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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


def load_skill():
    spec = importlib.util.spec_from_file_location(
        "jarvis_skill_aggregate_ocr_test", ROOT / "features" / "aggregate_ocr.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


SRC_DIR = Path(r"C:\Users\Mukul\Downloads\Mahira_Class-Nursery")


def test_import_and_meta():
    mod = load_skill()
    evidence = []
    evidence.append(f"module loaded; execute callable: {callable(getattr(mod, 'execute', None))}")
    meta = getattr(mod, "FEATURE_METADATA", {})
    evidence.append(f"FEATURE_METADATA name: {meta.get('name')}")
    evidence.append(f"required params: {meta.get('parameters', {}).get('required')}")
    if not callable(mod.execute) or meta.get("name") != "aggregate_ocr":
        fail("import + metadata", evidence)
    ok("import + metadata", evidence)


def test_error_paths():
    mod = load_skill()
    evidence = []
    r1 = mod.execute(directory_name="")
    evidence.append(f"missing directory_name -> helpful: {'I need the directory' in r1} ({r1[:60]})")
    r2 = mod.execute(directory_name="Z:/definitely/not/here")
    evidence.append(f"missing dir -> helpful: {'Directory not found' in r2} ({r2[:60]})")
    if not ("I need the directory" in r1 and "Directory not found" in r2):
        fail("error paths", evidence)
    ok("error paths", evidence)


def test_ocr_single_image(tmp_dir):
    mod = load_skill()
    d = Path(tmp_dir)
    img = next(f for f in SRC_DIR.iterdir() if f.suffix.lower() == ".jpeg")
    shutil.copy2(img, d / "test_image.jpeg")
    evidence = []
    result = mod.execute(directory_name=str(d))
    out_file = d / f"{d.name}_ocr_aggregated.txt"
    evidence.append(f"result first 120 chars: {result[:120]!r}")
    evidence.append(f"output file exists: {out_file.exists()}")
    content = out_file.read_text(encoding="utf-8", errors="replace") if out_file.exists() else ""
    evidence.append(f"file starts with block header: {content.startswith('--- test_image.jpeg ---')}")
    body = content.split("\n", 1)[1] if "\n" in content else ""
    evidence.append(f"extracted body length: {len(body.strip())} chars")
    evidence.append(f"body not placeholder: {'[OCR text for' not in body}")
    evidence.append(f"body not error: {'FAILED:' not in body and 'analysis failed' not in body}")
    if not (out_file.exists() and content.startswith("--- test_image.jpeg ---")
            and len(body.strip()) > 0
            and "[OCR text for" not in body and "FAILED:" not in body):
        fail("real OCR on one image", evidence)
    ok("real OCR on one image", evidence)
    return str(out_file)


def test_resume(tmp_dir, prev_out):
    mod = load_skill()
    d = Path(tmp_dir)
    shutil.copy2(prev_out, d / f"{d.name}_ocr_aggregated.txt")
    img = next(f for f in SRC_DIR.iterdir() if f.suffix.lower() == ".jpeg")
    shutil.copy2(img, d / "second_image.jpeg")
    result = mod.execute(directory_name=str(d))
    evidence = []
    evidence.append(f"resume result first 100 chars: {result[:100]!r}")
    evidence.append(f"mentions 1 this run: {'1 this run' in result}")
    out_file = d / f"{d.name}_ocr_aggregated.txt"
    content = out_file.read_text(encoding="utf-8", errors="replace")
    evidence.append(f"first block preserved: {'--- test_image.jpeg ---' in content}")
    evidence.append(f"second block appended: {'--- second_image.jpeg ---' in content}")
    if not ("1 this run" in result and "--- test_image.jpeg ---" in content
            and "--- second_image.jpeg ---" in content):
        fail("resume skips done images", evidence)
    ok("resume skips done images", evidence)


def main():
    print(f"aggregate_ocr skill verification — python {sys.version.split()[0]}")
    print(f"source images: {SRC_DIR} (exists: {SRC_DIR.exists()})")
    if not SRC_DIR.exists():
        fail("source dir", ["Mahira_Class-Nursery missing — cannot run positive control"])
    test_import_and_meta()
    test_error_paths()
    with tempfile.TemporaryDirectory(prefix="ocr_skill_") as t1:
        prev_out = test_ocr_single_image(t1)
        with tempfile.TemporaryDirectory(prefix="ocr_skill_") as t2:
            test_resume(t2, prev_out)
    print(f"\n=== ALL {PASSED}/4 TESTS PASSED ===")


if __name__ == "__main__":
    main()
