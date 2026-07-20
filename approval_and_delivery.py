"""
OBJ-005 Approval Workflow + OBJ-006 Email Delivery.

Approval is just a status flip + optional draft edit -- no email infrastructure
touched here. Sending is where OBJ-016 (email_provider) gets called for real.
Until GMAIL_ADDRESS/GMAIL_APP_PASSWORD are set, send_approved() fails cleanly
with EmailNotConfiguredError rather than silently pretending to send.
"""
from datetime import datetime, timezone

from app.db import get_conn
from app.integrations import email_provider
from app.models import SendResult
from app.services.administration import is_suppressed
from app.services.audit import log_event

VALID_TRANSITIONS_TO_APPROVE = {"Queued"}
VALID_TRANSITIONS_TO_REJECT = {"Queued"}


class ApprovalError(Exception):
    pass


def update_draft(campaign_id: int, prospect_row_id: int, subject: str, body: str):
    """Edit the subject/body of a still-Queued draft before approving it."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM campaign_prospects WHERE campaign_id = ? AND id = ?",
            (campaign_id, prospect_row_id),
        ).fetchone()
        if not row:
            raise ApprovalError("Campaign prospect not found")
        if row["status"] != "Queued":
            raise ApprovalError(f"Can only edit drafts while Queued (current status: {row['status']})")
        conn.execute(
            "UPDATE campaign_prospects SET subject = ?, body = ? WHERE id = ?",
            (subject, body, prospect_row_id),
        )


def approve(campaign_id: int, prospect_row_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM campaign_prospects WHERE campaign_id = ? AND id = ?",
            (campaign_id, prospect_row_id),
        ).fetchone()
        if not row:
            raise ApprovalError("Campaign prospect not found")
        if row["status"] not in VALID_TRANSITIONS_TO_APPROVE:
            raise ApprovalError(f"Cannot approve from status '{row['status']}'")
        conn.execute(
            "UPDATE campaign_prospects SET status = 'Approved', approved_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), prospect_row_id),
        )
    log_event("draft_approved", "campaign_prospect", str(prospect_row_id), f"Campaign {campaign_id}")


def reject(campaign_id: int, prospect_row_id: int):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM campaign_prospects WHERE campaign_id = ? AND id = ?",
            (campaign_id, prospect_row_id),
        ).fetchone()
        if not row:
            raise ApprovalError("Campaign prospect not found")
        if row["status"] not in VALID_TRANSITIONS_TO_REJECT:
            raise ApprovalError(f"Cannot reject from status '{row['status']}'")
        conn.execute(
            "UPDATE campaign_prospects SET status = 'Rejected' WHERE id = ?",
            (prospect_row_id,),
        )
    log_event("draft_rejected", "campaign_prospect", str(prospect_row_id), f"Campaign {campaign_id}")


def send_approved(campaign_id: int) -> SendResult:
    """Actually send every Approved prospect in this campaign via Gmail.
    Raises EmailNotConfiguredError immediately (before touching anything) if
    credentials aren't set, so a half-sent batch never happens because of
    missing config."""
    email_provider.require_configured()  # fail fast, before sending any

    with get_conn() as conn:
        rows = conn.execute(
            """SELECT cp.id, cp.subject, cp.body, pr.email
               FROM campaign_prospects cp
               JOIN prospects_raw pr ON pr.id = cp.prospect_id
               WHERE cp.campaign_id = ? AND cp.status = 'Approved'""",
            (campaign_id,),
        ).fetchall()

    attempted = len(rows)
    sent = 0
    failed = 0
    suppressed = 0
    errors = []
    now = datetime.now(timezone.utc).isoformat()

    for row in rows:
        if is_suppressed(row["email"]):
            with get_conn() as conn:
                conn.execute(
                    "UPDATE campaign_prospects SET status = 'Suppressed' WHERE id = ?",
                    (row["id"],),
                )
            suppressed += 1
            errors.append(f"{row['email']}: skipped — on suppression list, not sent")
            log_event("send_blocked_suppressed", "campaign_prospect", str(row["id"]), f"{row['email']} is on suppression list")
            continue
        try:
            email_provider.send_email(row["email"], row["subject"], row["body"])
            with get_conn() as conn:
                conn.execute(
                    "UPDATE campaign_prospects SET status = 'Sent', sent_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
            sent += 1
            log_event("email_sent", "campaign_prospect", str(row["id"]), f"Sent to {row['email']}")
        except email_provider.EmailSendError as e:
            failed += 1
            errors.append(f"{row['email']}: {e}")
            log_event("email_send_failed", "campaign_prospect", str(row["id"]), f"{row['email']}: {e}")

    return SendResult(campaign_id=campaign_id, attempted=attempted, sent=sent, failed=failed, suppressed=suppressed, errors=errors)
