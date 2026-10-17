"""csv_reader — structured reading of .csv / .tsv / tabular text files.

file_controller 'read' returns a CSV as raw truncated text and document_rag
only does fuzzy passage retrieval. This action understands the table
structure — columns, inferred types, row counts, previews, filters, numeric
stats, unique values and any-cell search — so questions like "what is the
total in the amount column?" get real answers.
"""
from __future__ import annotations

import csv
import io
from pathlib import Path

# Same path shortcuts (desktop/downloads/...) and safety checks as file_controller.
from actions.file_controller import _resolve_path, _is_safe_path, _format_size

_MAX_FILE_BYTES = 50 * 1024 * 1024     # refuse files larger than this (memory)
_MAX_ROWS = 100_000                    # hard cap on loaded rows (memory)
_MAX_COLS = 100                        # hard cap on columns
_DELIMITERS = ",;\t|"
_DEFAULT_ROWS = 10
_MAX_ROWS_SHOWN = 50
_CELL_CLIP = 32
_COL_CLIP = 24


def _decode(data: bytes):
    try:
        return data.decode("utf-8-sig"), "utf-8-sig"
    except UnicodeDecodeError:
        pass
    if data.startswith((b"\xff\xfe", b"\xfe\xff")) or b"\x00" in data[:256]:
        try:
            return data.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        pass
    return data.decode("latin-1"), "latin-1"


def _detect_delimiter(text: str) -> str:
    sample = text[:8192]
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS).delimiter
    except csv.Error:
        best, best_count = ",", -1
        for d in _DELIMITERS:
            c = sample.count(d)
            if c > best_count:
                best, best_count = d, c
        return best


def _dedupe_headers(raw_headers: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    out: list[str] = []
    for h in raw_headers:
        h = (h or "").strip() or "column"
        n = seen.get(h, 0) + 1
        seen[h] = n
        out.append(h if n == 1 else f"{h} #{n}")
    return out


def _infer_type(values: list[str]) -> str:
    """'number' when every non-empty sampled value parses as a float."""
    sample = [v for v in values[:1000] if v != ""]
    if not sample:
        return "text"
    for v in sample:
        try:
            float(v)
        except ValueError:
            return "text"
    return "number"


def _to_number(v: str):
    f = float(v)
    return int(f) if f.is_integer() else f


def _read_table(path: str):
    """Returns (meta, columns, rows) or raises ValueError with a user message."""
    p = _resolve_path(path)
    if not _is_safe_path(p):
        raise ValueError(f"Access denied: {p}")
    if not p.exists():
        raise ValueError(f"File not found: {p.name}")
    if not p.is_file():
        raise ValueError(f"Not a file: {p.name}")
    size = p.stat().st_size
    if size > _MAX_FILE_BYTES:
        raise ValueError(
            f"'{p.name}' is {_format_size(size)} — too large to read into memory. "
            "Split it into smaller files and try again.")

    text, encoding = _decode(p.read_bytes())
    delim = _detect_delimiter(text)

    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = []
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError(f"'{p.name}' is empty — nothing to read.")
    columns = _dedupe_headers(header)[:_MAX_COLS]
    n_cols = len(columns)

    truncated = False
    for raw in reader:
        if not raw or all(not (c or "").strip() for c in raw):
            continue
        if len(raw) > n_cols:
            raw = raw[:n_cols]
        elif len(raw) < n_cols:
            raw = raw + [""] * (n_cols - len(raw))
        if len(rows) >= _MAX_ROWS:
            truncated = True
            break
        rows.append([c.strip() for c in raw])

    meta = {
        "file": str(p),
        "name": p.name,
        "delimiter": repr(delim),
        "encoding": encoding,
        "rows": len(rows),
        "truncated": truncated,
        "size": _format_size(size),
    }
    return meta, columns, rows


def _column_index(columns: list[str], name: str) -> int:
    wanted = (name or "").strip().lower()
    for i, c in enumerate(columns):
        if c.lower() == wanted:
            return i
    raise ValueError(
        f"No column named '{name}'. Columns are: {', '.join(columns)}")


def _cell(v) -> str:
    v = str(v)
    return v if len(v) <= _CELL_CLIP else v[:_CELL_CLIP] + "…"


def _fmt_table(columns: list[str], rows: list[list[str]],
               shown: int | None = None, total: int | None = None) -> str:
    shown_rows = rows if shown is None else rows[:shown]
    widths = [min(max(len(c), *([len(_cell(r[i])) for r in shown_rows] or [0])),
                  _COL_CLIP) for i, c in enumerate(columns)]
    header = " | ".join((c if len(c) <= _COL_CLIP else c[:_COL_CLIP - 1] + "…").ljust(w)
                        for c, w in zip(columns, widths))
    lines = [header, "-+-".join("-" * w for w in widths)]
    for r in shown_rows:
        lines.append(" | ".join(_cell(r[i]).ljust(w)
                                for i, w in enumerate(widths)))
    if total is not None and total > (shown or 0):
        lines.append(f"… and {total - (shown or 0)} more row(s).")
    return "\n".join(lines)


def _fmt_num(v) -> str:
    return f"{v:,.4g}"


def _action_info(meta, columns, rows):
    n_cols = len(columns)
    col_stats = []
    for i, c in enumerate(columns):
        non_empty = sum(1 for r in rows if r[i] != "")
        t = _infer_type([r[i] for r in rows])
        sample = _cell(next((r[i] for r in rows if r[i] != ""), ""))
        col_stats.append(f"  {c}: {t}, {non_empty} non-empty, e.g. {sample}")
    lines = [
        f"'{meta['name']}' — {meta['size']}, delimiter {meta['delimiter']}",
        f"{meta['rows']} row(s), {n_cols} column(s):",
        *col_stats,
    ]
    if meta["truncated"]:
        lines.append(
            f"(only the first {_MAX_ROWS:,} rows were loaded — the file has more; "
            "filters/stats below only cover the loaded rows)")
    return "\n".join(lines)


def _action_preview(meta, columns, rows, n):
    if not rows:
        return f"'{meta['name']}' has a header but no data rows."
    return _fmt_table(columns, rows, shown=n, total=len(rows))


def _action_filter(meta, columns, rows, params):
    col_name = str(params.get("column") or "").strip()
    op = str(params.get("operator") or "equals").lower().strip()
    value = str(params.get("value") or "")
    n = min(int(params.get("rows", _DEFAULT_ROWS) or _DEFAULT_ROWS), _MAX_ROWS_SHOWN)

    if not col_name:
        return "I need a column name to filter on, sir."
    if not value:
        return "I need a value to compare against, sir."
    idx = _column_index(columns, col_name)
    col = columns[idx]
    numeric = _infer_type([r[idx] for r in rows]) == "number"
    known_ops = {"equals", "eq", "=", "==", "not_equals", "ne", "!=",
                 "contains", "gt", "gte", "lt", "lte"}
    if op not in known_ops:
        return (f"Unknown operator '{op}'. Use: equals, not_equals, contains, "
                "gt, gte, lt, lte.")

    if numeric:
        try:
            num_value = float(value)
        except ValueError:
            return (f"Column '{col}' holds numbers, but '{value}' is not a number. "
                    "Use a numeric value for gt/gte/lt/lte, or 'contains' to match text.")
        match = []
        for r in rows:
            try:
                cell = float(r[idx])
            except ValueError:
                continue
            if op in ("equals", "eq", "=", "==") and cell == num_value:
                match.append(r)
            elif op in ("not_equals", "ne", "!=") and cell != num_value:
                match.append(r)
            elif op == "gt" and cell > num_value:
                match.append(r)
            elif op == "gte" and cell >= num_value:
                match.append(r)
            elif op == "lt" and cell < num_value:
                match.append(r)
            elif op == "lte" and cell <= num_value:
                match.append(r)
            elif op == "contains" and value.lower() in r[idx].lower():
                match.append(r)
    else:
        match = []
        for r in rows:
            cell = r[idx]
            cell_l = cell.lower()
            if op in ("equals", "eq", "=", "==") and cell_l == value.lower():
                match.append(r)
            elif op in ("not_equals", "ne", "!=") and cell_l != value.lower():
                match.append(r)
            elif op == "contains" and value.lower() in cell_l:
                match.append(r)
            elif op == "gt" and cell_l > value.lower():
                match.append(r)
            elif op == "gte" and cell_l >= value.lower():
                match.append(r)
            elif op == "lt" and cell_l < value.lower():
                match.append(r)
            elif op == "lte" and cell_l <= value.lower():
                match.append(r)

    if not match:
        return (f"No rows where '{col}' {op} '{value}' "
                f"(scanned {len(rows):,} row(s)).")
    return (f"{len(match)} row(s) where '{col}' {op} '{value}':\n"
            + _fmt_table(columns, match, shown=n, total=len(match)))


def _action_stats(meta, columns, rows, params):
    col_name = str(params.get("column") or "").strip()
    if col_name:
        idx = _column_index(columns, col_name)
        col = columns[idx]
        values = [r[idx] for r in rows if r[idx] != ""]
        if _infer_type(values) != "number":
            uniq = len({v.lower() for v in values})
            return (f"Column '{col}' is text, not numeric — {len(values)} non-empty "
                    f"value(s), {uniq} unique. Use action 'unique' to list them.")
        nums = [float(v) for v in values]
        return (f"'{col}' ({len(nums)} value(s)): sum={_fmt_num(sum(nums))}, "
                f"mean={_fmt_num(sum(nums) / len(nums))}, "
                f"min={_fmt_num(min(nums))}, max={_fmt_num(max(nums))}")

    lines = [f"'{meta['name']}' — numeric columns:"]
    found = False
    for i, c in enumerate(columns):
        values = [r[i] for r in rows if r[i] != ""]
        if not values or _infer_type(values) != "number":
            continue
        found = True
        nums = [float(v) for v in values]
        lines.append(f"  {c}: {len(nums)} value(s), sum={_fmt_num(sum(nums))}, "
                     f"mean={_fmt_num(sum(nums) / len(nums))}, "
                     f"min={_fmt_num(min(nums))}, max={_fmt_num(max(nums))}")
    if not found:
        lines.append("  none — every column is text. Use action 'unique' on a "
                     "column, or action 'filter'.")
    return "\n".join(lines)


def _action_unique(meta, columns, rows, params):
    col_name = str(params.get("column") or "").strip()
    if not col_name:
        return "I need a column name, sir."
    idx = _column_index(columns, col_name)
    col = columns[idx]
    counts: dict[str, int] = {}
    for r in rows:
        v = r[idx].strip()
        if v:
            key = v
            counts[key] = counts.get(key, 0) + 1
    if not counts:
        return f"Column '{col}' has no non-empty values."
    n = min(int(params.get("rows", _DEFAULT_ROWS) or _DEFAULT_ROWS), _MAX_ROWS_SHOWN)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].lower()))
    lines = [f"'{col}' — {len(counts)} distinct value(s):"]
    for v, c in ranked[:n]:
        lines.append(f"  {_cell(v)} — {c}")
    if len(ranked) > n:
        lines.append(f"… and {len(ranked) - n} more distinct value(s).")
    return "\n".join(lines)


def _action_search(meta, columns, rows, params):
    query = str(params.get("query") or "").strip()
    if not query:
        return "I need a search term, sir."
    q = query.lower()
    n = min(int(params.get("rows", _DEFAULT_ROWS) or _DEFAULT_ROWS), _MAX_ROWS_SHOWN)
    match = [r for r in rows if any(q in c.lower() for c in r)]
    if not match:
        return f"No rows contain '{query}' (searched {len(rows):,} row(s))."
    return (f"{len(match)} row(s) contain '{query}':\n"
            + _fmt_table(columns, match, shown=n, total=len(match)))


def csv_reader(parameters: dict, player=None, session_memory=None) -> str:
    params = parameters or {}
    action = str(params.get("action") or "info").lower().strip()
    path = str(params.get("path") or "").strip()
    if not path:
        return "I need a file path, sir — e.g. path 'desktop/sales.csv'."

    if player:
        player.write_log(f"[csv] {action} {path}")

    try:
        meta, columns, rows = _read_table(path)
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"Could not read CSV: {e}"

    try:
        if action == "info":
            return _action_info(meta, columns, rows)
        if action == "preview":
            return _action_preview(meta, columns, rows,
                                   min(int(params.get("rows", 5) or 5), _MAX_ROWS_SHOWN))
        if action == "filter":
            return _action_filter(meta, columns, rows, params)
        if action == "stats":
            return _action_stats(meta, columns, rows, params)
        if action == "unique":
            return _action_unique(meta, columns, rows, params)
        if action == "search":
            return _action_search(meta, columns, rows, params)
        return (f"Unknown csv_reader action '{action}'. "
                "Available: info | preview | filter | stats | unique | search")
    except ValueError as e:
        return str(e)
    except Exception as e:
        return f"csv_reader error ({action}): {e}"


TOOL = {
    "name": "csv_reader",
    "description": (
        "Reads and understands .csv, .tsv or other tabular text files as a TABLE: "
        "columns, row counts, previews, filters, numeric totals/averages, distinct "
        "values, and finding rows. Use this whenever the user asks to read, inspect, "
        "summarize, or answer questions about a CSV/spreadsheet-export file — "
        "especially for questions like 'how many rows', 'what is the total of column "
        "X', 'which rows have...' — this tool understands structure, unlike the "
        "plain file read. Also handles .txt files with tabular data. NOT for .xlsx "
        "Excel files."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "info (columns, types, row count) | preview (first rows) | "
                    "filter (rows matching a column condition) | stats (sum/mean/"
                    "min/max) | unique (distinct values + counts) | search "
                    "(rows containing text anywhere). Default: info."
                ),
            },
            "path": {
                "type": "STRING",
                "description": (
                    "Path to the .csv/.tsv file — full path or shortcut like "
                    "'desktop/sales.csv'."
                ),
            },
            "column": {
                "type": "STRING",
                "description": "Column name to filter on / get stats for / list unique values of.",
            },
            "operator": {
                "type": "STRING",
                "description": (
                    "For filter: equals | not_equals | contains | gt | gte | lt | "
                    "lte (default: equals)."
                ),
            },
            "value": {
                "type": "STRING",
                "description": "For filter: the value to compare against (numbers compare numerically on numeric columns).",
            },
            "query": {
                "type": "STRING",
                "description": "For search: text to find anywhere in any column.",
            },
            "rows": {
                "type": "INTEGER",
                "description": "How many rows to show (default 10, max 50; preview default 5).",
            },
        },
        "required": ["action", "path"],
    },
    "handler": csv_reader,
}
