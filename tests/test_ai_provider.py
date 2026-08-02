"""
AI Brain (Claude) connector -- app/integrations/ai_provider.py's
config/status/test-connection behavior, and kb_qa.py's AI-first,
rule-based-fallback reply composition. The real request/response handling
against a mocked Anthropic client is covered by the subprocess probe
(_ai_provider_probe.py, same reasoning as test_followups.py's docstring:
needs app.db.DB_PATH fixed before any app import, in a process isolated
from pytest's own). HTTP-level config/status/auth-gating is covered here
via the normal live-server `server` fixture -- no real Anthropic API key
or network call anywhere in this file.
"""
import json
import subprocess
import sys
from pathlib import Path

from conftest import ADMIN_HEADERS, REPO_ROOT

PROBE = Path(__file__).parent / "_ai_provider_probe.py"


def _run_probe(tmp_path):
    db_path = tmp_path / "ai_provider_probe.db"
    result = subprocess.run(
        [sys.executable, str(PROBE)],
        cwd=str(REPO_ROOT),
        env={"APEX_DB_PATH": str(db_path), "PATH": __import__("os").environ.get("PATH", "")},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, f"probe failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    return json.loads(result.stdout)


def test_ai_composer_falls_back_to_rule_based_around_a_mocked_claude(tmp_path):
    data = _run_probe(tmp_path)
    assert data["not_configured_returns_none"] is True
    assert data["is_configured_false"] is True
    assert data["configured_uses_ai"] is True
    assert data["configured_ai_body_matches"] is True
    assert data["configured_ai_subject"] == "Re: quote"
    assert data["stored_draft_confidence"] == "ai"
    assert data["stored_draft_body_matches"] is True
    assert data["api_failure_returns_none"] is True
    assert data["api_failure_falls_back_to_rule_based"] is True
    assert data["test_connection_returns_model"] is True
    assert data["test_connection_raises_on_failure"] is True
    assert data["cleared_is_not_configured"] is True


# ---- HTTP-level: config/status/auth-gating, no real API key or network ----

def test_ai_status_defaults_and_not_configured(server):
    r = server.get("/ai/status")  # deliberately no header -- must stay public, like /email/status
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is False
    assert body["source"] == "none"
    assert body["model"] is None
    assert body["key_display"] is None


def test_ai_config_round_trip_and_key_never_echoed(server):
    r = server.put("/ai/config", json={"api_key": "sk-ant-faketestkey12345", "model": "claude-sonnet-5"}, headers=ADMIN_HEADERS)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] is True
    assert body["source"] == "database"
    assert body["model"] == "claude-sonnet-5"
    assert "faketestkey" not in json.dumps(body)  # the real key is never echoed back, only key_display
    assert body["key_display"].startswith("sk-ant-")
    assert body["key_display"] != "sk-ant-faketestkey12345"

    r = server.get("/ai/status")
    assert r.json()["configured"] is True

    r = server.delete("/ai/config", headers=ADMIN_HEADERS)
    assert r.status_code == 200
    assert r.json()["configured"] is False

    r = server.get("/ai/status")
    assert r.json()["configured"] is False


def test_ai_config_rejects_empty_key(server):
    r = server.put("/ai/config", json={"api_key": "   "}, headers=ADMIN_HEADERS)
    assert r.status_code == 422


def test_ai_test_connection_fails_cleanly_without_a_key(server):
    r = server.delete("/ai/config", headers=ADMIN_HEADERS)  # make sure nothing lingers from another test
    assert r.status_code == 200
    r = server.post("/ai/test-connection", headers=ADMIN_HEADERS)
    assert r.status_code == 400
    assert "not configured" in r.json()["detail"].lower()


def test_ai_routes_reject_missing_or_wrong_admin_password(server):
    r = server.put("/ai/config", json={"api_key": "sk-ant-x"})  # no header at all
    assert r.status_code == 401

    r = server.put("/ai/config", json={"api_key": "sk-ant-x"}, headers={"X-Admin-Password": "wrong"})
    assert r.status_code == 401

    r = server.post("/ai/test-connection", headers={"X-Admin-Password": "wrong"})
    assert r.status_code == 401
