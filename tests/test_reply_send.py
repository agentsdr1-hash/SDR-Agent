"""
Reply drafts send from the campaign's "Send all approved" batch, not
their own immediate approve-and-send path -- run via an isolated
subprocess probe (_reply_send_probe.py) for the same reason as
test_daily_send_limit.py: needs a mocked email_provider.send_email,
which only works with a direct import in a process that hasn't already
cached a different app.db.DB_PATH. See that probe's docstring.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PROBE = Path(__file__).parent / "_reply_send_probe.py"


def _run_probe(scenario, tmp_path):
    db_path = tmp_path / f"{scenario}.db"
    result = subprocess.run(
        [sys.executable, str(PROBE), scenario],
        cwd=str(REPO_ROOT),
        env={"APEX_DB_PATH": str(db_path), "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"probe failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    return json.loads(result.stdout)


def test_send_approved_sends_approved_reply_drafts_and_respects_suppression(tmp_path):
    data = _run_probe("combined", tmp_path)
    result = data["result"]

    # Both the fresh outreach row and the normal (non-suppressed) reply
    # draft sent from the same batch call.
    assert result["attempted"] == 3
    assert result["sent"] == 2
    assert result["suppressed"] == 1
    assert data["outreach_status"] == "Sent"
    assert data["normal_reply_status"] == "Sent"
    assert data["normal_reply_sent"] is True

    # The suppressed recipient's approved reply never sent -- flipped to
    # Suppressed instead, same as a suppressed fresh-outreach row would.
    assert data["suppressed_reply_status"] == "Suppressed"
    assert data["suppressed_reply_sent"] is False


def test_approved_replies_get_priority_over_fresh_outreach_under_the_daily_cap(tmp_path):
    data = _run_probe("priority", tmp_path)
    result = data["result"]

    # Daily cap of 1, one approved reply + one approved outreach row: the
    # reply (an already-engaged prospect waiting on an answer) sends,
    # outreach is left Approved for the next run.
    assert result["attempted"] == 2
    assert result["sent"] == 1
    assert result["skipped_daily_limit"] == 1
    assert data["reply_status"] == "Sent"
    assert data["outreach_status"] == "Approved"
