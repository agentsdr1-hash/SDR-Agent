"""
GET /leads -- the cross-campaign consolidated view -- plus its filters,
bulk actions, and the rule-based lead score / quote-readiness math that
feed the Leads tab's Score column and the Quote Document.
"""
from datetime import date, timedelta

from conftest import ADMIN_HEADERS

FULL_CHECKLIST = {
    "materials": "Flat bars", "sku_spec": "FB-50x5", "quantity": "50 tons",
    "unit_of_measure": "tons", "destination": "Dubai", "shipping_terms": "FOB",
    "delivery_date": "2026-08-01", "currency": "AED", "payment_terms": "Net 30",
    "packaging_requirements": "Bundled", "quote_notes": "Handle with care",
    "target_price": 150000,
}


def _create_campaign(server, name):
    return server.post("/campaigns", json={"name": name}).json()["id"]


def _assign(server, cid, pid):
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    prospects = server.get(f"/campaigns/{cid}/prospects").json()
    return next(p["id"] for p in prospects if p["lead_number"] == f"L-{pid:06d}")


def test_leads_list_includes_prospects_with_no_campaign(server):
    pid = server.seed_prospect()
    leads = server.get("/leads").json()
    lead = next(l for l in leads if l["prospect_id"] == pid)
    assert lead["status"] == "Imported -- not yet in a campaign"
    assert lead["campaign_id"] is None
    assert lead["lead_score"] == 5  # base score for an unqualified, uncontacted lead


def test_filter_by_validation_status(server):
    server.seed_prospect(status="Invalid")
    server.seed_prospect(status="Valid")
    # Invalid rows never became leads -- see list_leads()'s docstring --
    # so even an explicit filter for them returns nothing.
    invalid_leads = server.get("/leads", params={"validation_status": "Invalid"}).json()
    assert invalid_leads == []

    valid_leads = server.get("/leads", params={"validation_status": "Valid"}).json()
    assert len(valid_leads) >= 1
    assert all(l["validation_status"] == "Valid" for l in valid_leads)


def test_invalid_rows_excluded_from_leads_list_entirely(server):
    server.seed_prospect(status="Invalid")
    leads = server.get("/leads").json()
    assert all(l["validation_status"] != "Invalid" for l in leads)


def test_filter_by_campaign_status(server):
    cid = _create_campaign(server, "Status Filter Test")
    pid = server.seed_prospect()
    _assign(server, cid, pid)

    queued = server.get("/leads", params={"status": "Queued"}).json()
    assert any(l["prospect_id"] == pid for l in queued)

    sent = server.get("/leads", params={"status": "Sent"}).json()
    assert not any(l["prospect_id"] == pid for l in sent)


def test_filter_by_ever_sent_replied_quoted(server):
    cid = _create_campaign(server, "Activity Filter Test")
    pid = server.seed_prospect()
    row_id = _assign(server, cid, pid)
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")

    ever_sent = server.get("/leads", params={"ever_sent": "true"}).json()
    assert any(l["prospect_id"] == pid for l in ever_sent)
    ever_replied = server.get("/leads", params={"ever_replied": "true"}).json()
    assert not any(l["prospect_id"] == pid for l in ever_replied)

    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={"reply_body": "tell me more"})
    ever_replied = server.get("/leads", params={"ever_replied": "true"}).json()
    assert any(l["prospect_id"] == pid for l in ever_replied)


def test_lead_score_reflects_source_linkedin_and_stage(server):
    pid = server.seed_prospect()
    server.put(f"/prospects/{pid}", json={
        "first_name": "Jane", "last_name": "Doe", "email": f"score{pid}@example.com", "company": "Acme",
        "phone": "", "lead_source": "Referral", "linkedin_url": "https://linkedin.com/in/jane",
    })
    cid = _create_campaign(server, "Score Test")
    _assign(server, cid, pid)

    lead = next(l for l in server.get("/leads").json() if l["prospect_id"] == pid)
    # Queued(10) + Referral(15) + LinkedIn(5) + readiness(0) = 30
    assert lead["lead_score"] == 30
    assert lead["lead_source"] == "Referral"


def test_quote_readiness_and_quote_ready_filter(server):
    cid = _create_campaign(server, "Readiness Test")
    pid = server.seed_prospect()
    row_id = _assign(server, cid, pid)

    lead = next(l for l in server.get("/leads").json() if l["prospect_id"] == pid)
    assert lead["quote_readiness"] == {"filled": 0, "total": 11, "pct": 0.0, "ready": False}

    r = server.put(f"/campaigns/{cid}/prospects/{row_id}/quote-details", json=FULL_CHECKLIST)
    assert r.status_code == 200, r.text

    lead = next(l for l in server.get("/leads").json() if l["prospect_id"] == pid)
    assert lead["quote_readiness"] == {"filled": 11, "total": 11, "pct": 1.0, "ready": True}

    ready_leads = server.get("/leads", params={"quote_ready": "true"}).json()
    assert any(l["prospect_id"] == pid for l in ready_leads)


def _seed_with_due(server, due):
    pid = server.seed_prospect()
    r = server.put(f"/prospects/{pid}", json={
        "first_name": "Jane", "last_name": "Doe", "email": f"followup{pid}@example.com", "company": "Acme",
        "phone": "", "next_action": "Follow up", "next_action_due": due,
    })
    assert r.status_code == 200, r.text
    return pid


def test_next_action_due_round_trips_through_edit_and_leads_list(server):
    pid = _seed_with_due(server, "2026-08-01")
    lead = next(l for l in server.get("/leads").json() if l["prospect_id"] == pid)
    assert lead["next_action"] == "Follow up"
    assert lead["next_action_due"] == "2026-08-01"


def test_follow_up_due_filter_matches_only_today_or_earlier(server):
    today = date.today().isoformat()
    past = (date.today() - timedelta(days=3)).isoformat()
    future = (date.today() + timedelta(days=30)).isoformat()

    overdue_id = _seed_with_due(server, past)
    due_today_id = _seed_with_due(server, today)
    future_id = _seed_with_due(server, future)
    no_due_id = server.seed_prospect()

    due_ids = {l["prospect_id"] for l in server.get("/leads", params={"follow_up_due": "true"}).json()}
    assert overdue_id in due_ids
    assert due_today_id in due_ids
    assert future_id not in due_ids
    assert no_due_id not in due_ids


def test_bulk_assign(server):
    cid = _create_campaign(server, "Bulk Assign Test")
    ids = [server.seed_prospect() for _ in range(3)]
    r = server.post("/leads/bulk-assign", json={"prospect_ids": ids, "campaign_id": cid})
    assert r.status_code == 200
    body = r.json()
    assert body["succeeded"] == 3

    prospects = server.get(f"/campaigns/{cid}/prospects").json()
    assert len(prospects) == 3


def test_bulk_assign_reports_partial_failure_without_crashing(server):
    cid = _create_campaign(server, "Bulk Assign Partial Test")
    valid_id = server.seed_prospect()
    invalid_id = server.seed_prospect(status="Invalid")
    r = server.post("/leads/bulk-assign", json={"prospect_ids": [valid_id, invalid_id], "campaign_id": cid})
    body = r.json()
    assert body["succeeded"] == 1
    assert body["failed"] == 1
    assert len(body["errors"]) == 1


def test_bulk_suppress(server):
    ids = []
    emails = []
    for i in range(3):
        e = f"suppress{i}@example.com"
        emails.append(e)
        ids.append(server.seed_prospect(email=e))

    r = server.post("/leads/bulk-suppress", json={"prospect_ids": ids, "reason": "unsubscribed"})
    assert r.status_code == 200
    assert r.json()["succeeded"] == 3

    suppressed = {s["email"] for s in server.get("/admin/suppressed", headers=ADMIN_HEADERS).json()}
    assert set(emails).issubset(suppressed)
