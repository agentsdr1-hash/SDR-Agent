"""
OBJ-003 Campaign Management router.
Create campaigns, assign validated prospects, approve/edit/send drafts.
"""
from fastapi import APIRouter, HTTPException

from app.models import Campaign, CampaignCreate, AssignResult, CampaignProspect, DraftUpdate, SendResult, WonPayload, LostPayload, SimulateReplyPayload, BulkProspectIds, BulkActionResult, QuoteDetailsInput
from app.services.campaign_management import (
    create_campaign,
    get_campaign,
    list_campaigns,
    assign_batch_to_campaign,
    assign_prospect_to_campaign,
    list_campaign_prospects,
    CampaignError,
)
from app.services.approval_and_delivery import (
    update_draft,
    approve,
    reject,
    send_approved,
    simulate_sent,
    simulate_reply,
    ApprovalError,
)
from app.services.sales_outcomes import (
    request_quote, mark_won, mark_lost, reopen_outcome, update_quote_details,
    draft_quote_summary_email, OutcomeError,
)
from app.integrations.email_provider import EmailNotConfiguredError

router = APIRouter(prefix="/campaigns", tags=["OBJ-003"])


@router.post("", response_model=Campaign)
def create(payload: CampaignCreate):
    try:
        return create_campaign(payload.name, payload.send_days, payload.daily_send_limit)
    except CampaignError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("", response_model=list[Campaign])
def list_all():
    return list_campaigns()


@router.get("/{campaign_id}", response_model=Campaign)
def get_one(campaign_id: int):
    try:
        return get_campaign(campaign_id)
    except CampaignError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/{campaign_id}/assign/{batch_id}", response_model=AssignResult)
def assign(campaign_id: int, batch_id: str):
    try:
        return assign_batch_to_campaign(campaign_id, batch_id)
    except CampaignError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/assign-prospect/{prospect_id}")
def assign_prospect(campaign_id: int, prospect_id: int):
    """Add one specific lead to a campaign -- e.g. one just fixed via the
    Leads tab's edit form and now Valid -- without re-running the whole
    import batch it came from."""
    try:
        return assign_prospect_to_campaign(campaign_id, prospect_id)
    except CampaignError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/{campaign_id}/prospects", response_model=list[CampaignProspect])
def prospects(campaign_id: int):
    return list_campaign_prospects(campaign_id)


@router.put("/{campaign_id}/prospects/{prospect_row_id}/draft", tags=["OBJ-005"])
def edit_draft(campaign_id: int, prospect_row_id: int, payload: DraftUpdate):
    try:
        update_draft(campaign_id, prospect_row_id, payload.subject, payload.body)
        return {"status": "updated"}
    except ApprovalError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/prospects/{prospect_row_id}/approve", tags=["OBJ-005"])
def approve_one(campaign_id: int, prospect_row_id: int):
    try:
        approve(campaign_id, prospect_row_id)
        return {"status": "approved"}
    except ApprovalError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/prospects/{prospect_row_id}/reject", tags=["OBJ-005"])
def reject_one(campaign_id: int, prospect_row_id: int):
    try:
        reject(campaign_id, prospect_row_id)
        return {"status": "rejected"}
    except ApprovalError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/prospects/bulk-approve", response_model=BulkActionResult, tags=["OBJ-005"])
def bulk_approve(campaign_id: int, payload: BulkProspectIds):
    succeeded, errors = 0, []
    for pid in payload.prospect_row_ids:
        try:
            approve(campaign_id, pid)
            succeeded += 1
        except ApprovalError as e:
            errors.append(f"#{pid}: {e}")
    return BulkActionResult(succeeded=succeeded, failed=len(errors), errors=errors)


@router.post("/{campaign_id}/prospects/bulk-reject", response_model=BulkActionResult, tags=["OBJ-005"])
def bulk_reject(campaign_id: int, payload: BulkProspectIds):
    succeeded, errors = 0, []
    for pid in payload.prospect_row_ids:
        try:
            reject(campaign_id, pid)
            succeeded += 1
        except ApprovalError as e:
            errors.append(f"#{pid}: {e}")
    return BulkActionResult(succeeded=succeeded, failed=len(errors), errors=errors)


@router.post("/{campaign_id}/send", response_model=SendResult, tags=["OBJ-006"])
def send(campaign_id: int):
    """Actually sends every Approved prospect via Gmail. Fails clearly with
    a 503 if GMAIL_ADDRESS/GMAIL_APP_PASSWORD aren't set yet -- nothing
    pretends to send when it can't."""
    try:
        return send_approved(campaign_id)
    except EmailNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.post("/{campaign_id}/prospects/{prospect_row_id}/simulate-sent", tags=["testing"])
def simulate_sent_one(campaign_id: int, prospect_row_id: int):
    """QA-only: flips an Approved draft to Sent without touching Gmail, so
    the funnel/dashboard can be tested before real credentials exist."""
    try:
        simulate_sent(campaign_id, prospect_row_id)
        return {"status": "Sent (simulated -- no real email sent)"}
    except ApprovalError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/prospects/{prospect_row_id}/simulate-reply", tags=["testing"])
def simulate_reply_one(campaign_id: int, prospect_row_id: int, payload: SimulateReplyPayload):
    """QA-only: mimics what the real inbox poller would do on a matching
    reply (or opt-out), without an actual inbox."""
    try:
        simulate_reply(campaign_id, prospect_row_id, payload.reply_subject, payload.is_opt_out, payload.reply_body)
        status = "Suppressed" if payload.is_opt_out else "Replied"
        return {"status": f"{status} (simulated -- no real email received)"}
    except ApprovalError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/prospects/{prospect_row_id}/request-quote", tags=["OBJ-011"])
def quote(campaign_id: int, prospect_row_id: int):
    try:
        request_quote(campaign_id, prospect_row_id)
        return {"status": "QuoteRequested"}
    except OutcomeError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/prospects/{prospect_row_id}/won", tags=["OBJ-011"])
def won(campaign_id: int, prospect_row_id: int, payload: WonPayload):
    try:
        mark_won(campaign_id, prospect_row_id, payload.deal_value)
        return {"status": "Won"}
    except OutcomeError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/prospects/{prospect_row_id}/lost", tags=["OBJ-011"])
def lost(campaign_id: int, prospect_row_id: int, payload: LostPayload):
    try:
        mark_lost(campaign_id, prospect_row_id, payload.reason)
        return {"status": "Lost"}
    except OutcomeError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/prospects/{prospect_row_id}/reopen", tags=["OBJ-011"])
def reopen(campaign_id: int, prospect_row_id: int):
    """Back a Won or Lost deal out to Quote Requested -- e.g. it was logged
    by mistake, or the customer re-opened the conversation. To correct or
    switch an outcome without fully reopening it, call /won or /lost again
    directly -- both accept an already-closed prospect."""
    try:
        reopen_outcome(campaign_id, prospect_row_id)
        return {"status": "QuoteRequested"}
    except OutcomeError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.put("/{campaign_id}/prospects/{prospect_row_id}/quote-details", tags=["OBJ-011"])
def set_quote_details(campaign_id: int, prospect_row_id: int, payload: QuoteDetailsInput):
    """Quote Readiness Checklist + target price -- not a real quote or
    pricing calculation, just context for the human building the actual
    quote."""
    try:
        update_quote_details(campaign_id, prospect_row_id, payload.materials,
                              payload.quantity, payload.target_price, payload.quote_notes,
                              payload.sku_spec, payload.unit_of_measure, payload.destination,
                              payload.shipping_terms, payload.delivery_date, payload.currency,
                              payload.payment_terms, payload.packaging_requirements)
        return {"status": "saved"}
    except OutcomeError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{campaign_id}/prospects/{prospect_row_id}/draft-quote-summary", tags=["OBJ-011"])
def draft_quote_summary(campaign_id: int, prospect_row_id: int):
    """Draft a customer-facing recap of the Quote Readiness Checklist into
    the reply_drafts review queue -- edit/approve/send from there, same as
    a smart-reply draft. Nothing is sent until a human approves it."""
    try:
        draft_id = draft_quote_summary_email(campaign_id, prospect_row_id)
        return {"status": "drafted", "draft_id": draft_id}
    except OutcomeError as e:
        raise HTTPException(status_code=422, detail=str(e))
