"""
Lead lifecycle lookup.

A "lead" is a prospect from the moment it's imported -- L-000123 is a
human-referenceable, immutable identifier derived from prospects_raw.id
(already unique and monotonically increasing, so no separate counter is
needed; this is the same pattern Salesforce/HubSpot/etc. use for their
auto-numbered Lead/Deal fields, distinct from the internal record ID).

This is distinct from campaign_prospects.id, which tracks one specific
campaign membership's lifecycle (Queued -> ... -> Won/Lost). A lead can
have zero, one, or -- if re-engaged in a later campaign -- more than one
of those over time. get_lead_timeline() stitches them all into one view:
who they are, every campaign they've been part of, every stage timestamp,
and the audit trail, so "was this lead a win, a loss, what's it worth"
is answerable from one lookup instead of hunting across tables.
"""
import re
from datetime import datetime, timezone

from app.db import get_conn
from app.services.audit import log_event

LEAD_PREFIX = "L"

# Statuses that never become a visible "lead" -- Invalid (couldn't even be
# read as a person), Duplicate (already have this exact prospect from an
# earlier import), Existing Customer (already a paying customer, not a
# sales target), Already Contacted (a re-import of someone whose real,
# actionable lead already exists under a different row/lead number). Each
# still gets written and classified normally by prospect_validation.py, so
# the Dashboard's prospect funnel (a plain GROUP BY over prospects_raw)
# keeps an accurate running total of everything ever imported -- only
# list_leads()/get_lead_timeline() filter them out, so the Leads tab shows
# only what's actually actionable.
NON_LEAD_STATUSES = ("Invalid", "Duplicate", "Existing Customer", "Already Contacted")


def lead_number_for(prospect_id: int) -> str:
    return f"{LEAD_PREFIX}-{prospect_id:06d}"


def parse_lead_number(lead_number: str) -> int | None:
    # hyphen optional on input so old-format numbers (L000123, from before
    # the L-000123 format) still resolve -- lead_number_for() above always
    # emits the hyphenated form going forward.
    m = re.match(rf"^{LEAD_PREFIX}-?0*(\d+)$", (lead_number or "").strip().upper())
    return int(m.group(1)) if m else None


# Quote Readiness Checklist -- the high-level info a human needs on hand
# before building a real quote. (column on campaign_prospects, display label).
# target_price is deliberately not part of this list: it's a budget hint,
# not a checklist item the customer needs to have supplied.
QUOTE_CHECKLIST_FIELDS = [
    ("materials", "Product(s)"),
    ("sku_spec", "SKU or specification"),
    ("quantity", "Quantity"),
    ("unit_of_measure", "Unit of Measure"),
    ("destination", "Destination"),
    ("shipping_terms", "Shipping Terms (Incoterms)"),
    ("delivery_date", "Delivery Date"),
    ("currency", "Currency"),
    ("payment_terms", "Payment Terms"),
    ("packaging_requirements", "Packaging Requirements"),
    ("quote_notes", "Special instructions"),
]


def quote_readiness(m: dict | None) -> dict:
    """How much of the Quote Readiness Checklist is filled in for a
    membership's latest campaign row -- used to flag a lead as ready for a
    human to build a real quote from, and as one input to the lead score."""
    total = len(QUOTE_CHECKLIST_FIELDS)
    if not m:
        return {"filled": 0, "total": total, "pct": 0.0, "ready": False}
    filled = sum(1 for key, _ in QUOTE_CHECKLIST_FIELDS if m.get(key))
    return {"filled": filled, "total": total, "pct": round(filled / total, 2), "ready": filled == total}


# Rule-based, deterministic lead score (0-100) -- no AI, every point
# explainable. Combines funnel progress (the strongest single signal),
# lead source intent, LinkedIn presence (a proxy for a reachable, real
# decision-maker), and quote-readiness completeness (how close to
# quote-able the conversation actually is).
_STATUS_SCORE = {
    "Imported -- not yet in a campaign": 5, "Queued": 10, "Approved": 15,
    "Rejected": 0, "Sent": 20, "Replied": 45, "Suppressed": 0,
    "QuoteRequested": 65, "Won": 100, "Lost": 10,
}
_LEAD_SOURCE_SCORE = {"Referral": 15, "Trade Show": 10, "Website": 5}


def compute_lead_score(status: str, lead_source: str | None, linkedin_url: str | None,
                        readiness_pct: float) -> int:
    if status == "Won":
        return 100
    score = _STATUS_SCORE.get(status, 0)
    score += _LEAD_SOURCE_SCORE.get(lead_source or "", 0)
    if linkedin_url:
        score += 5
    score += round(readiness_pct * 15)
    return min(100, score)


def _summarize(memberships: list[dict]) -> dict:
    if not memberships:
        return {"overall_status": "Imported -- not yet in a campaign", "total_won_value": 0.0, "won": False, "lost": False}
    latest = memberships[-1]
    total_won_value = sum(m["deal_value"] or 0 for m in memberships if m["status"] == "Won")
    return {
        "overall_status": latest["status"],
        "total_won_value": total_won_value,
        "won": any(m["status"] == "Won" for m in memberships),
        "lost": any(m["status"] == "Lost" for m in memberships),
    }


# Reverse funnel order -- the first of these that's set on the latest
# membership is that membership's most recent stage, which (since the app
# only ever advances a status forward, never backdates one) is also its
# most recent timestamp. Used to sort the consolidated list by "most
# recently active" and to show a single "last activity" column per lead.
_TS_FIELDS_NEWEST_FIRST = [
    "lost_at", "won_at", "quote_requested_at", "replied_at", "sent_at", "approved_at", "added_at",
]


def _latest_timestamp(m: dict) -> str | None:
    for f in _TS_FIELDS_NEWEST_FIRST:
        if m.get(f):
            return m[f]
    return None


def list_leads(search: str | None = None, status: str | None = None,
               validation_status: str | None = None, ever_sent: bool | None = None,
               ever_replied: bool | None = None, ever_quoted: bool | None = None,
               quote_ready: bool | None = None, follow_up_due: bool | None = None) -> list[dict]:
    """Every prospect across every campaign (and prospects not yet in any
    campaign), one row per lead, for the consolidated Leads tab -- as
    opposed to get_lead_timeline()'s single-lead full-detail view, or the
    Dashboard/Campaigns tab's per-campaign or per-import-batch tables.

    Rows classified into any of NON_LEAD_STATUSES (Invalid, Duplicate,
    Existing Customer, Already Contacted) are excluded entirely -- none of
    them are a fresh, actionable lead, so none of them get a Lead # or
    show up here. Invalid rows stay visible in the Import tab's per-batch
    review table, where they can be corrected via edit_prospect(); the
    moment an edit re-validates one to Valid, it appears here like any
    other lead. The other three are permanent classifications (nothing to
    fix), but every row -- including these -- is still written and
    counted in the Dashboard's prospect funnel, so "how many total
    prospects were ever added, and how many were duplicates/existing
    customers/etc." stays answerable even though none of them clutter
    this tab.

    status/validation_status match the Dashboard's per-campaign status
    counts and prospect-funnel counts exactly (both are GROUP BY status
    snapshots). ever_sent/ever_replied/ever_quoted match the SDR-performance
    and value-captured stats, which count sent_at/replied_at/
    quote_requested_at IS NOT NULL -- a superset of "status is currently
    X", since those timestamps persist after the status moves on (e.g. a
    Won deal still has sent_at and replied_at set). follow_up_due looks at
    next_action_due directly on prospects_raw, not a campaign membership.
    All four campaign-based filters (like the rest of this function) look
    only at each lead's latest campaign membership, consistent with the
    rest of the Leads tab."""
    with get_conn() as conn:
        status_ph = ",".join("?" * len(NON_LEAD_STATUSES))
        prospects = [dict(r) for r in conn.execute(
            f"SELECT id, first_name, last_name, email, company, phone, status AS validation_status, "
            f"validation_notes, lead_source, linkedin_url, next_action, next_action_due, qualification_status "
            f"FROM prospects_raw WHERE status NOT IN ({status_ph}) ORDER BY id DESC",
            NON_LEAD_STATUSES,
        ).fetchall()]

        memberships_by_prospect: dict[int, list[dict]] = {}
        if prospects:
            ph = ",".join("?" * len(prospects))
            m_rows = conn.execute(
                f"""SELECT cp.*, c.name AS campaign_name
                    FROM campaign_prospects cp
                    JOIN campaigns c ON c.id = cp.campaign_id
                    WHERE cp.prospect_id IN ({ph})
                    ORDER BY cp.added_at""",
                [p["id"] for p in prospects],
            ).fetchall()
            for m in m_rows:
                memberships_by_prospect.setdefault(m["prospect_id"], []).append(dict(m))

    leads = []
    for p in prospects:
        memberships = memberships_by_prospect.get(p["id"], [])
        summary = _summarize(memberships)
        latest = memberships[-1] if memberships else None
        readiness = quote_readiness(latest)
        leads.append({
            "lead_number": lead_number_for(p["id"]),
            "prospect_id": p["id"],
            "first_name": p["first_name"],
            "last_name": p["last_name"],
            "email": p["email"],
            "company": p["company"],
            "phone": p["phone"],
            "validation_status": p["validation_status"],
            "validation_notes": p["validation_notes"],
            "lead_source": p["lead_source"],
            "linkedin_url": p["linkedin_url"],
            "next_action": p["next_action"],
            "next_action_due": p["next_action_due"],
            "qualification_status": p["qualification_status"],
            "campaign_count": len(memberships),
            "campaign_id": latest["campaign_id"] if latest else None,
            "campaign_prospect_id": latest["id"] if latest else None,
            "campaign_name": latest["campaign_name"] if latest else None,
            "status": summary["overall_status"],
            "deal_value": latest["deal_value"] if latest else None,
            "quote_number": latest["quote_number"] if latest else None,
            "lost_reason": latest["lost_reason"] if latest else None,
            "materials": latest["materials"] if latest else None,
            "quantity": latest["quantity"] if latest else None,
            "target_price": latest["target_price"] if latest else None,
            "won": summary["won"],
            "lost": summary["lost"],
            "last_activity_at": _latest_timestamp(latest) if latest else None,
            "lead_score": compute_lead_score(summary["overall_status"], p["lead_source"], p["linkedin_url"], readiness["pct"]),
            "quote_readiness": readiness,
            "_sent_at": latest["sent_at"] if latest else None,
            "_replied_at": latest["replied_at"] if latest else None,
            "_quote_requested_at": latest["quote_requested_at"] if latest else None,
        })

    if status:
        leads = [l for l in leads if l["status"] == status]
    if validation_status:
        leads = [l for l in leads if l["validation_status"] == validation_status]
    if ever_sent:
        leads = [l for l in leads if l["_sent_at"]]
    if ever_replied:
        leads = [l for l in leads if l["_replied_at"]]
    if ever_quoted:
        leads = [l for l in leads if l["_quote_requested_at"]]
    if quote_ready:
        leads = [l for l in leads if l["quote_readiness"]["ready"]]
    if follow_up_due:
        today = datetime.now(timezone.utc).date().isoformat()
        leads = [l for l in leads if l["next_action_due"] and l["next_action_due"] <= today]
    if search:
        s = search.strip().lower()
        leads = [
            l for l in leads
            if s in l["lead_number"].lower()
            or s in (l["first_name"] or "").lower()
            or s in (l["last_name"] or "").lower()
            or s in (l["email"] or "").lower()
            or s in (l["company"] or "").lower()
        ]

    leads.sort(key=lambda l: l["last_activity_at"] or "", reverse=True)
    for l in leads:
        l.pop("_sent_at", None)
        l.pop("_replied_at", None)
        l.pop("_quote_requested_at", None)
    return leads


def get_lead_timeline(lead_number: str) -> dict | None:
    prospect_id = parse_lead_number(lead_number)
    if prospect_id is None:
        return None

    with get_conn() as conn:
        prospect = conn.execute(
            """SELECT pr.*, ib.filename AS batch_filename, ib.imported_at AS batch_imported_at
               FROM prospects_raw pr
               LEFT JOIN import_batches ib ON ib.batch_id = pr.batch_id
               WHERE pr.id = ?""",
            (prospect_id,),
        ).fetchone()
        if not prospect:
            return None
        if prospect["status"] in NON_LEAD_STATUSES:
            # Never became a visible lead in the first place -- see
            # list_leads()'s matching exclusion for the full rationale.
            return None

        memberships = conn.execute(
            """SELECT cp.*, c.name AS campaign_name
               FROM campaign_prospects cp
               JOIN campaigns c ON c.id = cp.campaign_id
               WHERE cp.prospect_id = ?
               ORDER BY cp.added_at""",
            (prospect_id,),
        ).fetchall()
        memberships = [dict(m) for m in memberships]

        cp_id_list = [m["id"] for m in memberships]
        reply_drafts_by_cp: dict[int, list[dict]] = {}
        if cp_id_list:
            ph = ",".join("?" * len(cp_id_list))
            rd_rows = conn.execute(
                f"SELECT * FROM reply_drafts WHERE campaign_prospect_id IN ({ph}) ORDER BY created_at",
                cp_id_list,
            ).fetchall()
            for rd in rd_rows:
                reply_drafts_by_cp.setdefault(rd["campaign_prospect_id"], []).append(dict(rd))
        for m in memberships:
            m["reply_drafts"] = reply_drafts_by_cp.get(m["id"], [])

        events = list(conn.execute(
            "SELECT * FROM audit_log WHERE entity_type = 'batch' AND entity_id = ? ORDER BY timestamp",
            (prospect["batch_id"],),
        ).fetchall())
        events += conn.execute(
            "SELECT * FROM audit_log WHERE entity_type = 'prospect' AND entity_id = ? ORDER BY timestamp",
            (str(prospect_id),),
        ).fetchall()
        cp_ids = [str(m["id"]) for m in memberships]
        if cp_ids:
            placeholders = ",".join("?" * len(cp_ids))
            events += conn.execute(
                f"SELECT * FROM audit_log WHERE entity_type = 'campaign_prospect' AND entity_id IN ({placeholders}) ORDER BY timestamp",
                cp_ids,
            ).fetchall()
        events = sorted([dict(e) for e in events], key=lambda e: e["timestamp"])

        notes = [dict(n) for n in conn.execute(
            "SELECT * FROM lead_notes WHERE prospect_id = ? ORDER BY created_at DESC",
            (prospect_id,),
        ).fetchall()]

    summary = _summarize(memberships)
    latest = memberships[-1] if memberships else None
    readiness = quote_readiness(latest)
    return {
        "lead_number": lead_number_for(prospect_id),
        "prospect": dict(prospect),
        "notes": notes,
        "memberships": memberships,
        "timeline_events": events,
        "lead_score": compute_lead_score(summary["overall_status"], prospect["lead_source"], prospect["linkedin_url"], readiness["pct"]),
        "quote_readiness": readiness,
        **summary,
    }


def add_note(prospect_id: int, note: str) -> dict:
    """Append one timestamped note to a lead's history -- distinct from
    next_action, which is the single current task (with its own due date),
    not a log. Notes are never edited or deleted in place; the record of
    what was said/tried stays intact, same reasoning as audit_log."""
    note = (note or "").strip()
    if not note:
        raise ValueError("Note text can't be empty")
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM prospects_raw WHERE id = ?", (prospect_id,)).fetchone():
            raise ValueError(f"Lead {lead_number_for(prospect_id)} not found")
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO lead_notes (prospect_id, note, created_at) VALUES (?, ?, ?)",
            (prospect_id, note, now),
        )
        new_note = conn.execute(
            "SELECT * FROM lead_notes WHERE prospect_id = ? ORDER BY id DESC LIMIT 1", (prospect_id,)
        ).fetchone()
    log_event("lead_note_added", "prospect", str(prospect_id), note[:200])
    return dict(new_note)


def list_notes(prospect_id: int) -> list[dict]:
    with get_conn() as conn:
        if not conn.execute("SELECT 1 FROM prospects_raw WHERE id = ?", (prospect_id,)).fetchone():
            raise ValueError(f"Lead {lead_number_for(prospect_id)} not found")
        rows = conn.execute(
            "SELECT * FROM lead_notes WHERE prospect_id = ? ORDER BY created_at DESC", (prospect_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def delete_lead(prospect_id: int) -> dict:
    """Permanently removes a lead and everything tied to it (every
    campaign membership, and any reply/quote-summary drafts on those
    memberships) -- no soft-delete, no undo, by explicit choice: this is
    for clearing test/junk data (e.g. before a production go-live), not
    an everyday action, so the confirmation dialog in front of it is the
    safety net rather than a recovery step after the fact.

    The freed id is never reused: prospects_raw's AUTOINCREMENT/IDENTITY
    primary key guarantees the next imported lead gets a new, higher id
    regardless of what's been deleted, so a deleted lead's number
    (L-000123) is retired for good -- the same guarantee that already
    protects a number referenced in a sent email from ever silently
    meaning a different lead later."""
    with get_conn() as conn:
        prospect = conn.execute("SELECT id FROM prospects_raw WHERE id = ?", (prospect_id,)).fetchone()
        if not prospect:
            raise ValueError(f"Lead {lead_number_for(prospect_id)} not found")

        cp_ids = [r["id"] for r in conn.execute(
            "SELECT id FROM campaign_prospects WHERE prospect_id = ?", (prospect_id,)
        ).fetchall()]
        if cp_ids:
            placeholders = ",".join("?" * len(cp_ids))
            conn.execute(f"DELETE FROM reply_drafts WHERE campaign_prospect_id IN ({placeholders})", cp_ids)
            conn.execute("DELETE FROM campaign_prospects WHERE prospect_id = ?", (prospect_id,))
        conn.execute("DELETE FROM lead_notes WHERE prospect_id = ?", (prospect_id,))
        conn.execute("DELETE FROM prospects_raw WHERE id = ?", (prospect_id,))

    lead_number = lead_number_for(prospect_id)
    log_event("lead_deleted", "prospect", str(prospect_id), f"Permanently deleted {lead_number}")
    return {"status": "deleted", "lead_number": lead_number}
