"""
OBJ-002 Prospect Validation
Validate data, duplicates and existing customers.

Rules applied, in order, first failure wins per row:
  1. Required fields present (email, and at least one of first/last name)
  2. Email format valid
  3. Duplicate within the same import batch (same email seen twice)
  4. Already an existing customer (matched against customers table)
Anything surviving all four is marked 'Valid'.
"""
import re

from app.db import get_conn
from app.integrations.customer_provider import ACTIVE_PROVIDER
from app.models import ValidationSummary
from app.services.audit import log_event

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_batch(batch_id: str) -> ValidationSummary:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM prospects_raw WHERE batch_id = ? ORDER BY row_number",
            (batch_id,),
        ).fetchall()

        if not rows:
            raise ValueError(f"No rows found for batch_id '{batch_id}'")

        # OBJ-002 integration point: real customer matching source, not a hardcode.
        customer_emails = ACTIVE_PROVIDER.get_customer_emails()

        seen_emails: set[str] = set()
        counts = {"Valid": 0, "Invalid": 0, "Duplicate": 0, "Existing Customer": 0}

        for row in rows:
            status, note = _evaluate_row(row, seen_emails, customer_emails)
            counts[status] += 1
            if status not in ("Invalid",) and row["email"]:
                seen_emails.add(row["email"].lower())

            conn.execute(
                "UPDATE prospects_raw SET status = ?, validation_notes = ? WHERE id = ?",
                (status, note, row["id"]),
            )

    log_event(
        "prospect_validation", "batch", batch_id,
        f"Valid={counts['Valid']} Invalid={counts['Invalid']} Duplicate={counts['Duplicate']} ExistingCustomer={counts['Existing Customer']}"
    )

    return ValidationSummary(
        batch_id=batch_id,
        total=len(rows),
        valid=counts["Valid"],
        invalid=counts["Invalid"],
        duplicate=counts["Duplicate"],
        existing_customer=counts["Existing Customer"],
    )


def _evaluate_row(row, seen_emails: set[str], customer_emails: set[str]) -> tuple[str, str]:
    email = (row["email"] or "").strip()
    has_name = bool((row["first_name"] or "").strip() or (row["last_name"] or "").strip())

    if not email:
        return "Invalid", "Missing email address"
    if not EMAIL_RE.match(email):
        return "Invalid", f"Malformed email: '{email}'"
    if not has_name:
        return "Invalid", "Missing first and last name"
    if email.lower() in customer_emails:
        return "Existing Customer", "Email matches an existing customer record"
    if email.lower() in seen_emails:
        return "Duplicate", "Duplicate email within this import batch"

    return "Valid", ""
