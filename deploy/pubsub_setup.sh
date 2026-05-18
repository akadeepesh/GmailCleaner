#!/usr/bin/env bash
# GmailCleaner — Phase 4 Pub/Sub & Dashboard Setup
#
# Idempotent: safe to run multiple times.
# Run AFTER deploy/scheduler.sh (Phase 2) has set up the base GCP infrastructure.
#
# What this script does:
#   1. Creates a Pub/Sub topic for Gmail push notifications
#   2. Grants Gmail permission to publish to the topic
#   3. Builds and deploys the dashboard Cloud Run Service
#   4. Creates a Pub/Sub push subscription pointing to the Service webhook URL
#   5. Stores the DASHBOARD_SECRET in Secret Manager
#   6. Creates a Cloud Scheduler job to renew the Gmail watch daily
#
# Prerequisites:
#   - deploy/scheduler.sh has already been run
#   - config/credentials.json and config/token.json exist
#   - Docker is available locally
#
# Usage:
#   chmod +x deploy/pubsub_setup.sh
#   ./deploy/pubsub_setup.sh

set -euo pipefail

# ── Configuration — edit these ─────────────────────────────────────────────
GCP_PROJECT="${GCP_PROJECT_ID:-your-gcp-project-id}"
REGION="${GCP_REGION:-us-central1}"
TOPIC_NAME="gmail-cleaner-notifications"
SUBSCRIPTION_NAME="gmail-cleaner-push-sub"
SERVICE_NAME="gmail-cleaner-dashboard"
REPO_NAME="gmail-cleaner"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO_NAME}/gmailcleaner-web:latest"
SA_NAME="gmail-cleaner-sa"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
SECRET_DASHBOARD="gmail-cleaner-dashboard-secret"
SCHEDULER_WATCH_JOB="gmail-cleaner-watch-renew"
# Generate a random secret if not already set.
DASHBOARD_SECRET="${DASHBOARD_SECRET:-$(openssl rand -hex 24)}"
# ───────────────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  GmailCleaner Phase 4 — Pub/Sub & Dashboard Setup"
echo "  Project : ${GCP_PROJECT}"
echo "  Region  : ${REGION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

gcloud config set project "${GCP_PROJECT}"

# ── Enable additional APIs ─────────────────────────────────────────────────
echo "▸ Enabling Pub/Sub API..."
gcloud services enable pubsub.googleapis.com --quiet
echo "  ✓ Pub/Sub enabled"

# ── Create Pub/Sub topic ───────────────────────────────────────────────────
echo ""
echo "▸ Creating Pub/Sub topic: ${TOPIC_NAME}..."
FULL_TOPIC="projects/${GCP_PROJECT}/topics/${TOPIC_NAME}"

if ! gcloud pubsub topics describe "${TOPIC_NAME}" --quiet 2>/dev/null; then
  gcloud pubsub topics create "${TOPIC_NAME}" --quiet
  echo "  ✓ Topic created: ${FULL_TOPIC}"
else
  echo "  · Topic already exists: ${TOPIC_NAME}"
fi

# Grant Gmail (the Gmail API service account) permission to publish.
# This is required for gmail.users.watch() to work.
GMAIL_SA="gmail-api-push@system.gserviceaccount.com"
gcloud pubsub topics add-iam-policy-binding "${TOPIC_NAME}" \
  --member="serviceAccount:${GMAIL_SA}" \
  --role="roles/pubsub.publisher" \
  --quiet > /dev/null
echo "  ✓ Gmail SA granted Pub/Sub Publisher on topic"

# ── Store dashboard secret ─────────────────────────────────────────────────
echo ""
echo "▸ Storing dashboard secret in Secret Manager..."
if ! gcloud secrets describe "${SECRET_DASHBOARD}" --quiet 2>/dev/null; then
  echo -n "${DASHBOARD_SECRET}" | gcloud secrets create "${SECRET_DASHBOARD}" \
    --data-file=- --quiet
  echo "  ✓ Dashboard secret created"
else
  echo -n "${DASHBOARD_SECRET}" | gcloud secrets versions add "${SECRET_DASHBOARD}" \
    --data-file=- --quiet
  echo "  ✓ Dashboard secret updated"
fi

# Grant service account access to the dashboard secret.
gcloud secrets add-iam-policy-binding "${SECRET_DASHBOARD}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet > /dev/null

# ── Build and push web image ───────────────────────────────────────────────
echo ""
echo "▸ Building and pushing dashboard image..."
docker build -t "${IMAGE}" -f deploy/Dockerfile.web .
docker push "${IMAGE}"
echo "  ✓ Image pushed: ${IMAGE}"

# ── Deploy Cloud Run Service ───────────────────────────────────────────────
echo ""
echo "▸ Deploying Cloud Run Service: ${SERVICE_NAME}..."

gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --set-secrets="\
GOOGLE_CREDENTIALS_JSON=gmail-cleaner-credentials:latest,\
GOOGLE_TOKEN_JSON=gmail-cleaner-token:latest,\
DASHBOARD_SECRET=${SECRET_DASHBOARD}:latest" \
  --allow-unauthenticated \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=1 \
  --max-instances=3 \
  --port=8080 \
  --quiet

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --format="value(status.url)")
echo "  ✓ Service deployed: ${SERVICE_URL}"

# Update settings.yaml with topic name and service URL.
sed -i "s|topic_name: \"\"|topic_name: \"${FULL_TOPIC}\"|" config/settings.yaml
echo "  ✓ Updated config/settings.yaml with topic name"

# ── Create Pub/Sub push subscription ──────────────────────────────────────
echo ""
echo "▸ Creating Pub/Sub push subscription..."
WEBHOOK_URL="${SERVICE_URL}/webhook/pubsub"

if ! gcloud pubsub subscriptions describe "${SUBSCRIPTION_NAME}" --quiet 2>/dev/null; then
  gcloud pubsub subscriptions create "${SUBSCRIPTION_NAME}" \
    --topic="${TOPIC_NAME}" \
    --push-endpoint="${WEBHOOK_URL}" \
    --push-auth-service-account="${SA_EMAIL}" \
    --ack-deadline=60 \
    --message-retention-duration=1h \
    --quiet
  echo "  ✓ Push subscription created → ${WEBHOOK_URL}"
else
  gcloud pubsub subscriptions modify-push-config "${SUBSCRIPTION_NAME}" \
    --push-endpoint="${WEBHOOK_URL}" \
    --push-auth-service-account="${SA_EMAIL}" \
    --quiet
  echo "  · Push subscription updated → ${WEBHOOK_URL}"
fi

# Grant Pub/Sub permission to invoke the Cloud Run Service.
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
  --region="${REGION}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --quiet > /dev/null
echo "  ✓ Invoker permission granted to SA"

# ── Cloud Scheduler: daily watch renewal ──────────────────────────────────
echo ""
echo "▸ Creating watch renewal scheduler job..."
RENEW_URL="${SERVICE_URL}/api/watch/renew"

if ! gcloud scheduler jobs describe "${SCHEDULER_WATCH_JOB}" \
     --location="${REGION}" --quiet 2>/dev/null; then
  gcloud scheduler jobs create http "${SCHEDULER_WATCH_JOB}" \
    --location="${REGION}" \
    --schedule="0 2 * * *" \
    --time-zone="Asia/Kolkata" \
    --uri="${RENEW_URL}" \
    --http-method=POST \
    --oidc-service-account-email="${SA_EMAIL}" \
    --headers="Authorization=Bearer ${DASHBOARD_SECRET}" \
    --quiet
  echo "  ✓ Watch renewal job created (daily at 02:00 IST)"
else
  echo "  · Watch renewal job already exists"
fi

# ── Register the Gmail watch ───────────────────────────────────────────────
echo ""
echo "▸ Registering Gmail Pub/Sub watch..."
python main.py setup-watch || {
  echo "  ⚠ Watch registration failed — run manually after deployment:"
  echo "    python main.py setup-watch"
}

# ── Done ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ Phase 4 setup complete"
echo ""
echo "  Dashboard:     ${SERVICE_URL}"
echo "  Webhook:       ${WEBHOOK_URL}"
echo "  Pub/Sub topic: ${FULL_TOPIC}"
echo ""
echo "  Dashboard secret (save this securely):"
echo "  ${DASHBOARD_SECRET}"
echo ""
echo "  Access the dashboard:"
echo "  ${SERVICE_URL}?token=${DASHBOARD_SECRET}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""