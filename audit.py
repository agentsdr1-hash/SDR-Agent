"""
OBJ-014 Audit & Monitoring.
A single log_event() call other services use to record what happened, when,
and to what. Kept deliberately simple (one flat table) for a pilot -- this
is the record you'd pull if someone asks "why did this prospect get
suppressed" or "when did we send to X" six weeks from now.
"""
from datetime import datetime, timezone

from app.db import get_conn


def log_event(event_type: str, entity_type: str | None = None, entity_id: str | None = None,
              details: str | None = None, actor: str = "system"):
    """Fire-and-forget audit entry. Never raises -- a logging failure should
    never take down the operation it's trying to record."""
    try:
        with get_conn() as conn:
            conn.execute(
                """INSERT INTO audit_log (timestamp, event_type, entity_type, entity_id, details, actor)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(), event_type, entity_type, entity_id, details, actor),
            )
    except Exception:
        pass  # audit logging must never break the calling operation


def list_events(limit: int = 100, event_type: str | None = None, entity_type: str | None = None) -> list[dict]:
    query = "SELECT * FROM audit_log"
    conditions = []
    params = []
    if event_type:
        conditions.append("event_type = ?")
        params.append(event_type)
    if entity_type:
        conditions.append("entity_type = ?")
        params.append(entity_type)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()
    return [dict(r) for r in rows]


def event_type_counts() -> dict:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT event_type, COUNT(*) c FROM audit_log GROUP BY event_type ORDER BY c DESC"
        ).fetchall()
    return {r["event_type"]: r["c"] for r in rows}
