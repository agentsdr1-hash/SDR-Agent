"""
DELETE /leads/{lead_number} -- permanent lead delete, built for clearing
test data before a production go-live (see app/services/leads.py's
delete_lead() docstring for the no-undo rationale). Covers: deleting a
lead with no campaign history, deleting one with full campaign/reply-draft
history (FK-safe cascade), the 404s, the audit trail entry, and that a
deleted lead's number is never reused by a later import.
"""


def _create_campaign(server, name):
    return server.post("/campaigns", json={"name": name}).json()["id"]


def _assign(server, cid, pid):
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    prospects = server.get(f"/campaigns/{cid}/prospects").json()
    return next(p["id"] for p in prospects if p["lead_number"] == f"L-{pid:06d}")


def test_delete_lead_with_no_campaign_history(server):
    pid = server.seed_prospect()
    lead_number = f"L-{pid:06d}"

    r = server.delete(f"/leads/{lead_number}")
    assert r.status_code == 200, r.text
    assert r.json() == {"status": "deleted", "lead_number": lead_number}

    r = server.get(f"/leads/{lead_number}")
    assert r.status_code == 404

    leads = server.get("/leads").json()
    assert not any(l["lead_number"] == lead_number for l in leads)


def test_delete_lead_cascades_campaign_membership_and_reply_drafts(server):
    cid = _create_campaign(server, "Delete Cascade Test")
    pid = server.seed_prospect()
    row_id = _assign(server, cid, pid)
    lead_number = f"L-{pid:06d}"

    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={"reply_body": "tell me more"})

    drafts_before = server.get("/reply-drafts").json()
    assert any(d["campaign_prospect_id"] == row_id for d in drafts_before)

    r = server.delete(f"/leads/{lead_number}")
    assert r.status_code == 200, r.text

    # The campaign membership and its reply draft are gone too, not just the lead.
    drafts_after = server.get("/reply-drafts").json()
    assert not any(d["campaign_prospect_id"] == row_id for d in drafts_after)

    prospects = server.get(f"/campaigns/{cid}/prospects").json()
    assert not any(p["id"] == row_id for p in prospects)

    # The campaign itself survives -- only this lead's membership in it is gone.
    r = server.get(f"/campaigns/{cid}/prospects")
    assert r.status_code == 200


def test_delete_unknown_lead_number_returns_404(server):
    r = server.delete("/leads/L-999999")
    assert r.status_code == 404


def test_delete_malformed_lead_number_returns_404(server):
    r = server.delete("/leads/not-a-lead-number")
    assert r.status_code == 404


def test_delete_lead_logs_audit_event(server):
    pid = server.seed_prospect()
    lead_number = f"L-{pid:06d}"
    server.delete(f"/leads/{lead_number}")

    rows = server.raw_query(
        "SELECT * FROM audit_log WHERE event_type = 'lead_deleted' AND entity_id = ?", (str(pid),)
    )
    assert len(rows) == 1
    assert lead_number in rows[0]["details"]


def test_deleted_lead_number_is_never_reused(server):
    pid_1 = server.seed_prospect()
    lead_number_1 = f"L-{pid_1:06d}"
    server.delete(f"/leads/{lead_number_1}")

    pid_2 = server.seed_prospect()
    lead_number_2 = f"L-{pid_2:06d}"

    assert pid_2 > pid_1
    assert lead_number_2 != lead_number_1

    # The old number resolves to nothing -- it isn't silently pointing at the new lead.
    r = server.get(f"/leads/{lead_number_1}")
    assert r.status_code == 404
    r = server.get(f"/leads/{lead_number_2}")
    assert r.status_code == 200
