"""
Standalone probe (run as its own interpreter process, not imported by
pytest) for app/integrations/ai_provider.py + kb_qa.py's AI-first,
rule-based-fallback reply composition. Same reasoning as
_daily_limit_probe.py's docstring: app.db.DB_PATH is fixed at import time,
so a fresh process per run keeps this isolated from pytest's own process.

The real Anthropic client is never touched -- ai_provider._client() is
patched to return a small fake object shaped like the bits of the SDK this
app actually calls (client.messages.create(...).content[i].text), so this
exercises the real request-building/response-parsing/error-handling code
in ai_provider.py and kb_qa.py without any network call or real API key.

Prints one JSON object to stdout with one key per scenario.
"""
import json
import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import init_db, get_conn  # noqa: E402
from app.services import settings as app_settings  # noqa: E402
from app.integrations import ai_provider  # noqa: E402
from app.services import kb_qa  # noqa: E402


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeMessage:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]


class _FakeMessages:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc

    def create(self, **kwargs):
        self._last_kwargs = kwargs
        if self._exc:
            raise self._exc
        return _FakeMessage(self._text)


class _FakeClient:
    def __init__(self, text=None, exc=None):
        self.messages = _FakeMessages(text, exc)


def main():
    init_db(seed_customers=False)
    results = {}

    # --- Scenario 1: not configured at all -- AI composer must return
    # None (never raise), so create_reply_draft() falls back cleanly.
    draft = kb_qa.compose_smart_reply_ai("Ahmed", "Falcon Steel", "Do you carry rebar?")
    results["not_configured_returns_none"] = draft is None
    results["is_configured_false"] = ai_provider.is_configured() is False

    # --- Scenario 2: configured + a working fake client -- AI composer
    # should be used, confidence='ai', grounded in the real KB/stock data.
    app_settings.set_setting("anthropic_api_key", "fake-test-key")
    kb_qa.add_kb_entry("Do you carry rebar?", "Yes, Grade 60 rebar in stock.", "rebar")
    fake_body = "Hi Ahmed,\n\nYes, we carry Grade 60 rebar.\n\nBest,\nAKEIS Sales Team"
    with patch.object(ai_provider, "_client", return_value=_FakeClient(text=fake_body)):
        draft2 = kb_qa.compose_smart_reply_ai("Ahmed", "Falcon Steel", "Do you carry rebar?", "Re: quote")
        results["configured_uses_ai"] = draft2 is not None and draft2["confidence"] == "ai"
        results["configured_ai_body_matches"] = draft2["body"] == fake_body if draft2 else False
        results["configured_ai_subject"] = draft2["subject"] if draft2 else None

        # create_reply_draft() end-to-end -- stores a Draft row with the
        # AI-produced content, same schema as the rule-based path.
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO import_batches (batch_id, filename, row_count, imported_at) VALUES ('probe','probe.csv',1,'2026-01-01T00:00:00+00:00')"
            )
            cur = conn.execute(
                "INSERT INTO prospects_raw (batch_id, row_number, first_name, last_name, email, company, status) "
                "VALUES ('probe', 1, 'Ahmed', 'Test', 'ahmed@example.com', 'Falcon Steel', 'Valid') RETURNING id"
            )
            pid = cur.fetchone()["id"]
        from app.services.campaign_management import create_campaign, assign_prospect_to_campaign
        from app.services.approval_and_delivery import approve, simulate_sent
        campaign = create_campaign("AI Probe Campaign", "Mon,Tue,Wed,Thu,Fri", 25)
        assign_prospect_to_campaign(campaign.id, pid)
        with get_conn() as conn:
            row_id = conn.execute(
                "SELECT id FROM campaign_prospects WHERE campaign_id = ? AND prospect_id = ?", (campaign.id, pid)
            ).fetchone()["id"]
        approve(campaign.id, row_id)
        simulate_sent(campaign.id, row_id)
        draft_id = kb_qa.create_reply_draft(row_id, "Ahmed", "Falcon Steel", "Re: quote", "Do you carry rebar?")
        with get_conn() as conn:
            stored = conn.execute("SELECT confidence, body FROM reply_drafts WHERE id = ?", (draft_id,)).fetchone()
        results["stored_draft_confidence"] = stored["confidence"]
        results["stored_draft_body_matches"] = stored["body"] == fake_body

    # --- Scenario 3: configured but the API call fails -- AI composer
    # returns None (swallows the error), create_reply_draft() must still
    # produce a real draft via the rule-based fallback, not blow up.
    with patch.object(ai_provider, "_client", return_value=_FakeClient(exc=RuntimeError("network down"))):
        draft3 = kb_qa.compose_smart_reply_ai("Ahmed", "Falcon Steel", "Do you carry rebar?")
        results["api_failure_returns_none"] = draft3 is None
        draft_id_fallback = kb_qa.create_reply_draft(row_id, "Ahmed", "Falcon Steel", "Re: quote", "Do you carry rebar?")
        with get_conn() as conn:
            stored_fallback = conn.execute("SELECT confidence FROM reply_drafts WHERE id = ?", (draft_id_fallback,)).fetchone()
        results["api_failure_falls_back_to_rule_based"] = stored_fallback["confidence"] in ("matched", "fallback")

    # --- Scenario 4: ai_provider.test_connection() -- success path returns
    # the model name; failure path raises AIRequestError with a message,
    # not the raw exception.
    with patch.object(ai_provider, "_client", return_value=_FakeClient(text="pong")):
        model = ai_provider.test_connection()
        results["test_connection_returns_model"] = model == ai_provider.DEFAULT_MODEL

    with patch.object(ai_provider, "_client", return_value=_FakeClient(exc=RuntimeError("boom"))):
        try:
            ai_provider.test_connection()
            results["test_connection_raises_on_failure"] = False
        except ai_provider.AIRequestError:
            results["test_connection_raises_on_failure"] = True

    # --- Scenario 5: clearing credentials reverts to not-configured.
    app_settings.set_setting("anthropic_api_key", None)
    results["cleared_is_not_configured"] = ai_provider.is_configured() is False

    print(json.dumps(results))


if __name__ == "__main__":
    main()
