"""
Standalone probe (run as its own interpreter process, not imported by
pytest) for the automated follow-up cadence in app/services/followups.py.
Same reasoning as _daily_limit_probe.py's docstring: needs to mock
email_provider.send_email to avoid a real Gmail SMTP call, and app.db.
DB_PATH is fixed at import time, so a fresh process per run keeps this
isolated from pytest's own process and from other tests.

Each scenario uses its own campaign so they don't interfere with each
other inside the one shared probe DB. simulate_sent() flips a row to
'Sent' without touching Gmail; sent_at is then backdated directly via SQL
to simulate "N days ago" without actually waiting.

Gmail credentials are deliberately NOT configured until scenario 6 --
send_due_followups() only drafts now, it never sends, so scenarios 1-5
prove that drafting works with no Gmail configured at all (the whole
point of moving it outside the `is_configured()` gate in main.py's
background loop).

Prints one JSON object to stdout with one key per scenario.
"""
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db, get_conn  # noqa: E402
from app.services import settings as app_settings  # noqa: E402
from app.integrations import email_provider  # noqa: E402
from app.services.campaign_management import create_campaign, assign_prospect_to_campaign  # noqa: E402
from app.services.approval_and_delivery import approve, simulate_sent, simulate_reply, send_approved  # noqa: E402
from app.services.administration import add_to_suppression_list  # noqa: E402
from app.services import followups  # noqa: E402


_row_counter = [0]


def _seed_prospect(email, company="Probe Co"):
    _row_counter[0] += 1
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO prospects_raw (batch_id, row_number, first_name, last_name, email, company, status)
               VALUES ('probe-batch', ?, 'Probe', 'Person', ?, ?, 'Valid') RETURNING id""",
            (_row_counter[0], email, company),
        )
        return cur.fetchone()["id"]


def _sent_row(campaign_name, email, days_ago=None):
    """Creates a campaign+prospect, walks it to 'Sent' via simulate_sent
    (no real Gmail), and optionally backdates sent_at by days_ago days."""
    campaign = create_campaign(campaign_name, "Mon,Tue,Wed,Thu,Fri", 25)
    pid = _seed_prospect(email)
    assign_prospect_to_campaign(campaign.id, pid)
    with get_conn() as conn:
        row_id = conn.execute(
            "SELECT id FROM campaign_prospects WHERE campaign_id = ? AND prospect_id = ?", (campaign.id, pid)
        ).fetchone()["id"]
    approve(campaign.id, row_id)
    simulate_sent(campaign.id, row_id)
    if days_ago is not None:
        backdated = (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat()
        with get_conn() as conn:
            conn.execute("UPDATE campaign_prospects SET sent_at = ? WHERE id = ?", (backdated, row_id))
    return campaign.id, row_id


def fake_send_email(to_address, subject, body):
    return {"to": to_address, "sent_at": datetime.now(timezone.utc).isoformat()}


def _followup_status(row_id):
    with get_conn() as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT follow_up_number, status, sent_at FROM campaign_followups "
                "WHERE campaign_prospect_id = ? ORDER BY follow_up_number", (row_id,)
            ).fetchall()
        ]


def main():
    init_db(seed_customers=False)

    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO import_batches (batch_id, filename, row_count, imported_at) VALUES ('probe-batch', 'probe.csv', 1, ?)",
            (now,),
        )

    results = {}

    # --- Scenario 1: cadence gating -- day 5 gets #1 drafted but not #2,
    # day 9 gets #2 drafted too. No Gmail configured at all -- drafting
    # doesn't need it.
    cid1, row1 = _sent_row("Cadence Test", "cadence@probe.example", days_ago=5)
    r1a = followups.send_due_followups()
    after_day5 = _followup_status(row1)
    with get_conn() as conn:
        conn.execute("UPDATE campaign_prospects SET sent_at = ? WHERE id = ?",
                     ((datetime.now(timezone.utc) - timedelta(days=9)).isoformat(), row1))
    r1b = followups.send_due_followups()
    after_day9 = _followup_status(row1)
    results["cadence"] = {
        "after_day5_numbers": [r["follow_up_number"] for r in after_day5],
        "after_day5_all_drafts": all(r["status"] == "Draft" and r["sent_at"] is None for r in after_day5),
        "after_day9_numbers": [r["follow_up_number"] for r in after_day9],
        "run1_drafted": r1a["drafted"], "run2_drafted": r1b["drafted"],
    }

    # --- Scenario 2: a reply cancels further follow-ups
    cid2, row2 = _sent_row("Reply Cancels Test", "replycancel@probe.example", days_ago=9)
    simulate_reply(cid2, row2, "Re: hi", False, "stop emailing me follow-ups, I replied")
    r2 = followups.send_due_followups()
    with get_conn() as conn:
        count2 = conn.execute(
            "SELECT COUNT(*) c FROM campaign_followups WHERE campaign_prospect_id = ?", (row2,)
        ).fetchone()["c"]
    results["reply_cancels"] = {"followup_count_after_reply": count2, "run_drafted": r2["drafted"]}

    # --- Scenario 3: max 2 cap -- each pass advances by at most one
    # follow-up draft per lead (even though both windows are already
    # overdue here), and once both are drafted, a further pass drafts
    # nothing more -- a pending (unapproved) draft still occupies its slot,
    # so this also proves a due follow-up isn't re-drafted every poll cycle.
    cid3, row3 = _sent_row("Max Cap Test", "maxcap@probe.example", days_ago=9)
    followups.send_due_followups()  # drafts #1 only
    count_after_first_pass = len(_followup_status(row3))
    r3b = followups.send_due_followups()  # drafts #2
    count_after_second_pass = len(_followup_status(row3))
    r3c = followups.send_due_followups()  # already at the cap -- nothing left to draft
    results["max_cap"] = {
        "count_after_first_pass": count_after_first_pass,
        "count_after_second_pass": count_after_second_pass,
        "second_pass_drafted": r3b["drafted"],
        "third_pass_drafted": r3c["drafted"],
    }

    # --- Scenario 4: suppressed email never gets a follow-up drafted
    cid4, row4 = _sent_row("Suppressed Test", "suppressed@probe.example", days_ago=9)
    add_to_suppression_list("suppressed@probe.example", reason="test", source="manual")
    r4 = followups.send_due_followups()
    with get_conn() as conn:
        count4 = conn.execute(
            "SELECT COUNT(*) c FROM campaign_followups WHERE campaign_prospect_id = ?", (row4,)
        ).fetchone()["c"]
    results["suppressed"] = {"followup_count": count4, "run_drafted_at_least_one": r4["drafted"] > 0}

    # --- Scenario 5: approve + send_approved() -- the actual send path now,
    # separate from drafting. Also proves the daily send limit applies at
    # send time, not draft time: draft, approve, cap the limit to 0, send
    # -- left Approved, not sent; raise the limit, send again -- goes out.
    with patch.object(email_provider, "send_email", side_effect=fake_send_email):
        app_settings.set_setting("gmail_address", "test@example.com")
        app_settings.set_setting("gmail_app_password", "x" * 16)
        cid5, row5 = _sent_row("Approve Send Test", "approvesend@probe.example", days_ago=9)
        followups.send_due_followups()
        draft_id = _followup_status(row5)[0]
        with get_conn() as conn:
            draft_row = conn.execute(
                "SELECT id FROM campaign_followups WHERE campaign_prospect_id = ? AND follow_up_number = 1", (row5,)
            ).fetchone()
        followups.approve_followup_draft(draft_row["id"])
        after_approve_status = _followup_status(row5)[0]["status"]

        email_provider.set_daily_send_limit(0)
        send_approved(cid5)
        after_capped_send_status = _followup_status(row5)[0]["status"]

        email_provider.set_daily_send_limit(50)
        result = send_approved(cid5)
        after_real_send = _followup_status(row5)[0]
        with get_conn() as conn:
            cp_row = conn.execute("SELECT last_followup_at FROM campaign_prospects WHERE id = ?", (row5,)).fetchone()

        results["approve_and_send"] = {
            "after_approve_status": after_approve_status,
            "after_capped_send_status": after_capped_send_status,
            "sent_count_from_send_approved": result.sent,
            "after_real_send_status": after_real_send["status"],
            "after_real_send_has_sent_at": after_real_send["sent_at"] is not None,
            "last_followup_at_set": cp_row["last_followup_at"] is not None,
        }

        # --- Scenario 6: manual send_followup_now still sends immediately,
        # unchanged -- an explicit right-now action is its own review step.
        cid6, row6 = _sent_row("Manual Now Test", "manualnow@probe.example", days_ago=0)
        manual_result = followups.send_followup_now(cid6, row6)
        count6 = len(_followup_status(row6))
        manual_status = _followup_status(row6)[0]["status"]
        # a second manual call sends #2 immediately too
        followups.send_followup_now(cid6, row6)
        # a third should fail -- already at MAX_FOLLOW_UPS
        third_error = None
        try:
            followups.send_followup_now(cid6, row6)
        except followups.FollowUpError as e:
            third_error = str(e)
        results["manual_now"] = {
            "first_call_number": manual_result["follow_up_number"],
            "count_after_first_call": count6,
            "manual_status_is_sent": manual_status == "Sent",
            "third_call_error": third_error,
        }

    print(json.dumps(results))


if __name__ == "__main__":
    main()
