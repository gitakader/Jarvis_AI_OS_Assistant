"""
Offline tests for actions/csv_reader.py — headless, temp-dir CSVs only.

No registry, no UI, no network. Monkeypatches only module constants
(limits) and path resolution so nothing outside a temp dir is touched.

Run:  venv/Scripts/python.exe selfheal_tests/test_csv_reader.py
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

BASE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        FAILURES.append(f"{label} — {detail}" if detail else label)
        print(f"  FAIL  {label}" + (f"  [{detail}]" if detail else ""))


from actions import csv_reader as C

TMP = pathlib.Path(tempfile.mkdtemp(prefix="csv_reader_test_"))


def write_csv(name: str, text: str, encoding: str = "utf-8") -> pathlib.Path:
    p = TMP / name
    p.write_text(text, encoding=encoding)
    return p


def write_csv_bytes(name: str, data: bytes) -> pathlib.Path:
    p = TMP / name
    p.write_bytes(data)
    return p


def call(**params) -> str:
    return C.csv_reader(parameters=params)


# ─────────────────────────────────────────────────────────────────────────────
# SECTION 1 — discovery shape (same checks as core/action_loader._validate)
# ─────────────────────────────────────────────────────────────────────────────

from core import action_loader

_rec = action_loader._validate(C, "csv_reader.py")
check("TOOL dict validates for discovery", _rec.valid, _rec.error)
check("TOOL name is csv_reader", getattr(_rec, "name", "") == "csv_reader")
check("handler is callable", callable(C.csv_reader))
check("TOOL requires action+path", C.TOOL["parameters"].get("required") == ["action", "path"])

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 2 — parsing: delimiters, quotes, encodings, ragged rows, dup headers
# ─────────────────────────────────────────────────────────────────────────────

SALES = write_csv("sales.csv", "name,amount,city\nAlice,10,Dhaka\nBob,20,Khulna\nCarol,30,Dhaka\n")
check("comma csv: info shows 3 rows", "3 row(s)" in call(action="info", path=str(SALES)))

SEMI = write_csv("semi.csv", "name;amount\nAlice;10\nBob;20\n")
out = call(action="info", path=str(SEMI))
check("semicolon delimiter detected", "delimiter ';'" in out, out)
check("semicolon csv: rows parsed", "2 row(s)" in out)

TAB = write_csv("tab.tsv", "name\tamount\nAlice\t10\nBob\t20\n")
out = call(action="info", path=str(TAB))
check("tab delimiter detected", "delimiter '\\t'" in out, out)
check("tab csv: rows parsed", "2 row(s)" in out)

QUOTED = write_csv(
    "quoted.csv",
    'name,note,amount\n"Smith, John","line1\nline2",42\n"O\'Brien","plain",7\n')
out = call(action="preview", path=str(QUOTED), rows=10)
check("quoted comma survives as one cell", "Smith, John" in out, out)
check("quoted newline survives as one cell", "line1" in out and "line2" in out, out)
check("quoted csv: 2 data rows", "2 row(s)" in call(action="info", path=str(QUOTED)))

BOM = write_csv_bytes("bom.csv", "\ufeffname,amount\nAlice,10\n".encode("utf-8"))
out = call(action="preview", path=str(BOM), rows=5)
check("utf-8 BOM stripped from header", "name" in out and "\ufeff" not in out, out)

UTF16 = write_csv_bytes("utf16.csv", "name,amount\nAlice,10\nBob,20\n".encode("utf-16"))
out = call(action="info", path=str(UTF16))
check("utf-16 file decoded", "2 row(s)" in out and "Alice" in out, out)

CP1252 = write_csv_bytes("cp1252.csv", "name,city\nAlice,caf\xe9\nBob,nyc\n".encode("cp1252"))
out = call(action="info", path=str(CP1252))
check("cp1252 file decoded", "café" in out, out)

RAGGED = write_csv("ragged.csv", "a,b,c\n1,2\n4,5,6,7\n")
out = call(action="filter", path=str(RAGGED), column="a", operator="equals", value="1", rows=5)
check("short row padded without error", "1 row(s) where" in out, out)
out = call(action="preview", path=str(RAGGED), rows=10)
check("long row truncated without error", "4" in out and "6" in out and "7" not in out, out)

DUP = write_csv("dup.csv", "name,name,amount\nAlice,Bob,10\n")
out = call(action="info", path=str(DUP))
check("duplicate headers suffixed", "name" in out and "name #2" in out, out)
check("duplicate header filter works", "1 row(s)" in call(
    action="filter", path=str(DUP), column="name #2", operator="equals", value="Bob", rows=5))

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 3 — info / preview
# ─────────────────────────────────────────────────────────────────────────────

out = call(action="info", path=str(SALES))
check("info: row + column counts", "3 row(s), 3 column(s)" in out, out)
check("info: numeric type inferred", "amount: number" in out, out)
check("info: text type inferred", "name: text" in out, out)
check("info: non-empty counts", "3 non-empty" in out, out)
check("info: sample value shown", "e.g. Alice" in out, out)

BIG = write_csv("big.csv", "\n".join(
    ["name,amount"] + [f"p{i},{i}" for i in range(7)]))
out = call(action="preview", path=str(BIG))
check("preview defaults to 5 rows + note", "p0" in out and "p4" in out
      and "2 more row(s)" in out and "p5" not in out, out)
out = call(action="preview", path=str(BIG), rows=2)
check("preview honors rows param", "p1" in out and "5 more row(s)" in out and "p2" not in out, out)
check("preview renders column separator", " | " in out, out)

HEADER_ONLY = write_csv("header_only.csv", "name,amount\n")
check("header-only: clear message", "header but no data rows" in call(
    action="preview", path=str(HEADER_ONLY), rows=5))

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 4 — filter
# ─────────────────────────────────────────────────────────────────────────────

out = call(action="filter", path=str(SALES), column="city", operator="equals", value="dhaka", rows=10)
check("filter equals is case-insensitive", "2 row(s)" in out and "Alice" in out and "Carol" in out, out)

out = call(action="filter", path=str(SALES), column="amount", operator="gt", value="15", rows=10)
check("filter numeric gt", "2 row(s)" in out and "Bob" in out and "Carol" in out, out)

out = call(action="filter", path=str(SALES), column="amount", operator="gte", value="20", rows=10)
check("filter numeric gte", "2 row(s)" in out, out)

out = call(action="filter", path=str(SALES), column="amount", operator="lt", value="20", rows=10)
check("filter numeric lt", "1 row(s)" in out and "Alice" in out, out)

out = call(action="filter", path=str(SALES), column="amount", operator="lte", value="20", rows=10)
check("filter numeric lte", "2 row(s)" in out, out)

out = call(action="filter", path=str(SALES), column="name", operator="contains", value="li", rows=10)
check("filter contains", "Alice" in out and "Bob" not in out, out)

out = call(action="filter", path=str(SALES), column="city", operator="not_equals", value="Dhaka", rows=10)
check("filter not_equals", "1 row(s)" in out and "Bob" in out, out)

out = call(action="filter", path=str(SALES), column="city", operator="equals", value="nowhere", rows=10)
check("filter no match message", "No rows where" in out, out)

out = call(action="filter", path=str(SALES), column="city", operator="frobnicate", value="x")
check("filter unknown operator rejected", "Unknown operator 'frobnicate'" in out, out)

out = call(action="filter", path=str(SALES), column="nope", operator="equals", value="x")
check("filter unknown column lists real columns", "No column named 'nope'" in out
      and "name" in out and "amount" in out, out)

out = call(action="filter", path=str(SALES), column="amount", operator="gt", value="abc")
check("filter non-numeric value on numeric column rejected",
      "'abc' is not a number" in out, out)

out = call(action="filter", path=str(SALES), column="amount", operator="gt", value="10", rows=1)
check("filter rows cap + total count", "2 row(s) where" in out and "1 more row(s)" in out, out)

check("filter missing column param", "need a column name" in call(
    action="filter", path=str(SALES), value="x"))
check("filter missing value param", "need a value" in call(
    action="filter", path=str(SALES), column="name"))

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 5 — stats / unique / search
# ─────────────────────────────────────────────────────────────────────────────

out = call(action="stats", path=str(SALES), column="amount")
check("stats numeric column: sum", "sum=60" in out, out)
check("stats numeric column: mean", "mean=20" in out, out)
check("stats numeric column: min/max", "min=10" in out and "max=30" in out, out)

FRAC = write_csv("frac.csv", "name,score\nAlice,1.5\nBob,2.5\n")
out = call(action="stats", path=str(FRAC), column="score")
check("stats float column: sum", "sum=4" in out, out)

out = call(action="stats", path=str(SALES), column="name")
check("stats on text column redirects to unique",
      "is text, not numeric" in out and "unique" in out, out)

out = call(action="stats", path=str(SALES))
check("stats no column: lists numeric columns only",
      "amount:" in out and "name:" not in out, out)

TEXT_ONLY = write_csv("text_only.csv", "a,b\nx,y\n")
check("stats all-text file: honest none", "none" in call(
    action="stats", path=str(TEXT_ONLY)), )

out = call(action="unique", path=str(SALES), column="city")
check("unique: distinct count", "2 distinct value(s)" in out, out)
check("unique: sorted by count desc", out.index("Dhaka") < out.index("Khulna"), out)

UQ = write_csv("uq.csv", "k\n" + "\n".join(f"v{i}" for i in range(25)))
out = call(action="unique", path=str(UQ), column="k")
import re as _re
value_lines = _re.findall(r"^  v\d+ — \d+$", out, _re.MULTILINE)
check("unique: top-10 cap", len(value_lines) == 10 and "25 distinct" in out
      and "15 more distinct" in out, out)

out = call(action="unique", path=str(SALES), column="nope")
check("unique unknown column rejected", "No column named 'nope'" in out, out)

out = call(action="search", path=str(SALES), query="dhaka")
check("search finds any-cell matches", "2 row(s) contain 'dhaka'" in out
      and "Alice" in out and "Carol" in out, out)

out = call(action="search", path=str(SALES), query="zzz")
check("search no match message", "No rows contain 'zzz'" in out, out)

# ─────────────────────────────────────────────────────────────────────────────
# SECTION 6 — errors and limits
# ─────────────────────────────────────────────────────────────────────────────

check("missing file error", "File not found" in call(action="info", path=str(TMP / "nope.csv")))
check("empty file error", "is empty" in call(
    action="info", path=str(write_csv("empty.csv", ""))))
check("directory path error", "Not a file" in call(action="info", path=str(TMP)))
check("missing path param", "need a file path" in call(action="info"))
check("unknown action listed", "Unknown csv_reader action" in call(
    action="teleport", path=str(SALES)))

_old_max_rows = C._MAX_ROWS
C._MAX_ROWS = 5
try:
    out = call(action="info", path=str(BIG))
    check("row cap: truncation flagged", "only the first 5 rows were loaded" in out, out)
    check("row cap: loaded-row count reported", "5 row(s), 2 column(s)" in out, out)
finally:
    C._MAX_ROWS = _old_max_rows

_old_max_bytes = C._MAX_FILE_BYTES
C._MAX_FILE_BYTES = 50
try:
    out = call(action="info", path=str(SALES))
    check("size cap: too-large rejected with guidance", "too large" in out and "Split" in out, out)
finally:
    C._MAX_FILE_BYTES = _old_max_bytes

# Shortcut resolution ("desktop/...") is delegated to file_controller's
# _resolve_path — prove the action calls it by redirecting it to the temp dir.
_old_resolve = C._resolve_path
C._resolve_path = lambda raw: TMP / str(raw).replace("desktop/", "", 1)
try:
    out = call(action="info", path="desktop/sales.csv")
    check("shortcut-style path resolved through helper", "3 row(s)" in out, out)
finally:
    C._resolve_path = _old_resolve

# ─────────────────────────────────────────────────────────────────────────────
print(f"\ncsv_reader: {PASS} passed, {FAIL} failed")
if FAILURES:
    print("Failures:")
    for f in FAILURES:
        print(f"  - {f}")
    sys.exit(1)
