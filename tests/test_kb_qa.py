"""
Smart-reply drafting: KB/stock matching, and the spec-aware closing line
(the fix for replies always re-asking for a grade the customer already
gave, and for pushing toward closing once everything's been provided).

_specs_given() is pure regex with no DB access, so it's tested with a
normal in-process import. compose_smart_reply() reads KB entries from the
DB, so it goes through the isolated _kb_probe.py subprocess -- see that
file's docstring, same reasoning as the daily-send-limit probe.

Note for future tests: this file's top-level `from app.services.kb_qa
import _specs_given` imports app.db as a side effect, which permanently
resolves app.db.DB_PATH for the rest of this pytest process (to the
package-default path, since APEX_DB_PATH isn't set at collection time).
That's harmless here because nothing in this file calls get_conn()
in-process -- but it's exactly the trap _daily_limit_probe.py and
_kb_probe.py's docstrings warn about. Any new test that needs a real,
isolated DB and direct (non-HTTP) access to app.services.* code should
follow the same isolated-subprocess-probe pattern, not import and call
DB-touching code directly in this shared process.
"""
import json
import subprocess
import sys
from pathlib import Path

from app.services.kb_qa import _specs_given, reply_subject_for

REPO_ROOT = Path(__file__).parent.parent
PROBE = Path(__file__).parent / "_kb_probe.py"


# ---- pure-function tests (no DB, no server) --------------------------
def test_reply_subject_for_reuses_the_original_subject():
    assert reply_subject_for("Steel inquiry") == "Re: Steel inquiry"


def test_reply_subject_for_does_not_double_prefix():
    assert reply_subject_for("Re: Steel inquiry") == "Re: Steel inquiry"
    assert reply_subject_for("RE: Steel inquiry") == "RE: Steel inquiry"  # case preserved, not re-wrapped


def test_reply_subject_for_falls_back_when_theres_nothing_to_reuse():
    assert reply_subject_for(None) == "Re: your inquiry"
    assert reply_subject_for("") == "Re: your inquiry"
    assert reply_subject_for("   ") == "Re: your inquiry"


def test_specs_given_detects_grade():
    assert _specs_given("Grade A please") == {"grade": True, "size": False, "quantity": False}


def test_specs_given_detects_size_variants():
    assert _specs_given("50x5mm panels")["size"] is True
    assert _specs_given("50 x 5 mm")["size"] is True
    assert _specs_given("just 50mm thick")["size"] is True
    assert _specs_given("no dimensions here")["size"] is False


def test_specs_given_detects_quantity():
    assert _specs_given("20 tons please")["quantity"] is True
    assert _specs_given("need 500 pcs")["quantity"] is True
    assert _specs_given("no amount mentioned")["quantity"] is False


def test_specs_given_all_false_on_empty_or_none():
    assert _specs_given("") == {"grade": False, "size": False, "quantity": False}
    assert _specs_given(None) == {"grade": False, "size": False, "quantity": False}


# ---- compose_smart_reply via isolated probe ----------------------------
def _run_probe_with_db(tmp_path):
    db_path = tmp_path / "kb_probe.db"
    result = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=str(REPO_ROOT),
        env={"APEX_DB_PATH": str(db_path), "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"probe failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    return {r["label"]: r for r in json.loads(result.stdout)}


def test_grade_only_reply_is_not_asked_for_grade_again(tmp_path):
    results = _run_probe_with_db(tmp_path)
    body = results["grade_only"]["body"]
    assert "share the sizes, grades and quantities" not in body
    assert "Just need the size and quantity" in body
    assert "steel gratings" in body.lower()


def test_everything_given_pushes_toward_closing(tmp_path):
    results = _run_probe_with_db(tmp_path)
    body = results["everything_given"]["body"]
    assert "that's everything we need" in body.lower()
    assert "delivery window" in body.lower()
    assert "could you share the sizes" not in body.lower()


def test_nothing_given_keeps_the_generic_ask(tmp_path):
    results = _run_probe_with_db(tmp_path)
    body = results["nothing_given"]["body"]
    assert "could you share the sizes, grades and quantities" in body.lower()


def test_partial_specs_asks_only_for_whats_missing(tmp_path):
    results = _run_probe_with_db(tmp_path)
    body = results["grade_and_qty_only"]["body"]
    assert "just need the size to finalize" in body.lower()
    assert "grade" not in body.lower().split("just need")[1].split("finalize")[0]


# ---- integration: the real HTTP path produces the same behavior -------
def test_simulate_reply_creates_draft_using_the_spec_aware_closing(server):
    cid = server.post("/campaigns", json={"name": "KB Integration Test"}).json()["id"]
    pid = server.seed_prospect()
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")

    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={
        "reply_body": "Grade A, 50x5mm, 20 tons please",
    })

    drafts = server.get("/reply-drafts").json()
    draft = next(d for d in drafts if d["campaign_prospect_id"] == row_id)
    assert "that's everything we need" in draft["body"].lower()


def test_reply_draft_subject_reuses_the_incoming_reply_subject(server):
    # Previously every smart-reply draft got the same hardcoded "Re: your
    # question" regardless of what the customer's email actually said --
    # broke inbox threading and read as a non-sequitur. The draft's
    # subject should track the real conversation instead.
    cid = server.post("/campaigns", json={"name": "Subject Reuse Test"}).json()["id"]
    pid = server.seed_prospect()
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")

    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={
        "reply_subject": "RE: AKEIS Steel Supply -- quote for Acme", "reply_body": "interested",
    })

    drafts = server.get("/reply-drafts").json()
    draft = next(d for d in drafts if d["campaign_prospect_id"] == row_id)
    assert draft["subject"] == "RE: AKEIS Steel Supply -- quote for Acme"
    assert draft["subject"] != "Re: your question"
