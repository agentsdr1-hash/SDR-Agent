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
import socket
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

REPO_ROOT = Path(__file__).parent.parent


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class APIClient:
    """Thin requests wrapper bound to one server's base_url. Deliberately
    transparent (returns raw requests.Response) rather than magic -- tests
    check .status_code and call .json() themselves, same as reading a
    curl response, so failures are easy to read."""

    def __init__(self, base_url: str, db_path: str):
        self.base_url = base_url
        self.db_path = db_path
        self.session = requests.Session()

    def get(self, path: str, **kw) -> requests.Response:
        return self.session.get(self.base_url + path, **kw)

    def post(self, path: str, json=None, **kw) -> requests.Response:
        return self.session.post(self.base_url + path, json=json, **kw)

    def put(self, path: str, json=None, **kw) -> requests.Response:
        return self.session.put(self.base_url + path, json=json, **kw)

    def delete(self, path: str, **kw) -> requests.Response:
        return self.session.delete(self.base_url + path, **kw)

    # ---- direct-SQLite seed helpers -----------------------------------
    # Fast, deterministic test fixtures for downstream logic (campaigns,
    # outcomes, KB) that don't need the CSV-import path itself under test
    # (that path has its own dedicated coverage in test_prospect_pipeline).
    def seed_batch(self, batch_id: str = "seed-batch", filename: str = "seed.csv") -> str:
        now = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR IGNORE INTO import_batches (batch_id, filename, row_count, imported_at) VALUES (?, ?, ?, ?)",
            (batch_id, filename, 0, now),
        )
        conn.commit()
        conn.close()
        return batch_id

    def seed_prospect(self, first_name="Jane", last_name="Doe", email=None, company="Acme",
                       phone="", status="Valid", batch_id: str = "seed-batch") -> int:
        self.seed_batch(batch_id)
        email = email or f"{first_name.lower()}.{last_name.lower()}.{int(time.time()*1000)}@example.com"
        conn = sqlite3.connect(self.db_path)
        row_number = conn.execute(
            "SELECT COALESCE(MAX(row_number), 0) + 1 FROM prospects_raw WHERE batch_id = ?", (batch_id,)
        ).fetchone()[0]
        cur = conn.execute(
            """INSERT INTO prospects_raw (batch_id, row_number, first_name, last_name, email, company, phone, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (batch_id, row_number, first_name, last_name, email, company, phone, status),
        )
        conn.commit()
        prospect_id = cur.lastrowid
        conn.close()
        return prospect_id

    def raw_query(self, sql: str, params=()):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]


def _launch_server(tmp_path_factory, env_overrides=None) -> APIClient:
    db_path = str(tmp_path_factory.mktemp("apex_db") / "test.db")
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"

    env = {"APEX_DB_PATH": db_path, "PATH": __import__("os").environ.get("PATH", "")}
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

    client = APIClient(base_url, db_path)
    client._proc = proc
    return client


def _teardown_server(client: APIClient):
    client._proc.terminate()
    try:
        client._proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        client._proc.kill()
        client._proc.wait(timeout=5)


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """Fresh server + fresh SQLite DB for every test module."""
    client = _launch_server(tmp_path_factory)
    yield client
    _teardown_server(client)
