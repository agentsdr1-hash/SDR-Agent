"""
Review queue for smart-reply drafts (app/services/kb_qa.py generates
them). Same approve/reject/send shape as outbound drafts in
approval_and_delivery.py -- a draft only ever leaves this server after a
human approves it, and approval only succeeds if Gmail is actually
configured. Nothing here pretends to send.
"""
from datetime import datetime, timezone

from app.db import get_conn
from app.integrations import email_provider
from app.services.audit import log_event

VALID_FOR_APPROVE = {"Draft"}
VALID_FOR_REJECT = {"Draft"}


class ReplyDraftError(Exception):
    pass


def list_reply_drafts(status: str | None = None) -> list[dict]:
    query = """SELECT rd.*, pr.first_name, pr.last_name, pr.email, pr.company
               FROM reply_drafts rd
               JOIN campaign_prospects cp ON cp.id = rd.campaign_prospect_id
               JOIN prospects_raw pr ON pr.id = cp.prospect_id"""
    params = []
    if status:
        query += " WHERE rd.status = ?"
        params.append(status)
    query += " ORDER BY rd.created_at DESC"
    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def _get_status(conn, draft_id: int) -> tuple[str, str, str | None]:
    row = conn.execute(
        """SELECT rd.status, pr.email
           FROM reply_drafts rd
           JOIN campaign_prospects cp ON cp.id = rd.campaign_prospect_id
           JOIN prospects_raw pr ON pr.id = cp.prospect_id
           WHERE rd.id = ?""",
        (draft_id,),
    ).fetchone()
    if not row:
        raise ReplyDraftError("Reply draft not found")
    return row["status"], row["email"], row


def update_reply_draft(draft_id: int, subject: str, body: str):
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM reply_drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            raise ReplyDraftError("Reply draft not found")
        if row["status"] != "Draft":
            raise ReplyDraftError(f"Can only edit a draft while status is 'Draft' (current: '{row['status']}')")
        conn.execute("UPDATE reply_drafts SET subject = ?, body = ? WHERE id = ?", (subject, body, draft_id))


def reject_reply_draft(draft_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT status FROM reply_drafts WHERE id = ?", (draft_id,)).fetchone()
        if not row:
            raise ReplyDraftError("Reply draft not found")
        if row["status"] not in VALID_FOR_REJECT:
            raise ReplyDraftError(f"Cannot reject from status '{row['status']}'")
        conn.execute(
            "UPDATE reply_drafts SET status = 'Rejected', rejected_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), draft_id),
        )
    log_event("reply_draft_rejected", "reply_draft", str(draft_id), None)


def approve_and_send_reply_draft(draft_id: int):
    """Sends the approved reply via Gmail. Raises EmailNotConfiguredError
    (before touching anything) if credentials aren't set."""
    email_provider.require_configured()

    with get_conn() as conn:
        row = conn.execute(
            """SELECT rd.status, rd.subject, rd.body, pr.email
               FROM reply_drafts rd
               JOIN campaign_prospects cp ON cp.id = rd.campaign_prospect_id
               JOIN prospects_raw pr ON pr.id = cp.prospect_id
               WHERE rd.id = ?""",
            (draft_id,),
        ).fetchone()
        if not row:
            raise ReplyDraftError("Reply draft not found")
        if row["status"] not in VALID_FOR_APPROVE:
            raise ReplyDraftError(f"Cannot approve from status '{row['status']}'")

    now = datetime.now(timezone.utc).isoformat()
    try:
        email_provider.send_email(row["email"], row["subject"], row["body"])
    except email_provider.EmailSendError as e:
        log_event("reply_send_failed", "reply_draft", str(draft_id), f"{row['email']}: {e}")
        raise

    with get_conn() as conn:
        conn.execute(
            "UPDATE reply_drafts SET status = 'Sent', approved_at = ?, sent_at = ? WHERE id = ?",
            (now, now, draft_id),
        )
    log_event("reply_sent", "reply_draft", str(draft_id), f"Sent to {row['email']}")
