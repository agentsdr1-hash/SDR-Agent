"""
Automatic follow-up cadence -- the "keep working the lead" half of the
email piece.

Most buyers don't reply to a first cold email in this industry; the value
is in the persistence, not the first send. Once a campaign email is Sent
and nothing comes back, this sends up to MAX_FOLLOW_UPS automated
follow-ups on a fixed cadence (day 4, day 8 -- both measured from the
ORIGINAL send, not cumulative from the previous follow-up) and then
stops. A genuine reply at any point cancels every further follow-up for
that row: send_due_followups() only ever looks at rows still in status
'Sent' -- the moment inbox_monitor.poll_once() (or simulate_reply) flips
a row to 'Replied', it drops out of the candidate set here for good, and
the multi-round reply-conversation flow takes over instead (see
AWAITING_REPLY_STATUSES in inbox_monitor.py).

Unlike reply drafts, these send automatically -- no human approval step.
That's deliberate: a follow-up bump is low-risk, templated, and the whole
point of automating it is to not depend on a human remembering to nudge
every lead manually. It still respects the same daily send limit and
suppression list as every other outbound path (see
email_provider.sent_today_count(), which counts 'followup_sent' events
alongside real campaign/reply sends).

send_followup_now() is the manual counterpart -- same content and limits,
but skips the day-4/day-8 wait, for an SDR who wants to nudge one specific
lead right now instead of waiting for the schedule (or for testing).
"""
from datetime import datetime, timezone, timedelta

from app.db import get_conn
from app.integrations import email_provider
from app.services.administration import is_suppressed
from app.services.audit import log_event
from app.services.campaign_management import COMPANY_NAME

FOLLOW_UP_DELAYS_DAYS = {1: 4, 2: 8}
MAX_FOLLOW_UPS = 2


class FollowUpError(Exception):
    pass


# in-memory status, reset on restart -- mirrors inbox_monitor.py's
# _last_poll_at/_last_poll_replies_found/_last_poll_error, same rationale
_last_run_at: str | None = None
_last_run_sent_count: int | None = None
_last_run_error: str | None = None


def get_status() -> dict:
    return {
        "last_followup_run_at": _last_run_at,
        "last_followup_sent_count": _last_run_sent_count,
        "last_followup_error": _last_run_error,
    }


def _followup_content(follow_up_number: int, first_name: str | None, company: str | None) -> tuple[str, str]:
    """Distinct copy per round -- a first bump stays warm and assumes the
    email was simply missed; the second is short and gives them an easy
    out, since a lead who's ignored two emails needs a lower-pressure ask,
    not a third full pitch."""
    first = first_name or "there"
    co = company or "your team"
    if follow_up_number == 1:
        subject = f"Following up — {COMPANY_NAME} Steel Supply" + (f" for {co}" if company else "")
        body = (
            f"Hi {first},\n\n"
            f"Just following up on my note below in case it slipped past — still keen to hear whether "
            f"{co} has any steel requirements coming up.\n\n"
            f"Happy to send pricing or a full stock list whenever's useful.\n\nBest,\n{COMPANY_NAME} Sales Team"
        )
    else:
        subject = f"Re: Following up — {COMPANY_NAME} Steel Supply"
        body = (
            f"Hi {first},\n\n"
            f"One last note from me — if the timing isn't right, no worries at all, just let me know and "
            f"I'll close this out. If it is, I'm happy to help whenever you're ready.\n\nBest,\n{COMPANY_NAME} Sales Team"
        )
    return subject, body


def _followup_count(conn, campaign_prospect_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM campaign_followups WHERE campaign_prospect_id = ?",
        (campaign_prospect_id,),
    ).fetchone()
    return row["c"]


def _record_and_send(row_id: int, email: str, first_name: str | None, company: str | None, follow_up_number: int):
    """Sends the real email first -- only records/logs it once Gmail
    actually accepted it, same ordering send_approved() uses, so a failed
    send never gets counted as having gone out."""
    subject, body = _followup_content(follow_up_number, first_name, company)
    email_provider.send_email(email, subject, body)

    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO campaign_followups (campaign_prospect_id, follow_up_number, subject, body, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (row_id, follow_up_number, subject, body, now),
        )
        conn.execute(
            "UPDATE campaign_prospects SET last_followup_at = ? WHERE id = ?",
            (now, row_id),
        )
    log_event("followup_sent", "campaign_prospect", str(row_id), f"Follow-up #{follow_up_number} to {email}")


def send_due_followups() -> dict:
    """Background-loop entry point: sends every follow-up that's actually
    due right now. Safe to call even if Gmail isn't configured or nothing
    is due -- returns a clean, empty result rather than raising, since
    this runs unattended on a timer."""
    global _last_run_at, _last_run_sent_count, _last_run_error
    now = datetime.now(timezone.utc)
    checked_at = now.isoformat()

    if not email_provider.is_configured():
        _last_run_at = checked_at
        _last_run_error = "Not configured -- set GMAIL_ADDRESS and GMAIL_APP_PASSWORD"
        return {"checked_at": checked_at, "sent": 0, "skipped_daily_limit": 0, "errors": []}

    with get_conn() as conn:
        candidates = conn.execute(
            """SELECT cp.id, cp.sent_at, pr.email, pr.first_name, pr.company
               FROM campaign_prospects cp
               JOIN prospects_raw pr ON pr.id = cp.prospect_id
               WHERE cp.status = 'Sent'"""
        ).fetchall()

    sent, skipped_daily_limit, errors = 0, 0, []
    remaining = email_provider.remaining_sends_today()

    for row in candidates:
        if not row["email"] or not row["sent_at"]:
            continue
        with get_conn() as conn:
            count = _followup_count(conn, row["id"])
        if count >= MAX_FOLLOW_UPS:
            continue
        next_number = count + 1
        due_at = datetime.fromisoformat(row["sent_at"]) + timedelta(days=FOLLOW_UP_DELAYS_DAYS[next_number])
        if now < due_at:
            continue
        if is_suppressed(row["email"]):
            continue
        if remaining <= 0:
            skipped_daily_limit += 1
            continue

        try:
            _record_and_send(row["id"], row["email"], row["first_name"], row["company"], next_number)
            sent += 1
            remaining -= 1
        except email_provider.EmailSendError as e:
            errors.append(f"{row['email']}: {e}")
            log_event("followup_send_failed", "campaign_prospect", str(row["id"]), f"{row['email']}: {e}")

    _last_run_at = checked_at
    _last_run_sent_count = sent
    _last_run_error = "; ".join(errors) if errors else None
    return {"checked_at": checked_at, "sent": sent, "skipped_daily_limit": skipped_daily_limit, "errors": errors}


def send_followup_now(campaign_id: int, prospect_row_id: int) -> dict:
    """Manual, single-lead counterpart to send_due_followups() -- sends
    the next follow-up immediately, skipping the day-4/day-8 wait, for an
    SDR who wants to nudge one specific lead right now (or for testing the
    cadence without waiting real days). Still enforces everything else:
    status must be 'Sent', under MAX_FOLLOW_UPS, not suppressed, Gmail
    configured, and under today's send limit."""
    email_provider.require_configured()

    with get_conn() as conn:
        row = conn.execute(
            """SELECT cp.id, cp.status, pr.email, pr.first_name, pr.company
               FROM campaign_prospects cp
               JOIN prospects_raw pr ON pr.id = cp.prospect_id
               WHERE cp.campaign_id = ? AND cp.id = ?""",
            (campaign_id, prospect_row_id),
        ).fetchone()
        if not row:
            raise FollowUpError("Campaign prospect not found")
        if row["status"] != "Sent":
            raise FollowUpError(f"Can only send a follow-up while awaiting a reply (current status: '{row['status']}')")
        count = _followup_count(conn, row["id"])

    if count >= MAX_FOLLOW_UPS:
        raise FollowUpError(f"Already sent the maximum of {MAX_FOLLOW_UPS} follow-ups for this lead")
    if not row["email"]:
        raise FollowUpError("This lead has no email on file")
    if is_suppressed(row["email"]):
        raise FollowUpError(f"{row['email']} is on the suppression list")
    if email_provider.remaining_sends_today() <= 0:
        raise FollowUpError(f"Today's send limit of {email_provider.daily_send_limit()} has been reached")

    next_number = count + 1
    _record_and_send(row["id"], row["email"], row["first_name"], row["company"], next_number)
    return {"follow_up_number": next_number, "sent_to": row["email"]}


def list_followups(campaign_prospect_id: int) -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM campaign_followups WHERE campaign_prospect_id = ? ORDER BY follow_up_number",
            (campaign_prospect_id,),
        ).fetchall()
    return [dict(r) for r in rows]
