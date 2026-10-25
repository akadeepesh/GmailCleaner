# GmailCleaner — Cloud Run Job container (scheduled scans)
#
# Build:        docker build -t gmailcleaner -f deploy/Dockerfile .
# Run locally:  docker run --rm \
#                 -e GOOGLE_TOKEN_JSON="$(cat config/token.json)" \
#                 -e GEMINI_API_KEY=... \
#                 -v "$(pwd)/data:/app/data" \
#                 -e DATABASE_PATH=/app/data/gmailcleaner.db \
#                 -e PENDING_ACTIONS_PATH=/app/data/pending_actions.json \
#                 gmailcleaner scan

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    GMAILCLEANER_HEADLESS=1

# ca-certificates is needed for HTTPS unsubscribe calls.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps first (layer cached unless requirements.txt changes).
COPY requirements.txt .
RUN pip install -r requirements.txt

# Copy source — secrets are never baked in (the OAuth token arrives via
# GOOGLE_TOKEN_JSON from Secret Manager at runtime).
COPY version.py main.py ./
COPY core/       core/
COPY auth/       auth/
COPY ai/         ai/
COPY gmail/      gmail/
COPY engines/    engines/
COPY store/      store/
COPY output/     output/
COPY pubsub/     pubsub/
COPY config/settings.yaml config/settings.yaml

# Non-root user (uid 1000 matches the Cloud Storage volume mount options).
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p data output logs \
    && chown -R appuser:appuser /app
USER appuser

# Scan, send the digest and write the report. With --no-dry-run, actions
# approved by trusted_rules are executed (only when confirm_first: false).
ENTRYPOINT ["python", "main.py"]
CMD ["scan", "--no-dry-run"]
