# APEX SDR Pilot — OBJ-001 & OBJ-002

Live implementations matching the Build Tracker exactly:

| Object | Frontend | Backend | Integration |
|---|---|---|---|
| **OBJ-001 Prospect Import** | ✓ upload UI | ✓ parse/stage | ✓ import-from-URL |
| **OBJ-002 Prospect Validation** | — | ✓ rules engine | ✓ swappable customer-match provider |
| **OBJ-003 Campaign Management** | ✓ create/assign UI | ✓ campaign CRUD + queue | — (none required per tracker) |
| **OBJ-005 Approval Workflow** | — (API only, UI pending) | ✓ edit/approve/reject drafts | — |
| **OBJ-006 Email Delivery** | — | ✓ sends approved drafts | ✓ real Gmail SMTP send |
| **OBJ-013 Reporting Dashboard** | ✓ `/dashboard` page | ✓ `/reports/summary` | — (reads local data only) |
| **OBJ-016 Email Integration** | — | ✓ status/poll endpoints | ✓ Gmail SMTP send + IMAP reply polling |
| **OBJ-015 Administration Console** | ✓ `/admin` page | ✓ suppression list | — (rule-based opt-out keyword detection) |
| **OBJ-011-lite Sales Outcomes** | — (via API) | ✓ Quote → Won/Lost, deal value | — |
| **OBJ-014 Audit & Monitoring** | ✓ dashboard panel | ✓ event log across every object | — |

## Setting up email (OBJ-016) — Gmail via App Password

This works with a personal Gmail account, no Google Cloud project needed.

1. Turn on 2-Step Verification: https://myaccount.google.com/security
2. Generate an App Password: https://myaccount.google.com/apppasswords
3. Copy `.env.example` to `.env` and fill in `GMAIL_ADDRESS` and `GMAIL_APP_PASSWORD`
4. Restart the server — `/email/status` should now show `"configured": true`

Until those two variables are set, the app runs completely normally — importing,
validating, campaigns, approving, and editing drafts all work. Only the actual
**send** and **reply-polling** calls are gated, and they fail with a clear
message (503 on send) rather than pretending to succeed.

**How replies get detected:** the server polls the inbox every
`POLL_INTERVAL_MINUTES` (default 5, free either way — polling frequency
doesn't cost anything with Gmail) looking for unread mail from any prospect
currently in `Sent` status. No public URL or webhook needed — this reaches
out to Gmail, Gmail never has to reach in, so it works the same whether this
runs on your laptop or a hosted server.

## Run it

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open **http://localhost:8000** for the upload UI, **http://localhost:8000/dashboard** for the pipeline dashboard. API docs at `/docs`.

## Endpoints

| Method | Path | Object | Description |
|---|---|---|---|
| GET  | `/` | OBJ-001 frontend | Drag-and-drop upload UI, runs import → validate → assign-to-campaign |
| POST | `/prospects/import` | OBJ-001 | Upload a .csv/.xlsx file, returns `batch_id` |
| POST | `/prospects/import-from-url?url=...` | OBJ-001 integration | Pull a file from a shared drive / SFTP-backed link instead of a direct upload |
| POST | `/prospects/validate/{batch_id}` | OBJ-002 | Runs validation rules, returns counts |
| GET  | `/prospects/{batch_id}?status=Valid` | both | List staged rows, optional status filter |
| POST | `/campaigns` | OBJ-003 | Create a campaign (`name`, optional `send_days`, `daily_send_limit`) |
| GET  | `/campaigns` | OBJ-003 | List campaigns with queued-prospect counts |
| POST | `/campaigns/{id}/assign/{batch_id}` | OBJ-003 | Queue every `Valid` prospect from a batch into a campaign (idempotent), auto-generates a draft per prospect |
| GET  | `/campaigns/{id}/prospects` | OBJ-003 | List everyone queued in a campaign, including their draft and send/reply timestamps |
| PUT  | `/campaigns/{id}/prospects/{row_id}/draft` | OBJ-005 | Edit subject/body while still `Queued` |
| POST | `/campaigns/{id}/prospects/{row_id}/approve` | OBJ-005 | Approve a draft (must be `Queued`) |
| POST | `/campaigns/{id}/prospects/{row_id}/reject` | OBJ-005 | Reject a draft (must be `Queued`) |
| POST | `/campaigns/{id}/send` | OBJ-006 | Actually sends every `Approved` prospect via Gmail. **503 if not configured** |
| GET  | `/email/status` | OBJ-016 | Whether Gmail is configured, last poll time/result |
| POST | `/email/poll-now` | OBJ-016 | Manually trigger a reply check instead of waiting for the timer |
| GET  | `/admin` | OBJ-015 frontend | View/add/remove suppressed emails |
| GET  | `/admin/suppressed` | OBJ-015 | List the suppression list |
| POST | `/admin/suppressed` | OBJ-015 | Manually suppress an email (`email`, optional `reason`) |
| DELETE | `/admin/suppressed/{email}` | OBJ-015 | Remove an email from suppression |
| POST | `/campaigns/{id}/prospects/{row_id}/request-quote` | OBJ-011-lite | Move a `Replied` prospect to `QuoteRequested` |
| POST | `/campaigns/{id}/prospects/{row_id}/won` | OBJ-011-lite | Mark a deal Won (optional `deal_value` for turnover tracking) |
| POST | `/campaigns/{id}/prospects/{row_id}/lost` | OBJ-011-lite | Mark a deal Lost (optional `reason`) |
| GET  | `/audit/events?limit=&event_type=&entity_type=` | OBJ-014 | Chronological event log, filterable |
| GET  | `/audit/event-counts` | OBJ-014 | Count of events by type |
| GET  | `/dashboard` | OBJ-013 frontend | Live view of import/validation/campaign stats, value captured, SDR performance, audit log |
| GET  | `/reports/summary` | OBJ-013 | JSON summary powering the dashboard |

## Dashboard metrics (OBJ-013)

Beyond the outreach funnel, the dashboard now shows:

- **Value captured** — customers won, total turnover (sum of manually-entered
  deal values on Won outcomes), quotes requested, deals lost, win rate.
  Turnover requires a deal value at the moment a deal is marked Won — real
  quoting/pricing automation is explicitly out of Phase 1 scope, so this is
  where a human records the number after taking over.
- **SDR performance** — the same activity metrics you'd track for a human
  SDR, applied to APEX: total emails sent, replies received, response rate,
  and average reply time (computed from real `sent_at`/`replied_at`
  timestamps, not estimated).
- **Audit log** — every event streams into `/audit/events`: imports,
  validations, campaign creation, draft approve/reject, each send attempt
  (sent/failed/blocked-by-suppression), replies received, opt-outs detected,
  and deal outcomes. This is OBJ-014 — the record you'd pull to answer
  "why did this get suppressed" or "when did we send to X" weeks later.

## Suppression list (OBJ-015) — checked twice, never sent to

- **At campaign assignment** — a suppressed email is never even queued; `assign_batch_to_campaign` reports it under `skipped_suppressed`
- **At send time** — the final gate, right before OBJ-006 calls Gmail. If something was suppressed after being queued/approved (e.g. they opted out after your Sales Admin already approved the draft), it's caught here and flipped to `Suppressed` instead of sending
- **Auto-detection** — the OBJ-016 inbox poller scans incoming reply subject/body for opt-out language ("unsubscribe," "remove me," "do not contact," etc., same rule-based approach as OBJ-010) and adds the sender to suppression automatically instead of marking them a normal `Replied`

Tested live: an email suppressed before assignment never enters a campaign; one suppressed after being approved is still blocked at send with zero SMTP calls made.

## Example flow

```bash
curl -F "file=@sample_prospects.csv" http://localhost:8000/prospects/import
# -> {"batch_id": "6f347ca0", ...}

curl -X POST http://localhost:8000/prospects/validate/6f347ca0
# -> {"valid": 2, "invalid": 2, "duplicate": 1, "existing_customer": 1}

curl "http://localhost:8000/prospects/6f347ca0?status=Valid"
```

## Data persistence during the pilot (2-3 weeks, running locally)

`app/apex_pilot.db` accumulates everything — every import, validation,
campaign, and approval decision — across every time you stop and restart the
server, as long as you always run it from the same folder (or keep
`APEX_DB_PATH` consistent). Nothing resets unless the file is deleted.

Since it's a single file on one machine with no hosting service behind it,
back it up periodically so a deleted file or disk issue doesn't lose the
whole pilot's data. Simplest approach — copy it somewhere safe by hand or on
a schedule:

```bash
# manual backup
cp app/apex_pilot.db ~/apex-pilot-backups/apex_pilot_$(date +%Y%m%d).db

# macOS/Linux: run that daily via cron
# Windows: Task Scheduler running the same copy command
```

## Packaging: 1 tool, not 1-per-object

Every object in the Build Tracker (OBJ-001, OBJ-002, and everything after)
lives in **one FastAPI process** as a router in `app/routers/`. There is no
"OBJ-001 tool" and "OBJ-002 tool" to merge — `app/main.py` already includes
both. Adding OBJ-003+ later means adding a router file and one
`app.include_router(...)` line, not standing up a second service.

```
app/
  main.py            <- app shell, mounts all routers + the frontend
  routers/
    prospects.py      <- OBJ-001 + OBJ-002 (live)
    campaigns.py       <- OBJ-003 placeholder, shows the pattern
  services/            <- business logic per object
  integrations/         <- external-system adapters (CRM, etc.)
  static/index.html      <- OBJ-001 frontend, served at /
```

### Hosting it (turns this from "code on a laptop" into a live pilot URL)

A `Dockerfile` is included — it packages the backend, frontend, and all
dependencies into one container that exposes port 8000.

```bash
docker build -t apex-pilot .
docker run -p 8000:8000 -v apex_data:/srv/data apex-pilot
```

The `-v apex_data:/srv/data` volume keeps the SQLite file across restarts —
without it, every redeploy wipes imported/validated data.

To make it reachable outside your machine, push that same image to any
container host:
- **Render / Railway / Fly.io** — connect the repo, they build the
  Dockerfile automatically, you get a public HTTPS URL in a few minutes.
  Simplest option for a pilot.
- **Your own server** — `docker run` on any VM with the port opened, put
  nginx/Caddy in front for HTTPS.
- **AWS/Azure/GCP** — App Runner / Container Apps / Cloud Run all take a
  Dockerfile directly with no extra config.

Whichever you pick, you get **one URL** — that URL's `/` is the OBJ-001
upload page, and its `/prospects/...` routes are what OBJ-003 (once built)
will call to move a validated prospect into a campaign. Same app the whole
way through the lifecycle in your pilot diagram.


- **SQLite** for the pilot (`app/apex_pilot.db`, auto-created). The raw SQL is
  portable — swap the `sqlite3.connect` call in `db.py` for a Postgres driver
  when you move past pilot scale.
- **Column mapping** in `prospect_import.py` tolerates header variance
  (`"First Name"`, `first_name`, `firstname` all map to the same field) so the
  import doesn't break every time a sales admin exports from a different CRM.
- **Validation is additive, not blocking** — every row lands in the DB with a
  status; nothing gets silently dropped. That gives OBJ-013 (Reporting
  Dashboard) and OBJ-014 (Audit) something to report on later.
- Two customers are seeded (`jsmith@acmecorp.com`, `dlee@globex.com`) so the
  "Existing Customer" rule has something to match against.
- **Existing-customer matching (OBJ-002 integration)** goes through
  `app/integrations/customer_provider.py`. Today `ACTIVE_PROVIDER` is
  `LocalDBCustomerProvider` (reads the seeded table). Point it at your real
  CRM by implementing `CRMCustomerProvider.get_customer_emails()` — a
  Salesforce REST example is stubbed in the docstring — and flipping one line.
  Validation logic never has to change.
- **File source (OBJ-001 integration)** — `POST /prospects/import-from-url`
  fetches a file from anywhere reachable by HTTP (shared drive link, SFTP
  gateway that exposes HTTP, email-attachment staging URL) and runs it
  through the same import path as a direct upload.
- **Frontend (OBJ-001)** is `app/static/index.html`, served at `/`. Vanilla
  HTML/JS, no build step — drag/drop or paste a URL, watch import → validate
  run automatically, see a color-coded results table, then create/pick a
  campaign and queue the `Valid` rows into it.
- **Campaign assignment (OBJ-003)** is idempotent by design — the
  `(campaign_id, prospect_id)` pair is unique in `campaign_prospects`, so
  re-running an assign call on the same batch just reports what got skipped
  instead of erroring or duplicating.
- Campaigns only *queue* prospects (`status='Queued'`). Nothing sends yet —
  that's OBJ-006 (Email Delivery), which needs OBJ-016 (Email Integration)
  first. OBJ-016 is the next real object to build, but needs real Microsoft
  Graph/Gmail OAuth credentials before it can do anything, unlike OBJ-003
  which needed none.

## Next objects to wire up (per the tracker)

- OBJ-016 Email Integration — needed before OBJ-006 can actually send
- OBJ-003 Campaign Management — consumes `Valid` rows from this batch
