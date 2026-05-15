# GmailCleaner — Architecture

This document describes the internal design of GmailCleaner: how data flows, how components are structured, what each module does, and why specific technical decisions were made.

---

## High-Level System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        Google Cloud (your project)                  │
│                                                                     │
│  ┌──────────────────┐     triggers     ┌──────────────────────────┐ │
│  │  Cloud Scheduler │ ──────────────►  │      Cloud Run Job       │ │
│  │  (cron: daily)   │                  │   (GmailCleaner engine)  │ │
│  └──────────────────┘                  └──────────┬───────────────┘ │
│                                                   │                 │
│                    ┌──────────────────────────────┼──────────────┐  │
│                    │                              │              │  │
│                    ▼                              ▼              ▼  │
│           ┌──────────────┐              ┌──────────────┐  ┌──────┐  │
│           │  Gmail API   │              │  Gemini API  │  │  DB  │  │
│           │  (read/write)│              │ (classify)   │  │(SQLite│  │
│           └──────────────┘              └──────────────┘  └──────┘  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘

Phase 4 addition:
┌──────────────────┐    push notification    ┌──────────────────────┐
│  Gmail Pub/Sub   │ ──────────────────────► │  Cloud Run Service   │
│  (inbox events)  │                         │  (webhook handler)   │
└──────────────────┘                         └──────────────────────┘
```

---

## Execution Model

GmailCleaner runs as a **Cloud Run Job** — not a persistent server. It wakes up on a schedule, does its work, writes its output, and shuts down. This keeps it within GCP's free tier.

The two-phase execution model:

```
Phase A: SCAN (read-only)
  └── Fetch emails → Analyze → Produce pending_actions output → Send digest

Phase B: EXECUTE (write)
  └── User confirms → Apply approved actions via Gmail API → Log results
```

In the first release, Phase B is triggered manually by the user. In a future release, a trusted-rules engine will allow Phase B to run automatically for pre-approved action types.

---

## Repository Structure

```
GmailCleaner/
│
├── main.py                   # Entrypoint — orchestrates the pipeline
│
├── config/
│   ├── settings.yaml         # User configuration (see README)
│   └── credentials.json      # Google OAuth credentials (gitignored)
│
├── auth/
│   └── oauth.py              # OAuth 2.0 flow, token storage, refresh logic
│
├── gmail/
│   ├── client.py             # Gmail API wrapper (authenticated session)
│   ├── fetcher.py            # Fetches emails with filters and pagination
│   ├── parser.py             # Parses raw messages: headers, body, metadata
│   └── executor.py           # Applies approved actions (delete, label, send)
│
├── engines/
│   ├── unsubscribe.py        # Detects List-Unsubscribe headers, extracts targets
│   ├── bulk_cleanup.py       # Groups emails by sender/subject pattern
│   ├── triage.py             # AI-powered inbox classification
│   └── digest.py             # Builds and sends the daily digest email
│
├── ai/
│   └── gemini.py             # Gemini API client, prompt templates, response parsing
│
├── store/
│   └── db.py                 # SQLite interface — sender history, action log, state
│
├── output/
│   ├── reporter.py           # Generates pending_actions.json and console report
│   └── pending_actions.json  # Written each run, read during execute phase (gitignored)
│
├── deploy/
│   ├── Dockerfile            # Container definition for Cloud Run
│   ├── cloudbuild.yaml       # CI/CD pipeline definition
│   └── scheduler.sh          # Helper script to set up Cloud Scheduler job
│
├── requirements.txt
└── tests/
    └── ...
```

---

## Component Breakdown

### `auth/oauth.py`

Handles the entire Google OAuth 2.0 lifecycle.

- On first run: launches the browser-based consent flow and requests the following scopes:
  - `https://www.googleapis.com/auth/gmail.readonly` — for the scan phase
  - `https://www.googleapis.com/auth/gmail.modify` — for the execute phase (delete, label, send)
- Saves the resulting token (access token + refresh token) to `config/token.json` (gitignored).
- On subsequent runs: loads the stored token and uses the refresh token to get a fresh access token silently. No browser interaction after the first run.

**Why OAuth and not a Service Account?**
Service accounts cannot access a personal Gmail inbox. OAuth is required because GmailCleaner acts on behalf of a real user. A service account is appropriate only for Google Workspace with domain-wide delegation, which is out of scope here.

---

### `gmail/fetcher.py`

Queries Gmail using the `users.messages.list` API endpoint with configurable filters.

Key implementation decisions:
- Uses **label-based filtering** (`in:inbox`, `-in:trash`) to avoid re-processing archived or deleted messages.
- Fetches in **batches of 100** (Gmail API maximum per page) using `nextPageToken` for pagination.
- Retrieves only `METADATA` format first (headers only — fast and cheap on quota). Full body is fetched only when the engine specifically needs it (e.g. unsubscribe URL extraction).
- Respects the `lookback_days` setting using Gmail's `after:` query operator.

**Gmail API quota:** The free tier allows 1 billion quota units per day. A metadata list request costs 5 units. A full message fetch costs 5 units. Batch requests save on HTTP overhead but not quota units.

---

### `gmail/parser.py`

Parses raw Gmail API message objects into clean internal data structures.

Each parsed message becomes a `GmailMessage` dataclass:

```python
@dataclass
class GmailMessage:
    id: str
    thread_id: str
    sender: str               # Display name
    sender_email: str         # Normalized email address
    sender_domain: str        # e.g. "newsletter.shopify.com"
    subject: str
    date: datetime
    labels: list[str]         # Gmail system labels
    snippet: str              # First ~100 chars of body
    list_unsubscribe: str     # Raw List-Unsubscribe header value (if present)
    list_unsubscribe_post: str # RFC 8058 one-click unsubscribe (if present)
```

Normalization rules applied here:
- Sender email is lowercased and stripped of display name formatting.
- Sender domain is extracted from the email and stored separately for domain-level grouping.
- `List-Unsubscribe` header is parsed to extract HTTP URL and/or mailto address.

---

### `engines/unsubscribe.py`

Implements RFC 2369 (`List-Unsubscribe`) and RFC 8058 (`List-Unsubscribe-Post`) detection.

How it works:
1. Filters parsed messages where `list_unsubscribe` header is present.
2. Extracts the unsubscribe target — either an HTTPS URL or a `mailto:` address.
3. Deduplicates by sender domain (one unsubscribe candidate per domain, not per email).
4. Checks the local DB to skip senders already unsubscribed from in a previous run.
5. Produces a list of `UnsubscribeCandidate` objects for the reporter.

Execution (Phase B, after confirmation):
- **HTTP unsubscribe:** sends a `POST` request to the URL (RFC 8058 compliant) or opens the URL via `GET` if no POST endpoint exists.
- **Mailto unsubscribe:** composes a blank email to the unsubscribe address using the Gmail API `users.messages.send` endpoint.

**Why not just click links in a headless browser?**
Many unsubscribe links are time-limited or tied to tracking tokens. The RFC-compliant approach (hitting the `List-Unsubscribe` header target) is more reliable, faster, and does not trigger analytics tracking.

---

### `engines/bulk_cleanup.py`

Groups inbox emails to identify bulk deletion candidates.

Grouping strategy (applied in order):
1. **Exact sender domain** — all emails from `@newsletters.example.com`
2. **Subject prefix pattern** — emails sharing a normalized subject prefix (e.g. "Your daily digest from X")
3. **Thread clustering** — emails in threads with 10+ messages from the same sender

Each group is scored by:
- Total message count
- Date of oldest message in group
- Whether the sender domain has a `List-Unsubscribe` header (suggests newsletter, not transactional)
- Whether all messages in the group are unread (suggests never-opened bulk mail)

Output: a ranked list of `CleanupGroup` objects, each containing the group metadata and the list of message IDs to delete. Presented to the user for confirmation.

Execution (Phase B): calls `gmail.executor.batch_delete()` using Gmail's `batchDelete` endpoint, which supports up to 1,000 message IDs per request.

---

### `engines/triage.py`

Classifies each inbox message into one of four buckets using Gemini.

Buckets:
- `NEEDS_REPLY` — direct messages, questions, requests addressed to you
- `FYI` — receipts, confirmations, status updates worth keeping but no action needed
- `PROMOTIONS` — marketing, newsletters, offers
- `JUNK` — obvious noise with no value

What is sent to Gemini per message:
```
Sender: newsletters@example.com
Subject: Your weekly roundup is here
Snippet: Here's what happened this week in tech...
Labels: INBOX, UNREAD
```

Body content is **never sent**. Classification is done purely on metadata. This is a deliberate privacy decision and also keeps token usage low.

Gemini is prompted to return a JSON array of `{ message_id, bucket, confidence, reason }` objects. Responses are parsed and stored in the DB. Low-confidence classifications (below a configurable threshold) are flagged for user review rather than auto-assigned.

---

### `engines/digest.py`

Builds and sends the daily summary email to your inbox.

The digest contains:
- Count of new messages since last run
- Top items in `NEEDS_REPLY` (up to 5, with sender, subject, snippet)
- Count of `PROMOTIONS` and `JUNK` detected
- Pending unsubscribe candidates waiting for confirmation
- Pending bulk cleanup groups waiting for confirmation
- A confirmation link / command to execute the pending actions

The digest is sent as an HTML email via `gmail.executor.send_email()`. It is addressed to the same account GmailCleaner is authorized on, so no external email infrastructure is needed.

---

### `ai/gemini.py`

Thin wrapper around the Gemini 1.5 Flash API (via Google AI Studio / Vertex AI).

- Uses the `generativelanguage.googleapis.com` REST API directly (no SDK dependency required).
- Sends classification prompts in batches of up to 20 messages per request to reduce API call count.
- Parses responses defensively — if the model returns malformed JSON or unexpected structure, the affected messages are flagged as `UNCLASSIFIED` and included in the digest for manual review.
- Implements exponential backoff for rate limit responses (HTTP 429).

**Gemini 1.5 Flash free tier:** 15 requests per minute, 1 million tokens per day, 1,500 requests per day (as of 2025). For a personal inbox scan, this is more than sufficient.

---

### `store/db.py`

SQLite database for persistent state across runs.

Tables:

```sql
-- Tracks every sender seen, with metadata
CREATE TABLE senders (
    domain TEXT PRIMARY KEY,
    display_name TEXT,
    first_seen DATE,
    last_seen DATE,
    email_count INTEGER,
    unsubscribed_at DATE,
    trusted BOOLEAN DEFAULT FALSE,
    blocked BOOLEAN DEFAULT FALSE
);

-- Full log of every action taken
CREATE TABLE action_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    action_type TEXT,         -- 'unsubscribe', 'delete', 'label', 'skip'
    target TEXT,              -- message ID or sender domain
    status TEXT,              -- 'pending', 'confirmed', 'executed', 'failed'
    executed_at DATETIME,
    error TEXT
);

-- Per-run metadata
CREATE TABLE runs (
    id TEXT PRIMARY KEY,
    started_at DATETIME,
    completed_at DATETIME,
    emails_scanned INTEGER,
    actions_pending INTEGER,
    actions_executed INTEGER
);
```

The DB file lives at `data/gmailcleaner.db` (gitignored). On Cloud Run, it is mounted via a Cloud Storage FUSE volume or replaced with Firestore for production deployments.

---

### `output/reporter.py`

Produces two outputs at the end of each scan:

**1. `pending_actions.json`** — machine-readable, used by the execute phase:
```json
{
  "run_id": "2025-05-16T07:00:00",
  "unsubscribe": [
    { "domain": "newsletter.example.com", "display_name": "Example Weekly", "count": 47 }
  ],
  "delete_groups": [
    { "group_id": "grp_001", "domain": "deals.shop.com", "count": 132, "oldest": "2024-11-01" }
  ],
  "triage": {
    "needs_reply": [...],
    "promotions_count": 84,
    "junk_count": 23
  }
}
```

**2. Console / log output** — human-readable summary printed to stdout (visible in Cloud Run logs).

---

## Cloud Run Deployment

GmailCleaner is packaged as a Docker container and deployed as a **Cloud Run Job** (not a Service). A Job runs to completion and exits — it does not expose an HTTP endpoint or stay running.

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD ["python", "main.py", "--mode=scan"]
```

Cloud Scheduler fires the job on a cron schedule. The job authenticates using a **Google Service Account** (not OAuth) for the Cloud Run infrastructure itself. The Gmail OAuth token is stored as a **Secret Manager secret** and injected at runtime via environment variable — it never lives in the container image.

```
Cloud Scheduler
  └── triggers → Cloud Run Job (GmailCleaner container)
                    ├── reads Gmail OAuth token from Secret Manager
                    ├── runs scan pipeline
                    ├── writes pending_actions.json to Cloud Storage
                    └── sends digest email via Gmail API
```

---

## Phase 4: Gmail Pub/Sub Integration

Instead of polling on a schedule, Gmail can push real-time notifications to a Pub/Sub topic when your inbox changes.

Setup:
1. Create a Pub/Sub topic in your GCP project.
2. Call `gmail.users.watch()` to register the topic with Gmail. Gmail will publish a notification every time a new message arrives or a label changes.
3. Create a Pub/Sub push subscription pointing to a Cloud Run **Service** (a persistent HTTP endpoint, unlike the Job used for scheduled scans).
4. The Cloud Run Service receives the push, fetches only the changed messages, and runs a mini scan on them.

This replaces the daily cron with near-instant processing. The daily digest is still generated by the scheduled Job, but triage and unsubscribe detection happen within seconds of a new email arriving.

---

## Data Flow Summary

```
Gmail Inbox
    │
    ▼
fetcher.py          ← users.messages.list (metadata format, paginated)
    │
    ▼
parser.py           ← normalizes to GmailMessage dataclasses
    │
    ├──────────────────────────────────────────────┐
    ▼                                              ▼
unsubscribe.py                              bulk_cleanup.py
(RFC 2369 header detection)                 (domain + subject grouping)
    │                                              │
    └──────────────────┬────────────────────────────┘
                       ▼
                  triage.py
              (Gemini classification)
                       │
                       ▼
                  reporter.py
              (pending_actions.json)
                       │
                       ▼
                  digest.py
              (sends email to user)
                       │
              ┌────────▼────────┐
              │  USER REVIEWS   │
              │  AND CONFIRMS   │
              └────────┬────────┘
                       ▼
                  executor.py
          (batchDelete / unsubscribe / label)
                       │
                       ▼
                    db.py
                (action_log update)
```

---

## Key Technical Decisions

| Decision | Rationale |
|---|---|
| Cloud Run Job (not a server) | Free tier, zero idle cost, no infra to manage |
| SQLite over Firestore (Phase 1–2) | Zero cost, zero setup, sufficient for single-user use |
| Metadata-only fetch for triage | Privacy (no body sent to AI), quota efficiency |
| RFC 2369 unsubscribe over browser automation | More reliable, no tracking pixel triggers, no browser dependency |
| Gemini Flash over GPT-4 / Claude | Free tier available, Google-native (same GCP project), sufficient for classification |
| Confirm-first as default | Trust and safety — destructive actions require explicit user approval |
| Secret Manager for OAuth token | OAuth tokens are credentials; never bake into container images or env files |

---

## Security Considerations

- The OAuth token grants `gmail.modify` scope — treat it like a password. Store it only in Secret Manager in production.
- The `pending_actions.json` file contains email metadata (sender names, subjects). It is gitignored and should not be committed or shared.
- Cloud Run Jobs run in an isolated container with no inbound network access. The only outbound calls are to the Gmail API, Gemini API, and Secret Manager — all within Google's infrastructure.
- GmailCleaner never stores email body content anywhere.