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
from app.services.approval_and_delivery import approve, simulate_sent, simulate_reply  # noqa: E402
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


def main():
    init_db(seed_customers=False)
    app_settings.set_setting("gmail_address", "test@example.com")
    app_settings.set_setting("gmail_app_password", "x" * 16)
    email_provider.set_daily_send_limit(50)

    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO import_batches (batch_id, filename, row_count, imported_at) VALUES ('probe-batch', 'probe.csv', 1, ?)",
            (now,),
        )

    results = {}

    with patch.object(email_provider, "send_email", side_effect=fake_send_email):
        # --- Scenario 1: cadence gating -- day 5 gets #1 but not #2, day 9 gets #2 too
        cid1, row1 = _sent_row("Cadence Test", "cadence@probe.example", days_ago=5)
        r1a = followups.send_due_followups()
        with get_conn() as conn:
            after_day5 = conn.execute(
                "SELECT follow_up_number FROM campaign_followups WHERE campaign_prospect_id = ? ORDER BY follow_up_number", (row1,)
            ).fetchall()
        with get_conn() as conn:
            conn.execute("UPDATE campaign_prospects SET sent_at = ? WHERE id = ?",
                         ((datetime.now(timezone.utc) - timedelta(days=9)).isoformat(), row1))
        r1b = followups.send_due_followups()
        with get_conn() as conn:
            after_day9 = conn.execute(
                "SELECT follow_up_number FROM campaign_followups WHERE campaign_prospect_id = ? ORDER BY follow_up_number", (row1,)
            ).fetchall()
        results["cadence"] = {
            "after_day5_numbers": [r["follow_up_number"] for r in after_day5],
            "after_day9_numbers": [r["follow_up_number"] for r in after_day9],
            "run1_sent": r1a["sent"], "run2_sent": r1b["sent"],
        }

        # --- Scenario 2: a reply cancels further follow-ups
        cid2, row2 = _sent_row("Reply Cancels Test", "replycancel@probe.example", days_ago=9)
        simulate_reply(cid2, row2, "Re: hi", False, "stop emailing me follow-ups, I replied")
        r2 = followups.send_due_followups()
        with get_conn() as conn:
            count2 = conn.execute(
                "SELECT COUNT(*) c FROM campaign_followups WHERE campaign_prospect_id = ?", (row2,)
            ).fetchone()["c"]
        results["reply_cancels"] = {"followup_count_after_reply": count2, "run_sent": r2["sent"]}

        # --- Scenario 3: max 2 cap -- each pass advances by at most one
        # follow-up per lead (even though both windows are already overdue
        # here), and once both are sent, a further pass sends nothing more.
        cid3, row3 = _sent_row("Max Cap Test", "maxcap@probe.example", days_ago=9)
        followups.send_due_followups()  # sends #1 only
        with get_conn() as conn:
            count_after_first_pass = conn.execute(
                "SELECT COUNT(*) c FROM campaign_followups WHERE campaign_prospect_id = ?", (row3,)
            ).fetchone()["c"]
        r3b = followups.send_due_followups()  # sends #2
        with get_conn() as conn:
            count_after_second_pass = conn.execute(
                "SELECT COUNT(*) c FROM campaign_followups WHERE campaign_prospect_id = ?", (row3,)
            ).fetchone()["c"]
        r3c = followups.send_due_followups()  # already at the cap -- nothing left to send
        results["max_cap"] = {
            "count_after_first_pass": count_after_first_pass,
            "count_after_second_pass": count_after_second_pass,
            "second_pass_sent": r3b["sent"],
            "third_pass_sent": r3c["sent"],
        }

        # --- Scenario 4: suppressed email never gets a follow-up
        cid4, row4 = _sent_row("Suppressed Test", "suppressed@probe.example", days_ago=9)
        add_to_suppression_list("suppressed@probe.example", reason="test", source="manual")
        r4 = followups.send_due_followups()
        with get_conn() as conn:
            count4 = conn.execute(
                "SELECT COUNT(*) c FROM campaign_followups WHERE campaign_prospect_id = ?", (row4,)
            ).fetchone()["c"]
        results["suppressed"] = {"followup_count": count4, "run_sent_at_least_one": r4["sent"] > 0}

        # --- Scenario 5: daily limit -- earlier scenarios already sent several
        # real follow-ups today, so a cap of 1 is already fully spent by the
        # time this candidate becomes due -- it should be skipped, not sent.
        email_provider.set_daily_send_limit(1)
        cid5, row5 = _sent_row("Daily Limit Test", "dailylimit@probe.example", days_ago=9)
        r5 = followups.send_due_followups()
        results["daily_limit"] = {"sent": r5["sent"], "skipped_daily_limit": r5["skipped_daily_limit"]}
        email_provider.set_daily_send_limit(50)

        # --- Scenario 6: manual send_followup_now bypasses the day-wait
        cid6, row6 = _sent_row("Manual Now Test", "manualnow@probe.example", days_ago=0)
        manual_result = followups.send_followup_now(cid6, row6)
        with get_conn() as conn:
            count6 = conn.execute(
                "SELECT COUNT(*) c FROM campaign_followups WHERE campaign_prospect_id = ?", (row6,)
            ).fetchone()["c"]
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
            "third_call_error": third_error,
        }

    print(json.dumps(results))


if __name__ == "__main__":
    main()
