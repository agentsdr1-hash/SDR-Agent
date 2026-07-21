"""
Steel stock catalog router -- lets the stock list be uploaded and browsed,
and backs the category/example data that outreach drafts pull from.
"""
from fastapi import APIRouter, UploadFile, File, HTTPException

from app.models import StockImportSummary, StockCategory, StockItem
from app.services.stock_catalog import (
    import_stock_list,
    list_categories,
    list_items,
    total_count,
    StockImportError,
)

router = APIRouter(prefix="/knowledge-base/stock", tags=["knowledge-base"])


@router.post("/import", response_model=StockImportSummary)
async def import_stock(file: UploadFile = File(...)):
    content = await file.read()
    try:
        return import_stock_list(file.filename, content)
    except StockImportError as e:
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/categories", response_model=list[StockCategory])
def categories():
    return list_categories()


@router.get("/count")
def count():
    return {"total": total_count()}


@router.get("", response_model=list[StockItem])
def items(category: str | None = None, search: str | None = None, limit: int = 200):
    return list_items(category, search, limit)
