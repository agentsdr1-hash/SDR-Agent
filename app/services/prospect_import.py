"""
OBJ-001 Prospect Import
Upload and process CSV/Excel prospect files.

Design:
- Accepts .csv or .xlsx bytes
- Maps loosely-named source columns (First, first_name, "First Name"...) to a
  canonical schema so the tracker isn't broken by header variance
- Writes every row to prospects_raw with status='Pending' for OBJ-002 to pick up
- Never rejects a file for bad data here -- that's OBJ-002's job. Import only
  rejects structurally broken files (unreadable, no rows, no recognizable columns)
"""
import io
import uuid
from datetime import datetime, timezone

import pandas as pd
import requests

from app.db import get_conn
from app.models import ImportSummary
from app.services.audit import log_event

CANONICAL_COLUMNS = {
    "first_name": ["first_name", "first", "firstname", "given name"],
    "last_name": ["last_name", "last", "lastname", "surname"],
    "email": ["email", "email_address", "e-mail"],
    "company": ["company", "company_name", "organization", "employer"],
    "phone": ["phone", "phone_number", "telephone", "mobile"],
}


class ImportError_(Exception):
    pass


def _map_columns(columns: list[str]) -> dict[str, str]:
    """Return {canonical_name: source_column_name} for whatever we can match."""
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
    raise ImportError_(f"Unsupported file type: {filename}. Only .csv and .xlsx are accepted.")


def import_prospect_file_from_url(url: str, timeout: int = 15) -> ImportSummary:
    """
    OBJ-001 integration point: pull a prospect file from an external source
    instead of a direct browser upload -- a shared drive link, an SFTP-backed
    HTTP endpoint, an email-attachment staging URL, etc. Same downstream path
    as a direct upload once the bytes are in hand.
    """
    try:
        resp = requests.get(url, timeout=timeout)
        resp.raise_for_status()
    except requests.RequestException as e:
        raise ImportError_(f"Could not fetch file from '{url}': {e}")

    filename = url.split("/")[-1].split("?")[0] or "external_file"
    return import_prospect_file(filename, resp.content)


def import_prospect_file(filename: str, content: bytes) -> ImportSummary:
    try:
        df = _read_file(filename, content)
    except ImportError_:
        raise
    except Exception as e:
        raise ImportError_(f"Could not parse '{filename}': {e}")

    if df.empty:
        raise ImportError_(f"'{filename}' contains no data rows.")

    mapping = _map_columns(list(df.columns))
    if "email" not in mapping:
        raise ImportError_(
            f"'{filename}' has no recognizable email column. "
            f"Found columns: {list(df.columns)}"
        )

    batch_id = str(uuid.uuid4())[:8]
    now = datetime.now(timezone.utc).isoformat()

    with get_conn() as conn:
        conn.execute(
            "INSERT INTO import_batches (batch_id, filename, row_count, imported_at) VALUES (?, ?, ?, ?)",
            (batch_id, filename, len(df), now),
        )
        for i, row in df.iterrows():
            def get(col_key):
                src = mapping.get(col_key)
                if src is None:
                    return None
                val = row.get(src)
                return None if pd.isna(val) else str(val).strip()

            conn.execute(
                """INSERT INTO prospects_raw
                   (batch_id, row_number, first_name, last_name, email, company, phone, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending')""",
                (batch_id, i + 1, get("first_name"), get("last_name"),
                 get("email"), get("company"), get("phone")),
            )

    log_event("prospect_import", "batch", batch_id, f"Imported {len(df)} rows from '{filename}'")

    return ImportSummary(
        batch_id=batch_id,
        filename=filename,
        row_count=len(df),
        columns_mapped=mapping,
    )
