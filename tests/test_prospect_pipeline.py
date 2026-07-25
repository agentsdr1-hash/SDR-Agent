"""
OBJ-001/OBJ-002: import -> validate -> edit.

Covers the real CSV-upload path (not the direct-sqlite seed helper) since
this is the one piece of the pipeline that has actual parsing logic
(column-header aliasing, per-row classification) worth exercising for
real.

Import writes every row (never rejects one) -- duplicates, existing
customers, and already-contacted rows are still classified normally by
OBJ-002 and counted in the Dashboard funnel, just excluded from the
Leads tab (see NON_LEAD_STATUSES in app/services/leads.py). Each test
still builds its own CSV with a unique email domain/tag rather than
sharing one fixture, since cross-batch duplicate detection is stateful
across this module's shared DB and would otherwise make later tests'
uploads behave differently depending on what ran before them.
"""
import itertools

_tag_counter = itertools.count()


def _make_csv():
    """A 4-row CSV: one Valid row, one Invalid row (missing email), an
    intra-file duplicate of the Valid row, and a second distinct Valid
    row -- with emails unique to this call so tests never collide.
    Company names are deliberately NOT "Acme Corp"/"Globex Inc" -- those
    are the two demo customers db.py seeds by default, and this module's
    company-name-matching tests would otherwise collide with them."""
    tag = next(_tag_counter)
    return (
        "First Name,Last Name,Email,Company,Phone\n"
        f"Jane,Doe,jane{tag}@doeworks.example,Doeworks Steel,+971-50-0000000\n"
        "No,Email,,Missing Co,\n"                              # Invalid: no email
        f"Jane,Doe,jane{tag}@doeworks.example,Doeworks Steel,\n" # Duplicate of row 1, within this file
        f"Jill,Smith,jill{tag}@newco.example,NewCo,\n"          # Valid
    )


def _upload(server, content=None):
    return server.session.post(
        server.base_url + "/prospects/import",
        files={"file": ("prospects.csv", content or _make_csv(), "text/csv")},
    )


def test_import_maps_headers_and_stages_rows(server):
    r = _upload(server)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 4  # every row is written, nothing rejected at import
    assert body["columns_mapped"]["email"] == "Email"
    assert body["columns_mapped"]["first_name"] == "First Name"


def test_validate_classifies_each_row(server):
    r = _upload(server)
    batch_id = r.json()["batch_id"]

    r = server.post(f"/prospects/validate/{batch_id}")
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["total"] == 4
    assert summary["valid"] == 2       # Jane (first occurrence) + Jill
    assert summary["invalid"] == 1     # missing email
    assert summary["duplicate"] == 1   # Jane's second row, within this same batch


def test_lead_numbers_are_l_prefixed_and_zero_padded(server):
    r = _upload(server)
    batch_id = r.json()["batch_id"]
    server.post(f"/prospects/validate/{batch_id}")

    r = server.get(f"/prospects/{batch_id}")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 4
    for row in rows:
        # Nothing in NON_LEAD_STATUSES (Invalid/Duplicate/Existing
        # Customer/Already Contacted) becomes a lead -- no Lead # at all.
        if row["status"] in ("Invalid", "Duplicate"):
            assert row["lead_number"] is None
            continue
        assert row["lead_number"].startswith("L-")
        assert len(row["lead_number"]) == 8  # "L-" + 6 digits


def test_invalid_row_gets_no_lead_number_and_is_absent_from_leads_until_fixed(server):
    r = _upload(server)
    batch_id = r.json()["batch_id"]
    server.post(f"/prospects/validate/{batch_id}")

    rows = server.get(f"/prospects/{batch_id}").json()
    invalid_row = next(r for r in rows if r["status"] == "Invalid")
    assert invalid_row["lead_number"] is None

    # Not reachable as a lead at all -- not listed, and a direct lookup 404s
    # even though the underlying prospects_raw row (and its id) exists.
    all_leads = server.get("/leads").json()
    assert not any(l["prospect_id"] == invalid_row["id"] for l in all_leads)
    fake_lead_number = f"L-{invalid_row['id']:06d}"
    r = server.get(f"/leads/{fake_lead_number}")
    assert r.status_code == 404

    # Fixing it (same PUT /prospects/{id} flow the Import tab's "Save fix"
    # button drives) re-validates the row -- once it passes, it gets a
    # real Lead # and appears in the Leads tab like anything else.
    r = server.put(f"/prospects/{invalid_row['id']}", json={
        "first_name": "Fixed", "last_name": "Person", "email": "now.valid@example.com",
        "company": "Fixed Co", "phone": "",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Valid"

    rows = server.get(f"/prospects/{batch_id}").json()
    fixed = next(r for r in rows if r["id"] == invalid_row["id"])
    assert fixed["lead_number"] == fake_lead_number

    all_leads = server.get("/leads").json()
    assert any(l["prospect_id"] == invalid_row["id"] and l["lead_number"] == fake_lead_number for l in all_leads)

    r = server.get(f"/leads/{fake_lead_number}")
    assert r.status_code == 200


def test_edit_prospect_fixes_invalid_row_and_sets_qualification_fields(server):
    r = _upload(server)
    batch_id = r.json()["batch_id"]
    server.post(f"/prospects/validate/{batch_id}")

    rows = server.get(f"/prospects/{batch_id}").json()
    invalid_row = next(r for r in rows if r["status"] == "Invalid")
    assert invalid_row["email"] is None or invalid_row["email"] == ""

    r = server.put(f"/prospects/{invalid_row['id']}", json={
        "first_name": "Fixed", "last_name": "Person", "email": "fixed@example.com",
        "company": "Fixed Co", "phone": "",
        "lead_source": "Trade Show", "linkedin_url": "https://linkedin.com/in/fixed",
        "next_action": "Call Monday", "qualification_status": "Qualified",
    })
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "Valid"

    rows = server.get(f"/prospects/{batch_id}").json()
    fixed = next(r for r in rows if r["id"] == invalid_row["id"])
    assert fixed["lead_source"] == "Trade Show"
    assert fixed["linkedin_url"] == "https://linkedin.com/in/fixed"
    assert fixed["next_action"] == "Call Monday"
    assert fixed["qualification_status"] == "Qualified"


def test_reimporting_the_same_validated_file_is_hidden_but_still_counted(server):
    # Simulates a user uploading the same list 3 times (a real support
    # report: "we loaded file 3 times, showing so many records"). Every
    # pass still writes and classifies its rows -- re-imports land as
    # 'Duplicate' -- but none of them show up as a lead, and the
    # Dashboard funnel (a plain GROUP BY over prospects_raw.status) keeps
    # counting every row regardless, so "how many total, how many
    # duplicate" stays answerable across all 3 uploads.
    csv = (
        "First Name,Last Name,Email,Company,Phone\n"
        "Amir,Hassan,amir.hassan@reimport.example,Reimport Co,\n"
        "No,Email,,Missing Co,\n"                                 # Invalid: no email
        "Amir,Hassan,amir.hassan@reimport.example,Reimport Co,\n" # Duplicate of row 1, within this file
        "Sara,Ali,sara.ali@reimport.example,Reimport Co,\n"       # Valid
    )

    def _upload_reimport():
        r = server.session.post(
            server.base_url + "/prospects/import",
            files={"file": ("reimport.csv", csv, "text/csv")},
        )
        assert r.status_code == 200, r.text
        batch_id = r.json()["batch_id"]
        server.post(f"/prospects/validate/{batch_id}")
        return batch_id

    batch_1 = _upload_reimport()
    batch_2 = _upload_reimport()
    batch_3 = _upload_reimport()

    # Only the very first pass's Amir and Sara are ever real leads.
    leads = server.get("/leads", params={"search": "reimport.example"}).json()
    assert {l["email"] for l in leads} == {"amir.hassan@reimport.example", "sara.ali@reimport.example"}
    assert len(leads) == 2

    # But the second and third passes' rows are still there, still
    # classified -- just not as leads. Every row in every batch across
    # all 3 uploads is a real, distinct prospects_raw row.
    for batch_id in (batch_2, batch_3):
        rows = server.get(f"/prospects/{batch_id}").json()
        assert len(rows) == 4
        amir_rows = [r for r in rows if r["email"] == "amir.hassan@reimport.example"]
        sara_rows = [r for r in rows if r["email"] == "sara.ali@reimport.example"]
        assert all(r["status"] == "Duplicate" for r in amir_rows)
        assert all(r["status"] == "Duplicate" for r in sara_rows)
        assert all(r["lead_number"] is None for r in amir_rows + sara_rows)

    # The Dashboard funnel counts every row ever imported, across all 3
    # passes -- 3 people (Amir, missing-email, Sara) uploaded 3 times = 12
    # total rows for this test's own batches, not just the first pass's.
    funnel = server.get("/reports/summary").json()["prospect_status_counts"]
    rows_1 = server.get(f"/prospects/{batch_1}").json()
    rows_2 = server.get(f"/prospects/{batch_2}").json()
    rows_3 = server.get(f"/prospects/{batch_3}").json()
    all_rows = rows_1 + rows_2 + rows_3
    assert len(all_rows) == 12
    for status in ("Valid", "Invalid", "Duplicate"):
        expected = sum(1 for r in all_rows if r["status"] == status)
        assert funnel[status] >= expected  # >= since other tests in this module add their own rows to the same funnel


def test_existing_customer_is_classified_not_valid_and_excluded_from_leads(server):
    # jsmith@acmecorp.com is one of the two customers db.py seeds by
    # default on a fresh DB (init_db(seed_customers=True)) -- re-importing
    # that address should be caught, not treated as a fresh lead.
    csv = "First Name,Last Name,Email,Company\nJohn,Smith,jsmith@acmecorp.com,Acme Corp\n"
    r = server.session.post(
        server.base_url + "/prospects/import",
        files={"file": ("existing.csv", csv, "text/csv")},
    )
    batch_id = r.json()["batch_id"]
    r = server.post(f"/prospects/validate/{batch_id}")
    assert r.json()["existing_customer"] == 1

    rows = server.get(f"/prospects/{batch_id}").json()
    assert rows[0]["status"] == "Existing Customer"
    assert rows[0]["lead_number"] is None

    leads = server.get("/leads", params={"search": "jsmith@acmecorp.com"}).json()
    assert leads == []


def test_existing_customer_matched_by_company_name(server):
    # A *different* contact at Globex Inc (one of the two seeded demo
    # customers, matched by company here rather than by the exact
    # dlee@globex.com address on file) should also be caught -- a company
    # that's already a customer isn't a fresh sales target just because
    # the specific person is new.
    csv = "First Name,Last Name,Email,Company\nNew,Contact,new.contact@globex.com,Globex Inc\n"
    r = server.session.post(
        server.base_url + "/prospects/import",
        files={"file": ("company_match.csv", csv, "text/csv")},
    )
    batch_id = r.json()["batch_id"]
    r = server.post(f"/prospects/validate/{batch_id}")
    assert r.json()["existing_customer"] == 1

    rows = server.get(f"/prospects/{batch_id}").json()
    assert rows[0]["status"] == "Existing Customer"
    assert "Globex Inc" in rows[0]["validation_notes"]


def test_import_rejects_file_with_no_email_column(server):
    r = server.session.post(
        server.base_url + "/prospects/import",
        files={"file": ("bad.csv", "Name,Company\nJane,Acme\n", "text/csv")},
    )
    assert r.status_code == 422


def test_import_rejects_empty_file(server):
    r = server.session.post(
        server.base_url + "/prospects/import",
        files={"file": ("empty.csv", "Email,First Name\n", "text/csv")},
    )
    assert r.status_code == 422
