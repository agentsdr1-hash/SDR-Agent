"""
OBJ-001/OBJ-002: import -> validate -> edit.

Covers the real CSV-upload path (not the direct-sqlite seed helper) since
this is the one piece of the pipeline that has actual parsing logic
(column-header aliasing, per-row classification) worth exercising for
real.
"""

CSV_ROWS = (
    "First Name,Last Name,Email,Company,Phone\n"
    "Jane,Doe,jane@acme.example,Acme Corp,+971-50-0000000\n"
    "No,Email,,Missing Co,\n"                          # Invalid: no email
    "Jane,Doe,jane@acme.example,Acme Corp,\n"           # Duplicate of row 1
    "Jill,Smith,jill.smith@newco.example,NewCo,\n"      # Valid
)


def _upload(server):
    return server.session.post(
        server.base_url + "/prospects/import",
        files={"file": ("prospects.csv", CSV_ROWS, "text/csv")},
    )


def test_import_maps_headers_and_stages_rows(server):
    r = _upload(server)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["row_count"] == 4
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
    assert summary["duplicate"] == 1   # Jane's second row


def test_lead_numbers_are_l_prefixed_and_zero_padded(server):
    r = _upload(server)
    batch_id = r.json()["batch_id"]
    server.post(f"/prospects/validate/{batch_id}")

    r = server.get(f"/prospects/{batch_id}")
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 4
    for row in rows:
        assert row["lead_number"].startswith("L-")
        assert len(row["lead_number"]) == 8  # "L-" + 6 digits


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


def test_reimporting_the_same_validated_file_marks_rows_duplicate(server):
    # Simulates a user uploading the same list 3 times (a real support
    # report: "we loaded file 3 times, showing so many records" and the
    # Dashboard funnel counting the same 2 people as Valid on every pass).
    # Only the first, already-validated pass should count as Valid --
    # later passes must catch the repeat even though nothing in the first
    # pass was ever added to a campaign or sent (that's a separate check,
    # _get_contacted_emails, which this scenario doesn't reach). Uses its
    # own emails (not CSV_ROWS/Jane/Jill) since this module's server/DB is
    # shared across the whole file and earlier tests already imported those.
    reimport_csv = (
        "First Name,Last Name,Email,Company,Phone\n"
        "Amir,Hassan,amir.hassan@reimport.example,Reimport Co,\n"
        "No,Email,,Missing Co,\n"                                 # Invalid: no email
        "Amir,Hassan,amir.hassan@reimport.example,Reimport Co,\n" # Duplicate of row 1
        "Sara,Ali,sara.ali@reimport.example,Reimport Co,\n"       # Valid
    )

    def _upload_reimport():
        return server.session.post(
            server.base_url + "/prospects/import",
            files={"file": ("reimport.csv", reimport_csv, "text/csv")},
        )

    r = _upload_reimport()
    batch_1 = r.json()["batch_id"]
    summary_1 = server.post(f"/prospects/validate/{batch_1}").json()
    assert summary_1["valid"] == 2       # Amir + Sara
    assert summary_1["duplicate"] == 1   # Amir's second row, within this same batch

    r = _upload_reimport()
    batch_2 = r.json()["batch_id"]
    summary_2 = server.post(f"/prospects/validate/{batch_2}").json()
    assert summary_2["valid"] == 0       # Amir and Sara both already imported in batch_1
    assert summary_2["duplicate"] == 3   # Amir x2 + Sara, all duplicates of batch_1 (or each other)

    r = _upload_reimport()
    batch_3 = r.json()["batch_id"]
    summary_3 = server.post(f"/prospects/validate/{batch_3}").json()
    assert summary_3["valid"] == 0
    assert summary_3["duplicate"] == 3

    # Each re-imported row still gets its own immutable lead number (it's
    # a real row in prospects_raw, just flagged) -- but its validation
    # note points back at the original so it's identifiable, not silently
    # counted as a fresh lead.
    rows_2 = server.get(f"/prospects/{batch_2}").json()
    sara_dup = next(r for r in rows_2 if r["email"] == "sara.ali@reimport.example")
    assert sara_dup["status"] == "Duplicate"
    assert "Already imported earlier as" in sara_dup["validation_notes"]

    # Confirm per-batch outcomes directly rather than the site-wide funnel,
    # since this module's DB is shared across the whole file and earlier
    # tests contribute their own rows to it.
    for batch_id, expected_valid, expected_dup in [(batch_1, 2, 1), (batch_2, 0, 3), (batch_3, 0, 3)]:
        rows = server.get(f"/prospects/{batch_id}").json()
        assert sum(1 for r in rows if r["status"] == "Valid") == expected_valid
        assert sum(1 for r in rows if r["status"] == "Duplicate") == expected_dup


def test_existing_customer_is_classified_not_valid(server):
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
