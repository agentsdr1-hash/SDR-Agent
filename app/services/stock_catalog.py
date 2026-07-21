"""
Steel stock catalog -- reference data used to make outreach drafts specific
(real product categories/examples instead of generic copy) and to let a
human browse what's actually in stock.

Parses the same indented category-tree export the accounting system
produces (columns: Product Code, Product Name; indentation on Product Name
encodes hierarchy depth -- 5 spaces per level) rather than requiring a
flat re-formatted file. A leaf row (an actual stocked item, not a category
header) is one with no deeper row immediately following it.

Source files occasionally lose a row's indentation (observed: 2 rows out
of 884 in a real export), which would otherwise misread a real product as
a giant fake category swallowing everything after it. A sudden dedent of
more than one level mid-file is treated as that formatting glitch and
clamped back to the previous row's depth rather than trusted -- a legit
new top-level category never appears via a >1-level jump in this format.
"""
import io
from datetime import datetime, timezone

import openpyxl

from app.db import get_conn
from app.services.audit import log_event

MIN_LEAF_DEPTH = 3  # rows shallower than this are structural roots (e.g. "Products"), never real items


class StockImportError(Exception):
    pass


def _depth(name: str) -> int:
    return (len(name) - len(name.lstrip(" "))) // 5


def _parse_rows(rows: list[tuple]) -> list[dict]:
    items = []
    stack: list[tuple[int, str]] = []
    prev_depth = 0
    n = len(rows)
    for i in range(n):
        row = rows[i]
        code = row[0] if len(row) > 0 else None
        name = row[1] if len(row) > 1 else None
        if not name or not str(name).strip():
            continue
        d = _depth(str(name))
        if d < prev_depth - 1:
            d = prev_depth  # formatting glitch -- treat as a sibling of the previous row
        clean = str(name).strip().rstrip("-").strip()

        while stack and stack[-1][0] >= d:
            stack.pop()
        parent = stack[-1][1] if stack else None

        next_name = rows[i + 1][1] if i + 1 < n and len(rows[i + 1]) > 1 else None
        next_depth = _depth(str(next_name)) if next_name and str(next_name).strip() else -1
        has_child = next_depth > d and not (next_depth < d - 1)

        if not has_child and d >= MIN_LEAF_DEPTH and code and str(code).strip().upper() != "REPORT TOTAL":
            items.append({"code": str(code).strip(), "name": clean, "category": parent})

        stack.append((d, clean))
        prev_depth = d
    return items


def import_stock_list(filename: str, content: bytes) -> dict:
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    except Exception as e:
        raise StockImportError(f"Could not read file: {e}")

    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if len(rows) < 2:
        raise StockImportError("File has no data rows")

    items = _parse_rows(rows[1:])  # skip header row
    if not items:
        raise StockImportError("No stock items could be parsed from this file -- check it matches the expected Product Code / Product Name export format")

    now = datetime.now(timezone.utc).isoformat()
    categories = {it["category"] for it in items if it["category"]}
    with get_conn() as conn:
        # A stock list is a point-in-time snapshot, not additive transactional
        # data like prospects -- a new import supersedes the old one entirely.
        conn.execute("DELETE FROM stock_catalog")
        conn.executemany(
            "INSERT INTO stock_catalog (product_code, product_name, category, imported_at) VALUES (?, ?, ?, ?)",
            [(it["code"], it["name"], it["category"], now) for it in items],
        )

    log_event("stock_catalog_imported", "stock_catalog", None,
               f"Imported {len(items)} items across {len(categories)} categories from '{filename}' (replaced previous catalog)")

    return {"filename": filename, "item_count": len(items), "category_count": len(categories), "imported_at": now}


def list_categories() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) c FROM stock_catalog WHERE category IS NOT NULL GROUP BY category ORDER BY c DESC"
        ).fetchall()
    return [{"category": r["category"], "count": r["c"]} for r in rows]


def list_items(category: str | None = None, search: str | None = None, limit: int = 200) -> list[dict]:
    query = "SELECT id, product_code, product_name, category FROM stock_catalog"
    conditions, params = [], []
    if category:
        conditions.append("category = ?")
        params.append(category)
    if search:
        conditions.append("(product_name LIKE ? OR product_code LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY category, product_name LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def top_categories(n: int = 6) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) c FROM stock_catalog WHERE category IS NOT NULL GROUP BY category ORDER BY c DESC LIMIT ?",
            (n,),
        ).fetchall()
    return [r["category"] for r in rows]


def sample_items(n: int = 2) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT product_code, product_name, category FROM stock_catalog ORDER BY RANDOM() LIMIT ?", (n,)
        ).fetchall()
    return [dict(r) for r in rows]


def total_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM stock_catalog").fetchone()["c"]
