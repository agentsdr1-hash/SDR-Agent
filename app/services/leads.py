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

from app.db import get_conn

LEAD_PREFIX = "L"


def lead_number_for(prospect_id: int) -> str:
    return f"{LEAD_PREFIX}-{prospect_id:06d}"


def parse_lead_number(lead_number: str) -> int | None:
    # hyphen optional on input so old-format numbers (L000123, from before
    # the L-000123 format) still resolve -- lead_number_for() above always
    # emits the hyphenated form going forward.
    m = re.match(rf"^{LEAD_PREFIX}-?0*(\d+)$", (lead_number or "").strip().upper())
    return int(m.group(1)) if m else None


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
               ever_replied: bool | None = None, ever_quoted: bool | None = None) -> list[dict]:
    """Every prospect across every campaign (and prospects not yet in any
    campaign), one row per lead, for the consolidated Leads tab -- as
    opposed to get_lead_timeline()'s single-lead full-detail view, or the
    Dashboard/Campaigns tab's per-campaign or per-import-batch tables.

    status/validation_status match the Dashboard's per-campaign status
    counts and prospect-funnel counts exactly (both are GROUP BY status
    snapshots). ever_sent/ever_replied/ever_quoted match the SDR-performance
    and value-captured stats, which count sent_at/replied_at/
    quote_requested_at IS NOT NULL -- a superset of "status is currently
    X", since those timestamps persist after the status moves on (e.g. a
    Won deal still has sent_at and replied_at set). All four filters (like
    the rest of this function) look only at each lead's latest campaign
    membership, consistent with the rest of the Leads tab."""
    with get_conn() as conn:
        prospects = [dict(r) for r in conn.execute(
            "SELECT id, first_name, last_name, email, company, phone, status AS validation_status "
            "FROM prospects_raw ORDER BY id DESC"
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
        leads.append({
            "lead_number": lead_number_for(p["id"]),
            "prospect_id": p["id"],
            "first_name": p["first_name"],
            "last_name": p["last_name"],
            "email": p["email"],
            "company": p["company"],
            "phone": p["phone"],
            "validation_status": p["validation_status"],
            "campaign_count": len(memberships),
            "campaign_id": latest["campaign_id"] if latest else None,
            "campaign_prospect_id": latest["id"] if latest else None,
            "campaign_name": latest["campaign_name"] if latest else None,
            "status": summary["overall_status"],
            "deal_value": latest["deal_value"] if latest else None,
            "lost_reason": latest["lost_reason"] if latest else None,
            "won": summary["won"],
            "lost": summary["lost"],
            "last_activity_at": _latest_timestamp(latest) if latest else None,
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

    return {
        "lead_number": lead_number_for(prospect_id),
        "prospect": dict(prospect),
        "memberships": memberships,
        "timeline_events": events,
        **_summarize(memberships),
    }
