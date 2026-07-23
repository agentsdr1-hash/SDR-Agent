"""
Prospect pipeline router: OBJ-001 Prospect Import, OBJ-002 Prospect Validation.

This is the pattern every future object follows: its own router file here,
included once in app/main.py. One process, one deployed service, one URL --
new objects add routes, they don't add new tools.
"""
from fastapi import APIRouter, UploadFile, File, HTTPException

from app.db import get_conn
from app.models import ImportSummary, ValidationSummary, ProspectRecord, ProspectEdit
from app.services.prospect_import import (
    import_prospect_file,
    import_prospect_file_from_url,
    ImportError_,
)
from app.services.prospect_validation import validate_batch, edit_prospect
from app.services.leads import lead_number_for

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
                              payload.next_action, payload.qualification_status)
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

    return [ProspectRecord(**dict(r), lead_number=lead_number_for(r["id"])) for r in rows]
