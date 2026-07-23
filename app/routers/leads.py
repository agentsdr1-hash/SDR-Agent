"""
Lead lifecycle lookup -- given a L###### number, return everything
known about that lead: who they are, every campaign they've been part
of with full stage timestamps, and the audit trail. See
app/services/leads.py for the design rationale.
"""
from fastapi import APIRouter, HTTPException

from app.db import get_conn
from app.models import BulkAssignInput, BulkSuppressInput, BulkActionResult
from app.services.leads import get_lead_timeline, list_leads, lead_number_for
from app.services.campaign_management import assign_prospect_to_campaign, CampaignError
from app.services.administration import add_to_suppression_list, AdminError

router = APIRouter(prefix="/leads", tags=["leads"])


@router.get("")
def leads_list(search: str | None = None, status: str | None = None,
               validation_status: str | None = None, ever_sent: bool | None = None,
               ever_replied: bool | None = None, ever_quoted: bool | None = None):
    """Consolidated, cross-campaign lead list for the Leads tab -- every
    prospect, one row each, optionally filtered by status (current campaign
    status), validation_status (Valid/Invalid/etc.), ever_sent/ever_replied/
    ever_quoted (matches the Dashboard's SDR-performance/value-captured
    counts), or a free-text search across name/email/company/lead number."""
    return list_leads(search=search, status=status, validation_status=validation_status,
                       ever_sent=ever_sent, ever_replied=ever_replied, ever_quoted=ever_quoted)


@router.post("/bulk-assign", response_model=BulkActionResult)
def bulk_assign(payload: BulkAssignInput):
    """Assign multiple leads to a campaign in one go -- each still goes
    through assign_prospect_to_campaign()'s own checks (Valid, not
    suppressed, not already in this campaign), so a mixed selection just
    reports which ones didn't qualify rather than failing the whole batch."""
    succeeded, errors = 0, []
    for pid in payload.prospect_ids:
        try:
            assign_prospect_to_campaign(payload.campaign_id, pid)
            succeeded += 1
        except CampaignError as e:
            errors.append(f"{lead_number_for(pid)}: {e}")
    return BulkActionResult(succeeded=succeeded, failed=len(errors), errors=errors)


@router.post("/bulk-suppress", response_model=BulkActionResult)
def bulk_suppress(payload: BulkSuppressInput):
    """Add multiple leads' emails to the suppression list at once."""
    with get_conn() as conn:
        if not payload.prospect_ids:
            rows = []
        else:
            ph = ",".join("?" * len(payload.prospect_ids))
            rows = conn.execute(
                f"SELECT id, email FROM prospects_raw WHERE id IN ({ph})", payload.prospect_ids
            ).fetchall()
    email_by_id = {r["id"]: r["email"] for r in rows}

    succeeded, errors = 0, []
    for pid in payload.prospect_ids:
        email = email_by_id.get(pid)
        if not email:
            errors.append(f"{lead_number_for(pid)}: no email on file, cannot suppress")
            continue
        try:
            add_to_suppression_list(email, payload.reason, source="manual")
            succeeded += 1
        except AdminError as e:
            errors.append(f"{email}: {e}")
    return BulkActionResult(succeeded=succeeded, failed=len(errors), errors=errors)


@router.get("/{lead_number}")
def lead_timeline(lead_number: str):
    result = get_lead_timeline(lead_number)
    if result is None:
        raise HTTPException(status_code=404, detail=f"No lead found for '{lead_number}'")
    return result
