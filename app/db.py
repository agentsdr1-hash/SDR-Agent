"""
OBJ-001 / OBJ-002 shared storage layer.

SQLite by default (APEX_DB_PATH); set DATABASE_URL to a Postgres URI
(e.g. a Supabase connection string) to use that instead -- no other code
in this app changes. Every service/router file writes plain `?`-style
parameterized SQL; PGConnWrapper below translates that to Postgres's
`%s` style and makes rows dict-like the same way sqlite3.Row already is,
so app/services/* and app/routers/* never need to know which database
they're talking to. Local dev and the test suite stay on SQLite (fast,
no network, no external dependency) -- DATABASE_URL is meant for
production.

Everything that genuinely differs between the two engines is isolated to
this file:
  - the PK phrase in SCHEMA (AUTOINCREMENT vs GENERATED ALWAYS AS IDENTITY)
  - _ensure_column()'s column-introspection query (PRAGMA vs information_schema)
  - the customers seed's INSERT OR IGNORE (Postgres: ON CONFLICT DO NOTHING)
A handful of call sites elsewhere also need RETURNING id (in place of
.lastrowid, which Postgres cursors don't have) or a dialect-aware date
comparison -- each is commented at its call site with IS_POSTGRES.
"""
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL")
IS_POSTGRES = bool(DATABASE_URL)

if IS_POSTGRES:
    import psycopg2
    import psycopg2.extras
    import psycopg2.pool

    # A fresh psycopg2.connect() is a real TCP+TLS+auth round trip to a
    # remote host (Supabase, through its connection pooler) -- cheap for
    # SQLite's in-process file access, expensive here, and this app calls
    # get_conn() many times per single page load (one per service-layer
    # query). Without pooling, every one of those calls pays a fresh
    # network handshake, which is what turns "click a tab" into several
    # seconds: a handful of endpoints firing in parallel each open their
    # own new connection. A small pool of already-open connections,
    # created once at startup and reused across requests, removes that
    # cost from every request but the first few. Thread-safe (route
    # handlers here are plain `def`, which FastAPI runs in a threadpool,
    # so concurrent requests can call get_conn() from different threads
    # at once) and sized conservatively since Supabase's free-tier pooler
    # itself caps total concurrent connections project-wide.
    _pg_pool = psycopg2.pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=DATABASE_URL)
else:
    DB_PATH = Path(os.environ.get("APEX_DB_PATH", Path(__file__).parent / "apex_pilot.db"))
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_PK = "INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY" if IS_POSTGRES else "INTEGER PRIMARY KEY AUTOINCREMENT"

_RAW_SCHEMA = """
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
    lead_source TEXT,                -- Website/Trade Show/Referral
    linkedin_url TEXT,
    next_action TEXT,                -- free text -- the current single active task
    next_action_due TEXT,            -- ISO date (YYYY-MM-DD) that task is due -- drives the follow-ups-due queue
    qualification_status TEXT,       -- New/Contacted/Qualified/Unqualified/Nurturing
    FOREIGN KEY (batch_id) REFERENCES import_batches(batch_id)
);

CREATE TABLE IF NOT EXISTS lead_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    prospect_id INTEGER NOT NULL,
    note TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (prospect_id) REFERENCES prospects_raw(id)
);
CREATE INDEX IF NOT EXISTS idx_lead_notes_prospect ON lead_notes(prospect_id);

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
    quote_notes TEXT,                -- Quote Readiness Checklist: special instructions
    sku_spec TEXT,                   -- Quote Readiness Checklist: SKU or specification
    unit_of_measure TEXT,            -- Quote Readiness Checklist: unit of measure
    destination TEXT,                -- Quote Readiness Checklist: destination
    shipping_terms TEXT,             -- Quote Readiness Checklist: shipping terms (Incoterms)
    delivery_date TEXT,              -- Quote Readiness Checklist: delivery date
    currency TEXT,                   -- Quote Readiness Checklist: currency
    payment_terms TEXT,              -- Quote Readiness Checklist: payment terms
    packaging_requirements TEXT,     -- Quote Readiness Checklist: packaging requirements
    quote_number TEXT,               -- real ERP-issued quote number, entered by a human once sales creates it
    last_followup_at TEXT,           -- most recent automated follow-up send -- denormalized for "last activity" sorting; full history is in campaign_followups
    FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
    FOREIGN KEY (prospect_id) REFERENCES prospects_raw(id),
    UNIQUE (campaign_id, prospect_id)
);

CREATE TABLE IF NOT EXISTS campaign_followups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_prospect_id INTEGER NOT NULL,
    follow_up_number INTEGER NOT NULL,  -- 1 or 2, see app/services/followups.py
    subject TEXT NOT NULL,
    body TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    FOREIGN KEY (campaign_prospect_id) REFERENCES campaign_prospects(id)
);
CREATE INDEX IF NOT EXISTS idx_campaign_followups_cp ON campaign_followups(campaign_prospect_id);

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

# TEXT and REAL are standard SQL types both engines already accept as-is,
# so the PK phrase is the only thing that actually needs swapping here.
SCHEMA = _RAW_SCHEMA.replace("INTEGER PRIMARY KEY AUTOINCREMENT", _PK) if IS_POSTGRES else _RAW_SCHEMA

SEED_KB_ENTRIES = [
    ("What certifications do you have?",
     "We're ISO 9001:2015 certified, ensuring consistent quality standards across our full product range.",
     "certification,quality,iso,standards"),
    ("What is your stock and supply capacity?",
     "We maintain an extensive in-stock inventory across our structural steel range, backed by strong manufacturer partnerships for reliable, fast-turnaround supply.",
     "capacity,stock,supply,inventory"),
    ("Do you supply steel gratings?",
     "Yes -- steel gratings are one of our best-selling, most-requested products and we keep them well stocked across standard panel sizes, bar spacings and load ratings for fast turnaround. Share the size, load class and quantity you need and we'll put together a quote.",
     "steel grating,grating,gratings,walkway,flooring,platform,mesh,bar grating"),
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

class PGConnWrapper:
    """Makes a psycopg2 connection look enough like the sqlite3.Connection
    every service/router file already codes against: `?` placeholders
    (translated to `%s` here), dict-like rows (via RealDictCursor, same
    as sqlite3.Row -- both support row["col"] and dict(row)), and
    .executemany()/.executescript(). Only used when DATABASE_URL is set;
    the SQLite path below never touches this class."""

    _PLACEHOLDER_RE = re.compile(r"\?")

    def __init__(self, raw_conn):
        self._conn = raw_conn

    def _translate(self, sql: str) -> str:
        return self._PLACEHOLDER_RE.sub("%s", sql)

    def execute(self, sql, params=()):
        cur = self._conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(self._translate(sql), params)
        return cur

    def executemany(self, sql, seq_of_params):
        cur = self._conn.cursor()
        cur.executemany(self._translate(sql), seq_of_params)
        return cur

    def executescript(self, sql):
        # Postgres's simple-query protocol runs multiple ;-separated
        # statements from one execute() -- no per-statement looping needed,
        # confirmed against a real local Postgres 16 before relying on it.
        cur = self._conn.cursor()
        cur.execute(sql)
        return cur

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


@contextmanager
def get_conn():
    if IS_POSTGRES:
        raw = _pg_pool.getconn()
        conn = PGConnWrapper(raw)
        try:
            yield conn
            conn.commit()
        except Exception:
            # A failed query leaves the connection's transaction aborted;
            # roll it back before it goes back in the pool, or the next
            # caller to borrow this same physical connection would
            # immediately fail too ("current transaction is aborted").
            raw.rollback()
            raise
        finally:
            _pg_pool.putconn(raw)
    else:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def close_pool():
    """Release every pooled connection -- called on app shutdown. A no-op
    on SQLite, which never pools (each get_conn() call is its own
    connection already closed by its own finally block)."""
    if IS_POSTGRES:
        _pg_pool.closeall()


def _ensure_column(conn, table: str, column: str, coltype: str):
    """Additive, idempotent migration for a column added after a table
    already existed in production -- CREATE TABLE IF NOT EXISTS (above)
    only helps on a fresh DB; an already-deployed one needs the column
    added to it directly. Safe to call every startup."""
    if IS_POSTGRES:
        cols = {row["column_name"] for row in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' AND table_name = ?",
            (table,),
        )}
    else:
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
        _ensure_column(conn, "campaign_prospects", "sku_spec", "TEXT")
        _ensure_column(conn, "campaign_prospects", "unit_of_measure", "TEXT")
        _ensure_column(conn, "campaign_prospects", "destination", "TEXT")
        _ensure_column(conn, "campaign_prospects", "shipping_terms", "TEXT")
        _ensure_column(conn, "campaign_prospects", "delivery_date", "TEXT")
        _ensure_column(conn, "campaign_prospects", "currency", "TEXT")
        _ensure_column(conn, "campaign_prospects", "payment_terms", "TEXT")
        _ensure_column(conn, "campaign_prospects", "packaging_requirements", "TEXT")
        _ensure_column(conn, "campaign_prospects", "quote_number", "TEXT")
        _ensure_column(conn, "campaign_prospects", "last_followup_at", "TEXT")
        _ensure_column(conn, "prospects_raw", "lead_source", "TEXT")
        _ensure_column(conn, "prospects_raw", "linkedin_url", "TEXT")
        _ensure_column(conn, "prospects_raw", "next_action", "TEXT")
        _ensure_column(conn, "prospects_raw", "next_action_due", "TEXT")
        _ensure_column(conn, "prospects_raw", "qualification_status", "TEXT")
        if seed_customers:
            # Gated on a persistent flag (app_settings), not just "table is
            # empty" -- a reset-all-data clears the customers table too, and
            # without this flag the two demo rows would silently reappear
            # the next time init_db() runs (e.g. on the next deploy/restart),
            # which defeats the point of a reset done to keep test data out
            # of a production go-live.
            already_seeded = conn.execute(
                "SELECT 1 FROM app_settings WHERE key = 'demo_customers_seeded'"
            ).fetchone()
            if not already_seeded:
                insert_ignore_sql = (
                    "INSERT INTO customers (email, company) VALUES (?, ?) ON CONFLICT (email) DO NOTHING"
                    if IS_POSTGRES else
                    "INSERT OR IGNORE INTO customers (email, company) VALUES (?, ?)"
                )
                conn.executemany(
                    insert_ignore_sql,
                    [
                        ("jsmith@acmecorp.com", "Acme Corp"),
                        ("dlee@globex.com", "Globex Inc"),
                    ],
                )
                conn.execute(
                    "INSERT INTO app_settings (key, value, updated_at) VALUES ('demo_customers_seeded', 'true', ?)",
                    (datetime.now(timezone.utc).isoformat(),),
                )
            existing_kb = conn.execute("SELECT COUNT(*) c FROM kb_entries").fetchone()["c"]
            if existing_kb == 0:
                now = datetime.now(timezone.utc).isoformat()
                conn.executemany(
                    "INSERT INTO kb_entries (question, answer, tags, created_at) VALUES (?, ?, ?, ?)",
                    [(q, a, t, now) for q, a, t in SEED_KB_ENTRIES],
                )
