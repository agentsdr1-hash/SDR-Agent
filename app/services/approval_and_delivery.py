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
from app.services.administration import is_suppressed, add_to_suppression_list
from app.services.audit import log_event
from app.services import kb_qa

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
    skipped_daily_limit = 0
    errors = []
    now = datetime.now(timezone.utc).isoformat()
    # Checked once up front rather than attempted-and-caught per row -- once
    # the cap is hit, the remaining approved drafts are simply left Approved
    # (not marked failed) so they send on the next run rather than needing
    # to be re-approved.
    remaining = email_provider.remaining_sends_today()

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
        if remaining <= 0:
            skipped_daily_limit += 1
            continue
        try:
            email_provider.send_email(row["email"], row["subject"], row["body"])
            with get_conn() as conn:
                conn.execute(
                    "UPDATE campaign_prospects SET status = 'Sent', sent_at = ? WHERE id = ?",
                    (now, row["id"]),
                )
            sent += 1
            remaining -= 1
            log_event("email_sent", "campaign_prospect", str(row["id"]), f"Sent to {row['email']}")
        except email_provider.EmailSendError as e:
            failed += 1
            errors.append(f"{row['email']}: {e}")
            log_event("email_send_failed", "campaign_prospect", str(row["id"]), f"{row['email']}: {e}")

    if skipped_daily_limit:
        errors.append(
            f"{skipped_daily_limit} draft(s) left Approved — today's send limit of "
            f"{email_provider.daily_send_limit()} reached. They'll send next run, or raise the limit in Admin."
        )

    return SendResult(campaign_id=campaign_id, attempted=attempted, sent=sent, failed=failed,
                       suppressed=suppressed, skipped_daily_limit=skipped_daily_limit, errors=errors)


def _get_status(conn, campaign_id: int, prospect_row_id: int) -> str:
    row = conn.execute(
        "SELECT status FROM campaign_prospects WHERE campaign_id = ? AND id = ?",
        (campaign_id, prospect_row_id),
    ).fetchone()
    if not row:
        raise ApprovalError("Campaign prospect not found")
    return row["status"]


def simulate_sent(campaign_id: int, prospect_row_id: int):
    """QA/testing only -- flips an Approved draft straight to Sent without
    touching Gmail at all, so the downstream funnel (Replied/QuoteRequested/
    Won/Lost) and the dashboard math behind it can be exercised end to end
    before real GMAIL_ADDRESS/GMAIL_APP_PASSWORD credentials exist. No email
    leaves this server -- audit-logged distinctly from a real send so the
    two are never confused later."""
    with get_conn() as conn:
        status = _get_status(conn, campaign_id, prospect_row_id)
        if status != "Approved":
            raise ApprovalError(f"Can only simulate-send from status 'Approved' (current: '{status}')")
        conn.execute(
            "UPDATE campaign_prospects SET status = 'Sent', sent_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), prospect_row_id),
        )
    log_event("email_sent_simulated", "campaign_prospect", str(prospect_row_id),
               f"Campaign {campaign_id} -- TEST MODE, no real email was sent")


def simulate_reply(campaign_id: int, prospect_row_id: int, reply_subject: str | None, is_opt_out: bool, reply_body: str | None = None):
    """QA/testing only -- mimics what the real inbox poller (inbox_monitor.py)
    would do on a matching reply, without an actual inbox: flips a Sent
    prospect to Replied (or Suppressed, if simulating an opt-out), stamping
    the same fields a real detected reply would, and -- for a non-opt-out
    reply -- generates a smart-reply draft from reply_body exactly like a
    real detected reply would, so the KB/stock matching can be tested
    without Gmail."""
    with get_conn() as conn:
        status = _get_status(conn, campaign_id, prospect_row_id)
        if status != "Sent":
            raise ApprovalError(f"Can only simulate a reply from status 'Sent' (current: '{status}')")
        now = datetime.now(timezone.utc).isoformat()
        subject = reply_subject or ("Please unsubscribe" if is_opt_out else "Re: Quick question")
        new_status = "Suppressed" if is_opt_out else "Replied"
        row = conn.execute(
            """SELECT pr.email, pr.first_name, pr.company FROM campaign_prospects cp
               JOIN prospects_raw pr ON pr.id = cp.prospect_id WHERE cp.id = ?""",
            (prospect_row_id,),
        ).fetchone()
        conn.execute(
            "UPDATE campaign_prospects SET status = ?, replied_at = ?, reply_subject = ? WHERE id = ?",
            (new_status, now, subject, prospect_row_id),
        )

    if is_opt_out:
        add_to_suppression_list(row["email"], reason="Opt-out detected in simulated reply", source="auto-detected")
        log_event("opt_out_detected", "campaign_prospect", str(prospect_row_id),
                   f"{row['email']}: opt-out language detected -- TEST MODE, no real email was received")
    else:
        log_event("reply_received", "campaign_prospect", str(prospect_row_id),
                   f"{row['email']} -- TEST MODE, no real email was received")
        kb_qa.create_reply_draft(prospect_row_id, row["first_name"], row["company"], subject, reply_body or "")
