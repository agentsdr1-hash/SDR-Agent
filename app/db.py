"""
OBJ-001 / OBJ-002 shared storage layer.
SQLite for the pilot; swap DATABASE_URL for Postgres in production without
changing service code (raw SQL kept portable).
"""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(os.environ.get("APEX_DB_PATH", Path(__file__).parent / "apex_pilot.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS import_batches (
    batch_id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    imported_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prospects_raw (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    row_number INTEGER NOT NULL,
    first_name TEXT,
    last_name TEXT,
    email TEXT,
    company TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'Pending',       -- Pending/Valid/Invalid/Duplicate/Existing Customer
    validation_notes TEXT,
    FOREIGN KEY (batch_id) REFERENCES import_batches(batch_id)
);

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT UNIQUE NOT NULL,
    company TEXT
);

CREATE TABLE IF NOT EXISTS campaigns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft',       -- Draft/Active/Paused/Completed
    send_days TEXT NOT NULL DEFAULT 'Mon,Tue,Wed,Thu,Fri',
    daily_send_limit INTEGER NOT NULL DEFAULT 25,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS campaign_prospects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    prospect_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'Queued',      -- Queued/Approved/Rejected/Sent/Replied/Suppressed/QuoteRequested/Won/Lost
    subject TEXT,
    body TEXT,
    added_at TEXT NOT NULL,
    approved_at TEXT,
    sent_at TEXT,
    replied_at TEXT,
    reply_subject TEXT,
    quote_requested_at TEXT,
    won_at TEXT,
    lost_at TEXT,
    deal_value REAL,
    lost_reason TEXT,
    materials TEXT,                  -- high-level quote prep: what they need (e.g. "Flat bars, ERW pipes")
    quantity TEXT,                   -- free text -- "50 tons", "200 pcs", etc., not always a bare number
    target_price REAL,               -- budget/target price the prospect mentioned, if any
    quote_notes TEXT,                -- specs, grade, delivery timeline/location, anything else for the quote
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (prospect_id) REFERENCES prospects_raw(id),
    UNIQUE (campaign_id, prospect_id)
);

CREATE TABLE IF NOT EXISTS suppressed_emails (
    email TEXT PRIMARY KEY,
    reason TEXT,
    source TEXT NOT NULL DEFAULT 'manual',      -- manual/auto-detected
    added_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,        -- e.g. prospect_import, campaign_created, draft_approved, email_sent, reply_received, opt_out_detected, deal_won
    entity_type TEXT,                -- e.g. batch, campaign, campaign_prospect, suppression
    entity_id TEXT,
    details TEXT,
    actor TEXT NOT NULL DEFAULT 'system'   -- 'system' for automated actions, or a person's identifier once auth exists
);

CREATE TABLE IF NOT EXISTS stock_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT NOT NULL,
    product_name TEXT NOT NULL,
    category TEXT,                   -- nearest ancestor category label from the source file's hierarchy
    imported_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stock_catalog_category ON stock_catalog(category);

CREATE TABLE IF NOT EXISTS kb_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    tags TEXT,                       -- comma-separated, used for matching
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reply_drafts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_prospect_id INTEGER NOT NULL,
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Draft',   -- Draft/Approved/Rejected/Sent
    confidence TEXT,                        -- 'matched' or 'fallback' -- did the KB actually have something relevant
    matched_summary TEXT,                   -- human-readable note of what matched, for the reviewer
    source_reply_subject TEXT,
    source_reply_snippet TEXT,
    created_at TEXT NOT NULL,
    approved_at TEXT,
    rejected_at TEXT,
    sent_at TEXT,
    FOREIGN KEY (campaign_prospect_id) REFERENCES campaign_prospects(id)
);
"""

SEED_KB_ENTRIES = [
    ("What certifications do you have?",
     "We're ISO 9001:2015 certified, ensuring consistent quality standards across our full product range.",
     "certification,quality,iso,standards"),
    ("What is your stock and supply capacity?",
     "We maintain an extensive in-stock inventory across our structural steel range, backed by strong manufacturer partnerships for reliable, fast-turnaround supply.",
     "capacity,stock,supply,inventory"),
    ("Do you offer technical consultation on specifications?",
     "Yes -- our team is available for technical consultation on product specifications and standards, to help you get the right size, grade and thickness for your application.",
     "technical,specs,specifications,standards,consultation,grade,thickness"),
    ("Are you open to supply partnerships or bulk cooperation?",
     "Absolutely -- we're open to discussing business cooperation, supply agreements, and partnership opportunities. Share your requirements and we'll follow up with options.",
     "partnership,cooperation,bulk,supply,business"),
    ("What is your typical lead time?",
     "Lead time depends on the product and quantity, but our extensive stock position means most orders ship quickly -- happy to confirm exact timing once we know what you need.",
     "lead time,delivery,shipping,turnaround"),
    ("Do you provide pricing / quotes?",
     "Yes -- share the sizes, grades and quantities you need and we'll put together a formal quote.",
     "pricing,price,quote,cost,budget"),
]

@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

def _ensure_column(conn, table: str, column: str, coltype: str):
    """Additive, idempotent migration for a column added after a table
    already existed in production -- CREATE TABLE IF NOT EXISTS (above)
    only helps on a fresh DB; an already-deployed one needs the column
    added to it directly. Safe to call every startup."""
    cols = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db(seed_customers: bool = True):
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_column(conn, "campaign_prospects", "materials", "TEXT")
        _ensure_column(conn, "campaign_prospects", "quantity", "TEXT")
        _ensure_column(conn, "campaign_prospects", "target_price", "REAL")
        _ensure_column(conn, "campaign_prospects", "quote_notes", "TEXT")
        if seed_customers:
            existing = conn.execute("SELECT COUNT(*) c FROM customers").fetchone()["c"]
            if existing == 0:
                conn.executemany(
                    "INSERT OR IGNORE INTO customers (email, company) VALUES (?, ?)",
                    [
                        ("jsmith@acmecorp.com", "Acme Corp"),
                        ("dlee@globex.com", "Globex Inc"),
                    ],
                )
            existing_kb = conn.execute("SELECT COUNT(*) c FROM kb_entries").fetchone()["c"]
            if existing_kb == 0:
                now = datetime.now(timezone.utc).isoformat()
                conn.executemany(
                    "INSERT INTO kb_entries (question, answer, tags, created_at) VALUES (?, ?, ?, ?)",
                    [(q, a, t, now) for q, a, t in SEED_KB_ENTRIES],
                )
