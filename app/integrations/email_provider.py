"""
OBJ-016 Email Integration — Gmail via app password.

Works with a personal Gmail account (not Workspace) using an App Password
instead of full OAuth: https://myaccount.google.com/apppasswords
Requires 2-Step Verification to be enabled on the account first.

Configure via the Admin tab (Gmail integration panel) -- this writes to the
app_settings table via app.services.settings and takes effect immediately,
no restart needed. Environment variables still work as a fallback for
headless/CI setups (see .env.example):
    GMAIL_ADDRESS         - the sending/monitoring Gmail address
    GMAIL_APP_PASSWORD    - the 16-character app password (NOT the real password)
    POLL_INTERVAL_MINUTES - how often to check for replies (default 5)

Nothing in this module runs network calls at import time. Every function
checks is_configured() first and raises a clear error if credentials are
missing, so the rest of the app can import and start up fine before
credentials exist -- send/poll simply no-op with a clear message until then.

Design note: uses IMAP polling, not a webhook. This means no public URL or
hosting is required -- the tool reaches out to Gmail, Gmail never has to
reach in. Works identically whether this runs on a laptop or a hosted server.
"""
import os
import smtplib
import imaplib
import email as email_lib
from email.mime.text import MIMEText
from email.utils import parseaddr
from datetime import datetime, timezone

from app.services import settings as app_settings

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587
IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


class EmailNotConfiguredError(Exception):
    pass


class EmailSendError(Exception):
    pass


def _address() -> str | None:
    return app_settings.get_setting("gmail_address") or os.environ.get("GMAIL_ADDRESS")


def configured_address() -> str | None:
    """Public getter for display purposes (e.g. status endpoint)."""
    return _address() if is_configured() else None


def _app_password() -> str | None:
    return app_settings.get_setting("gmail_app_password") or os.environ.get("GMAIL_APP_PASSWORD")


def credential_source() -> str:
    """Where the active credentials came from, for the Admin UI: 'database'
    (saved via the Admin tab), 'environment' (GMAIL_* env vars only), or
    'none'. DB always wins when both are present, matching _address()/
    _app_password() above."""
    if app_settings.get_setting("gmail_address") and app_settings.get_setting("gmail_app_password"):
        return "database"
    if os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD"):
        return "environment"
    return "none"


def set_credentials(address: str, app_password: str):
    """Save Gmail credentials to the DB (Admin tab). Takes effect on the
    next send/poll -- no restart required."""
    app_settings.set_setting("gmail_address", address)
    app_settings.set_setting("gmail_app_password", app_password)


def clear_credentials():
    """Remove DB-stored credentials, reverting to the environment variables
    (if any) as fallback."""
    app_settings.set_setting("gmail_address", None)
    app_settings.set_setting("gmail_app_password", None)


def poll_interval_minutes() -> int:
    try:
        return int(os.environ.get("POLL_INTERVAL_MINUTES", "5"))
    except ValueError:
        return 5


def is_configured() -> bool:
    return bool(_address() and _app_password())


def _require_configured():
    if not is_configured():
        raise EmailNotConfiguredError(
            "Gmail is not configured. Set GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
            "environment variables (see .env.example) and restart the server."
        )


def require_configured():
    """Public wrapper -- raises EmailNotConfiguredError if not set up. Lets
    other modules fail fast before doing any partial work."""
    _require_configured()


def test_login() -> str:
    """Attempts an SMTP login only -- no message sent -- to verify the
    active credentials actually work with Gmail. Returns the address on
    success; raises EmailNotConfiguredError / EmailSendError otherwise."""
    _require_configured()
    address = _address()
    app_password = _app_password()

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(address, app_password)
    except smtplib.SMTPAuthenticationError as e:
        raise EmailSendError(
            f"Gmail rejected the login for {address}. Double-check the address and app "
            f"password, and that the app password hasn't been revoked. Details: {e}"
        )
    except (smtplib.SMTPException, OSError) as e:
        raise EmailSendError(f"Could not connect to Gmail: {e}")

    return address


def send_email(to_address: str, subject: str, body: str) -> dict:
    """Send one email via Gmail SMTP. Raises EmailNotConfiguredError if no
    credentials are set, or EmailSendError if Gmail rejects the send
    (bad credentials, address blocked, daily limit hit, etc.)."""
    _require_configured()
    address = _address()
    app_password = _app_password()

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = address
    msg["To"] = to_address

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
            server.starttls()
            server.login(address, app_password)
            server.send_message(msg)
    except smtplib.SMTPAuthenticationError as e:
        raise EmailSendError(
            f"Gmail rejected the login. Check GMAIL_ADDRESS/GMAIL_APP_PASSWORD are correct "
            f"and that the app password hasn't been revoked. Details: {e}"
        )
    except (smtplib.SMTPException, OSError) as e:
        raise EmailSendError(f"Gmail rejected the send to {to_address}: {e}")

    return {"to": to_address, "sent_at": datetime.now(timezone.utc).isoformat()}


def check_for_replies(known_emails: set[str]) -> list[dict]:
    """
    Poll the inbox for unread messages from any address in known_emails
    (the set of prospects we've sent to and are awaiting a reply from).
    Matched messages are marked as read on the server so they aren't
    reprocessed on the next poll. Returns a list of
    {from_email, subject, received_at} for every match found this poll.
    """
    _require_configured()
    address = _address()
    app_password = _app_password()
    known_emails_lower = {e.lower() for e in known_emails}
    replies = []

    try:
        with imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT, timeout=20) as imap:
            imap.login(address, app_password)
            imap.select("INBOX")
            status, data = imap.search(None, "UNSEEN")
            if status != "OK" or not data or not data[0]:
                return replies
            for num in data[0].split():
                status, msg_data = imap.fetch(num, "(RFC822)")
                if status != "OK" or not msg_data or not msg_data[0]:
                    continue
                msg = email_lib.message_from_bytes(msg_data[0][1])
                _, from_email = parseaddr(msg.get("From", ""))
                if from_email.lower() in known_emails_lower:
                    body_snippet = _extract_body_text(msg)[:500]
                    replies.append({
                        "from_email": from_email.lower(),
                        "subject": msg.get("Subject", ""),
                        "body_snippet": body_snippet,
                        "received_at": datetime.now(timezone.utc).isoformat(),
                    })
                    # leave it marked read so we don't reprocess it next poll
    except (imaplib.IMAP4.error, OSError) as e:
        raise EmailSendError(f"Gmail IMAP check failed: {e}")

    return replies


def _extract_body_text(msg) -> str:
    """Best-effort plain-text extraction from an email.message.Message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_payload(decode=True).decode(errors="ignore")
                except Exception:
                    continue
        return ""
    try:
        return msg.get_payload(decode=True).decode(errors="ignore")
    except Exception:
        return ""
