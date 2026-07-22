"""
OBJ-013 Reporting Dashboard
Real-time funnel visibility from prospect through won/lost outcome, plus
SDR-level activity metrics (volume, response rate, response speed) --
the same things you'd track for a human SDR's performance, applied to APEX.
"""
from datetime import datetime

from app.db import get_conn


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def get_summary() -> dict:
    with get_conn() as conn:
        total_batches = conn.execute("SELECT COUNT(*) c FROM import_batches").fetchone()["c"]

        status_rows = conn.execute(
            "SELECT status, COUNT(*) c FROM prospects_raw GROUP BY status"
        ).fetchall()
        prospect_status_counts = {r["status"]: r["c"] for r in status_rows}
        total_prospects = sum(prospect_status_counts.values())

        recent_batches = conn.execute(
            """SELECT batch_id, filename, row_count, imported_at
               FROM import_batches ORDER BY imported_at DESC LIMIT 10"""
        ).fetchall()

        campaigns = conn.execute(
            "SELECT id, name, status, created_at FROM campaigns ORDER BY created_at DESC"
        ).fetchall()

        campaign_summaries = []
        for c in campaigns:
            cp_rows = conn.execute(
                "SELECT status, COUNT(*) n FROM campaign_prospects WHERE campaign_id = ? GROUP BY status",
                (c["id"],),
            ).fetchall()
            cp_counts = {r["status"]: r["n"] for r in cp_rows}
            deal_value_row = conn.execute(
                "SELECT COALESCE(SUM(deal_value), 0) v FROM campaign_prospects WHERE campaign_id = ? AND status = 'Won'",
                (c["id"],),
            ).fetchone()
            campaign_summaries.append({
                "id": c["id"],
                "name": c["name"],
                "status": c["status"],
                "created_at": c["created_at"],
                "queued": cp_counts.get("Queued", 0),
                "approved": cp_counts.get("Approved", 0),
                "rejected": cp_counts.get("Rejected", 0),
                "sent": cp_counts.get("Sent", 0),
                "replied": cp_counts.get("Replied", 0),
                "suppressed": cp_counts.get("Suppressed", 0),
                "quote_requested": cp_counts.get("QuoteRequested", 0),
                "won": cp_counts.get("Won", 0),
                "lost": cp_counts.get("Lost", 0),
                "turnover": deal_value_row["v"],
                "total": sum(cp_counts.values()),
            })

        total_customers = conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]

        # ---- Value captured (OBJ-011-lite outcomes) ----
        outcome_row = conn.execute(
            """SELECT
                 SUM(CASE WHEN status = 'Won' THEN 1 ELSE 0 END) won,
                 SUM(CASE WHEN status = 'Lost' THEN 1 ELSE 0 END) lost,
                 SUM(CASE WHEN status IN ('QuoteRequested','Won','Lost') THEN 1 ELSE 0 END) quotes,
                 COALESCE(SUM(CASE WHEN status = 'Won' THEN deal_value ELSE 0 END), 0) turnover
               FROM campaign_prospects"""
        ).fetchone()
        won = outcome_row["won"] or 0
        lost = outcome_row["lost"] or 0
        quotes = outcome_row["quotes"] or 0
        turnover = outcome_row["turnover"] or 0
        win_rate = round((won / quotes) * 100, 1) if quotes else 0.0

        # ---- SDR-level activity metrics ----
        activity_row = conn.execute(
            """SELECT
                 SUM(CASE WHEN sent_at IS NOT NULL THEN 1 ELSE 0 END) total_sent,
                 SUM(CASE WHEN replied_at IS NOT NULL THEN 1 ELSE 0 END) total_replied
               FROM campaign_prospects"""
        ).fetchone()
        total_sent = activity_row["total_sent"] or 0
        total_replied = activity_row["total_replied"] or 0
        response_rate = round((total_replied / total_sent) * 100, 1) if total_sent else 0.0

        response_pairs = conn.execute(
            "SELECT sent_at, replied_at FROM campaign_prospects WHERE sent_at IS NOT NULL AND replied_at IS NOT NULL"
        ).fetchall()
        response_hours = []
        for r in response_pairs:
            sent = _parse(r["sent_at"])
            replied = _parse(r["replied_at"])
            if sent and replied and replied >= sent:
                response_hours.append((replied - sent).total_seconds() / 3600)
        avg_response_time_hours = round(sum(response_hours) / len(response_hours), 1) if response_hours else None

        sends_by_day_rows = conn.execute(
            "SELECT date(sent_at) d, COUNT(*) c FROM campaign_prospects WHERE sent_at IS NOT NULL GROUP BY d ORDER BY d"
        ).fetchall()
        sends_by_day = [{"date": r["d"], "count": r["c"]} for r in sends_by_day_rows]

        # ---- Activity over time (Dashboard chart) -- one row per day that
        # had ANY sent/replied/won event, counts per kind. Sparse by design;
        # the frontend fills gaps and re-buckets into week/month client-side.
        activity_rows = conn.execute(
            """SELECT d,
                      SUM(CASE WHEN kind = 'sent' THEN 1 ELSE 0 END) sent,
                      SUM(CASE WHEN kind = 'replied' THEN 1 ELSE 0 END) replied,
                      SUM(CASE WHEN kind = 'won' THEN 1 ELSE 0 END) won
               FROM (
                   SELECT date(sent_at) d, 'sent' kind FROM campaign_prospects WHERE sent_at IS NOT NULL
                   UNION ALL
                   SELECT date(replied_at) d, 'replied' kind FROM campaign_prospects WHERE replied_at IS NOT NULL
                   UNION ALL
                   SELECT date(won_at) d, 'won' kind FROM campaign_prospects WHERE won_at IS NOT NULL
               )
               GROUP BY d ORDER BY d"""
        ).fetchall()
        activity_by_day = [{"date": r["d"], "sent": r["sent"], "replied": r["replied"], "won": r["won"]} for r in activity_rows]

    return {
        "total_import_batches": total_batches,
        "total_prospects": total_prospects,
        "prospect_status_counts": {
            "Valid": prospect_status_counts.get("Valid", 0),
            "Invalid": prospect_status_counts.get("Invalid", 0),
            "Duplicate": prospect_status_counts.get("Duplicate", 0),
            "Existing Customer": prospect_status_counts.get("Existing Customer", 0),
            "Already Contacted": prospect_status_counts.get("Already Contacted", 0),
            "Pending": prospect_status_counts.get("Pending", 0),
        },
        "recent_batches": [dict(r) for r in recent_batches],
        "campaigns": campaign_summaries,
        "total_customers_on_file": total_customers,
        "value_captured": {
            "customers_won": won,
            "deals_lost": lost,
            "quotes_requested": quotes,
            "total_turnover": turnover,
            "win_rate_pct": win_rate,
        },
        "sdr_performance": {
            "total_emails_sent": total_sent,
            "total_replies_received": total_replied,
            "response_rate_pct": response_rate,
            "avg_response_time_hours": avg_response_time_hours,
            "sends_by_day": sends_by_day,
        },
        "activity_by_day": activity_by_day,
    }
