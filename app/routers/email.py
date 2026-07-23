"""
OBJ-016 Email Integration router.
Status check + manual poll trigger (for testing without waiting on the timer),
plus Gmail App Password configuration from the Admin tab (OBJ-015 console).
"""
from fastapi import APIRouter, HTTPException

from app.integrations import email_provider
from app.services import inbox_monitor
from app.models import EmailStatus, PollResult, EmailConfigInput, DailySendLimitInput

router = APIRouter(prefix="/email", tags=["OBJ-016"])


@router.get("/status", response_model=EmailStatus)
def status():
    poll_status = inbox_monitor.get_status()
    return EmailStatus(
        configured=email_provider.is_configured(),
        gmail_address=email_provider.configured_address(),
        source=email_provider.credential_source(),
        poll_interval_minutes=email_provider.poll_interval_minutes(),
        daily_send_limit=email_provider.daily_send_limit(),
        sent_today=email_provider.sent_today_count(),
        last_poll_at=poll_status["last_poll_at"],
        last_poll_replies_found=poll_status["last_poll_replies_found"],
        last_poll_error=poll_status["last_poll_error"],
    )


@router.put("/daily-send-limit", response_model=EmailStatus)
def set_daily_send_limit(payload: DailySendLimitInput):
    """Configure the daily cap on real sends (campaign + reply-draft sends
    combined) from the Admin tab. Takes effect immediately."""
    if payload.limit <= 0:
        raise HTTPException(status_code=422, detail="Daily send limit must be a positive number.")
    email_provider.set_daily_send_limit(payload.limit)
    return status()


@router.post("/poll-now", response_model=PollResult)
def poll_now():
    """Trigger a reply check immediately instead of waiting for the timer --
    useful for testing that a real reply gets picked up."""
    return inbox_monitor.poll_once()


@router.put("/config", response_model=EmailStatus)
def set_config(payload: EmailConfigInput):
    """Save Gmail credentials from the Admin tab. Takes effect immediately --
    no restart needed, unlike the GMAIL_* environment variable route."""
    address = payload.gmail_address.strip()
    app_password = payload.app_password.replace(" ", "").strip()
    if "@" not in address or "." not in address.split("@")[-1]:
        raise HTTPException(status_code=422, detail="Enter a valid Gmail address.")
    if len(app_password) != 16:
        raise HTTPException(
            status_code=422,
            detail="Gmail App Passwords are 16 characters (spaces in the Google-provided "
                   "copy are fine -- they're stripped automatically). Generate one at "
                   "https://myaccount.google.com/apppasswords.",
        )
    email_provider.set_credentials(address, app_password)
    return status()


@router.delete("/config", response_model=EmailStatus)
def clear_config():
    """Remove DB-stored Gmail credentials (falls back to GMAIL_* env vars, if set)."""
    email_provider.clear_credentials()
    return status()


@router.post("/test-connection")
def test_connection():
    """Attempts a real Gmail SMTP login with the active credentials -- no
    email is sent. Confirms the address/app-password pair actually works
    before you rely on it for a campaign send."""
    try:
        address = email_provider.test_login()
    except email_provider.EmailNotConfiguredError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except email_provider.EmailSendError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"status": "ok", "message": f"Gmail login succeeded for {address}."}
