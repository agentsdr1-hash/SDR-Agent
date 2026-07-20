"""
OBJ-014 Audit & Monitoring router.
Read-only views over the event log every other service writes to via
app.services.audit.log_event().
"""
from fastapi import APIRouter

from app.models import AuditEvent
from app.services.audit import list_events, event_type_counts

router = APIRouter(prefix="/audit", tags=["OBJ-014"])


@router.get("/events", response_model=list[AuditEvent])
def events(limit: int = 100, event_type: str | None = None, entity_type: str | None = None):
    return list_events(limit=limit, event_type=event_type, entity_type=entity_type)


@router.get("/event-counts")
def event_counts():
    return event_type_counts()
