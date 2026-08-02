"""
AI Brain (Claude) connector router -- config + status + connection test for
the bring-your-own-key Anthropic integration that upgrades reply drafting
from rule-based keyword matching to Claude-generated drafts. Same shape as
app/routers/email.py's Gmail config routes.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.integrations import ai_provider
from app.services.admin_auth import require_admin_password
from app.models import AIStatus, AIConfigInput

router = APIRouter(prefix="/ai", tags=["AI Brain"])


# GET /status is deliberately NOT gated -- it powers the small "AI: on/off"
# badge shown wherever reply-drafting is discussed, and only ever returns
# booleans/a masked key, never the real secret. Every other route here is a
# mutation (or the API test), gated behind the admin password the same as
# the rest of the Admin tab.
@router.get("/status", response_model=AIStatus)
def status():
    return AIStatus(
        configured=ai_provider.is_configured(),
        model=ai_provider.configured_model(),
        source=ai_provider.credential_source(),
        key_display=ai_provider.key_display(),
    )


@router.put("/config", response_model=AIStatus, dependencies=[Depends(require_admin_password)])
def set_config(payload: AIConfigInput):
    """Save the client's own Anthropic API key (and optional model name)
    from the Admin tab. Takes effect immediately -- no restart needed."""
    api_key = payload.api_key.strip()
    model = (payload.model or "").strip() or None
    if not api_key:
        raise HTTPException(status_code=422, detail="Enter an Anthropic API key.")
    ai_provider.set_credentials(api_key, model)
    return status()


@router.delete("/config", response_model=AIStatus, dependencies=[Depends(require_admin_password)])
def clear_config():
    """Remove the DB-stored Claude API key/model (falls back to ANTHROPIC_*
    env vars, if set, otherwise reply drafting reverts to the rule-based
    composer)."""
    ai_provider.clear_credentials()
    return status()


@router.post("/test-connection", dependencies=[Depends(require_admin_password)])
def test_connection():
    """Sends a minimal real request to Claude (1 output token, no drafting)
    to confirm the active API key/model actually work before relying on
    them for reply drafting."""
    try:
        model = ai_provider.test_connection()
    except ai_provider.AINotConfiguredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ai_provider.AIRequestError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "message": f"Claude connection succeeded (model: {model})."}
