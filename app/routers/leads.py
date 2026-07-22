"""
Lead lifecycle lookup -- given a L###### number, return everything
known about that lead: who they are, every campaign they've been part
of with full stage timestamps, and the audit trail. See
app/services/leads.py for the design rationale.
"""
from fastapi import APIRouter, HTTPException

from app.services.leads import get_lead_timeline, list_leads

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("")
def leads_list(search: str | None = None, status: str | None = None):
    """Consolidated, cross-campaign lead list for the Leads tab -- every
    prospect, one row each, optionally filtered by status or a free-text
    search across name/email/company/lead number."""
    return list_leads(search=search, status=status)


@router.get("/{lead_number}")
def lead_timeline(lead_number: str):
    result = get_lead_timeline(lead_number)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No lead found for '{lead_number}'")
    return result
