"""
AI-powered reply drafting + image understanding -- Claude (Anthropic),
bring-your-own-key.

Two things this app uses Claude for, both optional upgrades over a
deterministic fallback that keeps working with no key configured:
  1. Reply drafting (draft_reply()) -- kb_qa.py's rule-based keyword
     matching gets replaced by Claude-generated drafts grounded in this
     app's own Knowledge Base Q&A entries and stock catalog, so it stays
     on-topic and doesn't invent product details. Every draft still
     requires human approval before send, same as the rule-based path --
     nothing here auto-sends.
  2. Vision (vision_request()) -- business_card.py sends a photo of a
     business card and asks Claude to extract structured contact fields,
     used by the Import tab's "Scan a business card" flow.
Both configured from the Admin tab (same panel, same API key).

No API key is bundled with this app -- each deployment brings its own
Anthropic subscription/API key, configured the same way as Gmail:
    ANTHROPIC_API_KEY  - the client's own Anthropic API key
    ANTHROPIC_MODEL    - which Claude model to draft with (default below)
or via the Admin tab at runtime (DB-stored, takes effect immediately, no
restart -- and takes priority over the environment variable, same
precedence as Gmail credentials).

Nothing in this module runs network calls at import time -- is_configured()
gates every function, so the app imports and starts fine with no key set;
reply drafting simply falls back to the rule-based composer in kb_qa.py
until a key is added.
"""
import base64
import os

from app.services import settings as app_settings

# A current, generally-available Claude model -- overridable per deployment
# via the Admin tab or ANTHROPIC_MODEL, since which models a given API key
# has access to depends on that account's own plan, not this app.
DEFAULT_MODEL = "claude-sonnet-5"


class AINotConfiguredError(Exception):
    pass


class AIRequestError(Exception):
    pass


def _api_key() -> str | None:
    return app_settings.get_setting("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY")


def _model() -> str:
    return app_settings.get_setting("anthropic_model") or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL


def configured_model() -> str | None:
    """Public getter for display purposes (e.g. status endpoint) -- None
    when not configured, same pattern as email_provider.configured_address()."""
    return _model() if is_configured() else None


def key_display() -> str | None:
    """Masked key for the Admin UI -- the real value is never sent back to
    the browser once saved, only this. e.g. 'sk-ant-...ab12'."""
    key = _api_key()
    if not key:
        return None
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:7]}...{key[-4:]}"


def credential_source() -> str:
    """Where the active key came from, for the Admin UI: 'database' (saved
    via the Admin tab), 'environment' (ANTHROPIC_API_KEY env var only), or
    'none'. DB always wins when both are present, matching _api_key()."""
    if app_settings.get_setting("anthropic_api_key"):
        return "database"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "environment"
    return "none"


def set_credentials(api_key: str, model: str | None = None):
    """Save the client's own Anthropic API key (and optional model
    override) to the DB (Admin tab). Takes effect on the next reply draft --
    no restart required."""
    app_settings.set_setting("anthropic_api_key", api_key)
    if model:
        app_settings.set_setting("anthropic_model", model)


def clear_credentials():
    """Remove DB-stored key/model, reverting to ANTHROPIC_* env vars (if
    any) as fallback -- reply drafting falls back to the rule-based
    composer in kb_qa.py once neither is set."""
    app_settings.set_setting("anthropic_api_key", None)
    app_settings.set_setting("anthropic_model", None)


def is_configured() -> bool:
    return bool(_api_key())


def _require_configured():
    if not is_configured():
        raise AINotConfiguredError(
            "Claude is not configured. Add an Anthropic API key from the Admin tab, "
            "or set ANTHROPIC_API_KEY (see .env.example)."
        )


def require_configured():
    """Public wrapper -- raises AINotConfiguredError if no key is set."""
    _require_configured()


def _client():
    import anthropic
    return anthropic.Anthropic(api_key=_api_key())


def _friendly_error(e: Exception, model: str) -> str:
    import anthropic
    if isinstance(e, anthropic.AuthenticationError):
        return "Claude rejected the API key -- double-check it's correct and hasn't been revoked."
    if isinstance(e, anthropic.NotFoundError):
        return f"Model '{model}' isn't available on this API key's plan -- try a different model name in Admin."
    if isinstance(e, anthropic.RateLimitError):
        return "Claude rate limit reached -- try again shortly."
    if isinstance(e, anthropic.APIError):
        return f"Claude API error: {e}"
    return f"Could not reach Claude: {e}"


def test_connection() -> str:
    """Minimal real API call (1 output token) to confirm the active key/
    model actually work -- no drafting, just an auth+model check, same
    spirit as email_provider.test_login(). Returns the model name on
    success; raises AINotConfiguredError / AIRequestError otherwise."""
    _require_configured()
    model = _model()
    try:
        client = _client()
        client.messages.create(
            model=model,
            max_tokens=1,
            messages=[{"role": "user", "content": "hi"}],
        )
    except Exception as e:
        raise AIRequestError(_friendly_error(e, model))
    return model


def draft_reply(system_prompt: str, user_prompt: str, max_tokens: int = 700) -> str:
    """One-shot completion for reply drafting -- returns the raw response
    text. Raises AIRequestError on any failure (bad key, rate limit,
    network, bad model name) so the caller (kb_qa.py) can fall back to the
    rule-based composer instead of leaving a customer's reply unhandled."""
    _require_configured()
    model = _model()
    try:
        client = _client()
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
        return "".join(block.text for block in resp.content if block.type == "text").strip()
    except Exception as e:
        raise AIRequestError(_friendly_error(e, model))


def vision_request(system_prompt: str, user_prompt: str, image_bytes: bytes, media_type: str,
                    max_tokens: int = 500) -> str:
    """Same one-shot completion as draft_reply(), but the user turn also
    carries an image (base64-inlined, Claude's vision input format) --
    used for business-card scanning. Raises AIRequestError on any failure,
    same contract as draft_reply()."""
    _require_configured()
    model = _model()
    try:
        client = _client()
        image_b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}},
                    {"type": "text", "text": user_prompt},
                ],
            }],
        )
        return "".join(block.text for block in resp.content if block.type == "text").strip()
    except Exception as e:
        raise AIRequestError(_friendly_error(e, model))
