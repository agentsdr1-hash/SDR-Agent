"""
OBJ-015 admin gate -- one shared password, not a multi-user auth system.
Proportionate to what it protects: Gmail credentials, the suppression
list, and the destructive reset-all-data action, for a small internal
tool with no user accounts. Checked server-side on every gated request
via the X-Admin-Password header, not just hidden in the frontend -- a
direct API call without the header is rejected exactly the same as one
from the UI.

Configurable via the ADMIN_PASSWORD environment variable (falls back to
a default) so it isn't permanently pinned to one value in source control.
"""
import os

from fastapi import Header, HTTPException

DEFAULT_ADMIN_PASSWORD = "Apex!"


def _admin_password() -> str:
    return os.environ.get("ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD)


def verify_admin_password(password: str) -> bool:
    return password == _admin_password()


def require_admin_password(x_admin_password: str = Header(None)):
    """FastAPI dependency -- add to any route that should be gated. 401
    (not 422) so the frontend can tell "wrong/missing password" apart
    from a malformed request."""
    if not x_admin_password or not verify_admin_password(x_admin_password):
        raise HTTPException(status_code=401, detail="Incorrect admin password.")
