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
