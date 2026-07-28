"""
GET /reports/summary -- the Dashboard's aggregate math. Seeds one campaign
with four prospects taken to different funnel depths (sent-only, replied,
won, lost) and checks the aggregates agree with what was actually done,
not just that the endpoint returns 200.

Note: the earlier "Closed, no reply" mislabel bug (the funnel tile summing
the wrong status client-side) lived in app.html's JS aggregation, not in
this endpoint -- this file guards the backend math only; a UI-level test
would be needed to catch that specific class of bug recurring.
"""


def _drive_to(server, cid, row_id, target):
    """Walk one Approved prospect forward through the funnel up to (and
    including) `target`: one of 'sent', 'replied', 'won', 'lost'."""
    def step(name):
        endpoints = {
            "sent": (f"/campaigns/{cid}/prospects/{row_id}/simulate-sent", None),
            "replied": (f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", {"reply_body": "hi"}),
            "quoted": (f"/campaigns/{cid}/prospects/{row_id}/request-quote", None),
            "won": (f"/campaigns/{cid}/prospects/{row_id}/won", {"deal_value": 50000}),
            "lost": (f"/campaigns/{cid}/prospects/{row_id}/lost", {"reason": "price"}),
        }
        path, payload = endpoints[name]
        r = server.post(path, json=payload) if payload is not None else server.post(path)
        assert r.status_code == 200, f"{name} failed: {r.text}"

    if target == "sent":
        full_path = ["sent"]
    elif target == "replied":
        full_path = ["sent", "replied"]
    else:  # "won" or "lost"
        full_path = ["sent", "replied", "quoted", target]
    for name in full_path:
        step(name)


def test_summary_math_matches_seeded_funnel(server):
    cid = server.post("/campaigns", json={"name": "Reporting Test"}).json()["id"]

    row_ids = {}
    for label in ("sent_only", "replied_only", "won", "lost"):
        pid = server.seed_prospect(email=f"{label}@example.com")
        server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
        prospects = server.get(f"/campaigns/{cid}/prospects").json()
        row_id = next(p["id"] for p in prospects if p["email"] == f"{label}@example.com")
        server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
        row_ids[label] = row_id

    _drive_to(server, cid, row_ids["sent_only"], "sent")
    _drive_to(server, cid, row_ids["replied_only"], "replied")
    _drive_to(server, cid, row_ids["won"], "won")
    _drive_to(server, cid, row_ids["lost"], "lost")

    summary = server.get("/reports/summary").json()

    assert summary["value_captured"]["customers_won"] == 1
    assert summary["value_captured"]["deals_lost"] == 1
    assert summary["value_captured"]["quotes_requested"] == 2  # Won + Lost both passed through QuoteRequested
    assert summary["value_captured"]["total_turnover"] == 50000
    assert summary["value_captured"]["win_rate_pct"] == 50.0

    assert summary["sdr_performance"]["total_emails_sent"] == 4       # all 4 reached Sent
    assert summary["sdr_performance"]["unique_leads_replied"] == 3    # all but sent_only
    assert summary["sdr_performance"]["total_reply_messages"] == 3    # one reply round each here
    assert summary["sdr_performance"]["response_rate_pct"] == 75.0

    campaign_summary = next(c for c in summary["campaigns"] if c["id"] == cid)
    assert campaign_summary["won"] == 1
    assert campaign_summary["lost"] == 1
    assert campaign_summary["turnover"] == 50000
    assert campaign_summary["total"] == 4

    # 'replied' is the current-status snapshot -- only replied_only is
    # actually sitting in that status right now, since won/lost moved on
    # through it to Quote Requested and then their outcome. 'ever_replied'
    # is the fuller count: all 3 leads that replied at some point, whether
    # or not they're still sitting in that exact status -- this is what
    # the Campaigns tab and Dashboard overview tiles are built on, so a
    # 2-campaign summary doesn't read as "4 replied, 2 won" when the 2 Won
    # leads replied too on their way there.
    assert campaign_summary["replied"] == 1        # replied_only, currently in that status
    assert campaign_summary["ever_replied"] == 3    # replied_only + won + lost


def test_summary_counts_every_reply_round_not_just_unique_leads(server):
    # This industry runs on multi-round back-and-forth -- a lead who
    # writes back three times is still 1 unique lead who engaged, but
    # should count as 3 replies in the true volume metric and show up on
    # the activity chart 3 times, not silently collapse to 1 (or worse,
    # to whichever day the *last* round happened to land on).
    before = server.get("/reports/summary").json()
    before_perf = before["sdr_performance"]
    before_activity_replied = sum(day["replied"] for day in before["activity_by_day"])

    cid = server.post("/campaigns", json={"name": "Multi-Round Reporting Test"}).json()["id"]
    pid = server.seed_prospect(email="multiround@example.com")
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    row_id = server.get(f"/campaigns/{cid}/prospects").json()[0]["id"]
    server.post(f"/campaigns/{cid}/prospects/{row_id}/approve")
    server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-sent")

    for i in range(3):
        r = server.post(f"/campaigns/{cid}/prospects/{row_id}/simulate-reply", json={"reply_body": f"round {i+1}"})
        assert r.status_code == 200, r.text

    after = server.get("/reports/summary").json()
    after_perf = after["sdr_performance"]
    after_activity_replied = sum(day["replied"] for day in after["activity_by_day"])

    assert after_perf["unique_leads_replied"] - before_perf["unique_leads_replied"] == 1
    assert after_perf["total_reply_messages"] - before_perf["total_reply_messages"] == 3
    assert after_activity_replied - before_activity_replied == 3


def test_summary_prospect_status_counts_reflect_validation(server):
    server.seed_prospect(status="Valid")
    server.seed_prospect(status="Invalid")
    server.seed_prospect(status="Duplicate")

    summary = server.get("/reports/summary").json()
    counts = summary["prospect_status_counts"]
    assert counts["Valid"] >= 1
    assert counts["Invalid"] >= 1
    assert counts["Duplicate"] >= 1
    assert summary["total_prospects"] == sum(counts.values())
