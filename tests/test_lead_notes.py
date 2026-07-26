"""
Lead notes: POST/GET /leads/{lead_number}/notes -- an append-only,
timestamped log distinct from next_action (the single current task, with
its own due date tracked separately -- see test_leads_consolidated.py's
follow_up_due coverage). See add_note()'s docstring in app/services/leads.py
for why notes are never edited or deleted in place.
"""


def test_add_and_list_notes(server):
    pid = server.seed_prospect()
    lead_number = f"L-{pid:06d}"

    r = server.post(f"/leads/{lead_number}/notes", json={"note": "Called, left voicemail"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["note"] == "Called, left voicemail"
    assert body["prospect_id"] == pid
    assert "created_at" in body

    server.post(f"/leads/{lead_number}/notes", json={"note": "Followed up by email"})

    notes = server.get(f"/leads/{lead_number}/notes").json()
    assert len(notes) == 2
    # Newest first.
    assert notes[0]["note"] == "Followed up by email"
    assert notes[1]["note"] == "Called, left voicemail"


def test_notes_appear_in_lead_timeline(server):
    pid = server.seed_prospect()
    lead_number = f"L-{pid:06d}"
    server.post(f"/leads/{lead_number}/notes", json={"note": "Interested in flat bars"})

    detail = server.get(f"/leads/{lead_number}").json()
    assert len(detail["notes"]) == 1
    assert detail["notes"][0]["note"] == "Interested in flat bars"


def test_add_note_rejects_empty_text(server):
    pid = server.seed_prospect()
    lead_number = f"L-{pid:06d}"
    r = server.post(f"/leads/{lead_number}/notes", json={"note": "   "})
    assert r.status_code == 422


def test_add_note_404s_for_unknown_lead(server):
    r = server.post("/leads/L-999999/notes", json={"note": "hello"})
    assert r.status_code == 404
    r = server.get("/leads/L-999999/notes")
    assert r.status_code == 404


def test_deleting_a_lead_also_deletes_its_notes(server):
    pid = server.seed_prospect()
    lead_number = f"L-{pid:06d}"
    server.post(f"/leads/{lead_number}/notes", json={"note": "will be deleted"})

    r = server.delete(f"/leads/{lead_number}")
    assert r.status_code == 200, r.text

    remaining = server.raw_query("SELECT * FROM lead_notes WHERE prospect_id = ?", (pid,))
    assert remaining == []
