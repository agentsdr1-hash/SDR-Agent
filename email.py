"""
OBJ-016 Email Integration router.
Status check + manual poll trigger (for testing without waiting on the timer).
"""
from fastapi import APIRouter

from app.integrations import email_provider
from app.services import inbox_monitor
from app.models import EmailStatus, PollResult

router = APIRouter(prefix="/email", tags=["OBJ-016"])


@router.get("/status", response_model=EmailStatus)
def status():
    poll_status = inbox_monitor.get_status()
    return EmailStatus(
        configured=email_provider.is_configured(),
        gmail_address=email_provider.configured_address(),
        poll_interval_minutes=email_provider.poll_interval_minutes(),
        last_poll_at=poll_status["last_poll_at"],
        last_poll_replies_found=poll_status["last_poll_replies_found"],
        last_poll_error=poll_status["last_poll_error"],
    )


@router.post("/poll-now", response_model=PollResult)
def poll_now():
    """Trigger a reply check immediately instead of waiting for the timer --
    useful for testing that a real reply gets picked up."""
    return inbox_monitor.poll_once()
