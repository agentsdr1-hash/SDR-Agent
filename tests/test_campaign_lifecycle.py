"""
OBJ-003/005/006: campaign creation, draft approve/reject (single + bulk),
simulated send/reply, suppression on opt-out, and the safety guarantee
that a real send never pretends to work without Gmail configured.

The daily-send-limit *accounting* itself (the trickiest logic here -- it
must stop for real once the cap is hit, and leave the rest queued rather
than failed) is covered separately in test_daily_send_limit.py via an
isolated subprocess probe, since it needs a mocked send_email() that a
real HTTP call to a live server can't provide from outside.
"""


def _create_campaign(server, name="Test Campaign"):
    r = server.post("/campaigns", json={"name": name})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _assign(server, campaign_id, prospect_id):
    r = server.post(f"/campaigns/{campaign_id}/assign-prospect/{prospect_id}")
    assert r.status_code == 200, r.text
    return r.json()


def test_create_campaign_and_assign_prospect(server):
    cid = _create_campaign(server, "Assign Test")
    pid = server.seed_prospect()
    result = _assign(server, cid, pid)
    assert result["status"] == "Queued"

    prospects = server.get(f"/campaigns/{cid}/prospects").json()
    assert len(prospects) == 1
    assert prospects[0]["status"] == "Queued"


def test_cannot_assign_invalid_prospect(server):
    cid = _create_campaign(server, "Invalid Assign Test")
    pid = server.seed_prospect(status="Invalid")
    r = server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    assert r.status_code == 422


def test_approve_and_reject_single_draft(server):
    cid = _create_campaign(server, "Approve Reject Test")
    p1, p2 = server.seed_prospect(), server.seed_prospect()
    _assign(server, cid, p1)
    _assign(server, cid, p2)

    prospects = server.get(f"/campaigns/{cid}/prospects").json()
    row1, row2 = prospects[0]["id"], prospects[1]["id"]

    r = server.post(f"/campaigns/{cid}/prospects/{row1}/approve")
    assert r.status_code == 200
    r = server.post(f"/campaigns/{cid}/prospects/{row2}/reject")
    assert r.status_code == 200

    prospects = server.get(f"/campaigns/{cid}/prospects").json()
    statuses = {p["id"]: p["status"] for p in prospects}
    assert statuses[row1] == "Approved"
    assert statuses[row2] == "Rejected"

    # Can't approve a second time from Approved.
    r = server.post(f"/campaigns/{cid}/prospects/{row1}/approve")
    assert r.status_code == 422


def test_bulk_approve_and_reject(server):
    cid = _create_campaign(server, "Bulk Test")
    row_ids = []
    for _ in range(4):
        pid = server.seed_prospect()
        _assign(server, cid, pid)
    prospects = server.get(f"/campaigns/{cid}/prospects").json()
    row_ids = [p["id"] for p in prospects]

    r = server.post(f"/campaigns/{cid}/prospects/bulk-approve", json={"prospect_row_ids": row_ids[:2]})
    assert r.status_code == 200
    body = r.json()
    assert body["succeeded"] == 2
    assert body["failed"] == 0

    r = server.post(f"/campaigns/{cid}/prospects/bulk-reject", json={"prospect_row_ids": row_ids[2:]})
    assert r.json()["succeeded"] == 2

    # Retrying bulk-approve on already-approved rows should report failures, not crash.
    r = server.post(f"/campaigns/{cid}/prospects/bulk-approve", json={"prospect_row_ids": row_ids[:2]})
    body = r.json()
    assert body["succeeded"] == 0
    assert body["failed"] == 2


def test_simulate_sent_and_reply_flow(server):
    cid = _create_campaign(server, "Simulate Test")
    pid = server.seed_prospect()
    _assign(server, cid, pid)
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]

    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")
    assert r.status_code == 200

    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={
        "reply_subject": "Re: hello", "reply_body": "Sounds good, send details", "is_opt_out": False,
    })
    assert r.status_code == 200

    prospect = server.get(f"/campaigns/{cid}/prospects").json()[0]
    assert prospect["status"] == "Replied"

    # A reply draft should have been generated automatically.
    drafts = server.get("/reply-drafts").json()
    assert len(drafts) == 1
    assert drafts[0]["status"] == "Draft"


def test_simulate_opt_out_reply_suppresses_email(server):
    cid = _create_campaign(server, "Opt Out Test")
    pid = server.seed_prospect(email="optout@example.com")
    _assign(server, cid, pid)
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")

    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={"is_opt_out": True})
    assert r.status_code == 200

    prospect = server.get(f"/campaigns/{cid}/prospects").json()[0]
    assert prospect["status"] == "Suppressed"

    suppressed = server.get("/admin/suppressed").json()
    assert any(s["email"] == "optout@example.com" for s in suppressed)


def test_real_send_refuses_without_gmail_configured(server):
    # Core safety guarantee: nothing pretends to send when Gmail isn't set up.
    cid = _create_campaign(server, "Unconfigured Send Test")
    pid = server.seed_prospect()
    _assign(server, cid, pid)
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")

    r = server.post(f"/campaigns/{cid}/send")
    assert r.status_code == 503

    # Nothing should have moved past Approved.
    prospect = server.get(f"/campaigns/{cid}/prospects").json()[0]
    assert prospect["status"] == "Approved"
