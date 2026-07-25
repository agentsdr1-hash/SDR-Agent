"""
OBJ-001/OBJ-002: import -> validate -> edit.

Covers the real CSV-upload path (not the direct-sqlite seed helper) since
this is the one piece of the pipeline that has actual parsing logic
(column-header aliasing, per-row classification) worth exercising for
real.

Each test builds its own CSV with a unique email domain/tag rather than
sharing one fixture -- the import-time duplicate gate (see
prospect_import.py) rejects re-uploading an already-known email by
design, so two tests both using e.g. jane@acme.example would interfere
with each other's expected counts on this module's shared DB.
"""
import itertools

_tag_counter = itertools.count()


def _make_csv():
    """A 4-row CSV: one Valid row, one Invalid row (missing email), an
    intra-file duplicate of the Valid row, and a second distinct Valid
    row -- with emails unique to this call so tests never collide."""
    tag = next(_tag_counter)
    return (
        "First Name,Last Name,Email,Company,Phone\n"
        f"Jane,Doe,jane{tag}@acme.example,Acme Corp,+971-50-0000000\n"
        "No,Email,,Missing Co,\n"                          # Invalid: no email
        f"Jane,Doe,jane{tag}@acme.example,Acme Corp,\n"     # Duplicate of row 1, within this file
        f"Jill,Smith,jill{tag}@newco.example,NewCo,\n"      # Valid
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
    # 3 rows land (Jane once, the invalid row, Jill) -- Jane's second,
    # intra-file-duplicate row is rejected at the import gate itself and
    # never becomes a row at all.
    assert body["row_count"] == 3
    assert body["duplicate_count"] == 1
    assert body["columns_mapped"]["email"] == "Email"
    assert body["columns_mapped"]["first_name"] == "First Name"


def test_validate_classifies_each_row(server):
    r = _upload(server)
    batch_id = r.json()["batch_id"]

    r = server.post(f"/prospects/validate/{batch_id}")
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["total"] == 3
    assert summary["valid"] == 2       # Jane + Jill
    assert summary["invalid"] == 1     # missing email
    assert summary["duplicate"] == 0   # the intra-file dup never reached this batch to begin with


def test_lead_numbers_are_l_prefixed_and_zero_padded(server):
    r = _upload(server)
    batch_id = r.json()["batch_id"]
    server.post(f"/prospects/validate/{batch_id}")

    r = server.get(f"/prospects/{batch_id}")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 3
    for row in rows:
        # A row that failed import validation never became a lead -- it
        # gets no Lead # at all (see list_leads()'s docstring for why).
        if row["status"] == "Invalid":
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


def test_reimporting_the_same_validated_file_is_rejected_at_import(server):
    # Simulates a user uploading the same list 3 times (a real support
    # report: "we loaded file 3 times, showing so many records" and "no
    # value seeing I tried to upload same person 4 times -- the import is
    # the gatekeeping check"). Import is now the gate: a row whose email
    # already belongs to a real, already-validated prospect from an
    # earlier batch is rejected right there and never written -- there's
    # nothing to fix or show, so it doesn't get a row, a status, or a
    # place in any table.
    csv = (
        "First Name,Last Name,Email,Company,Phone\n"
        "Amir,Hassan,amir.hassan@reimport.example,Reimport Co,\n"
        "No,Email,,Missing Co,\n"                                 # Invalid: no email
        "Amir,Hassan,amir.hassan@reimport.example,Reimport Co,\n" # Duplicate of row 1, within this file
        "Sara,Ali,sara.ali@reimport.example,Reimport Co,\n"       # Valid
    )

    def _upload_reimport():
        return server.session.post(
            server.base_url + "/prospects/import",
            files={"file": ("reimport.csv", csv, "text/csv")},
        )

    # Pass 1: Amir's second row (an intra-file duplicate) is rejected at
    # import; the rest lands and validates normally.
    r = _upload_reimport()
    summary_1 = r.json()
    assert summary_1["row_count"] == 3        # Amir once, the invalid row, Sara
    assert summary_1["duplicate_count"] == 1  # Amir's second row within this file
    batch_1 = summary_1["batch_id"]
    validate_1 = server.post(f"/prospects/validate/{batch_1}").json()
    assert validate_1["valid"] == 2  # Amir + Sara
    assert validate_1["invalid"] == 1

    # Passes 2 and 3: Amir and Sara are now real, validated prospects, so
    # every row matching either email is rejected before it's even
    # written -- on the third re-upload too, not just the second. Only
    # the still-blank-email row has nothing to compare against, so it
    # lands (and validates Invalid) fresh on every pass.
    for _ in range(2):
        r = _upload_reimport()
        summary = r.json()
        assert summary["row_count"] == 1        # only the missing-email row
        assert summary["duplicate_count"] == 3  # Amir x2 + Sara
        validate = server.post(f"/prospects/validate/{summary['batch_id']}").json()
        assert validate["invalid"] == 1
        assert validate["valid"] == 0

    # Nothing from the repeat uploads shows up as an extra lead -- there's
    # nothing to show, because nothing new was ever written for them.
    leads = server.get("/leads", params={"search": "reimport.example"}).json()
    emails = {l["email"] for l in leads}
    assert emails == {"amir.hassan@reimport.example", "sara.ali@reimport.example"}


def test_reupload_of_entirely_known_emails_inserts_nothing(server):
    csv = (
        "First Name,Last Name,Email,Company\n"
        "Omar,Khan,omar.khan@allknown.example,Steel Co\n"
        "Layla,Nasser,layla.nasser@allknown.example,Metal Works\n"
    )
    r = server.session.post(
        server.base_url + "/prospects/import",
        files={"file": ("allknown1.csv", csv, "text/csv")},
    )
    batch_1 = r.json()["batch_id"]
    server.post(f"/prospects/validate/{batch_1}")

    r = server.session.post(
        server.base_url + "/prospects/import",
        files={"file": ("allknown2.csv", csv, "text/csv")},
    )
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["row_count"] == 0
    assert summary["duplicate_count"] == 2

    # The batch itself still exists (for audit history), but has no rows
    # -- nothing to review, nothing to validate.
    r = server.get(f"/prospects/{summary['batch_id']}")
    assert r.status_code == 404


def test_existing_customer_is_classified_not_valid(server):
    # jsmith@acmecorp.com is one of the two customers db.py seeds by
    # default on a fresh DB (init_db(seed_customers=True)) -- re-importing
    # that address should be caught, not treated as a fresh lead. This
    # doesn't hit the import-time duplicate gate (that only fires for an
    # email that's already a prospects_raw row; a customer-list match is
    # OBJ-002's job at validate time), so it still lands as its own row.
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
