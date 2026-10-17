"""
selfheal_tests/test_office_create.py — offline verification of actions/office_create.py.

Phase-mission style: each check prints an evidence block; STOPS at the first
failure. Runs with temp directories — no live JARVIS needed.

    venv\\Scripts\\python.exe -u selfheal_tests\\test_office_create.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from actions.office_create import (
    _normalise_data, _build_xlsx, _build_docx, _build_pptx, _write_cell,
    _HEADER_FILL, _HEADER_FONT,
    office_create,
)

try:
    import openpyxl
    _HAS_XLSX = True
except ImportError:
    _HAS_XLSX = False

try:
    from docx import Document
    _HAS_DOCX = True
except ImportError:
    _HAS_DOCX = False

try:
    from pptx import Presentation as PptxPresentation
    _HAS_PPTX = True
except ImportError:
    _HAS_PPTX = False

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


def tmp():
    return Path(tempfile.mkdtemp(prefix="office_"))


# ── 1. compile + imports ─────────────────────────────────────────────────────

def test_compile():
    import py_compile
    py_compile.compile(str(ROOT / "actions" / "office_create.py"), doraise=True)
    ok("compile + imports", [
        f"office_create.py compiles",
        f"openpyxl available: {_HAS_XLSX}",
        f"python-docx available: {_HAS_DOCX}",
        f"python-pptx available: {_HAS_PPTX}",
    ])


# ── 2. data normalisation ────────────────────────────────────────────────────

def test_normalise():
    evidence = []
    h1, r1 = _normalise_data({"headers": ["Name", "Score"], "rows": [["A", 10], ["B", 20]]})
    evidence.append(f"headers+rows: headers={h1} rows_count={len(r1)} first={r1[0]}")
    h2, r2 = _normalise_data([{"Name": "A", "Score": 10}, {"Name": "B", "Score": 20}])
    evidence.append(f"array-of-objects: headers={h2} rows_count={len(r2)} first={r2[0]}")
    for bad in (None, [], {}, "string"):
        try:
            _normalise_data(bad)
            raised = False
        except (ValueError, TypeError):
            raised = True
        evidence.append(f"bad input {type(bad).__name__} raises: {raised}")
    if not (h1 == ["Name", "Score"] and r1[0] == ["A", 10]
            and h2 == ["Name", "Score"] and r2[0] == ["A", 10]
            and all("raises: True" in e for e in evidence[-4:])):
        fail("data normalisation", evidence)
    ok("data normalisation", evidence)


# ── 3. create workbook (headers + rows) ──────────────────────────────────────

def test_build_xlsx_headers_rows():
    d = tmp()
    p = d / "report.xlsx"
    headers = ["Product", "Qty", "Price", "Date"]
    rows = [
        ["Widget", 5, 12.50, "2026-01-15"],
        ["Gadget", 3, 8.25, "2026-02-20"],
        ["Doohickey", 7, 3.00, "2026-03-10"],
    ]
    result = _build_xlsx(headers, rows, p)
    wb = openpyxl.load_workbook(str(p))
    ws = wb.active
    evidence = []
    evidence.append(f"file created: {p.exists()} ({p.stat().st_size} bytes)")
    evidence.append(f"confirmation: {result}")
    evidence.append(f"sheet title: {ws.title}")
    evidence.append(f"row count: {ws.max_row} (expected 4 = header + 3)")
    evidence.append(f"col count: {ws.max_column} (expected 4)")
    evidence.append(f"cell A1 (header): {ws['A1'].value} bold={ws['A1'].font.bold}")
    evidence.append(f"cell B2 (data): {ws['B2'].value} type={type(ws['B2'].value).__name__}")
    evidence.append(f"cell C3: {ws['C3'].value}")
    evidence.append(f"cell D4: {ws['D4'].value}")
    evidence.append(f"freeze panes: {ws.freeze_panes}")
    evidence.append(f"auto_filter: {ws.auto_filter.ref}")
    evidence.append(f"header fill: {ws['A1'].fill.start_color.rgb}")
    if not (p.exists() and ws.max_row == 4 and ws.max_column == 4
            and ws["A1"].value == "Product" and ws["A1"].font.bold
            and ws["B2"].value == 5 and ws["C3"].value == 8.25
            and ws.freeze_panes == "A2"):
        fail("build xlsx (headers+rows)", evidence)
    ok("build xlsx (headers+rows)", evidence)


# ── 4. create workbook (array of objects) ────────────────────────────────────

def test_build_xlsx_objects():
    d = tmp()
    p = d / "people.xlsx"
    data = [
        {"Name": "Alice", "Dept": "Engineering", "Salary": 75000},
        {"Name": "Bob", "Dept": "Design", "Salary": 65000},
    ]
    headers = list(data[0].keys())
    rows = [[row[h] for h in headers] for row in data]
    result = _build_xlsx(headers, rows, p)
    wb = openpyxl.load_workbook(str(p))
    ws = wb.active
    evidence = []
    evidence.append(f"file created: {p.exists()}")
    evidence.append(f"Name col: {ws['A1'].value} | {ws['A2'].value} | {ws['A3'].value}")
    evidence.append(f"Salary col: {ws['C2'].value} type={type(ws['C2'].value).__name__}")
    if not (p.exists() and ws["A2"].value == "Alice" and ws["A3"].value == "Bob"):
        fail("build xlsx (objects)", evidence)
    ok("build xlsx (objects)", evidence)


# ── 5. handler: missing parameters ───────────────────────────────────────────

def test_handler_errors():
    evidence = []
    r1 = office_create({"type": "pdf", "path": "desktop/x.pdf", "data": {"headers": ["A"], "rows": [[1]]}})
    evidence.append(f"unsupported type -> helpful: {'not supported yet' in r1} ({r1[:60]})")
    r2 = office_create({"type": "xlsx"}, player=None)
    evidence.append(f"missing path -> helpful: {'I need a path' in r2} ({r2[:50]})")
    r3 = office_create({"type": "docx", "path": "desktop/x.docx"}, player=None)
    evidence.append(f"missing data -> helpful: {'I need the data' in r3} ({r3[:50]})")
    r4 = office_create({"type": "xlsx", "path": "desktop/x.xlsx", "data": {"headers": [], "rows": []}}, player=None)
    evidence.append(f"empty headers -> error: {'at least one column' in r4} ({r4[:50]})")
    if not ("not supported yet" in r1 and "I need a path" in r2
            and "I need the data" in r3
            and "at least one column" in r4):
        fail("handler errors", evidence)
    ok("handler errors", evidence)


# ── 6. auto extension (xlsx) ─────────────────────────────────────────────────

def test_auto_extension_xlsx():
    d = tmp()
    result = office_create({
        "type": "xlsx",
        "path": str(d / "noext"),
        "data": {"headers": ["X"], "rows": [[1]]},
    })
    evidence = []
    evidence.append(f"result: {result}")
    evidence.append(f".xlsx auto-appended: {(d / 'noext.xlsx').exists()}")
    if not (d / "noext.xlsx").exists():
        fail("auto .xlsx extension", evidence)
    ok("auto .xlsx extension", evidence)


# ── 7. auto extension (docx) ─────────────────────────────────────────────────

def test_auto_extension_docx():
    d = tmp()
    result = office_create({
        "type": "docx",
        "path": str(d / "noext"),
        "data": {"headers": ["X"], "rows": [[1]]},
    })
    evidence = []
    evidence.append(f"result: {result}")
    evidence.append(f".docx auto-appended: {(d / 'noext.docx').exists()}")
    if not (d / "noext.docx").exists():
        fail("auto .docx extension", evidence)
    ok("auto .docx extension", evidence)


# ── 8. handler e2e (xlsx) ────────────────────────────────────────────────────

def test_handler_e2e_xlsx():
    d = tmp()
    p = d / "e2e_test.xlsx"
    result = office_create({
        "type": "xlsx",
        "path": str(p),
        "data": [{"Name": "Test", "Value": 42}],
    })
    evidence = []
    evidence.append(f"result: {result}")
    evidence.append(f"file exists: {p.exists()}")
    wb = openpyxl.load_workbook(str(p))
    ws = wb.active
    evidence.append(f"rows: {ws.max_row}, cols: {ws.max_column}")
    evidence.append(f"value: {ws['B2'].value} == 42")
    if not (p.exists() and ws.max_row == 2 and ws["B2"].value == 42):
        fail("handler e2e xlsx", evidence)
    ok("handler e2e xlsx", evidence)


# ── 9. numeric and date handling (xlsx) ──────────────────────────────────────

def test_numeric_date():
    d = tmp()
    p = d / "types.xlsx"
    _build_xlsx(
        ["Item", "Price", "Qty", "Total", "Date"],
        [
            ["Apple", 1.5, 10, 15.0, "2026-10-15"],
            ["Banana", 0.8, 20, 16.0, "2026-10-15"],
        ],
        p,
    )
    wb = openpyxl.load_workbook(str(p))
    ws = wb.active
    evidence = []
    evidence.append(f"Price (float): B2={ws['B2'].value} type={type(ws['B2'].value).__name__}")
    evidence.append(f"Qty (int): C2={ws['C2'].value} type={type(ws['C2'].value).__name__}")
    evidence.append(f"Total (float): D2={ws['D2'].value} type={type(ws['D2'].value).__name__}")
    evidence.append(f"Date: E2={ws['E2'].value}")
    evidence.append(f"Price alignment right: {ws['B2'].alignment.horizontal}")
    if not (isinstance(ws['B2'].value, (int, float))
            and isinstance(ws['C2'].value, (int, float))
            and ws["C2"].value == 10):
        fail("numeric & date handling", evidence)
    ok("numeric & date handling", evidence)


# ── 10. build docx (headers + rows) ──────────────────────────────────────────

def test_build_docx_headers_rows():
    d = tmp()
    p = d / "report.docx"
    headers = ["Item", "Category", "Amount", "Notes"]
    rows = [
        ["Office Chair", "Furniture", 450.00, "Ergonomic mesh back"],
        ["Standing Desk", "Furniture", 899.99, "Electric adjustable"],
        ["Monitor Arm", "Accessories", 129.50, "Dual mount"],
    ]
    result = _build_docx(headers, rows, p)
    doc = Document(str(p))
    evidence = []
    evidence.append(f"file created: {p.exists()} ({p.stat().st_size} bytes)")
    evidence.append(f"confirmation: {result}")
    tables = doc.tables
    evidence.append(f"table count: {len(tables)}")
    table = tables[0]
    evidence.append(f"rows: {len(table.rows)} (expected 4 = header + 3)")
    evidence.append(f"cols: {len(table.columns)} (expected 4)")
    # header row
    hcell = table.cell(0, 0)
    evidence.append(f"header A1 text: '{hcell.text}' bold={hcell.paragraphs[0].runs[0].font.bold}")
    # data
    evidence.append(f"cell B2: '{table.cell(1, 1).text}' (expected Furniture)")
    evidence.append(f"cell C3: '{table.cell(2, 2).text}' (expected 129.50)")
    evidence.append(f"cell D4: '{table.cell(3, 3).text}'")
    if not (p.exists() and len(table.rows) == 4 and len(table.columns) == 4
            and "Office Chair" in table.cell(1, 0).text
            and "899.99" in table.cell(2, 2).text
            and hcell.paragraphs[0].runs[0].font.bold):
        fail("build docx (headers+rows)", evidence)
    ok("build docx (headers+rows)", evidence)


# ── 11. build docx (array of objects) ────────────────────────────────────────

def test_build_docx_objects():
    d = tmp()
    p = d / "contacts.docx"
    data = [
        {"Name": "Mukul", "Role": "Engineer", "Location": "Dhaka"},
        {"Name": "Tamanna", "Role": "Designer", "Location": "Chittagong"},
    ]
    headers = list(data[0].keys())
    rows = [[row[h] for h in headers] for row in data]
    result = _build_docx(headers, rows, p)
    doc = Document(str(p))
    table = doc.tables[0]
    evidence = []
    evidence.append(f"file created: {p.exists()}")
    evidence.append(f"rows: {len(table.rows)}, cols: {len(table.columns)}")
    evidence.append(f"header: {[table.cell(0, c).text for c in range(3)]}")
    evidence.append(f"row 1: {[table.cell(1, c).text for c in range(3)]}")
    evidence.append(f"row 2: {[table.cell(2, c).text for c in range(3)]}")
    if not (p.exists() and table.cell(1, 0).text == "Mukul"
            and table.cell(2, 1).text == "Designer"):
        fail("build docx (objects)", evidence)
    ok("build docx (objects)", evidence)


# ── 12. handler e2e (docx) ───────────────────────────────────────────────────

def test_handler_e2e_docx():
    d = tmp()
    p = d / "e2e_test.docx"
    result = office_create({
        "type": "docx",
        "path": str(p),
        "data": [{"Name": "Test", "Value": 42}],
    })
    evidence = []
    evidence.append(f"result: {result}")
    evidence.append(f"file exists: {p.exists()}")
    doc = Document(str(p))
    table = doc.tables[0]
    evidence.append(f"rows: {len(table.rows)}, cols: {len(table.columns)}")
    evidence.append(f"A2: '{table.cell(1, 0).text}' B2: '{table.cell(1, 1).text}'")
    if not (p.exists() and len(table.rows) == 2
            and table.cell(1, 0).text == "Test"
            and table.cell(1, 1).text == "42"):
        fail("handler e2e docx", evidence)
    ok("handler e2e docx", evidence)


# ── 13. build pptx (headers + rows) ──────────────────────────────────────────

def test_build_pptx_headers_rows():
    d = tmp()
    p = d / "report.pptx"
    headers = ["Item", "Category", "Amount", "Notes"]
    rows = [
        ["Office Chair", "Furniture", 450.00, "Ergonomic mesh back"],
        ["Standing Desk", "Furniture", 899.99, "Electric adjustable"],
        ["Monitor Arm", "Accessories", 129.50, "Dual mount"],
    ]
    result = _build_pptx(headers, rows, p)
    prs = PptxPresentation(str(p))
    evidence = []
    evidence.append(f"file created: {p.exists()} ({p.stat().st_size} bytes)")
    evidence.append(f"confirmation: {result}")
    evidence.append(f"slide count: {len(prs.slides)}")
    slide = prs.slides[0]
    evidence.append(f"shapes on slide: {len(slide.shapes)}")
    table = slide.shapes[0].table
    evidence.append(f"rows: {len(table.rows)} (expected 4 = header + 3)")
    evidence.append(f"cols: {len(table.columns)} (expected 4)")
    # header row
    hcell = table.cell(0, 0)
    evidence.append(f"header A1 text: '{hcell.text}'")
    # Check bold via runs
    hrun = hcell.text_frame.paragraphs[0].runs[0]
    evidence.append(f"header A1 bold: {hrun.font.bold}")
    # data
    evidence.append(f"cell B2: '{table.cell(1, 1).text}' (expected 450.0)")
    evidence.append(f"cell C3: '{table.cell(2, 2).text}' (expected 129.5)")
    evidence.append(f"cell D4: '{table.cell(3, 3).text}'")
    if not (p.exists() and len(table.rows) == 4 and len(table.columns) == 4
            and "Office Chair" in table.cell(1, 0).text
            and "899.99" in table.cell(2, 2).text
            and hrun.font.bold):
        fail("build pptx (headers+rows)", evidence)
    ok("build pptx (headers+rows)", evidence)


# ── 14. build pptx (array of objects) ────────────────────────────────────────

def test_build_pptx_objects():
    d = tmp()
    p = d / "contacts.pptx"
    data = [
        {"Name": "Mukul", "Role": "Engineer", "Location": "Dhaka"},
        {"Name": "Tamanna", "Role": "Designer", "Location": "Chittagong"},
    ]
    headers = list(data[0].keys())
    rows = [[row[h] for h in headers] for row in data]
    result = _build_pptx(headers, rows, p)
    prs = PptxPresentation(str(p))
    table = prs.slides[0].shapes[0].table
    evidence = []
    evidence.append(f"file created: {p.exists()}")
    evidence.append(f"rows: {len(table.rows)}, cols: {len(table.columns)}")
    evidence.append(f"header: {[table.cell(0, c).text for c in range(3)]}")
    evidence.append(f"row 1: {[table.cell(1, c).text for c in range(3)]}")
    evidence.append(f"row 2: {[table.cell(2, c).text for c in range(3)]}")
    if not (p.exists() and table.cell(1, 0).text == "Mukul"
            and table.cell(2, 1).text == "Designer"):
        fail("build pptx (objects)", evidence)
    ok("build pptx (objects)", evidence)


# ── 15. handler e2e (pptx) ───────────────────────────────────────────────────

def test_handler_e2e_pptx():
    d = tmp()
    p = d / "e2e_test.pptx"
    result = office_create({
        "type": "pptx",
        "path": str(p),
        "data": [{"Name": "Test", "Value": 42}],
    })
    evidence = []
    evidence.append(f"result: {result}")
    evidence.append(f"file exists: {p.exists()}")
    prs = PptxPresentation(str(p))
    table = prs.slides[0].shapes[0].table
    evidence.append(f"rows: {len(table.rows)}, cols: {len(table.columns)}")
    evidence.append(f"A2: '{table.cell(1, 0).text}' B2: '{table.cell(1, 1).text}'")
    if not (p.exists() and len(table.rows) == 2
            and table.cell(1, 0).text == "Test"
            and table.cell(1, 1).text == "42"):
        fail("handler e2e pptx", evidence)
    ok("handler e2e pptx", evidence)


# ── 16. auto extension (pptx) ─────────────────────────────────────────────────

def test_auto_extension_pptx():
    d = tmp()
    result = office_create({
        "type": "pptx",
        "path": str(d / "noext"),
        "data": {"headers": ["X"], "rows": [[1]]},
    })
    evidence = []
    evidence.append(f"result: {result}")
    evidence.append(f".pptx auto-appended: {(d / 'noext.pptx').exists()}")
    if not (d / "noext.pptx").exists():
        fail("auto .pptx extension", evidence)
    ok("auto .pptx extension", evidence)


# ── run ──────────────────────────────────────────────────────────────────────

def main():
    print(f"office_create verification suite — python {sys.version.split()[0]}")
    print(f"project: {ROOT}")
    if not _HAS_XLSX:
        fail("openpyxl import", ["openpyxl not installed — cannot run tests"])
    if not _HAS_DOCX:
        fail("python-docx import", ["python-docx not installed — cannot run tests"])
    if not _HAS_PPTX:
        fail("python-pptx import", ["python-pptx not installed — cannot run tests"])
    test_compile()
    test_normalise()
    test_build_xlsx_headers_rows()
    test_build_xlsx_objects()
    test_handler_errors()
    test_auto_extension_xlsx()
    test_auto_extension_docx()
    test_handler_e2e_xlsx()
    test_numeric_date()
    test_build_docx_headers_rows()
    test_build_docx_objects()
    test_handler_e2e_docx()
    test_build_pptx_headers_rows()
    test_build_pptx_objects()
    test_handler_e2e_pptx()
    test_auto_extension_pptx()
    print(f"\n=== ALL {PASSED}/16 TESTS PASSED ===")


if __name__ == "__main__":
    main()