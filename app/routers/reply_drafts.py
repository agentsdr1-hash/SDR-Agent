"""
Review queue for smart-reply drafts. Approving here just flips the
status to Approved -- it never touches Gmail. Actual sending happens
from the campaign's "Send all approved" action
(POST /campaigns/{id}/send), alongside that campaign's fresh outreach,
so every real send goes through one chokepoint (suppression-list check,
daily-pacing cap) instead of a reply having its own separate
immediate-send path.
"""
from fastapi import APIRouter, HTTPException

from app.models import ReplyDraft, ReplyDraftUpdate
from app.services.reply_drafts import (
    list_reply_drafts,
    update_reply_draft,
    approve_reply_draft,
    reject_reply_draft,
    ReplyDraftError,
)

router = APIRouter(prefix="/reply-drafts", tags=["reply-drafts"])


@router.get("", response_model=list[ReplyDraft])
def list_drafts(status: str | None = None, campaign_id: int | None = None):
    """campaign_id scopes this to one campaign's pending replies -- the
    Campaigns tab's review panel always passes the currently selected
    campaign. Omit it for the (currently unused) cross-campaign view."""
    return list_reply_drafts(status, campaign_id)


@router.put("/{draft_id}")
def edit_draft(draft_id: int, payload: ReplyDraftUpdate):
    try:
        update_reply_draft(draft_id, payload.subject, payload.body)
        return {"status": "updated"}
    except ReplyDraftError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{draft_id}/approve")
def approve_draft(draft_id: int):
    """Marks the draft Approved -- queues it for the campaign's next 'Send
    all approved' batch rather than sending immediately."""
    try:
        approve_reply_draft(draft_id)
        return {"status": "Approved"}
    except ReplyDraftError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{draft_id}/reject")
def reject_draft(draft_id: int):
    try:
        reject_reply_draft(draft_id)
        return {"status": "Rejected"}
    except ReplyDraftError as e:
        raise HTTPException(status_code=422, detail=str(e))
