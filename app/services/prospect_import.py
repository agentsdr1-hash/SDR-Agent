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

Duplicates, existing customers, and already-contacted rows are NOT
filtered out at this stage -- every row still gets written and classified
by OBJ-002 (see prospect_validation.py), so the Dashboard's prospect
funnel (a plain GROUP BY over prospects_raw.status) keeps an accurate
running total of everything ever imported. What changes is visibility,
not existence: leads.py's NON_LEAD_STATUSES keeps those rows out of the
Leads tab without erasing them from the record.
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
    "first_name": ["first_name", "first", "firstname", "fname", "given_name", "given name", "forename"],
    "last_name": ["last_name", "last", "lastname", "lname", "surname", "family_name", "family name"],
    # A single combined name column is only used when there's no separate
    # first_name/last_name -- see the split logic in import_prospect_file().
    "full_name": ["name", "full_name", "fullname", "contact_name", "contact name", "prospect_name", "lead_name"],
    "email": ["email", "email_address", "e-mail"],
    "company": ["company", "company_name", "organization", "employer"],
    "phone": ["phone", "phone_number", "telephone", "mobile"],
}

# Second pass, only for canonical fields an exact whole-header match above
# missed -- catches a recognizable header buried in extra words ("Contact
# First Name", "Full Name (required)") by substring instead of exact
# match. Deliberately a separate, narrower list: bare tokens like "first"/
# "last"/"name" are exact-match-only up above, because as a *substring*
# check they false-positive constantly ("Last Updated", "Company Name",
# "Display Name" for something unrelated all contain one of those).
FALLBACK_SUBSTRINGS = {
    "first_name": ["first_name", "firstname", "given_name", "forename"],
    "last_name": ["last_name", "lastname", "surname", "family_name"],
    "full_name": ["full_name", "fullname", "contact_name", "prospect_name", "lead_name"],
}


class ImportError_(Exception):
    pass


def _map_columns(columns: list[str]) -> dict[str, str]:
    """Return {canonical_name: source_column_name} for whatever we can match.
    Exact whole-header match first (safest), then a narrower substring
    fallback (FALLBACK_SUBSTRINGS) for headers with extra words around the
    recognizable part -- see that constant's docstring for why it's kept
    separate from the exact-match alias lists rather than folded in."""
    normalized = {c: c.strip().lower().replace(" ", "_") for c in columns}
    mapping = {}
    for canonical, aliases in CANONICAL_COLUMNS.items():
        for source_col, norm in normalized.items():
            if norm in aliases:
                mapping[canonical] = source_col
                break

    mapped_source_cols = set(mapping.values())
    for canonical, substrings in FALLBACK_SUBSTRINGS.items():
        if canonical in mapping:
            continue
        for source_col, norm in normalized.items():
            if source_col in mapped_source_cols:
                continue
            if any(s in norm for s in substrings):
                mapping[canonical] = source_col
                mapped_source_cols.add(source_col)
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

    # A combined "Name" column only gets used when there's no separate
    # first_name/last_name -- an explicit split always wins over a guess at
    # splitting free text, so a file with both isn't second-guessed.
    use_full_name = "full_name" in mapping and "first_name" not in mapping and "last_name" not in mapping

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

            if use_full_name:
                # Splits on the first run of whitespace: "Jane van der Berg"
                # -> first="Jane", last="van der Berg" -- the surname stays
                # intact rather than losing everything past the second word.
                full = get("full_name") or ""
                parts = full.split(None, 1)
                first_name = parts[0] if parts else None
                last_name = parts[1] if len(parts) > 1 else None
            else:
                first_name, last_name = get("first_name"), get("last_name")

            conn.execute(
                """INSERT INTO prospects_raw
                   (batch_id, row_number, first_name, last_name, email, company, phone, status)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'Pending')""",
                (batch_id, i + 1, first_name, last_name,
                 get("email"), get("company"), get("phone")),
            )

    log_event("prospect_import", "batch", batch_id, f"Imported {len(df)} rows from '{filename}'")

    return ImportSummary(
        batch_id=batch_id,
        filename=filename,
        row_count=len(df),
        columns_mapped=mapping,
    )
