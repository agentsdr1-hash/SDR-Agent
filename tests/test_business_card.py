"""
Business card scanning (app/services/business_card.py) -- the Import
tab's "scan a business card" flow. The real Claude vision request/
response/error handling against a mocked Anthropic client is covered by
the subprocess probe (_business_card_probe.py, same reasoning as
test_ai_provider.py's docstring). HTTP-level behavior -- the scan
endpoint refusing cleanly without a key, and confirmed fields flowing
into the exact same validate/assign pipeline as a CSV import -- is
covered here via the normal live-server `server` fixture. No real
Anthropic API key or network call anywhere in this file.
"""
import json
import subprocess
import sys
from pathlib import Path

from conftest import REPO_ROOT

PROBE = Path(__file__).parent / "_business_card_probe.py"


def _run_probe(tmp_path):
    db_path = tmp_path / "business_card_probe.db"
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


def test_scan_business_card_around_a_mocked_claude(tmp_path):
    data = _run_probe(tmp_path)
    assert data["not_configured_raises"] is True
    assert data["not_configured_message_mentions_admin"] is True
    assert data["bad_media_type_raises"] is True
    assert data["clean_json_parsed"] == {
        "first_name": "Ahmed", "last_name": "Rashid", "email": "ahmed@falconsteel.ae",
        "company": "Falcon Steel", "phone": "+971-50-1234567", "title": "Procurement Manager",
    }
    assert data["fenced_json_parsed_email"] == "ahmed@falconsteel.ae"
    assert data["partial_card_missing_fields_are_none"] is True
    assert data["partial_card_first_name_kept"] is True
    assert data["garbage_response_raises"] is True
    assert data["request_failure_raises_scan_error"] is True


# ---- HTTP-level: scan endpoint's not-configured error + confirm -> validate pipeline ----

def test_scan_endpoint_fails_cleanly_without_a_claude_key(server):
    r = server.post(
        "/prospects/scan-business-card",
        files={"file": ("card.jpg", b"fake-image-bytes", "image/jpeg")},
    )
    assert r.status_code == 422
    assert "not configured" in r.json()["detail"].lower()


def test_confirm_scan_creates_a_one_row_batch_that_flows_through_validate(server):
    r = server.post("/prospects/scan-business-card/confirm", json={
        "first_name": "Layla", "last_name": "Hassan", "email": "layla.hassan@marinastruct.ae",
        "company": "Marina Structures", "phone": "+971-55-9876543", "title": "Purchasing Lead",
    })
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["row_count"] == 1
    assert summary["filename"] == "business_card_scan"
    batch_id = summary["batch_id"]

    rows = server.get(f"/prospects/{batch_id}").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["first_name"] == "Layla"
    assert row["email"] == "layla.hassan@marinastruct.ae"
    assert row["lead_source"] == "Trade Show"
    assert row["status"] == "Pending"

    # Same validate endpoint every CSV/URL import uses -- no special-casing.
    r = server.post(f"/prospects/validate/{batch_id}")
    assert r.status_code == 200, r.text
    summary2 = r.json()
    assert summary2["valid"] == 1

    rows_after = server.get(f"/prospects/{batch_id}").json()
    assert rows_after[0]["status"] == "Valid"
    assert rows_after[0]["lead_number"] is not None

    # The title Claude read isn't a prospects_raw column -- it should have
    # landed as a note instead of being silently dropped.
    lead_number = rows_after[0]["lead_number"]
    detail = server.get(f"/leads/{lead_number}").json()
    notes_text = json.dumps(detail.get("notes", []))
    assert "Purchasing Lead" in notes_text


def test_confirm_scan_with_missing_email_still_creates_a_row_marked_invalid(server):
    """Matches import_prospect_file()'s own philosophy (see that module's
    docstring): a business-card scan with a field OCR couldn't read still
    gets written and flows through validation like any other row -- it
    just comes out Invalid, fixable inline, not silently rejected at
    creation time."""
    r = server.post("/prospects/scan-business-card/confirm", json={
        "first_name": "NoEmail", "last_name": "Card", "email": None, "company": "Some Co",
    })
    assert r.status_code == 200
    batch_id = r.json()["batch_id"]
    r = server.post(f"/prospects/validate/{batch_id}")
    assert r.status_code == 200
    assert r.json()["invalid"] == 1
