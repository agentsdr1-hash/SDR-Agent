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
