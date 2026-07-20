"""
OBJ-011-lite: Sales outcome tracking (Quote Requested -> Won/Lost).

Phase 1 scope explicitly excludes automated quotation/pricing -- this is
just where a human records the outcome after taking over. It exists so
OBJ-013's dashboard can show real business value (customers won, turnover),
not just outreach activity.
"""
from datetime import datetime, timezone

from app.db import get_conn
from app.services.audit import log_event

VALID_FOR_QUOTE = {"Replied"}
VALID_FOR_OUTCOME = {"QuoteRequested"}


class OutcomeError(Exception):
    pass


def _get_status(conn, campaign_id: int, prospect_row_id: int) -> str:
    row = conn.execute(
        "SELECT status FROM campaign_prospects WHERE campaign_id = ? AND id = ?",
        (campaign_id, prospect_row_id),
    ).fetchone()
    if not row:
        raise OutcomeError("Campaign prospect not found")
    return row["status"]


def request_quote(campaign_id: int, prospect_row_id: int):
    with get_conn() as conn:
        status = _get_status(conn, campaign_id, prospect_row_id)
        if status not in VALID_FOR_QUOTE:
            raise OutcomeError(f"Can only request a quote from status 'Replied' (current: '{status}')")
        conn.execute(
            "UPDATE campaign_prospects SET status = 'QuoteRequested', quote_requested_at = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), prospect_row_id),
        )
    log_event("quote_requested", "campaign_prospect", str(prospect_row_id), f"Campaign {campaign_id}")


def mark_won(campaign_id: int, prospect_row_id: int, deal_value: float | None = None):
    with get_conn() as conn:
        status = _get_status(conn, campaign_id, prospect_row_id)
        if status not in VALID_FOR_OUTCOME:
            raise OutcomeError(f"Can only mark Won from status 'QuoteRequested' (current: '{status}')")
        conn.execute(
            "UPDATE campaign_prospects SET status = 'Won', won_at = ?, deal_value = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), deal_value, prospect_row_id),
        )
    log_event("deal_won", "campaign_prospect", str(prospect_row_id),
               f"Campaign {campaign_id}, deal_value={deal_value}")


def mark_lost(campaign_id: int, prospect_row_id: int, reason: str | None = None):
    with get_conn() as conn:
        status = _get_status(conn, campaign_id, prospect_row_id)
        if status not in VALID_FOR_OUTCOME:
            raise OutcomeError(f"Can only mark Lost from status 'QuoteRequested' (current: '{status}')")
        conn.execute(
            "UPDATE campaign_prospects SET status = 'Lost', lost_at = ?, lost_reason = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), reason, prospect_row_id),
        )
    log_event("deal_lost", "campaign_prospect", str(prospect_row_id), f"Campaign {campaign_id}: {reason or 'no reason given'}")
