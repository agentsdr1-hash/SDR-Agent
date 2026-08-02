"""
Knowledge Base Q&A + smart reply drafting.

Two composers, tried in order:
  1. compose_smart_reply_ai() -- Claude (Anthropic), grounded in this app's
     own KB entries + stock catalog, when a client-supplied API key is
     configured (app/integrations/ai_provider.py, set from the Admin tab --
     bring-your-own-subscription, same pattern as Gmail). Produces a more
     natural, human-sounding draft than keyword matching can.
  2. compose_smart_reply() -- rule-based (keyword/tag overlap against
     stored KB entries and the real stock catalog), used whenever Claude
     isn't configured or a request to it fails for any reason. This is the
     original always-available path -- the app works out of the box with
     no AI key, same as it always has.
Every draft either one produces requires human approval before it's sent
(app/services/approval_and_delivery.py handles the actual Gmail send),
same as outbound drafts -- nothing here auto-sends. If neither path finds
anything to say, the fallback is a short holding reply flagged low
confidence, not a confident-sounding guess.
"""
import re
from datetime import datetime, timezone

from app.db import get_conn
from app.integrations import ai_provider
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
        # RETURNING id -- portable across SQLite 3.35+ and Postgres, unlike
        # cursor.lastrowid which Postgres cursors don't have.
        cur = conn.execute(
            "INSERT INTO kb_entries (question, answer, tags, created_at) VALUES (?, ?, ?, ?) RETURNING id",
            (question, answer, tags or "", now),
        )
        new_id = cur.fetchone()["id"]
        row = conn.execute("SELECT id, question, answer, tags FROM kb_entries WHERE id = ?", (new_id,)).fetchone()
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
# Deal-progression signal -- has the customer already given what we'd
# otherwise ask for? Rule-based, not NLP: a handful of patterns for the
# three things a quote actually needs (grade, size, quantity). Without
# this, the closing question was static and would ask "share your
# grade" verbatim even when the reply plainly said "Grade A" -- annoying,
# and the opposite of moving a deal forward.
# ══════════════════════════════════════════════════════
_GRADE_RE = re.compile(r"\bgrade\s*[:\-]?\s*[a-z0-9]+\b", re.I)
_QTY_RE = re.compile(r"\b\d+(\.\d+)?\s*(tons?|kgs?|pcs?|pieces?|units?|sqm|m2|m²|meters?|metres?|nos?|pallets?)\b", re.I)
_SIZE_RE = re.compile(r"\d+(\.\d+)?\s*(x|×)\s*\d+(\.\d+)?|\d+(\.\d+)?\s*mm\b", re.I)


def _specs_given(text: str) -> dict:
    text = text or ""
    return {
        "grade": bool(_GRADE_RE.search(text)),
        "size": bool(_SIZE_RE.search(text)),
        "quantity": bool(_QTY_RE.search(text)),
    }


# ══════════════════════════════════════════════════════
# Smart reply drafting
# ══════════════════════════════════════════════════════
def reply_subject_for(original_subject: str | None) -> str:
    """Keeps the customer's own email thread intact -- reuses (a
    normalized) 'Re: <subject>' instead of inventing an unrelated subject
    on every response, which breaks threading in their inbox and reads as
    a non-sequitur ("why did the subject change again?"). Never
    double-prefixes a subject that's already 'Re: ...'."""
    subject = (original_subject or "").strip()
    if not subject:
        return "Re: your inquiry"
    if re.match(r"^re:\s*", subject, re.IGNORECASE):
        return subject
    return f"Re: {subject}"


def compose_smart_reply(first_name: str | None, company: str | None, reply_text: str,
                         reply_subject: str | None = None) -> dict:
    """Rule-based reply composer -- matches reply_text against KB entries
    and stock-catalog product families, then assembles a short human-ish
    reply. Returns confidence + a plain-English note of what matched so a
    reviewer can sanity-check it before approving.

    reply_subject is the subject of the message being replied to (real
    inbound reply, or simulated) -- see reply_subject_for() for why the
    response subject is derived from it rather than a fixed string.

    The closing line adapts to how much the customer already gave us
    (grade/size/quantity, detected via _specs_given): full specs -> push
    toward closing the deal, partial -> ask only for what's still
    missing, none -> the original generic ask. This is the fix for the
    reply always re-asking for a grade the customer had already given."""
    first = first_name or "there"
    co = company or "your team"
    kb_matches = _match_kb(reply_text)
    family_matches = _match_stock_families(reply_text)
    specs = _specs_given(reply_text)
    missing = [label for key, label in (("size", "size"), ("grade", "grade"), ("quantity", "quantity")) if not specs[key]]

    lines = [f"Hi {first},", "", "Thanks for getting back to us."]
    if kb_matches:
        for m in kb_matches:
            lines.append("")
            lines.append(m["answer"])
    if family_matches:
        lines.append("")
        if missing:
            lines.append(
                f"On {', '.join(family_matches)} specifically -- we carry these in various sizes, grades and "
                f"thicknesses and can put together a quote once we know your exact specs and quantity."
            )
        else:
            lines.append(f"On {', '.join(family_matches)} specifically -- that's well within what we stock.")
    if not kb_matches and not family_matches:
        lines.append("")
        lines.append("Let me pull together the right details for your question and follow up shortly with specifics.")

    lines.append("")
    if not missing:
        lines.append(
            f"That's everything we need -- I'll get a formal quote drafted for {co} and back to you shortly. "
            f"Would you like to lock in a delivery window while we finalize it?"
        )
    elif len(missing) < 3:
        lines.append(f"Just need the {' and '.join(missing)} to finalize -- share that and we'll get a formal quote over to {co} right away.")
    else:
        lines.append(f"Could you share the sizes, grades and quantities you're looking at so we can put together a quote for {co}?")
    lines.append("")
    lines.append(f"Best,\n{COMPANY_NAME} Sales Team")

    confidence = "matched" if (kb_matches or family_matches) else "fallback"
    summary_parts = [m["question"] for m in kb_matches] + family_matches
    if any(specs.values()):
        summary_parts.append("specs given: " + ", ".join(k for k, v in specs.items() if v))
    matched_summary = "; ".join(summary_parts) if summary_parts else "No strong match -- generic holding reply, review before sending"

    return {
        "subject": reply_subject_for(reply_subject),
        "body": "\n".join(lines),
        "confidence": confidence,
        "matched_summary": matched_summary,
    }


_AI_SYSTEM_PROMPT_TEMPLATE = """You are a sales rep at {company}, a structural steel distributor. Draft a short, warm, human-sounding email reply to a prospect who just replied to our outreach.

Ground every factual claim ONLY in the knowledge base and stock catalog below -- never invent products, pricing, or policies that aren't there. If the reply doesn't match anything in the knowledge base, keep the reply short and ask a clarifying question instead of guessing.

End by moving the conversation toward a quote: ask for whatever grade/size/quantity specs are still missing, or say a formal quote is on its way shortly if everything needed has already been given.

Knowledge base Q&A:
{kb_context}

Stock catalog families we carry:
{stock_context}

Reply with ONLY the email body text -- no subject line, no preamble, no markdown formatting -- just the plain-text email, starting with a greeting and ending with a sign-off as "Best,\n{company} Sales Team"."""


def compose_smart_reply_ai(first_name: str | None, company: str | None, reply_text: str,
                            reply_subject: str | None = None) -> dict | None:
    """Attempts an AI-drafted reply via Claude, grounded in the same KB
    entries + stock catalog the rule-based composer searches. Returns None
    (never raises) whenever Claude isn't configured or a request to it
    fails for any reason -- this function's only job is to produce a
    better draft when it can, not to surface AI-provider errors to
    whoever's reviewing a reply; create_reply_draft() falls back to the
    always-available rule-based composer on None."""
    if not ai_provider.is_configured():
        return None
    kb_entries = list_kb_entries()
    kb_context = "\n".join(f"Q: {e['question']}\nA: {e['answer']}" for e in kb_entries) or "(no Q&A entries yet)"
    stock_families = stock_catalog.top_families(30)
    stock_context = ", ".join(stock_families) or "(no stock catalog loaded)"
    system_prompt = _AI_SYSTEM_PROMPT_TEMPLATE.format(company=COMPANY_NAME, kb_context=kb_context, stock_context=stock_context)
    user_prompt = f"Prospect: {first_name or 'there'} at {company or 'their company'}\n\nTheir message:\n{reply_text}"
    try:
        body = ai_provider.draft_reply(system_prompt, user_prompt)
    except ai_provider.AIRequestError:
        return None
    if not body:
        return None
    entry_word = "entry" if len(kb_entries) == 1 else "entries"
    return {
        "subject": reply_subject_for(reply_subject),
        "body": body,
        "confidence": "ai",
        "matched_summary": f"Drafted by Claude ({ai_provider.configured_model()}), grounded in {len(kb_entries)} KB {entry_word} + stock catalog",
    }


def create_reply_draft(campaign_prospect_id: int, first_name: str | None, company: str | None,
                        reply_subject: str | None, reply_text: str) -> int:
    """Generate and store a smart-reply draft for a real or simulated inbound
    reply. Returns the new reply_drafts.id. Tries the AI composer first
    (compose_smart_reply_ai), falling back to the rule-based one whenever
    Claude isn't configured or its request fails -- see their respective
    docstrings.

    source_reply_snippet stores the customer's actual message essentially in
    full (capped at 8000 chars, matching email_provider.check_for_replies'
    cap on the inbound side) -- this is the record of what they actually
    said, not just enough text to eyeball a one-line preview. The Lead
    Detail view renders it in full alongside our reply, not as a truncated
    quoted line."""
    draft = compose_smart_reply_ai(first_name, company, reply_text, reply_subject) \
        or compose_smart_reply(first_name, company, reply_text, reply_subject)
    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO reply_drafts
               (campaign_prospect_id, subject, body, status, confidence, matched_summary,
                source_reply_subject, source_reply_snippet, created_at)
               VALUES (?, ?, ?, 'Draft', ?, ?, ?, ?, ?) RETURNING id""",
            (campaign_prospect_id, draft["subject"], draft["body"], draft["confidence"],
             draft["matched_summary"], reply_subject, (reply_text or "")[:8000], now),
        )
        draft_id = cur.fetchone()["id"]
    log_event("reply_draft_created", "reply_draft", str(draft_id),
               f"confidence={draft['confidence']} matched={draft['matched_summary']}")
    return draft_id
