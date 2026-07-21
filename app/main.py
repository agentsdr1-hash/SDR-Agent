"""
APEX Pilot — single FastAPI service, single deploy.

Every object from the Build Tracker lives here as a router. Adding OBJ-003+
later means adding a router file and one include_router line below -- not a
new service to host.
"""
import asyncio
import logging
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # picks up a local .env file if present; no-op if it doesn't exist

from fastapi import FastAPI
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from app.db import init_db
from app.routers import prospects, campaigns, reports, email as email_router, admin, audit, dbtables, knowledge_base
from app.integrations import email_provider
from app.services import inbox_monitor

logger = logging.getLogger("apex.email_poll")

app = FastAPI(title="APEX SDR Pilot", version="0.1.0")

app.include_router(prospects.router)
app.include_router(campaigns.router)
app.include_router(reports.router)
app.include_router(email_router.router)
app.include_router(admin.router)
app.include_router(audit.router)
app.include_router(dbtables.router)
app.include_router(knowledge_base.router)
# Future objects plug in the same way, e.g.:
# from app.routers import outreach
# app.include_router(outreach.router)

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_poll_task: asyncio.Task | None = None


async def _email_poll_loop():
    """OBJ-016 background polling. Runs forever at POLL_INTERVAL_MINUTES,
    quietly does nothing each cycle until GMAIL_ADDRESS/GMAIL_APP_PASSWORD
    are set -- no crash, no noisy retries, just waits for configuration."""
    while True:
        interval = email_provider.poll_interval_minutes()
        if email_provider.is_configured():
            try:
                result = inbox_monitor.poll_once()
                if result.replies_found:
                    logger.info(f"Email poll: {result.replies_found} new reply(ies) found")
            except Exception as e:
                logger.warning(f"Email poll failed (will retry next cycle): {e}")
        await asyncio.sleep(interval * 60)


@app.on_event("startup")
def startup():
    init_db()
    global _poll_task
    _poll_task = asyncio.create_task(_email_poll_loop())


@app.on_event("shutdown")
def shutdown():
    if _poll_task:
        _poll_task.cancel()


@app.get("/", tags=["frontend"])
def frontend():
    return FileResponse(STATIC_DIR / "app.html")


@app.get("/app", tags=["frontend"])
def app_page():
    return FileResponse(STATIC_DIR / "app.html")


@app.get("/dashboard", tags=["frontend"])
def dashboard():
    return FileResponse(STATIC_DIR / "app.html")


@app.get("/admin", tags=["frontend"])
def admin_page():
    return FileResponse(STATIC_DIR / "app.html")


@app.get("/health")
def health():
    return JSONResponse({"status": "ok"})
