"""
Existing-customer list upload (Knowledge Base tab): POST
/knowledge-base/customers/import, and its two read endpoints.

Mirrors the stock catalog's replace-on-import semantics (see
app/services/customers.py's docstring) -- each upload is a full
snapshot that replaces the previous list, not an additive merge. A
fresh server (module-scoped, see conftest.py) starts with the two demo
customers db.py seeds by default (jsmith@acmecorp.com/Acme Corp,
dlee@globex.com/Globex Inc); the first test below relies on that
starting state, and every import after it replaces those seed rows too.
"""


def _upload_customers(server, content, filename="customers.csv"):
    return server.session.post(
        server.base_url + "/knowledge-base/customers/import",
        files={"file": (filename, content, "text/csv")},
    )


def test_starts_with_seeded_demo_customers(server):
    r = server.get("/knowledge-base/customers/count")
    assert r.status_code == 200
    assert r.json()["total"] == 2

    rows = server.get("/knowledge-base/customers").json()
    assert {r["email"] for r in rows} == {"jsmith@acmecorp.com", "dlee@globex.com"}


def test_import_replaces_the_seeded_list_entirely(server):
    csv = (
        "Email,Company\n"
        "buyer1@steelbuyer.example,Steel Buyer Co\n"
        "buyer2@steelbuyer.example,Steel Buyer Co\n"
    )
    r = _upload_customers(server, csv)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["customer_count"] == 2
    assert body["filename"] == "customers.csv"
    assert "imported_at" in body

    assert server.get("/knowledge-base/customers/count").json()["total"] == 2
    rows = server.get("/knowledge-base/customers").json()
    emails = {r["email"] for r in rows}
    assert emails == {"buyer1@steelbuyer.example", "buyer2@steelbuyer.example"}
    # The old seeded demo customers are gone -- a real replace, not a merge.
    assert "jsmith@acmecorp.com" not in emails
    assert "dlee@globex.com" not in emails


def test_reimport_replaces_the_previous_upload(server):
    first = "Email,Company\nold@replaced.example,Old Co\n"
    _upload_customers(server, first, filename="first.csv")
    assert server.get("/knowledge-base/customers/count").json()["total"] == 1

    second = "Email,Company\nnew1@replaced.example,New Co\nnew2@replaced.example,New Co\n"
    r = _upload_customers(server, second, filename="second.csv")
    assert r.json()["customer_count"] == 2

    rows = server.get("/knowledge-base/customers").json()
    emails = {r["email"] for r in rows}
    assert emails == {"new1@replaced.example", "new2@replaced.example"}
    assert "old@replaced.example" not in emails


def test_import_dedupes_repeated_emails_within_the_file(server):
    csv = (
        "Email,Company\n"
        "dupe@onefile.example,Dupe Co\n"
        "dupe@onefile.example,Dupe Co\n"
        "unique@onefile.example,Unique Co\n"
    )
    r = _upload_customers(server, csv)
    assert r.json()["customer_count"] == 2

    rows = server.get("/knowledge-base/customers").json()
    assert {r["email"] for r in rows} == {"dupe@onefile.example", "unique@onefile.example"}


def test_import_skips_rows_with_no_email_but_keeps_the_rest(server):
    csv = (
        "Email,Company\n"
        "hasmail@skiprow.example,Has Mail Co\n"
        ",No Mail Co\n"
    )
    r = _upload_customers(server, csv)
    assert r.json()["customer_count"] == 1
    rows = server.get("/knowledge-base/customers").json()
    assert [r["email"] for r in rows] == ["hasmail@skiprow.example"]


def test_import_accepts_aliased_column_headers(server):
    csv = "email_address,organization\naliased@headers.example,Aliased Co\n"
    r = _upload_customers(server, csv)
    assert r.status_code == 200, r.text
    assert r.json()["customer_count"] == 1
    rows = server.get("/knowledge-base/customers").json()
    assert rows[0]["email"] == "aliased@headers.example"
    assert rows[0]["company"] == "Aliased Co"


def test_import_rejects_file_with_no_email_column(server):
    r = _upload_customers(server, "Company\nNo Email Co\n")
    assert r.status_code == 422


def test_import_rejects_empty_file(server):
    r = _upload_customers(server, "Email,Company\n")
    assert r.status_code == 422


def test_import_rejects_file_where_every_row_has_no_email(server):
    r = _upload_customers(server, "Email,Company\n,No Mail Co\n")
    assert r.status_code == 422


def test_customers_search_matches_email_and_company(server):
    csv = (
        "Email,Company\n"
        "match.me@searchtest.example,Findable Steel LLC\n"
        "other@searchtest.example,Other Metals\n"
    )
    _upload_customers(server, csv)

    by_email = server.get("/knowledge-base/customers", params={"search": "match.me"}).json()
    assert [r["email"] for r in by_email] == ["match.me@searchtest.example"]

    by_company = server.get("/knowledge-base/customers", params={"search": "Findable"}).json()
    assert [r["email"] for r in by_company] == ["match.me@searchtest.example"]


def test_uploaded_customer_is_caught_by_prospect_existing_customer_check(server):
    # The whole point of this list: OBJ-002's validation reads it live, so
    # a prospect import matching a freshly-uploaded customer (not just the
    # original demo seed) is classified Existing Customer too.
    _upload_customers(
        server,
        "Email,Company\nprocurement@newlyknown.example,Newly Known Steel\n",
        filename="newly_known.csv",
    )

    prospect_csv = (
        "First Name,Last Name,Email,Company\n"
        "Some,Buyer,procurement@newlyknown.example,Newly Known Steel\n"
    )
    r = server.session.post(
        server.base_url + "/prospects/import",
        files={"file": ("prospect.csv", prospect_csv, "text/csv")},
    )
    batch_id = r.json()["batch_id"]
    server.post(f"/prospects/validate/{batch_id}")

    rows = server.get(f"/prospects/{batch_id}").json()
    assert rows[0]["status"] == "Existing Customer"
    assert rows[0]["lead_number"] is None
