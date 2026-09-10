# GmailCleaner

> A self-hosted, privacy-first Gmail intelligence engine. No subscriptions. No data selling. You own your inbox.

---

## What is GmailCleaner?

GmailCleaner is an open-source tool that runs on a schedule against your Gmail inbox. It identifies junk, surfaces what matters, and asks for your confirmation before taking any action. Nothing is deleted or unsubscribed without your explicit approval.

This is the philosophy of the project: **show first, act second**.

---

## Features

### Unsubscribe Engine
Detects emails with `List-Unsubscribe` headers and extracts the unsubscribe method (HTTP link or mailto). Presents you a list of senders you can choose to unsubscribe from — one click per sender, or confirm all at once.

### Bulk Cleanup
Groups emails by sender domain and subject patterns to identify repetitive clutter. Shows you the groups with counts and previews. You choose which groups to delete.

### Smart Triage
Classifies your inbox using AI into priority buckets: **Needs Reply**, **FYI / Read Later**, **Promotions**, and **Junk**. You review the classification before any labels are applied or emails are moved.

### Daily Digest
Every morning, you receive a single email summarizing your inbox: what needs attention, what arrived overnight, and what GmailCleaner is queued to act on pending your approval.

---

## Confirm-First Model

Every action in GmailCleaner goes through a confirmation step. When the job runs, it produces a **pending actions report** — a structured list of what it wants to do. You review and approve. Only then does it execute.

In future releases, users will be able to configure trusted rules (e.g. "always delete from this domain", "auto-unsubscribe from anything tagged Promotions") to skip confirmation for actions they are comfortable automating.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Gmail Access | Gmail API via Google OAuth 2.0 |
| AI Classification | Gemini 1.5 Flash (free tier) via Google AI Studio |
| Scheduling | Google Cloud Scheduler (triggers Cloud Run jobs) |
| Hosting | Google Cloud Run (serverless, free tier eligible) |
| State / Memory | SQLite (local) or Cloud Firestore (GCP, optional) |
| Notifications | Gmail API (sends digest to your own inbox) |

---

## Project Phases

### Phase 1 — Gmail OAuth + Unsubscribe Engine
Connect to Gmail, read inbox, detect `List-Unsubscribe` headers, generate a dry-run report of unsubscribe candidates. No actions are taken yet.

**What you learn:** Gmail API scopes, OAuth 2.0 authorization flow, refresh tokens, reading email headers, RFC 2369.

### Phase 2 — Bulk Cleanup + GCP Deployment
Build sender-grouping logic, add the delete workflow (confirm-first), and deploy to Google Cloud Run with Cloud Scheduler as the trigger.

**What you learn:** Gmail API batch requests, threading model, GCP project setup, Cloud Run jobs, Cloud Scheduler cron syntax, IAM basics.

### Phase 3 — AI Triage + Daily Digest
Integrate Gemini to classify emails, generate triage buckets, and produce the daily digest email.

**What you learn:** Gemini API, prompt engineering for classification tasks, structured output parsing, composing and sending email via Gmail API.

### Phase 4 — Real-time Processing + Web Dashboard
Replace polling with Gmail Pub/Sub push notifications. Add a lightweight dashboard to review pending actions, configure rules, and see inbox stats.

**What you learn:** Google Cloud Pub/Sub, webhook handling, basic web UI deployment on Cloud Run.

---

## Getting Started

### Prerequisites

- A Google account
- Google Cloud project (free tier is sufficient for all phases)
- Python 3.11+ or Node.js 20+
- `gcloud` CLI installed and authenticated

### 1. Clone the repository

```bash
git clone https://github.com/yourusername/GmailCleaner.git
cd GmailCleaner
```

### 2. Set up Google Cloud

```bash
# Authenticate
gcloud auth login

# Create a new project
gcloud projects create gmail-cleaner --name="GmailCleaner"
gcloud config set project gmail-cleaner

# Enable required APIs
gcloud services enable gmail.googleapis.com
gcloud services enable run.googleapis.com
gcloud services enable cloudscheduler.googleapis.com
```

### 3. Configure OAuth credentials

- Go to [Google Cloud Console](https://console.cloud.google.com) → APIs & Services → Credentials
- Create an **OAuth 2.0 Client ID** (Desktop app for local dev, Web app for Cloud Run)
- Download the credentials JSON and place it at `config/credentials.json`

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Run locally in dry-run mode

```bash
python main.py scan
```

This authenticates, scans your inbox, and writes `output/pending_actions.json` — a report of what GmailCleaner would do — without touching anything. `confirm_first: true` in `config/settings.yaml` is the master safety switch and is on by default.

Other useful commands:

```bash
python main.py info            # show authenticated account + config summary
python main.py setup-watch     # register Gmail Pub/Sub push notifications (Phase 4)
```

### 6. Review and confirm

Open `output/pending_actions.json`, set `"confirmed": true` on the unsubscribe/delete items you approve, then run:

```bash
python main.py execute --session output/pending_actions.json
```

---

## Configuration

All configuration lives in `config/settings.yaml`:

```yaml
scan:
  max_emails: 500           # How many emails to scan per run
  lookback_days: 30         # How far back to look

triage:
  enabled: true
  model: gemini-1.5-flash

digest:
  enabled: true
  send_time: "07:00"        # Digest delivery time (your local timezone)
  recipient: your@gmail.com

confirm_first: true         # Master switch — set false to enable auto-execution (advanced users)

trusted_senders: []         # Senders GmailCleaner may auto-act on without confirmation
blocked_senders: []         # Senders to always skip / never touch
```

---

## Privacy

- GmailCleaner runs entirely inside your own GCP project — no shared servers.
- AI classification sends only subject lines and sender names to Gemini — never email body content.
- OAuth tokens are stored locally in your environment and never leave it.
- No analytics, no telemetry, no data collection of any kind.

---

## Project Status

**All four phases are implemented and passing tests (254/254).** This is further along than the checklist below used to suggest — code for OAuth, unsubscribe detection, bulk cleanup, GCP deployment, Gemini-based triage, the daily digest, Pub/Sub real-time watching, and the Flask web dashboard all exist and run.

- [x] Phase 1: OAuth + Unsubscribe detection (dry-run)
- [x] Phase 2: Bulk cleanup + Cloud Run deployment
- [x] Phase 3: AI triage (Gemini) + daily digest
- [x] Phase 4: Pub/Sub real-time + web dashboard
- [ ] Trusted rules engine (config field `trusted_rules` and `confirm_first` exist and are read by the executor, but no code path yet acts on `trusted_rules` to bypass confirmation automatically — currently every action still requires manual confirmation)
- [ ] Multi-account support
- [ ] Inbox health export (PDF / CSV)

### Known gaps / before you deploy this publicly

- `gmail/executor.py` — the module that performs **permanent, un-trashed deletion** via `batchDelete` and sends unsubscribe requests — has an empty test file (`tests/test_executer.py`). Everything else has real test coverage; this doesn't yet.
- `config/settings.yaml` ships with a real personal email hardcoded as the digest `recipient`. Replace it with your own address (or a placeholder) before committing/sharing this repo.
- Two bugs were found and fixed during review: the Flask app's `template_folder` pointed at a nonexistent `templates/` subfolder (the entire web dashboard 404'd), and the subject-clustering regex in the bulk cleanup engine didn't correctly strip `#123`-style issue numbers, so numbered newsletter series (e.g. "Weekly Digest #1", "#2", "#3") failed to group. Both are fixed as of this commit.

---

## Contributing

Contributions are welcome. Please open an issue before submitting a PR so the change can be discussed first. See `CONTRIBUTING.md` for guidelines.

---

## License

MIT License. See `LICENSE` for details.