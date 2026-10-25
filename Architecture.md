# GmailCleaner — Architecture

This document describes the internal design of GmailCleaner: how data flows, how components are structured, what each module does, and why specific technical decisions were made.

---

## System overview

```
┌──────────────────────────────── Google Cloud (your project) ────────────────────────────────┐
│                                                                                             │
│  Cloud Scheduler ──(daily)──► Cloud Run Job ─────────┐                                      │
│                                (scan · digest ·      │                                      │
│                                 trusted rules)       │                                      │
│                                                      ▼                                      │
│                                          Cloud Storage volume  ◄── SQLite DB +              │
│                                                      ▲             pending_actions.json     │
│  Gmail ──push──► Pub/Sub ──OIDC push──► Cloud Run Service                                   │
│                                          (dashboard · webhook ·                             │
│  Cloud Scheduler ──(daily)──────────────► /api/watch/renew)                                 │
│                                                                                             │
│  Secret Manager: OAuth token · Gemini API key · dashboard secret                            │
└─────────────────────────────────────────────────────────────────────────────────────────────┘
        │                                   │
        ▼                                   ▼
   Gmail API (read / modify / send)    Gemini API (metadata-only classification)
```

The same code runs locally: the CLI and `python -m web.app` read files under `config/`, `data/` and `output/` instead of the mounted volume and Secret Manager.

---

## Execution model

GmailCleaner separates **deciding** from **acting**:

```
SCAN (read-only)
  └── fetch metadata → engines → digest → pending actions report (+ action_log rows)

CONFIRM
  └── user sets "confirmed": true (dashboard / JSON) — or trusted rules auto-confirm

EXECUTE (write)
  └── executor runs confirmed, not-yet-executed items → stamps executed_at → action_log
```

- The **scheduled job** runs `scan --no-dry-run`. With the default `confirm_first: true`, this only reports. With `confirm_first: false` and `trusted_rules`, it also executes the matching actions.
- The **dashboard** and `main.py execute` both call the same `execute_report()` function, so the CLI and web paths behave identically.
- Each scan replaces the report. Pending `action_log` rows from older runs are marked `superseded`.

---

## Repository structure

```
GmailCleaner/
├── main.py                 # CLI: info · scan · execute · export · setup-watch
├── version.py              # single version string
├── core/
│   └── settings.py         # settings.yaml loading, env overrides, multi-account paths
├── config/
│   └── settings.yaml       # user configuration (documented inline)
├── auth/
│   └── oauth.py            # OAuth lifecycle; GOOGLE_TOKEN_JSON for headless/Cloud Run
├── gmail/
│   ├── client.py           # typed Gmail API wrapper (batch get, batch trash, watch, history)
│   ├── fetcher.py          # paginated metadata fetch
│   ├── parser.py           # raw API dicts → GmailMessage dataclasses
│   └── executor.py         # unsubscribe / trash execution + execute_report()
├── engines/
│   ├── unsubscribe.py      # RFC 2369 / 8058 candidate detection
│   ├── bulk_cleanup.py     # domain + subject-pattern grouping and scoring
│   ├── triage.py           # Gemini classification into buckets
│   ├── digest.py           # HTML digest email
│   └── rules.py            # trusted rules engine
├── ai/
│   └── gemini.py           # Gemini REST client, prompt, defensive parsing, backoff
├── store/
│   └── db.py               # SQLite: senders, action_log, runs, triage, watch, stats
├── output/
│   ├── reporter.py         # pending actions report (atomic writes) + console output
│   └── exporter.py         # CSV inbox health export
├── pubsub/
│   ├── watcher.py          # gmail.users.watch registration and renewal
│   └── webhook.py          # Pub/Sub push handling (history delta → mini-scan)
├── web/
│   ├── app.py              # Flask dashboard, API and webhook
│   └── *.html              # Jinja templates
├── deploy/
│   ├── Dockerfile          # Cloud Run Job image
│   ├── Dockerfile.web      # Cloud Run Service image (gunicorn)
│   ├── deploy_job.sh       # job + scheduler + bucket + secrets
│   ├── deploy_dashboard.sh # service + Pub/Sub + watch renewal
│   └── cloudbuild.yaml     # CI/CD: test → build → roll out
└── tests/                  # offline test suite (all external APIs mocked)
```

---

## Components

### `core/settings.py`

Loads `settings.yaml` and resolves every runtime path in one place. Precedence: **environment variable → settings.yaml → default**. With an account name (`--account` / `GMAILCLEANER_ACCOUNT`), the token, database and report paths are namespaced per account. Explicit environment overrides still win, which is how Cloud Run points both containers at the shared volume.

### `auth/oauth.py`

- **Local:** the first run opens the browser consent flow (`InstalledAppFlow`). The token, which includes the refresh token, is saved to `config/token.json` with `0600` permissions and refreshed silently afterwards.
- **Production:** `GOOGLE_TOKEN_JSON` (injected from Secret Manager) takes precedence. The authorized-user JSON already contains the client ID and secret, so no credentials file is needed. Refreshed access tokens stay in memory.
- **Headless guard:** on Cloud Run (or with `GMAILCLEANER_HEADLESS=1`), a missing or invalid token raises a clear error instead of trying to open a browser.

Scopes: `gmail.readonly`, `gmail.modify`, `gmail.send`.

**Why OAuth and not a service account?** Service accounts cannot access a personal Gmail inbox. Domain-wide delegation applies only to Google Workspace and is out of scope.

### `gmail/fetcher.py` and `gmail/parser.py`

A two-step fetch: `users.messages.list` pages through IDs (up to 500 per page, bounded by `max_emails`, with the date filter applied via Gmail's `after:` operator), then `batch_get_messages` fetches **metadata only** in batches with 429 backoff. The parser normalizes each message into a `GmailMessage`: lowercased sender email and domain, UTC date, uppercase labels, and a structured `UnsubscribeTarget` (HTTP URL, mailto, one-click flag).

### `engines/unsubscribe.py`

Groups messages by sender domain. It keeps domains that have a valid unsubscribe target and at least `min_email_count` messages, and skips blocked or already-unsubscribed domains. For each domain it picks the most capable target (one-click > HTTP > mailto). Candidates are ranked noisiest first.

**Why RFC headers instead of clicking links in a browser?** They are more reliable, don't fire tracking pixels, and need no browser dependency.

### `engines/bulk_cleanup.py`

Pass 1 groups by sender domain. Pass 2 groups the remaining messages by normalized subject, stripping numbers, dates and `#123`-style issue counters so that newsletter series cluster together. Each group is scored on volume, unread ratio, promotional signals and age, then labelled high, medium or low.

### `engines/triage.py` and `ai/gemini.py`

Messages that a cleanup group already claims are skipped. Results classified in the last 24h are reused from the DB. The rest go to Gemini in batches of 20, with sender, subject, snippet and labels only. The model returns JSON, which is parsed defensively: malformed or missing entries become `UNCLASSIFIED`. Results below `confidence_threshold` are flagged for review. 429 and 503 responses are retried with exponential backoff and jitter.

### `engines/rules.py`

Trusted rules are parsed from config and active **only** when `confirm_first: false`. A rule matches an exact domain or a `*.domain` wildcard (the apex plus subdomains). `blocked_senders` always override. A bare `*` is rejected. Delete rules match only domain groups, never subject groups. `apply()` marks matching report items `confirmed` + `auto_confirmed`.

### `gmail/executor.py`

- **Unsubscribe:** RFC 8058 one-click POST (`List-Unsubscribe=One-Click`), otherwise a GET, falling back to mailto via the Gmail API. HTTP 200/202/204/302 count as success. A 1s courtesy delay separates requests.
- **Cleanup:** `users.messages.batchModify` adds `TRASH` and removes `INBOX` in chunks of 1,000. Messages are recoverable for 30 days. Permanent `batchDelete` is deliberately not used: it would need the full `https://mail.google.com/` scope, and nothing could be undone.
- **`execute_report()`:** runs every confirmed item that has no `executed_at` yet, stamps successes, updates `action_log`, and records an `execute` run. Running it again is safe.

### `engines/digest.py`

Builds an HTML email (needs-reply items, FYI, promotions with unsubscribe candidates, junk with cleanup groups, items needing review) and sends it to the configured recipient, or to the account itself. When `DASHBOARD_URL` is set, it links straight to the Pending page.

### `output/reporter.py` and `output/exporter.py`

The reporter writes `pending_actions.json` **atomically** (temp file + rename), so the job, dashboard and webhook never see a half-written file. It also merges real-time unsubscribe candidates into the existing report. The exporter renders `summary`, `senders`, `actions` and `runs` as CSV for `main.py export` and the dashboard's `/export/<dataset>.csv`.

### `store/db.py`

```sql
senders            -- domain, display_name, first/last_seen, email_count, unsubscribed_at, method, trusted, blocked
action_log         -- run_id, action_type, target, target_meta, status, created/executed_at, error
                   --   status: pending | confirmed | executed | failed | skipped | superseded
runs               -- id, mode (scan|execute), timestamps, scanned / pending / executed / failed counts
triage_results     -- message_id, bucket, confidence, reason, model, classified_at
watch_registration -- singleton: history_id, expiration_ms, topic_name
inbox_events       -- per-notification counts for dashboard charts
kv_store           -- last processed Gmail historyId
```

The journal mode is configurable (`SQLITE_JOURNAL_MODE`). WAL is used on local disk and DELETE on the Cloud Storage FUSE volume, which cannot provide WAL's shared memory.

### `pubsub/watcher.py` and `pubsub/webhook.py`

`users.watch` registers the Inbox with the Pub/Sub topic. Gmail expires watches after 7 days, so Cloud Scheduler calls `POST /api/watch/renew` daily. On each push, the webhook:

1. verifies the OIDC JWT (audience = the webhook URL, issuer = the configured service account),
2. decodes `{emailAddress, historyId}` and skips anything older than the stored baseline,
3. reads `users.history.list` since the baseline. History records carry IDs only, so metadata is then batch-fetched,
4. runs unsubscribe detection and triage on just those messages, merges new candidates into the report, increments sender counts and records an inbox event,
5. advances the baseline and always returns 200, so Pub/Sub doesn't retry forever.

### `web/app.py`

A Flask app served by gunicorn (one worker, eight threads, because of the single SQLite writer). Routes: `/`, `/pending`, `/stats`, `/history`, `/export/<dataset>.csv`, `/health`, `POST /api/confirm`, `POST /api/execute`, `POST /api/watch/renew`, `POST /webhook/pubsub`.

Authentication uses `DASHBOARD_SECRET`, accepted as a Bearer header, a `?token=` parameter (which sets an HttpOnly, SameSite=Strict, Secure cookie) or that cookie, and is compared in constant time. The SameSite=Strict cookie also protects the POST APIs from cross-site requests.

---

## Data flow

```
Gmail inbox
   │  users.messages.list + batch get (metadata)
   ▼
fetcher.py → parser.py ──► GmailMessage[]
   │
   ├──► unsubscribe.py ─┐
   ├──► bulk_cleanup.py ┼──► triage.py (Gemini) ──► digest.py ──► email to you
   │                    │
   ▼                    ▼
reporter.py ──► pending_actions.json ◄── webhook.py (real-time candidates)
   │
   ├── rules.py (auto-confirm, if enabled)
   ├── dashboard / JSON edit (manual confirm)
   ▼
executor.py ──► unsubscribe (POST/GET/mailto) · batchModify → Trash
   │
   ▼
db.py (action_log, senders, runs)
```

---

## Key technical decisions

| Decision | Rationale |
|---|---|
| Cloud Run Job for scans, Service for the dashboard | Scans need no idle server; the dashboard scales to zero between visits |
| SQLite on a Cloud Storage volume | Zero cost and zero setup for a single-user workload; one writer instance |
| Trash instead of permanent delete | Recoverable mistakes, and no need for the full-mailbox scope |
| Metadata-only fetch and AI input | Privacy, plus lower quota and token usage |
| RFC 2369/8058 unsubscribe | Reliable, no tracking-pixel triggers, no browser |
| Gemini Flash | Free tier available, sufficient for classification, same Google project |
| Confirm-first by default; trusted rules opt-in | Destructive actions need explicit approval |
| Token in Secret Manager, injected as env | Credentials never live in images or the repo |
| Shared `execute_report()` | CLI, dashboard and trusted rules can't drift apart |

---

## Security considerations

- The OAuth token grants `gmail.modify` and `gmail.send`. Treat it like a password: store it in Secret Manager in production and never commit it (`config/token.json` and `config/tokens/` are gitignored and excluded from Docker builds).
- `pending_actions.json` and the database contain sender names and subjects. They are gitignored and live only on your machine or in your private bucket, which has public access prevention enabled.
- The dashboard service is reachable on the internet but enforces token authentication. The webhook accepts only Google-signed tokens for the configured audience and service account.
- Containers run as a non-root user. The only outbound calls go to the Gmail API, the Gemini API and the unsubscribe endpoints that senders publish.
- GmailCleaner never stores email body content anywhere.
