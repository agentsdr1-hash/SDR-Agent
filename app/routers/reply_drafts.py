"""
Review queue for smart-reply drafts. A draft only leaves this server
after a human approves it here -- approval sends via Gmail immediately
and fails with 503 if not configured, same pattern as outbound sends.
"""
from fastapi import APIRouter, HTTPException

from app.models import ReplyDraft, ReplyDraftUpdate
from app.services.reply_drafts import (
    list_reply_drafts,
    update_reply_draft,
    approve_and_send_reply_draft,
    reject_reply_draft,
    ReplyDraftError,
)
from app.integrations.email_provider import EmailNotConfiguredError, EmailSendError

router = APIRouter(prefix="/reply-drafts", tags=["reply-drafts"])


@router.get("", response_model=list[ReplyDraft])
def list_drafts(status: str | None = None):
    return list_reply_drafts(status)


@router.put("/{draft_id}")
def edit_draft(draft_id: int, payload: ReplyDraftUpdate):
    try:
        update_reply_draft(draft_id, payload.subject, payload.body)
        return {"status": "updated"}
    except ReplyDraftError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{draft_id}/approve")
def approve_draft(draft_id: int):
    """Approves AND sends in one step -- there's no meaningful 'approved but
    not sent' state for a reply the way there is for a fresh campaign send,
    since a reply draft only exists once, for one recipient."""
    try:
        approve_and_send_reply_draft(draft_id)
        return {"status": "Sent"}
    except EmailNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except EmailSendError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except ReplyDraftError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/{draft_id}/reject")
def reject_draft(draft_id: int):
    try:
        reject_reply_draft(draft_id)
        return {"status": "Rejected"}
    except ReplyDraftError as e:
        raise HTTPException(status_code=422, detail=str(e))
