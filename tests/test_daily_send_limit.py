"""
Daily send limit accounting -- run via an isolated subprocess probe
(_daily_limit_probe.py) rather than the shared live-server fixture. See
that file's docstring for why: it needs to mock email_provider.send_email
to avoid a real Gmail SMTP call, which only works with a direct import in
a process that hasn't already cached a different app.db.DB_PATH.
"""
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
PROBE = Path(__file__).parent / "_daily_limit_probe.py"


def test_send_approved_stops_at_the_daily_cap_and_leaves_the_rest_queued(tmp_path):
    db_path = tmp_path / "probe.db"
    result = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=str(REPO_ROOT),
        env={"APEX_DB_PATH": str(db_path), "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"probe failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    data = json.loads(result.stdout)

    first, second = data["first_run"], data["second_run"]

    # Limit was set to 2, 4 prospects approved: exactly 2 send, 2 are left
    # queued (not failed) for the next run.
    assert first["attempted"] == 4
    assert first["sent"] == 2
    assert first["failed"] == 0
    assert first["skipped_daily_limit"] == 2

    # A second call the same "day" (limit already fully spent) must not
    # send anything further, and must not report the already-sent ones as
    # failed -- only the 2 still-Approved rows are attempted at all.
    assert second["attempted"] == 2
    assert second["sent"] == 0
    assert second["skipped_daily_limit"] == 2
