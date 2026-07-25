"""
OBJ-015 Administration Console.
Phase 1 scope: opt-out / blacklist management -- the "Opt-Out/Unsubscribe"
exception path from the pilot lifecycle diagram. Once suppressed, an email
is permanently excluded from any future send, checked at the last possible
moment (right before OBJ-006 actually sends) so nothing can slip through.
"""
from datetime import datetime, timezone

from app.db import get_conn
from app.models import SuppressionEntry
from app.services.audit import log_event

OPT_OUT_KEYWORDS = [
    "unsubscribe", "opt out", "opt-out", "remove me", "take me off",
    "stop emailing", "no longer interested", "do not contact", "don't contact",
]


class AdminError(Exception):
    pass


def is_suppressed(email: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM suppressed_emails WHERE email = ?", (email.lower(),)
        ).fetchone()
    return row is not None


def add_to_suppression_list(email: str, reason: str | None = None, source: str = "manual"):
    email = email.strip().lower()
    if not email:
        raise AdminError("Email is required")
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO suppressed_emails (email, reason, source, added_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(email) DO UPDATE SET reason=excluded.reason, source=excluded.source""",
            (email, reason, source, datetime.now(timezone.utc).isoformat()),
        )
    log_event("email_suppressed", "suppression", email, f"reason={reason}, source={source}")


def remove_from_suppression_list(email: str):
    email = email.strip().lower()
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM suppressed_emails WHERE email = ?", (email,))
        if cur.rowcount == 0:
            raise AdminError(f"'{email}' is not on the suppression list")
    log_event("email_unsuppressed", "suppression", email)


def list_suppressed() -> list[SuppressionEntry]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM suppressed_emails ORDER BY added_at DESC"
        ).fetchall()
    return [SuppressionEntry(**dict(r)) for r in rows]


def detect_opt_out(text: str) -> bool:
    """Rule-based keyword scan, same pattern as OBJ-010's qualification
    extraction. Upgrade to LLM-based intent detection later for messier
    real-world replies -- this catches the common explicit phrasings."""
    lower = (text or "").lower()
    return any(kw in lower for kw in OPT_OUT_KEYWORDS)


# Deleted in dependency order (children before the tables they reference)
# so foreign keys never block a row from clearing. Deliberately NOT
# cleared: kb_entries and stock_catalog (business content, not test lead
# data -- re-entering the steel gratings Q&A or re-importing the product
# catalog after every reset would be needless busywork) and app_settings
# (Gmail credentials, daily send limit -- configuration, not lead data;
# losing Gmail config on every reset would be actively harmful before a
# go-live). audit_log IS cleared even though it's not "lead data" per se
# -- its rows reference the very leads/campaigns/sends being wiped, so
# keeping old entries around after their subjects are gone would just be
# confusing orphaned history, not a useful record.
RESET_TABLES = [
    "reply_drafts",
    "campaign_prospects",
    "campaigns",
    "prospects_raw",
    "import_batches",
    "suppressed_emails",
    "customers",
    "audit_log",
]


def reset_all_data():
    """Wipe every table of lead/campaign/outreach data -- e.g. clearing
    out test data before a production go-live so it never mixes with
    real leads. Permanent, not a soft-delete: there is no undo. Gmail
    configuration, the daily send limit, the KB Q&A entries, and the
    stock catalog are preserved -- see RESET_TABLES' comment for why."""
    with get_conn() as conn:
        for table in RESET_TABLES:
            conn.execute(f"DELETE FROM {table}")
    # Logged after the audit_log wipe above (own connection, so it lands
    # as the first row of a clean log) -- a permanent marker that this
    # happened, when, even though everything it could have cross-referenced
    # is now gone.
    log_event("data_reset", None, None, "All lead/campaign/outreach data cleared via Admin reset")
