"""
OBJ-001 / OBJ-002 shared storage layer.
SQLite for the pilot; swap DATABASE_URL for Postgres in production without
changing service code (raw SQL kept portable).
"""
import os
import sqlite3
from contextlib import contextmanager
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
"""

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

def init_db(seed_customers: bool = True):
    with get_conn() as conn:
        conn.executescript(SCHEMA)
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
