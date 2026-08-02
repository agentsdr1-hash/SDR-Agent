"""
Prospect pipeline router: OBJ-001 Prospect Import, OBJ-002 Prospect Validation.

This is the pattern every future object follows: its own router file here,
included once in app/main.py. One process, one deployed service, one URL --
new objects add routes, they don't add new tools.
"""
from fastapi import APIRouter, UploadFile, File, HTTPException

from app.db import get_conn
from app.models import ImportSummary, ValidationSummary, ProspectRecord, ProspectEdit, BusinessCardFields, BusinessCardConfirm
from app.services.prospect_import import (
    import_prospect_file,
    import_prospect_file_from_url,
    create_prospect_from_scan,
    ImportError_,
)
from app.services.prospect_validation import validate_batch, edit_prospect
from app.services.leads import lead_number_for, NON_LEAD_STATUSES
from app.services.business_card import scan_business_card, BusinessCardScanError

router = APIRouter(prefix="/prospects", tags=["prospects"])


@router.post("/import", response_model=ImportSummary, tags=["OBJ-001"])
async def import_prospects(file: UploadFile = File(...)):
    content = await file.read()
    try:
        return import_prospect_file(file.filename, content)
    except ImportError_ as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/import-from-url", response_model=ImportSummary, tags=["OBJ-001"])
def import_prospects_from_url(url: str):
    try:
        return import_prospect_file_from_url(url)
    except ImportError_ as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/scan-business-card", response_model=BusinessCardFields, tags=["AI Brain"])
async def scan_business_card_endpoint(file: UploadFile = File(...)):
    """Reads a photo of a business card via Claude vision and returns the
    extracted fields for review -- nothing is written to the database
    yet. Requires the AI Brain (Claude) connector to be configured from
    the Admin tab; fails with a clear 422 otherwise, same as any other
    not-configured-yet error in this app."""
    content = await file.read()
    media_type = file.content_type or "image/jpeg"
    try:
        return scan_business_card(content, media_type)
    except BusinessCardScanError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.post("/scan-business-card/confirm", response_model=ImportSummary, tags=["AI Brain"])
def confirm_business_card_scan(payload: BusinessCardConfirm):
    """Writes the reviewed/edited fields from a business-card scan as a
    one-row import batch -- the returned batch_id is then handed to the
    normal POST /prospects/validate/{batch_id}, same as any CSV import,
    so the rest of the Import tab's flow (validation results, campaign
    assignment) needs no special-casing for where the row came from."""
    return create_prospect_from_scan(payload.model_dump())


@router.post("/validate/{batch_id}", response_model=ValidationSummary, tags=["OBJ-002"])
def validate_prospects(batch_id: str):
    try:
        return validate_batch(batch_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.put("/{prospect_id}", tags=["OBJ-002"])
def edit_prospect_endpoint(prospect_id: int, payload: ProspectEdit):
    """Correct a prospect's own data (e.g. a missing/malformed email that
    validation caught) and re-validate that one row -- lets a lead move
    from Invalid to Valid without touching the rest of its import batch."""
    try:
        return edit_prospect(prospect_id, payload.first_name, payload.last_name,
                              payload.email, payload.company, payload.phone,
                              payload.lead_source, payload.linkedin_url,
                              payload.next_action, payload.qualification_status,
                              payload.next_action_due)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/{batch_id}", response_model=list[ProspectRecord], tags=["OBJ-001", "OBJ-002"])
def list_prospects(batch_id: str, status: str | None = None):
    query = "SELECT * FROM prospects_raw WHERE batch_id = ?"
    params = [batch_id]
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY row_number"

    with get_conn() as conn:
        rows = conn.execute(query, params).fetchall()

    if not rows:
        raise HTTPException(status_code=404, detail=f"No prospects found for batch '{batch_id}'")

    return [
        ProspectRecord(**dict(r), lead_number=None if r["status"] in NON_LEAD_STATUSES else lead_number_for(r["id"]))
        for r in rows
    ]
