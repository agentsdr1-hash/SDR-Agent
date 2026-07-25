"""
OBJ-001 Prospect Import
Upload and process CSV/Excel prospect files.

Design:
- Accepts .csv or .xlsx bytes
- Maps loosely-named source columns (First, first_name, "First Name"...) to a
  canonical schema so the tracker isn't broken by header variance
- Writes every row to prospects_raw with status='Pending' for OBJ-002 to pick up
- Never rejects a file for bad data here -- that's OBJ-002's job (missing/
  malformed email, missing name -- things worth surfacing and letting a
  human fix). Import only rejects structurally broken files (unreadable,
  no rows, no recognizable columns).
- Duplicates are the one thing rejected right here, before a row is ever
  written: re-uploading the same file (or the same person appearing twice
  in one file) has no value to record -- there's nothing to fix and
  nothing new to know, so it doesn't get a row, a status, or a place in
  any table. This is stricter than OBJ-002's post-import 'Duplicate'
  classification (still used for a genuinely edge-case collision, e.g.
  two different rows in one file that happen to share an email after
  data cleanup) -- but the common case, re-uploading a whole list,
  never reaches that far.
"""
import io
import uuid
from datetime import datetime, timezone

import pandas as pd
import requests

from app.db import get_conn
from app.models import ImportSummary
from app.services.audit import log_event
from app.services.prospect_validation import REAL_PROSPECT_STATUSES

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

    ph = ",".join("?" * len(REAL_PROSPECT_STATUSES))
    with get_conn() as conn:
        # Row_count is a placeholder until the loop below knows the real
        # post-dedup total -- prospects_raw.batch_id is a foreign key
        # into this table, so the parent row has to exist before any
        # child row referencing it can be inserted.
        conn.execute(
            "INSERT INTO import_batches (batch_id, filename, row_count, imported_at) VALUES (?, ?, ?, ?)",
            (batch_id, filename, len(df), now),
        )

        # Every email that's already a real, identified prospect from any
        # earlier import -- the gate for "you already imported this
        # person." Checked once up front rather than per row.
        already_known = {
            r["email"].strip().lower()
            for r in conn.execute(
                f"SELECT email FROM prospects_raw WHERE email IS NOT NULL AND status IN ({ph})",
                REAL_PROSPECT_STATUSES,
            ).fetchall()
            if r["email"]
        }

        seen_in_file: set[str] = set()
        inserted = 0
        duplicate_count = 0
        for i, row in df.iterrows():
            def get(col_key):
                src = mapping.get(col_key)
                if src is None:
                    return None
                val = row.get(src)
                return None if pd.isna(val) else str(val).strip()

            email = get("email")
            email_key = (email or "").lower()
            # A row with no/malformed email isn't excluded here -- that's
            # not a duplicate, it's OBJ-002's "Invalid" case, which still
            # needs its own row to be visible and fixable.
            if email_key and (email_key in already_known or email_key in seen_in_file):
                duplicate_count += 1
                continue
            if email_key:
                seen_in_file.add(email_key)

            conn.execute(
                """INSERT INTO prospects_raw
                   (batch_id, row_number, first_name, last_name, email, company, phone, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending')""",
                (batch_id, i + 1, get("first_name"), get("last_name"),
                 email, get("company"), get("phone")),
            )
            inserted += 1

        conn.execute(
            "UPDATE import_batches SET row_count = ? WHERE batch_id = ?",
            (inserted, batch_id),
        )

    log_event(
        "prospect_import", "batch", batch_id,
        f"Imported {inserted} row(s) from '{filename}'"
        + (f", skipped {duplicate_count} already-known duplicate(s)" if duplicate_count else "")
    )

    return ImportSummary(
        batch_id=batch_id,
        filename=filename,
        row_count=inserted,
        duplicate_count=duplicate_count,
        columns_mapped=mapping,
    )
