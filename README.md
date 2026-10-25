# GmailCleaner

> A self-hosted, privacy-first Gmail intelligence engine. No subscriptions. No data selling. You own your inbox.

[![CI](https://github.com/akadeepesh/GmailCleaner/actions/workflows/ci.yml/badge.svg)](https://github.com/akadeepesh/GmailCleaner/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

---

## What is GmailCleaner?

GmailCleaner runs on a schedule against your Gmail inbox. It finds junk, shows you what matters, and asks for your confirmation before it does anything. Nothing is unsubscribed or cleaned up without your explicit approval, unless you opt in to rules that you write yourself.

The philosophy is simple: **show first, act second.**

---

## Features

**Unsubscribe engine.** Detects `List-Unsubscribe` headers (RFC 2369) and one-click unsubscribe (RFC 8058). It proposes one candidate per sender domain and unsubscribes through the most reliable method available: one-click POST, then HTTP, then mailto.

**Bulk cleanup.** Groups clutter by sender domain and by repeating subject patterns (e.g. "Weekly Digest #1, #2, #3…"), then ranks each group by volume, how often it goes unread, and how promotional it looks. Confirmed groups are **moved to Trash**, so they stay recoverable for 30 days.

**AI triage.** Gemini sorts your inbox into **Needs Reply**, **FYI**, **Promotions** and **Junk**. Low-confidence results are flagged for review instead of being guessed. Only metadata is sent: sender, subject, snippet and labels, never the email body.

**Daily digest.** One email per scan: what needs a reply, what's noise, and what's waiting for your approval, with a link straight to the dashboard.

**Web dashboard.** Review and confirm pending actions, execute them with one click, see inbox stats and run history, and download CSV exports.

**Real-time processing.** Gmail Pub/Sub push notifications trigger unsubscribe detection and triage within seconds of new mail arriving.

**Trusted rules.** Pre-approve specific senders (e.g. "always unsubscribe from `news.example.com`") so routine cleanup runs unattended.

**Multi-account.** Manage several Gmail accounts from one install. Each gets its own token, database and report.

**Inbox health export.** Sender, action, run and summary data as CSV, from the CLI or the dashboard.

---

## How it works

```
 scan ─► fetch metadata ─► unsubscribe / cleanup / triage engines ─► digest email
                                                                     │
                                                    pending_actions.json (report)
                                                                     │
                             you confirm (dashboard, JSON or trusted rules)
                                                                     │
 execute ─► one-click / HTTP / mailto unsubscribe · move groups to Trash ─► action log
```

Every scan produces a **pending actions report**. Every item in it starts as `"confirmed": false`. Only confirmed items are executed, and each is executed once.

---

## Quick start (local)

### Prerequisites

- Python 3.11+
- A Google account and a Google Cloud project (the free tier is enough)
- Optional: a [Google AI Studio](https://aistudio.google.com/app/apikey) API key for AI triage and the digest

### 1. Install

```bash
git clone https://github.com/akadeepesh/GmailCleaner.git
cd GmailCleaner
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp env.example .env              # then add GEMINI_API_KEY if you have one
```

### 2. Create OAuth credentials

1. In the [Google Cloud Console](https://console.cloud.google.com), enable the **Gmail API** for your project.
2. Configure the **OAuth consent screen** (External, add yourself as a user), then **publish it to "In production"**. While an app is in *Testing*, Google expires refresh tokens after 7 days, and scheduled runs would start failing. For personal use you don't need verification; you'll just click through an "unverified app" warning once.
3. Under **APIs & Services → Credentials**, create an **OAuth client ID** of type **Desktop app**, download the JSON, and save it as `config/credentials.json`.

### 3. Sign in and scan

```bash
python main.py info     # opens the browser once to authorize, then shows your account
python main.py scan     # writes output/pending_actions.json, changes nothing
```

### 4. Review and execute

Either open the dashboard:

```bash
python -m web.app       # http://127.0.0.1:8080 → Pending Actions
```

…or edit `output/pending_actions.json` and set `"confirmed": true` on the items you approve. Then run:

```bash
python main.py execute
```

---

## Commands

| Command | What it does |
|---|---|
| `python main.py info` | Authorizes on first use, then shows the account and a summary of the active configuration |
| `python main.py scan` | Scans, sends the digest, writes the pending actions report. Changes nothing in Gmail |
| `python main.py scan --no-dry-run` | Same as above, then executes actions approved by trusted rules (needs `confirm_first: false`) |
| `python main.py execute [--session PATH] [--yes]` | Executes confirmed, not-yet-executed actions from the report |
| `python main.py export [--out DIR]` | Writes `summary.csv`, `senders.csv`, `actions.csv` and `runs.csv` |
| `python main.py setup-watch` | Registers Gmail push notifications for local or self-hosted setups |
| `python main.py --version` | Prints the version |

Global options: `--account NAME`, `--config PATH`, `--log-level LEVEL`.

---

## Configuration

All settings live in [`config/settings.yaml`](config/settings.yaml), which documents every option inline. The key ones:

```yaml
scan:
  max_emails: 500                 # per run
  lookback_days: 30
  query_filter: "in:inbox -in:trash -in:spam"

triage:
  enabled: true
  model: gemini-3.5-flash
  confidence_threshold: 0.75      # below this → "Needs Review"

digest:
  enabled: true
  recipient: ""                   # empty = send to the authenticated account

confirm_first: true               # master safety switch
trusted_rules: []                 # see below
blocked_senders: []               # never touched, whatever else is configured
```

Environment variables override the file (see [`env.example`](env.example)). This is how the Cloud Run deployment points the app at its shared storage.

### Trusted rules

Trusted rules let specific actions skip manual confirmation. They are honoured **only** when `confirm_first: false`:

```yaml
confirm_first: false
trusted_rules:
  - action: unsubscribe          # unsubscribe | delete (move to Trash)
    domain: news.example.com     # exact sender domain
  - action: delete
    domain: "*.deals.example"    # the domain and all of its subdomains
  - promo.example.org            # shorthand: both actions for this domain
```

`scan --no-dry-run`, which is what the scheduled job runs, executes matching actions automatically. A plain `scan` only marks them as confirmed so you can see what would happen. Safety limits:

- `blocked_senders` always win.
- A bare `*` wildcard is rejected.
- Subject-pattern cleanup groups span many senders, so they always need manual confirmation.

Auto-confirmed items are labelled "by trusted rule" on the dashboard.

### Multiple accounts

```bash
python main.py --account work info      # authorize the work account
python main.py --account work scan
python main.py --account personal scan
```

Each account keeps its own `config/tokens/<account>.json`, `data/<account>/gmailcleaner.db` and `output/<account>/pending_actions.json`. To run the dashboard for one account, set `GMAILCLEANER_ACCOUNT=work`.

---

## Deploying to Google Cloud

The production setup runs entirely inside your own GCP project and fits within the free tier for personal use:

| Component | Runs as | Purpose |
|---|---|---|
| Scheduled scan | Cloud Run **Job** + Cloud Scheduler | Daily scan, digest, trusted-rule execution |
| Dashboard + webhook | Cloud Run **Service** | Review/execute actions, receive Pub/Sub pushes |
| Shared state | Cloud Storage bucket (mounted as a volume) | SQLite DB and pending actions report |
| Secrets | Secret Manager | OAuth token, Gemini key, dashboard secret |

Prerequisites: the `gcloud` CLI (authenticated), Docker, billing enabled, and `config/token.json` created by `python main.py info`.

```bash
# 1. Scheduled scan (default 07:00 UTC daily)
GCP_PROJECT_ID=my-project \
GEMINI_API_KEY=... \
SCAN_SCHEDULE="0 7 * * *" SCAN_TIMEZONE="Europe/London" \
./deploy/deploy_job.sh

# 2. Dashboard + real-time Pub/Sub (optional but recommended)
GCP_PROJECT_ID=my-project ./deploy/deploy_dashboard.sh
```

Both scripts are idempotent, so re-run them to redeploy. At the end, the dashboard script prints the service URL and how to read the dashboard secret. Open `https://<service-url>/?token=<secret>` once; a secure, HttpOnly cookie keeps you signed in after that.

For continuous delivery, connect [`deploy/cloudbuild.yaml`](deploy/cloudbuild.yaml) to a Cloud Build trigger on `main`. It runs the tests, builds both images and rolls them out.

Useful operations:

```bash
gcloud run jobs execute gmail-cleaner --region=us-central1                      # scan now
gcloud run jobs execute gmail-cleaner --region=us-central1 --args=execute,--yes # execute confirmed
```

---

## Privacy & security

- **Your project, your data.** Everything runs in your own GCP project or on your own machine. There are no shared servers, analytics or telemetry.
- **Metadata only.** Email bodies are never fetched. Gemini receives sender, subject, a short snippet and labels.
- **Recoverable cleanup.** Cleanup moves mail to Trash. GmailCleaner never permanently deletes anything and doesn't request the full-mailbox `https://mail.google.com/` scope.
- **Least-privilege scopes.** `gmail.readonly`, `gmail.modify` (labels and Trash) and `gmail.send` (the digest and mailto unsubscribes).
- **Secrets stay secret.** The OAuth token is stored locally with `0600` permissions or in Secret Manager, and never baked into an image.
- **Authenticated endpoints.** The dashboard requires `DASHBOARD_SECRET` (compared in constant time). The webhook verifies Pub/Sub's signed OIDC token, including its audience and service account.
- Treat `pending_actions.json` and the database as personal data: they contain senders and subjects.

> Running the dashboard without `DASHBOARD_SECRET` disables authentication. Do that only on `127.0.0.1`.

---

## Limitations

- State is a single SQLite file. On Cloud Run it lives on a Cloud Storage volume with one dashboard instance. That's ideal for one person's inbox, but not designed for many concurrent writers.
- Some senders' unsubscribe endpoints require a browser or a login. Those show up as failed actions, and you can unsubscribe manually.
- Real-time processing covers new mail in the Inbox. Bulk cleanup groups are recalculated by the scheduled scan.

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

The test suite runs fully offline: Gmail, Gemini and HTTP are all mocked. See [Architecture.md](Architecture.md) for the internals and [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines.

---

## License

MIT. See [LICENSE](LICENSE).
