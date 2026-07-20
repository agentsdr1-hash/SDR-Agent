from fastapi import APIRouter

from app.services.reporting import get_summary

router = APIRouter(prefix="/reports", tags=["OBJ-013"])


@router.get("/summary")
def summary():
    return get_summary()
