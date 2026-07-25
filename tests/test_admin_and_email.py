"""
OBJ-015 (suppression list) + OBJ-016 (email status/config/daily limit).
No real Gmail credentials are used -- /email/config only validates shape
(address format, 16-char app password), it doesn't attempt a real SMTP
login (that's /email/test-connection, deliberately not exercised here
since it requires a live Gmail account).
"""


def test_suppression_add_list_remove(server):
    r = server.post("/admin/suppressed", json={"email": "Block@Example.com", "reason": "opted out"})
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["email"] == "block@example.com"  # normalized to lowercase
    assert entry["source"] == "manual"

    entries = server.get("/admin/suppressed").json()
    assert any(e["email"] == "block@example.com" for e in entries)

    r = server.delete("/admin/suppressed/block@example.com")
    assert r.status_code == 200

    entries = server.get("/admin/suppressed").json()
    assert not any(e["email"] == "block@example.com" for e in entries)


def test_removing_unsuppressed_email_is_a_404(server):
    r = server.delete("/admin/suppressed/never-suppressed@example.com")
    assert r.status_code == 404


def test_email_status_defaults_and_not_configured(server):
    r = server.get("/email/status")
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["daily_send_limit"] == 20  # DEFAULT_DAILY_SEND_LIMIT
    assert body["sent_today"] == 0
    assert body["source"] == "none"


def test_daily_send_limit_get_and_put(server):
    r = server.put("/email/daily-send-limit", json={"limit": 5})
    assert r.status_code == 200
    assert r.json()["daily_send_limit"] == 5

    r = server.get("/email/status")
    assert r.json()["daily_send_limit"] == 5


def test_daily_send_limit_rejects_non_positive(server):
    r = server.put("/email/daily-send-limit", json={"limit": 0})
    assert r.status_code == 422
    r = server.put("/email/daily-send-limit", json={"limit": -5})
    assert r.status_code == 422


def test_email_config_validates_address_and_app_password_shape(server):
    r = server.put("/email/config", json={"gmail_address": "not-an-email", "app_password": "x" * 16})
    assert r.status_code == 422

    r = server.put("/email/config", json={"gmail_address": "sales@example.com", "app_password": "tooshort"})
    assert r.status_code == 422

    r = server.put("/email/config", json={"gmail_address": "sales@example.com", "app_password": "abcd efgh ijkl mnop"})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["gmail_address"] == "sales@example.com"
    assert body["source"] == "database"

    r = server.delete("/email/config")
    assert r.status_code == 200
    assert r.json()["configured"] is False
