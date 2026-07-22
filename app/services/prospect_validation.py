"""
OBJ-002 Prospect Validation
Validate data, duplicates and existing customers.

Rules applied, in order, first failure wins per row:
  1. Required fields present (email, and at least one of first/last name)
  2. Email format valid
  3. Already an existing customer (matched against customers table)
  4. Already contacted (email was actually sent an outreach email in any
     prior campaign/batch, not just this one -- email is the key, since a
     prospect can come back through a re-imported file with a new batch_id)
  5. Duplicate within the same import batch (same email seen twice here)
Anything surviving all five is marked 'Valid'.
"""
import re

from app.db import get_conn
from app.integrations.customer_provider import ACTIVE_PROVIDER
from app.models import ValidationSummary
from app.services.audit import log_event

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _get_contacted_emails(conn) -> set[str]:
    """Every email that has actually had an outreach email sent to it, in
    any campaign, ever -- not scoped to the current batch. This is what lets
    a re-imported file (new batch_id, same prospect) get caught instead of
    validating as a fresh 'Valid' lead."""
    rows = conn.execute(
        """SELECT DISTINCT pr.email
           FROM campaign_prospects cp
           JOIN prospects_raw pr ON pr.id = cp.prospect_id
           WHERE cp.sent_at IS NOT NULL"""
    ).fetchall()
    return {r["email"].lower() for r in rows if r["email"]}


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
        contacted_emails = _get_contacted_emails(conn)

        seen_emails: set[str] = set()
        counts = {"Valid": 0, "Invalid": 0, "Duplicate": 0, "Existing Customer": 0, "Already Contacted": 0}

        for row in rows:
            status, note = _evaluate_row(row, seen_emails, customer_emails, contacted_emails)
            counts[status] += 1
            if status not in ("Invalid",) and row["email"]:
                seen_emails.add(row["email"].lower())

            conn.execute(
                "UPDATE prospects_raw SET status = ?, validation_notes = ? WHERE id = ?",
                (status, note, row["id"]),
            )

    log_event(
        "prospect_validation", "batch", batch_id,
        f"Valid={counts['Valid']} Invalid={counts['Invalid']} Duplicate={counts['Duplicate']} "
        f"ExistingCustomer={counts['Existing Customer']} AlreadyContacted={counts['Already Contacted']}"
    )

    return ValidationSummary(
        batch_id=batch_id,
        total=len(rows),
        valid=counts["Valid"],
        invalid=counts["Invalid"],
        duplicate=counts["Duplicate"],
        existing_customer=counts["Existing Customer"],
        already_contacted=counts["Already Contacted"],
    )


def edit_prospect(prospect_id: int, first_name: str, last_name: str, email: str,
                   company: str, phone: str) -> dict:
    """Correct a prospect's own data (e.g. a missing/malformed email caught
    by validation) and re-run this one row through the same rules
    validate_batch() uses, so it can move from Invalid to Valid (or the
    reverse, if the edit breaks something) without re-validating the whole
    batch. Duplicate-within-batch is checked against the batch's other
    non-Invalid rows, same as the original batch pass."""
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM prospects_raw WHERE id = ?", (prospect_id,)).fetchone()
        if not row:
            raise ValueError(f"Prospect {prospect_id} not found")

        customer_emails = ACTIVE_PROVIDER.get_customer_emails()
        contacted_emails = _get_contacted_emails(conn)
        batchmates = conn.execute(
            "SELECT email FROM prospects_raw WHERE batch_id = ? AND id != ? AND status != 'Invalid'",
            (row["batch_id"], prospect_id),
        ).fetchall()
        seen_emails = {r["email"].strip().lower() for r in batchmates if r["email"]}

        updated = {**dict(row), "first_name": first_name, "last_name": last_name,
                   "email": email, "company": company, "phone": phone}
        status, note = _evaluate_row(updated, seen_emails, customer_emails, contacted_emails)

        conn.execute(
            """UPDATE prospects_raw SET first_name = ?, last_name = ?, email = ?, company = ?,
               phone = ?, status = ?, validation_notes = ? WHERE id = ?""",
            (first_name, last_name, email, company, phone, status, note, prospect_id),
        )

    log_event("prospect_edited", "prospect", str(prospect_id), f"Re-validated as {status}")
    return {"id": prospect_id, "status": status, "validation_notes": note}


def _evaluate_row(row, seen_emails: set[str], customer_emails: set[str], contacted_emails: set[str]) -> tuple[str, str]:
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
    if email.lower() in contacted_emails:
        return "Already Contacted", "An outreach email was already sent to this address in a prior campaign"
    if email.lower() in seen_emails:
        return "Duplicate", "Duplicate email within this import batch"

    return "Valid", ""
