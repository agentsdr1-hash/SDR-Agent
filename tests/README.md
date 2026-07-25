# Regression suite

Run the whole thing:

```bash
pip install -r requirements-dev.txt
pytest -v
```

Run one file, or one test:

```bash
pytest tests/test_sales_outcomes.py -v
pytest tests/test_kb_qa.py::test_grade_only_reply_is_not_asked_for_grade_again -v
```

## What this covers

One file per feature area, matching the app's own service boundaries:

| File | Covers |
|---|---|
| `test_prospect_pipeline.py` | CSV import + header aliasing, validation classification (Valid/Invalid/Duplicate/Existing Customer), lead numbering, editing a prospect (incl. qualification fields) |
| `test_campaign_lifecycle.py` | Campaign creation, draft approve/reject (single + bulk), simulated send/reply, opt-out -> suppression, real-send safety gate |
| `test_daily_send_limit.py` | The daily send cap actually stops sends once hit and leaves the rest queued (not failed) |
| `test_leads_consolidated.py` | `/leads` filters, bulk assign/suppress, lead score math, quote-readiness math |
| `test_sales_outcomes.py` | Quote Requested -> Won/Lost/reopen transitions, the Quote Readiness Checklist, ERP quote-number tie-back, quote-summary draft email |
| `test_kb_qa.py` | Smart-reply KB/stock matching, and the spec-aware closing line (doesn't re-ask for a grade already given, pushes to close once everything's provided) |
| `test_admin_and_email.py` | Suppression list CRUD, email status/config/daily-limit endpoints |
| `test_dashboard_reporting.py` | `/reports/summary` aggregate math against a known seeded funnel |

## How it's built

**Real server, real HTTP, real SQLite file** -- not FastAPI's in-process
TestClient. `tests/conftest.py`'s `server` fixture launches an actual
`uvicorn` subprocess against a fresh, throwaway SQLite file (one per test
*module*, not per test or per whole run -- see the fixture's docstring for
the isolation-vs-speed tradeoff), waits for `/health`, and yields a small
`APIClient` wrapper (`server.get/.post/.put/.delete`, all returning the
raw `requests.Response`). This mirrors the exact pattern used throughout
this app's development to manually verify every feature (spin up a
scratch-DB server, hit it with curl/requests/Playwright) rather than
introducing a different code path for tests than what's actually deployed.

`server.seed_prospect(...)` / `server.seed_batch(...)` insert directly
into the SQLite file for fast, deterministic setup when the import
pipeline itself isn't what's under test. `server.raw_query(sql, params)`
lets a test inspect DB state the API doesn't expose.

### The "isolated probe" pattern (`_daily_limit_probe.py`, `_kb_probe.py`)

A few things can't be tested through the live-server fixture at all:

- **Daily send limit accounting** needs `email_provider.send_email` mocked
  out (no real Gmail SMTP in CI), which only works with a *direct* Python
  import in-process -- not through HTTP to a subprocess.
- Direct-import DB access is otherwise avoided in this suite because
  `app.db.DB_PATH` is resolved once, at import time, from `APEX_DB_PATH`.
  Every normal test talks to its DB over HTTP, so nothing in the shared
  pytest process ever imports `app.db` in a way that touches disk -- but
  the instant something does, that path is cached for the rest of the
  session, and a later test can't silently repoint it to a different DB.

The fix used here: files prefixed `_` (e.g. `_daily_limit_probe.py`) are
**not test files** -- pytest won't collect them. They're small standalone
scripts, run via `subprocess.run([sys.executable, "tests/_foo_probe.py"],
env={"APEX_DB_PATH": <fresh tmp path>})`, each in its own interpreter with
its own DB, printing a JSON result to stdout that the real test parses and
asserts on. Run one by hand to see its raw output while debugging:

```bash
APEX_DB_PATH=/tmp/probe.db python3 tests/_daily_limit_probe.py
```

If you add a new test that needs mocking or direct (non-HTTP) access to
`app.services.*`/`app.db`, follow this same pattern rather than importing
those modules directly into a shared test file.

## What's not covered (yet)

This is backend/API-level only. Frontend wiring bugs in `app/static/app.html`
(a broken template literal, a missing DOM id, a stale `onclick` reference)
won't be caught here -- those have historically been found via ad hoc
Playwright scripts during manual verification. A `tests/ui/` Playwright
layer would be the natural next addition if that becomes a recurring
source of regressions.
