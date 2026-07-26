"""
Automated follow-up cadence (app/services/followups.py) -- the day-4/day-8
auto-send follow-ups, gated on: still awaiting a reply, under the 2-per-lead
cap, not suppressed, and today's daily send limit. Run via an isolated
subprocess probe (_followups_probe.py) for the same reason
test_daily_send_limit.py does: it needs to mock email_provider.send_email
to avoid a real Gmail SMTP call, which only works with a direct import in
a process that hasn't already cached a different app.db.DB_PATH.

The HTTP-level behavior (the manual send-followup endpoint refusing to
pretend to send without Gmail configured, and follow-up data showing up on
the campaign-prospects/list-leads endpoints) is covered separately below
via the normal live-server `server` fixture.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PROBE = Path(__file__).parent / "_followups_probe.py"


def _run_probe(tmp_path):
    db_path = tmp_path / "followups_probe.db"
    result = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=str(REPO_ROOT),
        env={"APEX_DB_PATH": str(db_path), "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"probe failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    return json.loads(result.stdout)


def test_cadence_sends_followup_1_at_day_4_and_followup_2_at_day_8(tmp_path):
    data = _run_probe(tmp_path)["cadence"]
    assert data["after_day5_numbers"] == [1]        # day 4 window passed, day 8 hasn't -- only #1
    assert data["after_day9_numbers"] == [1, 2]      # day 8 window now passed too -- #2 joins it
    assert data["run1_sent"] == 1
    assert data["run2_sent"] == 1


def test_a_reply_cancels_all_further_followups(tmp_path):
    data = _run_probe(tmp_path)["reply_cancels"]
    assert data["followup_count_after_reply"] == 0
    assert data["run_sent"] == 0


def test_max_two_followups_per_lead(tmp_path):
    data = _run_probe(tmp_path)["max_cap"]
    assert data["count_after_first_pass"] == 1
    assert data["count_after_second_pass"] == 2
    assert data["second_pass_sent"] == 1
    assert data["third_pass_sent"] == 0  # already at the cap -- nothing left to send


def test_suppressed_email_never_gets_a_followup(tmp_path):
    data = _run_probe(tmp_path)["suppressed"]
    assert data["followup_count"] == 0
    assert data["run_sent_at_least_one"] is False


def test_daily_send_limit_skips_rather_than_sends(tmp_path):
    data = _run_probe(tmp_path)["daily_limit"]
    assert data["sent"] == 0
    assert data["skipped_daily_limit"] == 1


def test_manual_send_now_bypasses_the_day_wait_but_still_enforces_the_cap(tmp_path):
    data = _run_probe(tmp_path)["manual_now"]
    assert data["first_call_number"] == 1     # sent immediately despite sent_at being "today"
    assert data["count_after_first_call"] == 1
    assert data["third_call_error"] == "Already sent the maximum of 2 follow-ups for this lead"


# ---- HTTP-level: the manual endpoint's safety guarantee + data visibility ----

def _create_campaign(server, name="Followup HTTP Test"):
    return server.post("/campaigns", json={"name": name}).json()["id"]


def test_send_followup_refuses_without_gmail_configured(server):
    cid = _create_campaign(server, "Followup No Gmail Test")
    pid = server.seed_prospect()
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")

    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/send-followup")
    assert r.status_code == 503


def test_send_followup_refuses_before_a_real_send(server):
    cid = _create_campaign(server, "Followup Not Sent Test")
    pid = server.seed_prospect()
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    # Still Queued -- never approved or sent.
    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/send-followup")
    assert r.status_code in (422, 503)  # 503 if Gmail isn't configured in this test run either


def test_follow_up_count_and_last_followup_at_default_to_zero_and_null(server):
    cid = _create_campaign(server, "Followup Fields Default Test")
    pid = server.seed_prospect()
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    row = server.get(f"/campaigns/{cid}/prospects").json()[0]
    assert row["follow_up_count"] == 0
    assert row["last_followup_at"] is None


def test_lead_timeline_includes_empty_follow_ups_list_when_none_sent(server):
    cid = _create_campaign(server, "Followup Timeline Test")
    pid = server.seed_prospect()
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    lead_number = f"L-{pid:06d}"
    detail = server.get(f"/leads/{lead_number}").json()
    assert detail["memberships"][0]["follow_ups"] == []
