"""
Knowledge Base Q&A + smart reply drafting.

Rule-based (keyword/tag overlap against stored KB entries and the real
stock catalog), not an LLM -- there's no model API key wired into this
app. Every draft this produces requires human approval before it's sent
(app/services/approval_and_delivery.py handles the actual Gmail send),
same as outbound drafts -- nothing here auto-sends. If a reply doesn't
match anything, the fallback is a short holding reply flagged low
confidence, not a confident-sounding guess.
"""
import re
from datetime import datetime, timezone

from app.db import get_conn
from app.services import stock_catalog
from app.services.audit import log_event
from app.services.campaign_management import COMPANY_NAME

STOPWORDS = {
    "the", "a", "an", "is", "are", "do", "you", "your", "we", "our", "for", "of", "to",
    "and", "or", "in", "on", "with", "have", "has", "can", "please", "hi", "hello",
    "thanks", "thank", "would", "could", "about", "any", "some", "if", "it", "us",
}


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z']+", (text or "").lower())
    return {w for w in words if w not in STOPWORDS and len(w) > 2}


# ══════════════════════════════════════════════════════
# KB entries -- persisted Q&A
# ══════════════════════════════════════════════════════
def import_kb_entries(entries: list[dict], replace: bool = False) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        if replace:
            conn.execute("DELETE FROM kb_entries")
        conn.executemany(
            "INSERT INTO kb_entries (question, answer, tags, created_at) VALUES (?, ?, ?, ?)",
            [(e["question"], e["answer"], e.get("tags") or "", now) for e in entries],
        )
        count = conn.execute("SELECT COUNT(*) c FROM kb_entries").fetchone()["c"]
    log_event("kb_entries_imported", "kb_entries", None, f"Imported {len(entries)} entries (replace={replace})")
    return {"entry_count": count}


def list_kb_entries() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, question, answer, tags FROM kb_entries ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def add_kb_entry(question: str, answer: str, tags: str | None) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO kb_entries (question, answer, tags, created_at) VALUES (?, ?, ?, ?)",
            (question, answer, tags or "", now),
        )
        row = conn.execute("SELECT id, question, answer, tags FROM kb_entries WHERE id = ?", (cur.lastrowid,)).fetchone()
    log_event("kb_entry_added", "kb_entries", str(row["id"]), question)
    return dict(row)


def delete_kb_entry(entry_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM kb_entries WHERE id = ?", (entry_id,))
    log_event("kb_entry_deleted", "kb_entries", str(entry_id), None)


# ══════════════════════════════════════════════════════
# Matching
# ══════════════════════════════════════════════════════
def _match_kb(text: str, top_n: int = 2) -> list[dict]:
    tokens = _tokenize(text)
    if not tokens:
        return []
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM kb_entries").fetchall()
    scored = []
    for r in rows:
        entry_tokens = _tokenize(r["question"]) | _tokenize(r["tags"] or "")
        overlap = tokens & entry_tokens
        if overlap:
            scored.append((len(overlap), dict(r)))
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored[:top_n]]


def _match_stock_families(text: str, top_n: int = 3) -> list[str]:
    tokens = _tokenize(text)
    if not tokens:
        return []
    all_families = stock_catalog.top_families(50)
    matched = [f for f in all_families if _tokenize(f) & tokens]
    return matched[:top_n]


# ══════════════════════════════════════════════════════
# Smart reply drafting
# ══════════════════════════════════════════════════════
def compose_smart_reply(first_name: str | None, company: str | None, reply_text: str) -> dict:
    """Rule-based reply composer -- matches reply_text against KB entries
    and stock-catalog product families, then assembles a short human-ish
    reply. Returns confidence + a plain-English note of what matched so a
    reviewer can sanity-check it before approving."""
    first = first_name or "there"
    co = company or "your team"
    kb_matches = _match_kb(reply_text)
    family_matches = _match_stock_families(reply_text)

    lines = [f"Hi {first},", "", "Thanks for getting back to us."]
    if kb_matches:
        for m in kb_matches:
            lines.append("")
            lines.append(m["answer"])
    if family_matches:
        lines.append("")
        lines.append(
            f"On {', '.join(family_matches)} specifically -- we carry these in various sizes, grades and "
            f"thicknesses and can put together a quote once we know your exact specs and quantity."
        )
    if not kb_matches and not family_matches:
        lines.append("")
        lines.append("Let me pull together the right details for your question and follow up shortly with specifics.")

    lines.append("")
    lines.append(f"Could you share the sizes, grades and quantities you're looking at so we can put together a quote for {co}?")
    lines.append("")
    lines.append(f"Best,\n{COMPANY_NAME} Sales Team")

    confidence = "matched" if (kb_matches or family_matches) else "fallback"
    summary_parts = [m["question"] for m in kb_matches] + family_matches
    matched_summary = "; ".join(summary_parts) if summary_parts else "No strong match -- generic holding reply, review before sending"

    return {
        "subject": "Re: your question",
        "body": "\n".join(lines),
        "confidence": confidence,
        "matched_summary": matched_summary,
    }


def create_reply_draft(campaign_prospect_id: int, first_name: str | None, company: str | None,
                        reply_subject: str | None, reply_text: str) -> int:
    """Generate and store a smart-reply draft for a real or simulated inbound
    reply. Returns the new reply_drafts.id."""
    draft = compose_smart_reply(first_name, company, reply_text)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO reply_drafts
               (campaign_prospect_id, subject, body, status, confidence, matched_summary,
                source_reply_subject, source_reply_snippet, created_at)
               VALUES (?, ?, ?, 'Draft', ?, ?, ?, ?, ?)""",
            (campaign_prospect_id, draft["subject"], draft["body"], draft["confidence"],
             draft["matched_summary"], reply_subject, (reply_text or "")[:500], now),
        )
        draft_id = cur.lastrowid
    log_event("reply_draft_created", "reply_draft", str(draft_id),
               f"confidence={draft['confidence']} matched={draft['matched_summary']}")
    return draft_id
