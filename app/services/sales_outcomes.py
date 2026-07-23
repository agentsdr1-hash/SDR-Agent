"""
OBJ-011-lite: Sales outcome tracking (Quote Requested -> Won/Lost).

Phase 1 scope explicitly excludes automated quotation/pricing -- this is
just where a human records the outcome after taking over. It exists so
OBJ-013's dashboard can show real business value (customers won, turnover),
not just outreach activity.

Won/Lost aren't dead ends: mark_won()/mark_lost() also accept an
already-Won or already-Lost prospect, so a deal can be corrected (fix a
typo'd amount, add a reason after the fact) or switched (turned out to be
a loss after all) in one call, without an intermediate "reopen" step.
reopen_outcome() is the separate, explicit action for backing all the way
out to Quote Requested -- e.g. the outcome was logged by mistake, or the
customer re-opened the conversation. Every transition clears the fields
that belong to the *other* outcome so a row is never "Lost" with a stale
deal_value, or "Won" with a stale lost_reason.
"""
from datetime import datetime, timezone

from app.db import get_conn
from app.services.audit import log_event

VALID_FOR_QUOTE = {"Replied"}
VALID_FOR_OUTCOME = {"QuoteRequested", "Won", "Lost"}
REOPENABLE_FROM = {"Won", "Lost"}


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
            raise OutcomeError(f"Can only mark Won from 'Quote Requested', 'Won', or 'Lost' (current: '{status}')")
        conn.execute(
            "UPDATE campaign_prospects SET status = 'Won', won_at = ?, deal_value = ?, "
            "lost_at = NULL, lost_reason = NULL WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), deal_value, prospect_row_id),
        )
    event = "deal_won" if status == "QuoteRequested" else "deal_won_edited"
    log_event(event, "campaign_prospect", str(prospect_row_id),
              f"Campaign {campaign_id}, deal_value={deal_value} (was {status})")


def mark_lost(campaign_id: int, prospect_row_id: int, reason: str | None = None):
    with get_conn() as conn:
        status = _get_status(conn, campaign_id, prospect_row_id)
        if status not in VALID_FOR_OUTCOME:
            raise OutcomeError(f"Can only mark Lost from 'Quote Requested', 'Won', or 'Lost' (current: '{status}')")
        conn.execute(
            "UPDATE campaign_prospects SET status = 'Lost', lost_at = ?, lost_reason = ?, "
            "won_at = NULL, deal_value = NULL WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(), reason, prospect_row_id),
        )
    event = "deal_lost" if status == "QuoteRequested" else "deal_lost_edited"
    log_event(event, "campaign_prospect", str(prospect_row_id),
              f"Campaign {campaign_id}: {reason or 'no reason given'} (was {status})")


def update_quote_details(campaign_id: int, prospect_row_id: int, materials: str | None,
                          quantity: str | None, target_price: float | None, quote_notes: str | None):
    """High-level notes a human gathers toward preparing a real quote --
    what materials, how much, any target price or spec/delivery notes the
    prospect mentioned. Deliberately free-text and status-independent (no
    VALID_FOR_* gate): useful to jot down as soon as a Replied conversation
    starts touching specifics, not just once QuoteRequested is reached.
    This is not itself a quote or a price calculation -- OBJ-011-lite's
    scope explicitly excludes automated quotation/pricing; it's context a
    person builds the real quote from."""
    with get_conn() as conn:
        _get_status(conn, campaign_id, prospect_row_id)  # raises OutcomeError if not found
        conn.execute(
            "UPDATE campaign_prospects SET materials = ?, quantity = ?, target_price = ?, quote_notes = ? "
            "WHERE id = ?",
            (materials, quantity, target_price, quote_notes, prospect_row_id),
        )
    log_event("quote_details_updated", "campaign_prospect", str(prospect_row_id), f"Campaign {campaign_id}")


def reopen_outcome(campaign_id: int, prospect_row_id: int):
    with get_conn() as conn:
        status = _get_status(conn, campaign_id, prospect_row_id)
        if status not in REOPENABLE_FROM:
            raise OutcomeError(f"Can only reopen from 'Won' or 'Lost' (current: '{status}')")
        conn.execute(
            "UPDATE campaign_prospects SET status = 'QuoteRequested', won_at = NULL, lost_at = NULL, "
            "deal_value = NULL, lost_reason = NULL WHERE id = ?",
            (prospect_row_id,),
        )
    log_event("deal_reopened", "campaign_prospect", str(prospect_row_id), f"Campaign {campaign_id}: reopened from {status}")
