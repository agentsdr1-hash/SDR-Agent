"""
Knowledge Base router -- the steel stock catalog (lets the stock list be
uploaded/browsed, backs product-family mentions in outreach drafts) and
the Q&A entries (backs smart-reply drafting in app/services/kb_qa.py).
"""
from fastapi import APIRouter, UploadFile, File, HTTPException

from app.models import (
    StockImportSummary, StockCategory, StockItem,
    KBEntry, KBEntryCreate, KBImportSummary,
)
from app.services.stock_catalog import (
    import_stock_list,
    list_categories,
    list_items,
    total_count,
    StockImportError,
)
from app.services import kb_qa

router = APIRouter(prefix="/knowledge-base", tags=["knowledge-base"])


# ── Stock catalog ──────────────────────────────────────────────────────
@router.post("/stock/import", response_model=StockImportSummary)
async def import_stock(file: UploadFile = File(...)):
    content = await file.read()
    try:
        return import_stock_list(file.filename, content)
    except StockImportError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/stock/categories", response_model=list[StockCategory])
def categories():
    return list_categories()


@router.get("/stock/count")
def count():
    return {"total": total_count()}


@router.get("/stock", response_model=list[StockItem])
def items(category: str | None = None, search: str | None = None, limit: int = 200):
    return list_items(category, search, limit)


# ── Q&A entries ─────────────────────────────────────────────────────────
@router.get("/qa", response_model=list[KBEntry])
def list_qa():
    return kb_qa.list_kb_entries()


@router.post("/qa", response_model=KBEntry)
def add_qa(payload: KBEntryCreate):
    return kb_qa.add_kb_entry(payload.question, payload.answer, payload.tags)


@router.post("/qa/import", response_model=KBImportSummary)
def import_qa(entries: list[KBEntryCreate], replace: bool = False):
    return kb_qa.import_kb_entries([e.model_dump() for e in entries], replace=replace)


@router.delete("/qa/{entry_id}")
def delete_qa(entry_id: int):
    kb_qa.delete_kb_entry(entry_id)
    return {"status": "deleted"}
