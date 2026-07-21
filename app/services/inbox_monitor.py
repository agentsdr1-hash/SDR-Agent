"""
OBJ-016 Inbox monitoring.

Polls Gmail for unread messages from anyone we've sent to and are still
awaiting a reply from, and flips their campaign_prospects status to
'Replied'. This is the "receive" half of email integration -- pairs with
approval_and_delivery.send_approved() for the "send" half.

Called on a timer from main.py's background task, and also exposed as a
manual POST /email/poll-now endpoint for testing without waiting.
"""
from datetime import datetime, timezone

from app.db import get_conn
from app.integrations import email_provider
from app.models import PollResult
from app.services.administration import detect_opt_out, add_to_suppression_list
from app.services.audit import log_event
from app.services import kb_qa

# in-memory status, reset on restart -- fine for a pilot; move to a DB table
# if you need poll history to survive restarts
_last_poll_at: str | None = None
_last_poll_replies_found: int | None = None
_last_poll_error: str | None = None


def get_status() -> dict:
    return {
        "last_poll_at": _last_poll_at,
        "last_poll_replies_found": _last_poll_replies_found,
        "last_poll_error": _last_poll_error,
    }


def poll_once() -> PollResult:
    """Check for replies and update matching prospects. Safe to call even if
    not configured -- returns a clean error rather than raising, since this
    is meant to be called from a background loop that shouldn't crash."""
    global _last_poll_at, _last_poll_replies_found, _last_poll_error
    now = datetime.now(timezone.utc).isoformat()

    if not email_provider.is_configured():
        _last_poll_at = now
        _last_poll_error = "Not configured -- set GMAIL_ADDRESS and GMAIL_APP_PASSWORD"
        return PollResult(checked_at=now, replies_found=0, updated_prospects=[])

    with get_conn() as conn:
        awaiting = conn.execute(
            """SELECT cp.id, pr.email, pr.first_name, pr.company
               FROM campaign_prospects cp
               JOIN prospects_raw pr ON pr.id = cp.prospect_id
               WHERE cp.status = 'Sent'"""
        ).fetchall()

    email_to_row_ids = {}
    row_context = {}  # row_id -> (first_name, company), for drafting a reply
    for row in awaiting:
        email_to_row_ids.setdefault(row["email"].lower(), []).append(row["id"])
        row_context[row["id"]] = (row["first_name"], row["company"])

    if not email_to_row_ids:
        _last_poll_at = now
        _last_poll_replies_found = 0
        _last_poll_error = None
        return PollResult(checked_at=now, replies_found=0, updated_prospects=[])

    try:
        replies = email_provider.check_for_replies(set(email_to_row_ids.keys()))
    except email_provider.EmailSendError as e:
        _last_poll_at = now
        _last_poll_error = str(e)
        return PollResult(checked_at=now, replies_found=0, updated_prospects=[])

    updated = []
    if replies:
        with get_conn() as conn:
            for reply in replies:
                row_ids = email_to_row_ids.get(reply["from_email"], [])
                is_opt_out = detect_opt_out(reply.get("subject", "") + " " + reply.get("body_snippet", ""))
                for row_id in row_ids:
                    if is_opt_out:
                        conn.execute(
                            """UPDATE campaign_prospects
                               SET status = 'Suppressed', replied_at = ?, reply_subject = ?
                               WHERE id = ? AND status = 'Sent'""",
                            (reply["received_at"], reply["subject"], row_id),
                        )
                    else:
                        conn.execute(
                            """UPDATE campaign_prospects
                               SET status = 'Replied', replied_at = ?, reply_subject = ?
                               WHERE id = ? AND status = 'Sent'""",
                            (reply["received_at"], reply["subject"], row_id),
                        )
                if is_opt_out:
                    add_to_suppression_list(reply["from_email"], reason="Opt-out detected in reply", source="auto-detected")
                    log_event("opt_out_detected", "campaign_prospect", str(row_ids[0]) if row_ids else None,
                               f"{reply['from_email']}: opt-out language detected in reply")
                else:
                    log_event("reply_received", "campaign_prospect", str(row_ids[0]) if row_ids else None,
                               f"{reply['from_email']}")
                    for row_id in row_ids:
                        first_name, company = row_context.get(row_id, (None, None))
                        kb_qa.create_reply_draft(
                            row_id, first_name, company,
                            reply.get("subject"), reply.get("body_snippet", ""),
                        )
                updated.append(reply["from_email"])

    _last_poll_at = now
    _last_poll_replies_found = len(replies)
    _last_poll_error = None
    return PollResult(checked_at=now, replies_found=len(replies), updated_prospects=updated)
