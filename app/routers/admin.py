"""
OBJ-015 Administration Console router.
Manual + auto-detected opt-out/suppression list management, the admin
password gate, and the destructive reset-all-data action.
"""
from fastapi import APIRouter, Depends, HTTPException

from app.models import SuppressionEntry, SuppressionAdd, AdminPasswordCheck
from app.services.administration import (
    add_to_suppression_list,
    remove_from_suppression_list,
    list_suppressed,
    reset_all_data,
    AdminError,
)
from app.services.admin_auth import verify_admin_password, require_admin_password

router = APIRouter(prefix="/admin", tags=["OBJ-015"])


@router.post("/verify-password")
def verify_password(payload: AdminPasswordCheck):
    """Checked when the Admin tab is first opened each browser session --
    the tab's content only renders after this succeeds. Every other admin
    route re-checks the password itself via require_admin_password (the
    frontend sends it as a header on each request), so this endpoint isn't
    a session/token grant -- it's just how the UI decides whether to show
    the screen at all."""
    if not verify_admin_password(payload.password):
        raise HTTPException(status_code=401, detail="Incorrect admin password.")
    return {"status": "ok"}


@router.get("/suppressed", response_model=list[SuppressionEntry], dependencies=[Depends(require_admin_password)])
def list_all():
    return list_suppressed()


@router.post("/suppressed", response_model=SuppressionEntry, dependencies=[Depends(require_admin_password)])
def add(payload: SuppressionAdd):
    try:
        add_to_suppression_list(payload.email, payload.reason, source="manual")
    except AdminError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return next(e for e in list_suppressed() if e.email == payload.email.strip().lower())


@router.delete("/suppressed/{email}", dependencies=[Depends(require_admin_password)])
def remove(email: str):
    try:
        remove_from_suppression_list(email)
        return {"status": "removed", "email": email.lower()}
    except AdminError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/reset-all-data", dependencies=[Depends(require_admin_password)])
def reset_all_data_endpoint():
    """Permanently clears every lead/campaign/outreach table -- e.g. before
    a production go-live, so test data never mixes with real data. No
    undo. Gmail config, daily send limit, KB entries, and the stock
    catalog are preserved -- see reset_all_data()'s docstring."""
    reset_all_data()
    return {"status": "reset"}
