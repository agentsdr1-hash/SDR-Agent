"""
Runtime-configurable settings, stored in the DB so they can be changed from
the Admin UI -- e.g. the Gmail App Password -- without touching environment
variables or restarting the server. A value set here takes priority over the
matching environment variable; see app/integrations/email_provider.py for
the fallback order.

Same trust boundary as every other table in this SQLite file: not encrypted
at rest. The Admin console has no login/access control yet, so treat access
to this database the same way you'd treat access to a .env file.
"""
from datetime import datetime, timezone

from app.db import get_conn


def get_setting(key: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def set_setting(key: str, value: str | None):
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        if value is None:
            conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, now),
            )
