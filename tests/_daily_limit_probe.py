"""
Standalone probe (run as its own interpreter process, not imported by
pytest) for the daily-send-limit accounting inside send_approved().

Why a separate process instead of a normal pytest test: app.db.DB_PATH is
computed once, at import time, from the APEX_DB_PATH environment variable.
Every other test file in this suite talks to its DB over real HTTP against
a subprocess server, so nothing in the pytest process itself ever imports
app.db -- but the moment something did, that env-var read would be
permanently baked in for the rest of the pytest session, and a later
attempt to point a second "instance" at a different DB by mutating
os.environ would silently do nothing (the module is already cached).
Running this file as `python tests/_daily_limit_probe.py` sidesteps that
entirely: a fresh interpreter, APEX_DB_PATH set before any app import
happens, no shared state with pytest's process or with any other test.

Exercises exactly the risk this feature was built to guard against: Gmail
flags a personal account that fires a whole approved batch at once, so
send_approved() must stop for real once today's cap is hit and leave the
rest queued (not failed) for tomorrow -- not silently ignore the cap.

Prints one JSON object to stdout: {"first_run": {...SendResult}, "second_run": {...SendResult}}
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db  # noqa: E402
from app.services import settings as app_settings  # noqa: E402
from app.integrations import email_provider  # noqa: E402
from app.services.campaign_management import create_campaign, assign_prospect_to_campaign  # noqa: E402
from app.services.approval_and_delivery import approve, send_approved  # noqa: E402
from app.db import get_conn  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def main():
    init_db(seed_customers=False)

    # Fake-but-valid-shaped credentials so is_configured() is True --
    # send_email() itself is mocked below, so no real SMTP call happens.
    app_settings.set_setting("gmail_address", "test@example.com")
    app_settings.set_setting("gmail_app_password", "x" * 16)
    email_provider.set_daily_send_limit(2)

    campaign = create_campaign("Probe Campaign", "Mon,Tue,Wed,Thu,Fri", 25)
    campaign_id = campaign.id

    now = datetime.now(timezone.utc).isoformat()
    prospect_ids = []
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO import_batches (batch_id, filename, row_count, imported_at) VALUES ('probe','probe.csv',4,?)",
            (now,),
        )
        for i in range(4):
            cur = conn.execute(
                """INSERT INTO prospects_raw (batch_id, row_number, first_name, last_name, email, company, status)
                   VALUES ('probe', ?, ?, 'Test', ?, 'Probe Co', 'Valid')""",
                (i + 1, f"P{i}", f"p{i}@example.com"),
            )
            prospect_ids.append(cur.lastrowid)

    row_ids = []
    for pid in prospect_ids:
        assign_prospect_to_campaign(campaign_id, pid)
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id FROM campaign_prospects WHERE campaign_id = ? ORDER BY id", (campaign_id,)
        ).fetchall()
        row_ids = [r["id"] for r in rows]
    for rid in row_ids:
        approve(campaign_id, rid)

    def fake_send_email(to_address, subject, body):
        return {"to": to_address, "sent_at": datetime.now(timezone.utc).isoformat()}

    with patch.object(email_provider, "send_email", side_effect=fake_send_email):
        first = send_approved(campaign_id)
        second = send_approved(campaign_id)  # same day, limit already spent -> should skip the rest too

    print(json.dumps({"first_run": first.model_dump(), "second_run": second.model_dump()}))


if __name__ == "__main__":
    main()
