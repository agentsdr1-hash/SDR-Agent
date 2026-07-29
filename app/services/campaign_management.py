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
from app.services.leads import lead_number_for

VALID_DAYS = {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}

# Single place to fix if the spelling/capitalization needs correcting --
# every draft pulls from here rather than being hardcoded per-template.
COMPANY_NAME = "AKEIS"
COMPANY_FULL_NAME = "Al Khaleej Equipment and Industrial Suppliers LLC"


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

        # RETURNING id (not cursor.lastrowid) -- Postgres cursors don't have
        # .lastrowid; RETURNING works identically on both SQLite 3.35+ and
        # Postgres, so this one line is portable rather than dialect-branched.
        cur = conn.execute(
            "INSERT INTO campaigns (name, status, send_days, daily_send_limit, created_at) "
            "VALUES (?, 'Draft', ?, ?, ?) RETURNING id",
            (name, send_days, daily_send_limit, now),
        )
        campaign_id = cur.fetchone()["id"]

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


ABOUT_LINE = (
    f"I'm reaching out from {COMPANY_NAME} -- {COMPANY_FULL_NAME}. With over 40 years in the UAE market, "
    f"we're a leading supplier of structural steel products in Abu Dhabi, ISO 9001:2015 certified, with "
    f"strong partnerships with global manufacturers for consistent quality and reliable supply."
)


def _draft_for(first_name: str | None, company: str | None) -> tuple[str, str]:
    """Mail-merge draft, personalized with the prospect's company and backed
    by the real stock catalog (app/services/stock_catalog.py) so it reads as
    specific to what we actually sell -- at the product-family level
    (pipes, flat bars, angles...) an industry buyer actually talks in, never
    a specific stock-keeping-unit name/dimension. This is what OBJ-004
    replaces with real AI-generated copy once an LLM API key is available --
    everything downstream (approval, send, tracking) works identically
    either way."""
    first = first_name or "there"
    co = company or "your team"
    families = stock_catalog.top_families(5)
    subject = f"{COMPANY_NAME} Steel Supply — quote for {co}" if company else f"{COMPANY_NAME} Steel Supply — quick question"

    if families:
        range_line = ", ".join(families)
        body = (
            f"Hi {first},\n\n"
            f"{ABOUT_LINE}\n\n"
            f"We carry {range_line} in various sizes, grades and thicknesses, with technical consultation "
            f"available if you need help specifying, and an extensive inventory for fast delivery.\n\n"
            f"I wanted to check whether {co} has any upcoming steel requirements we could quote on -- "
            f"happy to send over pricing or a full stock list.\n\n"
            f"Would you have a few minutes this week?\n\nBest,\n{COMPANY_NAME} Sales Team"
        )
    else:
        # No stock catalog imported yet -- fall back to a version that
        # still uses the real company positioning, just without naming a
        # product range we can't currently back up with stock data.
        body = (
            f"Hi {first},\n\n"
            f"{ABOUT_LINE}\n\n"
            f"I wanted to check whether {co} has any upcoming steel requirements we could quote on.\n\n"
            f"Would you be open to a quick conversation this week?\n\nBest,\n{COMPANY_NAME} Sales Team"
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


def assign_prospect_to_campaign(campaign_id: int, prospect_id: int) -> dict:
    """Single-lead version of assign_batch_to_campaign() -- for adding one
    lead (e.g. one just fixed via edit_prospect() and now Valid) to a
    campaign without re-running the whole batch it came from."""
    with get_conn() as conn:
        campaign = conn.execute("SELECT id FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        if not campaign:
            raise CampaignError(f"Campaign {campaign_id} not found")

        p = conn.execute(
            "SELECT id, status, first_name, company, email FROM prospects_raw WHERE id = ?", (prospect_id,)
        ).fetchone()
        if not p:
            raise CampaignError(f"Prospect {prospect_id} not found")
        if p["status"] != "Valid":
            raise CampaignError(f"Can only assign a 'Valid' prospect to a campaign (current: '{p['status']}')")
        if is_suppressed(p["email"]):
            raise CampaignError(f"{p['email']} is on the suppression list")

        subject, body = _draft_for(p["first_name"], p["company"])
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                """INSERT INTO campaign_prospects
                   (campaign_id, prospect_id, status, subject, body, added_at)
                   VALUES (?, ?, 'Queued', ?, ?, ?)""",
                (campaign_id, prospect_id, subject, body, now),
            )
        except Exception:
            raise CampaignError("Already in this campaign")

    log_event("prospects_assigned", "campaign", str(campaign_id), f"Prospect {prospect_id} assigned individually")
    return {"status": "Queued", "campaign_id": campaign_id, "prospect_id": prospect_id}


def _same_company_peers_by_cp_id(conn, rows) -> dict[int, list[dict]]:
    """For each row, every OTHER campaign_prospects row -- in ANY campaign,
    not just this one -- whose prospect shares the same company (trimmed,
    case-insensitive), excluding Rejected rows (a dead end, not something
    that's actually going to send). Matched on company name alone since
    that's all a CSV import reliably gives us; two prospects at "Acme LLC"
    and "Acme L.L.C." won't be caught, but exact-name variants (the common
    case: three different contacts pasted from the same company website)
    will.

    Multiple contacts at one company aren't necessarily a mistake -- but
    silently cold-pitching three of them from three different campaigns
    with three unrelated messages reads as spam to the recipient. This
    is visibility, not a block: nothing here stops an approve or send,
    it just makes the overlap impossible to miss before doing either."""
    companies = {row["company"].strip().lower() for row in rows if row["company"] and row["company"].strip()}
    if not companies:
        return {}
    ph = ",".join("?" * len(companies))
    all_rows = conn.execute(
        f"""SELECT cp.id AS cp_id, cp.prospect_id, cp.status, c.name AS campaign_name,
                   pr.first_name, pr.last_name, pr.company
            FROM campaign_prospects cp
            JOIN prospects_raw pr ON pr.id = cp.prospect_id
            JOIN campaigns c ON c.id = cp.campaign_id
            WHERE LOWER(TRIM(pr.company)) IN ({ph}) AND cp.status != 'Rejected'""",
        list(companies),
    ).fetchall()

    by_company: dict[str, list[dict]] = {}
    for r in all_rows:
        key = r["company"].strip().lower()
        by_company.setdefault(key, []).append({
            "cp_id": r["cp_id"],
            "lead_number": lead_number_for(r["prospect_id"]),
            "name": " ".join(x for x in [r["first_name"], r["last_name"]] if x) or "—",
            "campaign_name": r["campaign_name"],
            "status": r["status"],
        })

    result: dict[int, list[dict]] = {}
    for row in rows:
        if not row["company"] or not row["company"].strip():
            continue
        key = row["company"].strip().lower()
        peers = [p for p in by_company.get(key, []) if p["cp_id"] != row["id"]]
        if peers:
            result[row["id"]] = peers
    return result


def list_campaign_prospects(campaign_id: int) -> list[CampaignProspect]:
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT cp.id, cp.prospect_id, cp.status, cp.subject, cp.body, cp.added_at,
                      cp.approved_at, cp.sent_at, cp.replied_at, cp.reply_subject,
                      cp.quote_requested_at, cp.won_at, cp.lost_at, cp.deal_value, cp.lost_reason,
                      cp.last_followup_at,
                      pr.first_name, pr.last_name, pr.email, pr.company
               FROM campaign_prospects cp
               JOIN prospects_raw pr ON pr.id = cp.prospect_id
               WHERE cp.campaign_id = ?
               ORDER BY cp.added_at""",
            (campaign_id,),
        ).fetchall()
        # follow_up_count is the "N/2 sent" badge -- actually delivered
        # follow-ups only, not drafts still waiting on review.
        # pending_followup_count separately flags a draft that needs
        # approval, same shape as the reply-pending badge.
        counts = conn.execute(
            """SELECT campaign_prospect_id, COUNT(*) c FROM campaign_followups
               WHERE campaign_prospect_id IN (SELECT id FROM campaign_prospects WHERE campaign_id = ?)
                 AND status = 'Sent'
               GROUP BY campaign_prospect_id""",
            (campaign_id,),
        ).fetchall()
        pending_counts = conn.execute(
            """SELECT campaign_prospect_id, COUNT(*) c FROM campaign_followups
               WHERE campaign_prospect_id IN (SELECT id FROM campaign_prospects WHERE campaign_id = ?)
                 AND status = 'Draft'
               GROUP BY campaign_prospect_id""",
            (campaign_id,),
        ).fetchall()
        same_company = _same_company_peers_by_cp_id(conn, rows)
    count_by_cp = {c["campaign_prospect_id"]: c["c"] for c in counts}
    pending_by_cp = {c["campaign_prospect_id"]: c["c"] for c in pending_counts}
    return [
        CampaignProspect(**dict(r), lead_number=lead_number_for(r["prospect_id"]), follow_up_count=count_by_cp.get(r["id"], 0),
                          pending_followup_count=pending_by_cp.get(r["id"], 0),
                          same_company_peers=same_company.get(r["id"], []))
        for r in rows
    ]
