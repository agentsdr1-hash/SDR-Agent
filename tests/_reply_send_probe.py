"""
Standalone probe (run as its own interpreter process, not imported by
pytest) for send_approved() actually sending Approved reply drafts --
same reasoning as _daily_limit_probe.py's docstring: needs a mocked
email_provider.send_email, which only works with a direct import in a
process that hasn't already cached a different app.db.DB_PATH.

Exercises what used to be a gap: reply drafts had their own separate
immediate-send path (approving one sent it on the spot) that skipped the
suppression-list check and daily-pacing cap the campaign's "Send all
approved" batch already applied to fresh outreach. Now approving a reply
only queues it (reply_drafts.approve_reply_draft), and it only actually
sends from that same batch action -- this probe checks that a suppressed
recipient's approved reply is blocked (not sent), and that replies and
outreach share one daily-pacing cap with replies going first.

Prints one JSON object to stdout: {"combined": {...}, "priority": {...}}
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db, get_conn  # noqa: E402
from app.services import settings as app_settings  # noqa: E402
from app.integrations import email_provider  # noqa: E402
from app.services.administration import add_to_suppression_list  # noqa: E402
from app.services.campaign_management import create_campaign, assign_prospect_to_campaign  # noqa: E402
from app.services.approval_and_delivery import approve, simulate_sent, simulate_reply, send_approved  # noqa: E402
from app.services.reply_drafts import approve_reply_draft  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def _seed_prospect(email, batch_id):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        existing = conn.execute("SELECT 1 FROM import_batches WHERE batch_id = ?", (batch_id,)).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO import_batches (batch_id, filename, row_count, imported_at) VALUES (?, ?, 1, ?)",
                (batch_id, f"{batch_id}.csv", now),
            )
        cur = conn.execute(
            """INSERT INTO prospects_raw (batch_id, row_number, first_name, last_name, email, company, status)
               VALUES (?, 1, 'Test', 'Prospect', ?, 'Probe Co', 'Valid') RETURNING id""",
            (batch_id, email),
        )
        return cur.fetchone()["id"]


def _row_id_for(campaign_id, prospect_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM campaign_prospects WHERE campaign_id = ? AND prospect_id = ?",
            (campaign_id, prospect_id),
        ).fetchone()
        return row["id"]


def _reply_draft_id_for(row_id):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT id FROM reply_drafts WHERE campaign_prospect_id = ? ORDER BY id DESC LIMIT 1",
            (row_id,),
        ).fetchone()
        return row["id"]


def _make_approved_reply(campaign_id, email, batch_id):
    pid = _seed_prospect(email, batch_id)
    assign_prospect_to_campaign(campaign_id, pid)
    row_id = _row_id_for(campaign_id, pid)
    approve(campaign_id, row_id)
    simulate_sent(campaign_id, row_id)
    simulate_reply(campaign_id, row_id, "Re: hi", False, "interested")
    draft_id = _reply_draft_id_for(row_id)
    approve_reply_draft(draft_id)
    return row_id, draft_id


def fake_send_email(to_address, subject, body):
    return {"to": to_address, "sent_at": datetime.now(timezone.utc).isoformat()}


def scenario_combined_send_and_suppression():
    init_db(seed_customers=False)
    app_settings.set_setting("gmail_address", "test@example.com")
    app_settings.set_setting("gmail_app_password", "x" * 16)
    email_provider.set_daily_send_limit(20)  # high enough to not interfere here

    campaign = create_campaign("Reply Send Probe A", "Mon,Tue,Wed,Thu,Fri", 25)
    cid = campaign.id

    # A normal approved reply -- should send.
    _, normal_draft_id = _make_approved_reply(cid, "normal@example.com", "probe-normal")

    # A suppressed recipient's approved reply -- must NOT send, must flip
    # to Suppressed instead, and must not count against the daily cap.
    _, suppressed_draft_id = _make_approved_reply(cid, "blocked@example.com", "probe-suppressed")
    add_to_suppression_list("blocked@example.com", reason="probe test", source="manual")

    # A fresh Approved outreach row in the same campaign, sent from the
    # same batch action.
    outreach_pid = _seed_prospect("fresh@example.com", "probe-outreach")
    assign_prospect_to_campaign(cid, outreach_pid)
    outreach_row_id = _row_id_for(cid, outreach_pid)
    approve(cid, outreach_row_id)

    with patch.object(email_provider, "send_email", side_effect=fake_send_email):
        result = send_approved(cid)

    with get_conn() as conn:
        normal = conn.execute("SELECT status, sent_at FROM reply_drafts WHERE id = ?", (normal_draft_id,)).fetchone()
        suppressed = conn.execute("SELECT status, sent_at FROM reply_drafts WHERE id = ?", (suppressed_draft_id,)).fetchone()
        outreach = conn.execute("SELECT status FROM campaign_prospects WHERE id = ?", (outreach_row_id,)).fetchone()

    return {
        "result": result.model_dump(),
        "normal_reply_status": normal["status"],
        "normal_reply_sent": normal["sent_at"] is not None,
        "suppressed_reply_status": suppressed["status"],
        "suppressed_reply_sent": suppressed["sent_at"] is not None,
        "outreach_status": outreach["status"],
    }


def scenario_replies_get_priority_under_the_daily_cap():
    init_db(seed_customers=False)
    app_settings.set_setting("gmail_address", "test@example.com")
    app_settings.set_setting("gmail_app_password", "x" * 16)
    email_provider.set_daily_send_limit(1)  # only room for one send total

    campaign = create_campaign("Reply Send Probe B", "Mon,Tue,Wed,Thu,Fri", 25)
    cid = campaign.id

    _, draft_id = _make_approved_reply(cid, "reply@example.com", "probe-priority-reply")

    outreach_pid = _seed_prospect("outreach@example.com", "probe-priority-outreach")
    assign_prospect_to_campaign(cid, outreach_pid)
    outreach_row_id = _row_id_for(cid, outreach_pid)
    approve(cid, outreach_row_id)

    with patch.object(email_provider, "send_email", side_effect=fake_send_email):
        result = send_approved(cid)

    with get_conn() as conn:
        reply = conn.execute("SELECT status FROM reply_drafts WHERE id = ?", (draft_id,)).fetchone()
        outreach = conn.execute("SELECT status FROM campaign_prospects WHERE id = ?", (outreach_row_id,)).fetchone()

    return {
        "result": result.model_dump(),
        "reply_status": reply["status"],
        "outreach_status": outreach["status"],
    }


SCENARIOS = {
    "combined": scenario_combined_send_and_suppression,
    "priority": scenario_replies_get_priority_under_the_daily_cap,
}


def main():
    # One scenario per process, each against its own fresh APEX_DB_PATH
    # (set by the caller) -- running both in one process against one DB
    # would let scenario 1's real sends bleed into scenario 2's
    # sent_today_count(), which is exactly the per-day accounting this
    # probe exists to check, so they can't share a day/DB.
    name = sys.argv[1] if len(sys.argv) > 1 else "combined"
    print(json.dumps(SCENARIOS[name]()))


if __name__ == "__main__":
    main()
