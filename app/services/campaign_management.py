"""
OBJ-003 Campaign Management
Configure outreach campaigns and schedules.

Scope for this object: create/list campaigns, and assign prospects from a
validated import batch into a campaign queue. Sending itself is OBJ-006
(Email Delivery) and OBJ-016 (Email Integration) -- this object only decides
who's in a campaign and on what cadence, it doesn't touch email infrastructure.
"""
from datetime import datetime, timezone

from app.db import get_conn
from app.models import Campaign, AssignResult, CampaignProspect
from app.services.administration import is_suppressed
from app.services.audit import log_event
from app.services import stock_catalog

VALID_DAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}

# Single place to fix if the spelling/capitalization needs correcting --
# every draft pulls from here rather than being hardcoded per-template.
COMPANY_NAME = "AKEIS"


class CampaignError(Exception):
    pass


def _validate_send_days(send_days: str):
    days = [d.strip() for d in send_days.split(",") if d.strip()]
    if not days:
        raise CampaignError("send_days cannot be empty")
    bad = [d for d in days if d not in VALID_DAYS]
    if bad:
        raise CampaignError(f"Invalid day(s) in send_days: {bad}. Use Mon/Tue/Wed/Thu/Fri/Sat/Sun")
    return ",".join(days)


def create_campaign(name: str, send_days: str = "Mon,Tue,Wed,Thu,Fri", daily_send_limit: int = 25) -> Campaign:
    name = name.strip()
    if not name:
        raise CampaignError("Campaign name is required")
    if daily_send_limit <= 0:
        raise CampaignError("daily_send_limit must be positive")
    send_days = _validate_send_days(send_days)

    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        # Names must be unique (case-insensitive) so campaigns created on
        # different days stay distinguishable in lists/dropdowns instead of
        # silently colliding -- e.g. two runs both named "Outbound".
        existing = conn.execute("SELECT id FROM campaigns WHERE LOWER(name) = LOWER(?)", (name,)).fetchone()
        if existing:
            today = datetime.now(timezone.utc).date().isoformat()
            suggestion = f"{today} — {name}" if not name.startswith(today) else f"{name} (2)"
            raise CampaignError(
                f"A campaign named '{name}' already exists (id {existing['id']}). "
                f"Campaign names must be unique -- try something like '{suggestion}'."
            )

        cur = conn.execute(
            "INSERT INTO campaigns (name, status, send_days, daily_send_limit, created_at) VALUES (?, 'Draft', ?, ?, ?)",
            (name, send_days, daily_send_limit, now),
        )
        campaign_id = cur.lastrowid

    log_event("campaign_created", "campaign", str(campaign_id), f"Created campaign '{name}'")
    return get_campaign(campaign_id)


def get_campaign(campaign_id: int) -> Campaign:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not row:
            raise CampaignError(f"Campaign {campaign_id} not found")
        count = conn.execute(
            "SELECT COUNT(*) c FROM campaign_prospects WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()["c"]
    return Campaign(**dict(row), prospect_count=count)


def list_campaigns() -> list[Campaign]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            count = conn.execute(
                "SELECT COUNT(*) c FROM campaign_prospects WHERE campaign_id = ?", (row["id"],)
            ).fetchone()["c"]
            result.append(Campaign(**dict(row), prospect_count=count))
    return result


def _draft_for(first_name: str | None, company: str | None) -> tuple[str, str]:
    """Mail-merge draft, personalized with the prospect's company and backed
    by the real stock catalog (app/services/stock_catalog.py) so it reads as
    specific to what we actually sell rather than generic outreach copy.
    This is what OBJ-004 replaces with real AI-generated copy once an LLM
    API key is available -- everything downstream (approval, send,
    tracking) works identically either way."""
    first = first_name or "there"
    co = company or "your team"
    categories = stock_catalog.top_categories(5)
    subject = f"{COMPANY_NAME} Steel Supply — quote for {co}" if company else f"{COMPANY_NAME} Steel Supply — quick question"

    if categories:
        range_line = ", ".join(categories)
        total = stock_catalog.total_count()
        examples = stock_catalog.sample_items(2)
        example_line = ""
        if examples:
            ex_text = " and ".join(e["product_name"] for e in examples)
            example_line = f" For example, we currently hold {ex_text} in stock, ready to ship."
        body = (
            f"Hi {first},\n\n"
            f"I'm reaching out from {COMPANY_NAME} -- we're a steel stockist and distributor carrying "
            f"an extensive in-stock range including {range_line}, out of {total}+ steel products total.\n\n"
            f"I wanted to check whether {co} has any upcoming steel requirements we could quote on.{example_line}\n\n"
            f"Happy to send over pricing or a full stock list -- would you have a few minutes this week?\n\n"
            f"Best,\n{COMPANY_NAME} Sales Team"
        )
    else:
        # No stock catalog imported yet -- fall back to a generic version
        # rather than referencing a range we can't actually back up.
        body = (
            f"Hi {first},\n\n"
            f"I'm reaching out from {COMPANY_NAME} -- we're a steel stockist and distributor, and I noticed "
            f"{co} might have steel supply needs we could help with.\n\n"
            f"Would you be open to a quick conversation about your upcoming requirements?\n\n"
            f"Best,\n{COMPANY_NAME} Sales Team"
        )
    return subject, body


def assign_batch_to_campaign(campaign_id: int, batch_id: str) -> AssignResult:
    """Queue every 'Valid' prospect from a batch into a campaign. Idempotent --
    re-running on the same batch/campaign just skips already-assigned rows."""
    with get_conn() as conn:
        campaign = conn.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not campaign:
            raise CampaignError(f"Campaign {campaign_id} not found")

        prospects = conn.execute(
            "SELECT id, status, first_name, company, email FROM prospects_raw WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        if not prospects:
            raise CampaignError(f"No prospects found for batch '{batch_id}'")

        assigned = 0
        skipped_not_valid = 0
        skipped_already = 0
        skipped_suppressed = 0
        now = datetime.now(timezone.utc).isoformat()

        for p in prospects:
            if p["status"] != "Valid":
                skipped_not_valid += 1
                continue
            if is_suppressed(p["email"]):
                skipped_suppressed += 1
                continue
            subject, body = _draft_for(p["first_name"], p["company"])
            try:
                conn.execute(
                    """INSERT INTO campaign_prospects
                       (campaign_id, prospect_id, status, subject, body, added_at)
                       VALUES (?, ?, 'Queued', ?, ?, ?)""",
                    (campaign_id, p["id"], subject, body, now),
                )
                assigned += 1
            except Exception:
                skipped_already += 1

    log_event(
        "prospects_assigned", "campaign", str(campaign_id),
        f"Batch {batch_id}: assigned={assigned} skipped_suppressed={skipped_suppressed} skipped_not_valid={skipped_not_valid}"
    )

    return AssignResult(
        campaign_id=campaign_id,
        batch_id=batch_id,
        assigned=assigned,
        skipped_already_in_campaign=skipped_already,
        skipped_not_valid=skipped_not_valid,
        skipped_suppressed=skipped_suppressed,
    )


def list_campaign_prospects(campaign_id: int) -> list[CampaignProspect]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT cp.id, cp.prospect_id, cp.status, cp.subject, cp.body, cp.added_at,
                      cp.approved_at, cp.sent_at, cp.replied_at, cp.reply_subject,
                      cp.quote_requested_at, cp.won_at, cp.lost_at, cp.deal_value, cp.lost_reason,
                      pr.first_name, pr.last_name, pr.email, pr.company
               FROM campaign_prospects cp
               JOIN prospects_raw pr ON pr.id = cp.prospect_id
               WHERE cp.campaign_id = ?
               ORDER BY cp.added_at""",
            (campaign_id,),
        ).fetchall()
    return [CampaignProspect(**dict(r)) for r in rows]
