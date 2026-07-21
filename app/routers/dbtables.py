"""
Raw database table browser -- lets you inspect every SQLite table directly
from the UI for QA/spot-checking that data is actually persisting, without
needing shell/sqlite3 access to the host.

Table names are only ever used as dict lookups against a fixed whitelist,
never interpolated into SQL from user input, so this is safe despite the
table name coming off the URL.
"""
from fastapi import APIRouter, HTTPException

from app.db import get_conn

router = APIRouter(prefix="/db", tags=["debug"])

TABLE_QUERIES = {
    "import_batches": "SELECT * FROM import_batches ORDER BY imported_at DESC",
    "prospects_raw": "SELECT * FROM prospects_raw ORDER BY id DESC LIMIT 500",
    "customers": "SELECT * FROM customers ORDER BY id",
    "campaigns": "SELECT * FROM campaigns ORDER BY created_at DESC",
    "campaign_prospects": "SELECT * FROM campaign_prospects ORDER BY id DESC LIMIT 500",
    "suppressed_emails": "SELECT * FROM suppressed_emails ORDER BY added_at DESC",
    "audit_log": "SELECT * FROM audit_log ORDER BY id DESC LIMIT 500",
    "stock_catalog": "SELECT * FROM stock_catalog ORDER BY category, product_name LIMIT 900",
}


@router.get("/tables")
def list_tables():
    with get_conn() as conn:
        return [
            {"name": name, "row_count": conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]}
            for name in TABLE_QUERIES
        ]


@router.get("/tables/{table_name}")
def get_table(table_name: str):
    query = TABLE_QUERIES.get(table_name)
    if not query:
        raise HTTPException(status_code=404, detail=f"Unknown table '{table_name}'. Valid: {list(TABLE_QUERIES)}")
    with get_conn() as conn:
        rows = conn.execute(query).fetchall()
    return [dict(r) for r in rows]
