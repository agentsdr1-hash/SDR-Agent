"""
Existing-customer list -- reference data used by OBJ-002's "Existing
Customer" validation check (see prospect_validation.py), matched by both
email and company name. Not fed by the prospect import pipeline; this is
who you *already* sell to, kept separate from prospects_raw the same way
the stock catalog is kept separate from campaign data.

Mirrors stock_catalog.py's design: a customer list export is a
point-in-time snapshot of a real system of record, not additive
transactional data like prospects -- a new import replaces the previous
list entirely rather than merging into it.
"""
import io
from datetime import datetime, timezone

import pandas as pd

from app.db import get_conn
from app.services.audit import log_event

CANONICAL_COLUMNS = {
    "email": ["email", "email_address", "e-mail"],
    "company": ["company", "company_name", "organization", "employer", "account", "account_name"],
}


class CustomerImportError(Exception):
    pass


def _map_columns(columns: list[str]) -> dict[str, str]:
    normalized = {c: c.strip().lower().replace(" ", "_") for c in columns}
    mapping = {}
    for canonical, aliases in CANONICAL_COLUMNS.items():
        for source_col, norm in normalized.items():
            if norm in aliases:
                mapping[canonical] = source_col
                break
    return mapping


def _read_file(filename: str, content: bytes) -> pd.DataFrame:
    if filename.lower().endswith(".csv"):
        return pd.read_csv(io.BytesIO(content), dtype=str)
    elif filename.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(content), dtype=str)
    raise CustomerImportError(f"Unsupported file type: {filename}. Only .csv and .xlsx are accepted.")


def import_customer_list(filename: str, content: bytes) -> dict:
    try:
        df = _read_file(filename, content)
    except CustomerImportError:
        raise
    except Exception as e:
        raise CustomerImportError(f"Could not parse '{filename}': {e}")

    if df.empty:
        raise CustomerImportError(f"'{filename}' contains no data rows.")

    mapping = _map_columns(list(df.columns))
    if "email" not in mapping:
        raise CustomerImportError(
            f"'{filename}' has no recognizable email column. Found columns: {list(df.columns)}"
        )

    rows = []
    seen_emails: set[str] = set()
    for _, row in df.iterrows():
        def get(col_key):
            src = mapping.get(col_key)
            if src is None:
                return None
            val = row.get(src)
            return None if pd.isna(val) else str(val).strip()

        email = get("email")
        if not email:
            continue  # a customer record with no email can't be matched against anything -- skip, don't fail the whole file
        key = email.lower()
        if key in seen_emails:
            continue  # last occurrence would otherwise violate the UNIQUE(email) constraint on insert
        seen_emails.add(key)
        rows.append((email, get("company")))

    if not rows:
        raise CustomerImportError(f"'{filename}' has no rows with a usable email address.")

    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute("DELETE FROM customers")
        conn.executemany("INSERT INTO customers (email, company) VALUES (?, ?)", rows)

    log_event("customers_imported", "customers", None,
               f"Imported {len(rows)} customer(s) from '{filename}' (replaced previous list)")

    return {"filename": filename, "customer_count": len(rows), "imported_at": now}


def list_customers(search: str | None = None, limit: int = 200) -> list[dict]:
    query = "SELECT id, email, company FROM customers"
    params = []
    if search:
        query += " WHERE email LIKE ? OR company LIKE ?"
        like = f"%{search}%"
        params.extend([like, like])
    query += " ORDER BY company, email LIMIT ?"
    params.append(limit)
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def total_count() -> int:
    with get_conn() as conn:
        return conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
