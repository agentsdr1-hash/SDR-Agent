"""
OBJ-015 (suppression list, admin password gate, reset-all-data) + OBJ-016
(email status/config/daily limit). No real Gmail credentials are used --
/email/config only validates shape (address format, 16-char app password),
it doesn't attempt a real SMTP login (that's /email/test-connection,
deliberately not exercised here since it requires a live Gmail account).

Every mutating admin/email route requires the X-Admin-Password header
(ADMIN_HEADERS, from conftest) except GET /email/status, which stays
public -- it powers the always-visible top-bar Gmail badge, not just the
Admin tab, and returns nothing secret.
"""
import subprocess
import sys
from pathlib import Path

from conftest import ADMIN_HEADERS, REPO_ROOT


def test_suppression_add_list_remove(server):
    r = server.post("/admin/suppressed", json={"email": "Block@Example.com", "reason": "opted out"}, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    entry = r.json()
    assert entry["email"] == "block@example.com"  # normalized to lowercase
    assert entry["source"] == "manual"

    entries = server.get("/admin/suppressed", headers=ADMIN_HEADERS).json()
    assert any(e["email"] == "block@example.com" for e in entries)

    r = server.delete("/admin/suppressed/block@example.com", headers=ADMIN_HEADERS)
    assert r.status_code == 200

    entries = server.get("/admin/suppressed", headers=ADMIN_HEADERS).json()
    assert not any(e["email"] == "block@example.com" for e in entries)


def test_removing_unsuppressed_email_is_a_404(server):
    r = server.delete("/admin/suppressed/never-suppressed@example.com", headers=ADMIN_HEADERS)
    assert r.status_code == 404


def test_email_status_defaults_and_not_configured(server):
    r = server.get("/email/status")  # deliberately no header -- must stay public
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["daily_send_limit"] == 20  # DEFAULT_DAILY_SEND_LIMIT
    assert body["sent_today"] == 0
    assert body["source"] == "none"


def test_daily_send_limit_get_and_put(server):
    r = server.put("/email/daily-send-limit", json={"limit": 5}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["daily_send_limit"] == 5

    r = server.get("/email/status")
    assert r.json()["daily_send_limit"] == 5


def test_daily_send_limit_rejects_non_positive(server):
    r = server.put("/email/daily-send-limit", json={"limit": 0}, headers=ADMIN_HEADERS)
    assert r.status_code == 422
    r = server.put("/email/daily-send-limit", json={"limit": -5}, headers=ADMIN_HEADERS)
    assert r.status_code == 422


def test_email_config_validates_address_and_app_password_shape(server):
    r = server.put("/email/config", json={"gmail_address": "not-an-email", "app_password": "x" * 16}, headers=ADMIN_HEADERS)
    assert r.status_code == 422

    r = server.put("/email/config", json={"gmail_address": "sales@example.com", "app_password": "tooshort"}, headers=ADMIN_HEADERS)
    assert r.status_code == 422

    r = server.put("/email/config", json={"gmail_address": "sales@example.com", "app_password": "abcd efgh ijkl mnop"}, headers=ADMIN_HEADERS)
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True
    assert body["gmail_address"] == "sales@example.com"
    assert body["source"] == "database"

    r = server.delete("/email/config", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["configured"] is False


# ---- admin password gate ------------------------------------------------
def test_admin_routes_reject_missing_or_wrong_password(server):
    r = server.get("/admin/suppressed")  # no header at all
    assert r.status_code == 401

    r = server.get("/admin/suppressed", headers={"X-Admin-Password": "wrong"})
    assert r.status_code == 401

    r = server.put("/email/daily-send-limit", json={"limit": 5}, headers={"X-Admin-Password": "wrong"})
    assert r.status_code == 401


def test_verify_password_endpoint(server):
    r = server.post("/admin/verify-password", json={"password": "wrong"})
    assert r.status_code == 401

    r = server.post("/admin/verify-password", json={"password": "Apex!"})
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


# ---- reset all data ------------------------------------------------------
def test_reset_all_data_requires_password(server):
    r = server.post("/admin/reset-all-data")
    assert r.status_code == 401


def test_reset_all_data_clears_leads_but_preserves_config_and_kb(server):
    # Seed some lead/campaign data, plus confirm KB entries and Gmail
    # config survive the reset (they're business content/configuration,
    # not test lead data -- see reset_all_data()'s docstring).
    cid = server.post("/campaigns", json={"name": "Reset Test Campaign"}).json()["id"]
    pid = server.seed_prospect()
    server.post(f"/campaigns/{cid}/assign-prospect/{pid}")
    server.post("/admin/suppressed", json={"email": "keep-me-out@example.com"}, headers=ADMIN_HEADERS)
    server.put("/email/config", json={"gmail_address": "sales@example.com", "app_password": "abcd efgh ijkl mnop"}, headers=ADMIN_HEADERS)

    kb_before = server.get("/knowledge-base/qa").json()
    assert len(kb_before) > 0  # seeded KB entries exist

    r = server.post("/admin/reset-all-data", headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text

    assert server.get("/leads").json() == []
    assert server.get("/campaigns").json() == []
    assert server.get("/admin/suppressed", headers=ADMIN_HEADERS).json() == []

    # Preserved: KB content and Gmail config.
    kb_after = server.get("/knowledge-base/qa").json()
    assert len(kb_after) == len(kb_before)
    status = server.get("/email/status").json()
    assert status["configured"] is True
    assert status["gmail_address"] == "sales@example.com"


def test_reset_all_data_does_not_resurrect_demo_customers(tmp_path):
    # The two demo customers db.py seeds on a fresh DB should NOT come
    # back after a reset clears the customers table AND a subsequent
    # init_db() call (e.g. the next deploy/restart after the reset) --
    # see the demo_customers_seeded flag in app/db.py's init_db(). Uses
    # the isolated-subprocess pattern (_reset_reseed_probe.py) since this
    # needs two separate init_db() calls with a real reset in between,
    # which the shared live-server fixture can't drive directly.
    db_path = tmp_path / "reseed_probe.db"
    result = subprocess.run(
        [sys.executable, str(Path(__file__).parent / "_reset_reseed_probe.py")],
        cwd=str(REPO_ROOT),
        env={"APEX_DB_PATH": str(db_path), "DATABASE_URL": "", "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.stdout.strip() == "PASS", f"stdout={result.stdout}\nstderr={result.stderr}"
