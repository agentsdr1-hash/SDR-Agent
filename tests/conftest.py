"""
Shared fixtures for the regression suite.

Design choice: tests run against a *real* uvicorn subprocess talking to a
throwaway SQLite file, exercised over real HTTP -- the same pattern used
manually (curl/requests + a scratch APEX_DB_PATH) to verify every feature
built in this app so far. This is deliberate over FastAPI's in-process
TestClient: it catches the same class of bugs a real deployment would hit
(startup/shutdown, the actual init_db() migration path, real HTTP
semantics), and it's what's already been proven to work in this sandbox.

Scope: one server+DB per test *module* (not per function, not one shared
session). That's the balance point between speed (a fresh subprocess per
test would be slow) and isolation (one process for the whole suite would
let tests bleed into each other's state, e.g. daily-send-limit counts or
lead numbering). Within a module, test functions are allowed to build on
state left by earlier functions in the same file -- these are ordered
integration flows (Queued -> Approved -> Sent -> ... -> Won), not
independent unit tests, and pytest runs a file's tests in source order.
"""
import os
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

# Pure constant, no DB access -- safe to import directly in the shared
# pytest process (unlike app.db/app.services.* generally, see the
# TEST_DATABASE_URL comment below and tests/README.md's "isolated probe"
# section for why that distinction matters here).
from app.services.admin_auth import DEFAULT_ADMIN_PASSWORD

REPO_ROOT = Path(__file__).parent.parent
ADMIN_HEADERS = {"X-Admin-Password": DEFAULT_ADMIN_PASSWORD}

# Opt-in only: default `pytest` runs stay on SQLite (fast, hermetic, no
# setup). Set TEST_DATABASE_URL to an *admin*-capable Postgres connection
# (e.g. the local instance used to validate the Supabase migration, or
# Supabase's own connection string) to run this exact same suite against
# real Postgres instead -- one fresh, throwaway database per test module,
# created and dropped the same way a throwaway SQLite file is. This is
# how the dual-dialect db.py layer gets genuine parity-tested rather than
# just eyeballed.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class APIClient:
    """Thin requests wrapper bound to one server's base_url. Deliberately
    transparent (returns raw requests.Response) rather than magic -- tests
    check .status_code and call .json() themselves, same as reading a
    curl response, so failures are easy to read.

    dsn is either a SQLite file path (default) or a Postgres connection
    URL (when TEST_DATABASE_URL drives a parity run against real
    Postgres -- see _launch_server). The seed helpers below talk to
    whichever one this instance was built with, translating `?`->`%s`
    for Postgres the same way app/db.py's PGConnWrapper does, so test
    files never need to know or care which engine is under them."""

    def __init__(self, base_url: str, dsn: str, is_postgres: bool = False):
        self.base_url = base_url
        self.dsn = dsn
        self.db_path = dsn  # kept for readability at call sites that are SQLite-only in intent
        self.is_postgres = is_postgres
        self.session = requests.Session()

    def get(self, path: str, **kw) -> requests.Response:
        return self.session.get(self.base_url + path, **kw)

    def post(self, path: str, json=None, **kw) -> requests.Response:
        return self.session.post(self.base_url + path, json=json, **kw)

    def put(self, path: str, json=None, **kw) -> requests.Response:
        return self.session.put(self.base_url + path, json=json, **kw)

    def delete(self, path: str, **kw) -> requests.Response:
        return self.session.delete(self.base_url + path, **kw)

    def _raw_conn(self):
        if self.is_postgres:
            import psycopg2
            return psycopg2.connect(self.dsn)
        return sqlite3.connect(self.dsn)

    def _exec(self, sql: str, params=(), returning: bool = False):
        """returning=True reads back the RETURNING id row (or SQLite's
        lastrowid) before the connection closes -- the cursor itself
        doesn't survive past this method either way."""
        conn = self._raw_conn()
        try:
            sql = sql.replace("?", "%s") if self.is_postgres else sql
            cur = conn.cursor()
            cur.execute(sql, params)
            new_id = (cur.fetchone()[0] if self.is_postgres else cur.lastrowid) if returning else None
            conn.commit()
            return new_id
        finally:
            conn.close()

    # ---- direct-DB seed helpers -----------------------------------
    # Fast, deterministic test fixtures for downstream logic (campaigns,
    # outcomes, KB) that don't need the CSV-import path itself under test
    # (that path has its own dedicated coverage in test_prospect_pipeline).
    def seed_batch(self, batch_id: str = "seed-batch", filename: str = "seed.csv") -> str:
        now = datetime.now(timezone.utc).isoformat()
        insert_ignore = (
            "INSERT INTO import_batches (batch_id, filename, row_count, imported_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (batch_id) DO NOTHING"
            if self.is_postgres else
            "INSERT OR IGNORE INTO import_batches (batch_id, filename, row_count, imported_at) VALUES (?, ?, ?, ?)"
        )
        self._exec(insert_ignore, (batch_id, filename, 0, now))
        return batch_id

    def seed_prospect(self, first_name="Jane", last_name="Doe", email=None, company="Acme",
                       phone="", status="Valid", batch_id: str = "seed-batch") -> int:
        self.seed_batch(batch_id)
        email = email or f"{first_name.lower()}.{last_name.lower()}.{int(time.time()*1000)}@example.com"
        row_number = self.raw_query(
            "SELECT COALESCE(MAX(row_number), 0) + 1 AS n FROM prospects_raw WHERE batch_id = ?", (batch_id,)
        )[0]["n"]
        return self._exec(
            """INSERT INTO prospects_raw (batch_id, row_number, first_name, last_name, email, company, phone, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""" + (" RETURNING id" if self.is_postgres else ""),
            (batch_id, row_number, first_name, last_name, email, company, phone, status),
            returning=True,
        )

    def raw_query(self, sql: str, params=()):
        conn = self._raw_conn()
        try:
            if self.is_postgres:
                import psycopg2.extras
                cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
                cur.execute(sql.replace("?", "%s"), params)
                return [dict(r) for r in cur.fetchall()]
            conn.row_factory = sqlite3.Row
            rows = conn.execute(sql, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()


def _create_pg_test_db() -> tuple[str, str]:
    """Create a throwaway database on the TEST_DATABASE_URL server, named
    apex_test_<uuid>, and return (full_url_to_new_db, db_name) so the
    caller can DROP it on teardown. Mirrors "fresh SQLite file per
    module" but for a real server that doesn't have per-file isolation."""
    import psycopg2
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(TEST_DATABASE_URL)
    db_name = f"apex_test_{uuid.uuid4().hex[:12]}"

    admin_conn = psycopg2.connect(TEST_DATABASE_URL)
    admin_conn.autocommit = True  # CREATE DATABASE can't run inside a transaction block
    admin_conn.cursor().execute(f'CREATE DATABASE "{db_name}"')
    admin_conn.close()

    new_url = urlunparse(parsed._replace(path=f"/{db_name}"))
    return new_url, db_name


def _drop_pg_test_db(db_name: str):
    import psycopg2

    admin_conn = psycopg2.connect(TEST_DATABASE_URL)
    admin_conn.autocommit = True
    # Terminate any lingering backends from the just-killed uvicorn subprocess
    # first -- DROP DATABASE fails while other connections are still open.
    admin_conn.cursor().execute(
        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = %s AND pid <> pg_backend_pid()",
        (db_name,),
    )
    admin_conn.cursor().execute(f'DROP DATABASE IF EXISTS "{db_name}"')
    admin_conn.close()


def _launch_server(tmp_path_factory, env_overrides=None) -> APIClient:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    pg_db_name = None

    if TEST_DATABASE_URL:
        pg_url, pg_db_name = _create_pg_test_db()
        env = {
            "DATABASE_URL": pg_url,
            "APEX_DB_PATH": "",
            "PATH": os.environ.get("PATH", ""),
        }
        dsn, is_postgres = pg_url, True
    else:
        db_path = str(tmp_path_factory.mktemp("apex_db") / "test.db")
        # DATABASE_URL="" (present but empty) rather than simply omitted: app.main
        # calls load_dotenv() on startup, which -- if this repo has a local .env
        # with a real DATABASE_URL in it (e.g. a Supabase URI for production use)
        # -- would otherwise silently pull that into this subprocess and point
        # the whole regression suite at a real network database. python-dotenv
        # only fills in keys that aren't *already* in os.environ, so pre-setting
        # it empty here keeps the suite hermetic regardless of what .env exists
        # on disk. bool("") is False, so app.db still resolves to SQLite.
        env = {
            "APEX_DB_PATH": db_path,
            "DATABASE_URL": "",
            "PATH": os.environ.get("PATH", ""),
        }
        dsn, is_postgres = db_path, False

    if env_overrides:
        env.update(env_overrides)

    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(REPO_ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    deadline = time.time() + 20
    last_error = None
    while time.time() < deadline:
        if proc.poll() is not None:
            out = proc.stdout.read().decode(errors="replace")
            raise RuntimeError(f"Server process exited early (code {proc.returncode}):\n{out}")
        try:
            r = requests.get(base_url + "/health", timeout=1)
            if r.status_code == 200:
                break
        except requests.RequestException as e:
            last_error = e
        time.sleep(0.2)
    else:
        proc.terminate()
        raise RuntimeError(f"Server never became healthy at {base_url}: {last_error}")

    client = APIClient(base_url, dsn, is_postgres=is_postgres)
    client._proc = proc
    client._pg_db_name = pg_db_name
    return client


def _teardown_server(client: APIClient):
    client._proc.terminate()
    try:
        client._proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        client._proc.kill()
        client._proc.wait(timeout=5)
    if client._pg_db_name:
        _drop_pg_test_db(client._pg_db_name)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Fresh server + fresh DB for every test module -- SQLite by default,
    or a real throwaway Postgres database if TEST_DATABASE_URL is set."""
    client = _launch_server(tmp_path_factory)
    yield client
    _teardown_server(client)
