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
from conftest import ADMIN_HEADERS


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


def test_approving_a_reply_draft_only_flips_status_does_not_send(server):
    # Approving used to send the reply immediately (its own path, separate
    # from the campaign's "Send all approved" batch) -- which meant a
    # reply could go out without the suppression-list check or
    # daily-pacing cap that batch already applies to fresh outreach.
    # Approving now just queues it; nothing leaves this server, so this
    # succeeds even with Gmail unconfigured (the live-server fixture never
    # configures it).
    cid = _create_campaign(server, "Approve Only Test")
    pid = server.seed_prospect()
    _assign(server, cid, pid)
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={"reply_body": "interested"})

    draft_id = server.get("/reply-drafts").json()[0]["id"]
    r = server.post(f"/reply-drafts/{draft_id}/approve")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Approved"

    drafts = server.get("/reply-drafts", params={"status": "Approved"}).json()
    approved = next(d for d in drafts if d["id"] == draft_id)
    assert approved["sent_at"] is None
    assert approved["approved_at"] is not None

    # No longer shows up in the Draft queue.
    assert not any(d["id"] == draft_id for d in server.get("/reply-drafts", params={"status": "Draft"}).json())


def test_send_all_approved_requires_gmail_for_approved_reply_drafts_too(server):
    # The campaign's "Send all approved" batch is now the only place a
    # reply draft actually sends from -- it must fail the same safe way
    # fresh outreach does when Gmail isn't configured, not silently do
    # nothing or send anyway.
    cid = _create_campaign(server, "Send Gate Reply Test")
    pid = server.seed_prospect()
    _assign(server, cid, pid)
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={"reply_body": "interested"})
    draft_id = server.get("/reply-drafts").json()[0]["id"]
    server.post(f"/reply-drafts/{draft_id}/approve")

    r = server.post(f"/campaigns/{cid}/send")
    assert r.status_code == 503

    # Left Approved, not silently marked Sent or dropped.
    approved = next(d for d in server.get("/reply-drafts", params={"status": "Approved"}).json() if d["id"] == draft_id)
    assert approved["sent_at"] is None


def test_reply_drafts_can_be_scoped_to_one_campaign(server):
    # The Campaigns tab's review panel used to show every pending reply
    # from every campaign regardless of which one was selected -- confusing
    # since it looked scoped to the campaign in view. campaign_id fixes that.
    cid1 = _create_campaign(server, "Scope Test A")
    cid2 = _create_campaign(server, "Scope Test B")
    pid1, pid2 = server.seed_prospect(), server.seed_prospect()
    _assign(server, cid1, pid1)
    _assign(server, cid2, pid2)
    row1 = server.get(f"/campaigns/{cid1}/prospects").json()[0]["id"]
    row2 = server.get(f"/campaigns/{cid2}/prospects").json()[0]["id"]

    for cid, row in [(cid1, row1), (cid2, row2)]:
        server.post(f"/campaigns/{cid}/prospects/{row}/approve")
        server.post(f"/campaigns/{cid}/prospects/{row}/simulate-sent")
        server.post(f"/campaigns/{cid}/prospects/{row}/simulate-reply", json={
            "reply_subject": "Re: hi", "reply_body": "interested", "is_opt_out": False,
        })

    drafts_a = server.get("/reply-drafts", params={"campaign_id": cid1}).json()
    drafts_b = server.get("/reply-drafts", params={"campaign_id": cid2}).json()
    assert len(drafts_a) == 1 and drafts_a[0]["campaign_prospect_id"] == row1
    assert len(drafts_b) == 1 and drafts_b[0]["campaign_prospect_id"] == row2
    assert drafts_a[0]["campaign_id"] == cid1
    assert drafts_b[0]["campaign_id"] == cid2

    # Omitting campaign_id still returns the cross-campaign view.
    all_drafts = server.get("/reply-drafts").json()
    assert {d["id"] for d in (drafts_a + drafts_b)}.issubset({d["id"] for d in all_drafts})


def test_long_reply_body_is_not_truncated_in_the_lead_timeline(server):
    # Real quote-conversation replies run several paragraphs -- the old
    # 500-char cap on source_reply_snippet silently chopped them, and the
    # lead timeline was reported as showing "only my responses" as a
    # result. This locks in that a long reply survives intact end to end.
    cid = _create_campaign(server, "Long Reply Test")
    pid = server.seed_prospect()
    _assign(server, cid, pid)
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")

    long_body = "We need the following specs confirmed before we can proceed. " * 20  # well over 500 chars
    assert len(long_body) > 500
    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={
        "reply_subject": "Re: specs", "reply_body": long_body, "is_opt_out": False,
    })
    assert r.status_code == 200

    prospect = server.get(f"/campaigns/{cid}/prospects").json()[0]
    lead_number = prospect["lead_number"]
    detail = server.get(f"/leads/{lead_number}").json()
    reply_drafts = detail["memberships"][0]["reply_drafts"]
    assert len(reply_drafts) == 1
    assert reply_drafts[0]["source_reply_snippet"] == long_body


def test_simulate_reply_can_fire_a_second_round_after_the_first(server):
    # This industry runs on multi-round back-and-forth -- a prospect who
    # already replied once should still be reachable for a second (and
    # third) round, not frozen at "Replied" forever. See
    # AWAITING_REPLY_STATUSES in inbox_monitor.py.
    cid = _create_campaign(server, "Second Round Test")
    pid = server.seed_prospect()
    _assign(server, cid, pid)
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")

    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={
        "reply_subject": "Re: hello", "reply_body": "What sizes do you carry?", "is_opt_out": False,
    })
    assert r.status_code == 200
    assert server.get(f"/campaigns/{cid}/prospects").json()[0]["status"] == "Replied"

    # A second reply from the same still-Replied prospect is not rejected --
    # the row stays open for the next inbound message.
    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={
        "reply_subject": "Re: Re: hello", "reply_body": "Also need 20 tons of flat bars", "is_opt_out": False,
    })
    assert r.status_code == 200, r.text

    prospect = server.get(f"/campaigns/{cid}/prospects").json()[0]
    assert prospect["status"] == "Replied"
    assert prospect["reply_subject"] == "Re: Re: hello"  # updated to the latest round

    drafts = server.get("/reply-drafts").json()
    matching = [d for d in drafts if d["campaign_prospect_id"] == row_id]
    assert len(matching) == 2  # one reply draft per round


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

    suppressed = server.get("/admin/suppressed", headers=ADMIN_HEADERS).json()
    assert any(s["email"] == "optout@example.com" for s in suppressed)

    # Unlike Replied, Suppressed is a real dead end -- no further "reply"
    # is accepted for this row.
    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={"reply_body": "wait, don't unsubscribe me"})
    assert r.status_code == 422


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
