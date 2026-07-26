"""
OBJ-011-lite: Quote Requested -> Won/Lost/reopen, the Quote Readiness
Checklist, the ERP quote-number tie-back, and the quote-summary draft
email. Nothing here does real pricing/quotation -- it's status transitions
and free-text checklist fields, but the transition rules and the
quote_number COALESCE-on-Won behavior are exactly the kind of logic that's
easy to silently break while touching something else.
"""

FULL_CHECKLIST = {
    "materials": "Flat bars", "sku_spec": "FB-50x5", "quantity": "50 tons",
    "unit_of_measure": "tons", "destination": "Dubai", "shipping_terms": "FOB",
    "delivery_date": "2026-08-01", "currency": "AED", "payment_terms": "Net 30",
    "packaging_requirements": "Bundled", "quote_notes": "Handle with care",
    "target_price": 150000,
}


def _replied_prospect(server, campaign_name):
    cid = server.post("/campaigns", json={"name": campaign_name}).json()["id"]
    pid = server.seed_prospect()
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={"reply_body": "interested"})
    return cid, row_id


def test_request_quote_allowed_from_queued_approved_sent_or_replied(server):
    # A quote request doesn't require any email exchange first -- a phone
    # call or walk-in can jump straight from Queued to QuoteRequested, not
    # just after a reply. Covers all four pre-quote statuses.
    for status_to_reach, expect_ok in [("Queued", True), ("Approved", True), ("Sent", True), ("Replied", True)]:
        cid = server.post("/campaigns", json={"name": f"Quote Gate {status_to_reach}"}).json()["id"]
        pid = server.seed_prospect()
        server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
        row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
        if status_to_reach in ("Approved", "Sent", "Replied"):
            server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
        if status_to_reach in ("Sent", "Replied"):
            server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")
        if status_to_reach == "Replied":
            server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={"reply_body": "interested"})

        r = server.post(f"/campaigns/{cid}/prospects/{row_id}/request-quote")
        assert r.status_code == 200, f"{status_to_reach}: {r.text}"
        prospect = server.get(f"/campaigns/{cid}/prospects").json()[0]
        assert prospect["status"] == "QuoteRequested"


def test_request_quote_rejected_from_dead_end_or_already_quoted_statuses(server):
    cid = server.post("/campaigns", json={"name": "Quote Gate Rejected"}).json()["id"]
    pid = server.seed_prospect()
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/reject")

    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/request-quote")
    assert r.status_code == 422  # Rejected is a dead end

    cid2, row_id2 = _replied_prospect(server, "Quote Gate Rejected 2")
    server.post(f"/campaigns/{cid2}/prospects/{row_id2}/request-quote")
    r = server.post(f"/campaigns/{cid2}/prospects/{row_id2}/request-quote")
    assert r.status_code == 422  # already QuoteRequested


def test_full_checklist_round_trips(server):
    cid, row_id = _replied_prospect(server, "Checklist Test")
    r = server.put(f"/campaigns/{cid}/prospects/{row_id}/quote-details", json=FULL_CHECKLIST)
    assert r.status_code == 200, r.text

    lead = next(l for l in server.get("/leads").json() if l["campaign_prospect_id"] == row_id)
    assert lead["materials"] == "Flat bars"
    assert lead["target_price"] == 150000
    assert lead["quote_readiness"]["ready"] is True


def test_won_lost_reopen_transitions(server):
    cid, row_id = _replied_prospect(server, "Outcome Test")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/request-quote")

    # Won requires QuoteRequested/Won/Lost -- not Replied.
    cid2, row_id2 = _replied_prospect(server, "Outcome Test Direct Won")
    r = server.post(f"/campaigns/{cid2}/prospects/{row_id2}/won", json={"deal_value": 1000})
    assert r.status_code == 422

    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/won", json={"deal_value": 50000})
    assert r.status_code == 200
    prospect = server.get(f"/campaigns/{cid}/prospects").json()[0]
    assert prospect["status"] == "Won"
    assert prospect["deal_value"] == 50000

    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/lost", json={"reason": "went with competitor"})
    assert r.status_code == 200
    prospect = server.get(f"/campaigns/{cid}/prospects").json()[0]
    assert prospect["status"] == "Lost"
    assert prospect["lost_reason"] == "went with competitor"
    assert prospect["deal_value"] is None  # switching outcomes clears the other one's fields

    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/reopen")
    assert r.status_code == 200
    prospect = server.get(f"/campaigns/{cid}/prospects").json()[0]
    assert prospect["status"] == "QuoteRequested"
    assert prospect["lost_reason"] is None

    # reopen only valid from Won/Lost.
    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/reopen")
    assert r.status_code == 422


def test_quote_number_set_standalone_and_preserved_through_won(server):
    cid, row_id = _replied_prospect(server, "Quote Number Test")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/request-quote")

    r = server.put(f"/campaigns/{cid}/prospects/{row_id}/quote-number", json={"quote_number": "AKEIS-Q-2026-0001"})
    assert r.status_code == 200

    # Marking Won without passing a quote_number must NOT clear the one already set.
    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/won", json={"deal_value": 20000})
    assert r.status_code == 200

    lead = next(l for l in server.get("/leads").json() if l["campaign_prospect_id"] == row_id)
    assert lead["quote_number"] == "AKEIS-Q-2026-0001"

    # Won *with* a quote_number should overwrite it.
    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/won", json={"deal_value": 20000, "quote_number": "AKEIS-Q-2026-0002"})
    assert r.status_code == 200
    lead = next(l for l in server.get("/leads").json() if l["campaign_prospect_id"] == row_id)
    assert lead["quote_number"] == "AKEIS-Q-2026-0002"


def test_draft_quote_summary_requires_at_least_one_filled_field(server):
    cid, row_id = _replied_prospect(server, "Empty Checklist Draft Test")
    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/draft-quote-summary")
    assert r.status_code == 422


def test_draft_quote_summary_creates_reviewable_draft(server):
    cid, row_id = _replied_prospect(server, "Draft Summary Test")
    server.put(f"/campaigns/{cid}/prospects/{row_id}/quote-details", json=FULL_CHECKLIST)

    r = server.post(f"/campaigns/{cid}/prospects/{row_id}/draft-quote-summary")
    assert r.status_code == 200, r.text

    drafts = server.get("/reply-drafts").json()
    draft = next(d for d in drafts if d["campaign_prospect_id"] == row_id)
    assert draft["status"] == "Draft"
    assert draft["confidence"] == "quote_summary"
    assert "Flat bars" in draft["body"]
    assert "FOB" in draft["body"]
    assert draft["subject"].startswith("Quote request summary")

    # Nothing was actually sent -- still just a draft awaiting human approval.
    assert draft["sent_at"] is None
