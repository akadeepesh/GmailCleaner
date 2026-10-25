# Contributing to GmailCleaner

Thanks for your interest in improving GmailCleaner. This guide covers how to get set up and what a good change looks like.

## Before you start

Open an issue first for anything bigger than a small fix, so the approach can be agreed before you write code. Bug reports are most useful when they include the command you ran, the relevant log output (with email addresses and subjects redacted), and your Python version.

## Development setup

```bash
git clone https://github.com/akadeepesh/GmailCleaner.git
cd GmailCleaner
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp env.example .env
python -m pytest
```

The test suite needs no Google account, network access or credentials: every Gmail, Gemini and HTTP call is mocked.

## Ground rules

These protect users' inboxes and privacy. Changes that break them won't be merged.

- **Confirm first.** Nothing may unsubscribe, trash or otherwise modify mail unless the user confirmed it or it matches a trusted rule while `confirm_first: false`. New action types must go through `gmail/executor.py` and the pending actions report.
- **Recoverable by default.** Cleanup moves mail to Trash. Do not add permanent deletion paths.
- **No message bodies.** Only metadata (sender, subject, snippet, labels) may be fetched, stored or sent to the AI model.
- **No telemetry.** No analytics, tracking or calls to services the user hasn't configured.
- **Secrets stay out of images and git.** Tokens arrive via files that are gitignored or via `GOOGLE_TOKEN_JSON`.

## Making a change

1. Branch from `main`.
2. Match the style of the surrounding code: type hints, docstrings on public functions, and module docstrings that explain the design.
3. Add or update tests in `tests/` for every behaviour change. Mock external APIs and never write outside `tmp_path`.
4. Run `python -m pytest` and make sure everything passes.
5. Update `README.md` / `Architecture.md` if you change commands, configuration or behaviour.
6. Open a pull request describing what changed and why, and link the issue.

## Project layout

See [Architecture.md](Architecture.md) for how the modules fit together.
