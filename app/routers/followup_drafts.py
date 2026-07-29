"""
Review queue for automated day-4/day-8 follow-up drafts (app/services/
followups.py generates them once due). Same Draft -> Approved -> Sent
shape as outbound campaign drafts and reply drafts -- approving here only
flips the status, it never touches Gmail. Sending is a separate,
deliberate step -- the campaign's "Send all approved" batch
(approval_and_delivery.send_approved()) picks up Approved follow-up
drafts alongside fresh outreach and replies.
"""
from fastapi import APIRouter, HTTPException

from app.models import FollowUpDraft
from app.services.followups import (
    list_followup_drafts,
    approve_followup_draft,
    reject_followup_draft,
    revert_followup_draft_to_draft,
    FollowUpError,
)

router = APIRouter(prefix="/followup-drafts", tags=["followup-drafts"])


@router.get("", response_model=list[FollowUpDraft])
def list_drafts(status: str | None = None, campaign_id: int | None = None):
    """campaign_id scopes this to one campaign's pending follow-ups -- same
    pattern as GET /reply-drafts."""
    return list_followup_drafts(status, campaign_id)


@router.post("/{draft_id}/approve")
def approve_draft(draft_id: int):
    """Marks the draft Approved -- queues it for the campaign's next 'Send
    all approved' batch rather than sending immediately."""
    try:
        approve_followup_draft(draft_id)
        return {"status": "Approved"}
    except FollowUpError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{draft_id}/reject")
def reject_draft(draft_id: int):
    try:
        reject_followup_draft(draft_id)
        return {"status": "Rejected"}
    except FollowUpError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{draft_id}/back-to-draft")
def back_to_draft_endpoint(draft_id: int):
    """Un-approves an Approved follow-up back to Draft -- left out of the
    next 'Send all approved' without rejecting it outright."""
    try:
        revert_followup_draft_to_draft(draft_id)
        return {"status": "Draft"}
    except FollowUpError as e:
        raise HTTPException(status_code=422, detail=str(e))
