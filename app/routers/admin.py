"""
OBJ-015 Administration Console router.
Manual + auto-detected opt-out/suppression list management.
"""
from fastapi import APIRouter, HTTPException

from app.models import SuppressionEntry, SuppressionAdd
from app.services.administration import (
    add_to_suppression_list,
    remove_from_suppression_list,
    list_suppressed,
    AdminError,
)

router = APIRouter(prefix="/admin", tags=["OBJ-015"])


@router.get("/suppressed", response_model=list[SuppressionEntry])
def list_all():
    return list_suppressed()


@router.post("/suppressed", response_model=SuppressionEntry)
def add(payload: SuppressionAdd):
    try:
        add_to_suppression_list(payload.email, payload.reason, source="manual")
    except AdminError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return next(e for e in list_suppressed() if e.email == payload.email.strip().lower())


@router.delete("/suppressed/{email}")
def remove(email: str):
    try:
        remove_from_suppression_list(email)
        return {"status": "removed", "email": email.lower()}
    except AdminError as e:
        raise HTTPException(status_code=404, detail=str(e))
