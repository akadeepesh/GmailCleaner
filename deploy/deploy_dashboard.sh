#!/usr/bin/env bash
# GmailCleaner — Deploy the web dashboard + real-time Gmail Pub/Sub processing
#
# Idempotent: safe to run multiple times.
# Run AFTER deploy/deploy_job.sh (it creates the service account, bucket,
# Artifact Registry repo and token secret this script reuses).
#
# What this script does:
#   1. Creates a Pub/Sub topic and lets Gmail publish to it
#   2. Stores (or reuses) the dashboard secret in Secret Manager
#   3. Builds and deploys the dashboard Cloud Run Service on the shared state bucket
#   4. Creates a Pub/Sub push subscription with OIDC auth to /webhook/pubsub
#   5. Creates a Cloud Scheduler job that renews the Gmail watch daily
#   6. Registers the Gmail watch through the deployed service
#   7. Points the scheduled job's digest at the dashboard URL
#
# Usage:
#   GCP_PROJECT_ID=my-project ./deploy/deploy_dashboard.sh
#
# Optional environment variables:
#   GCP_REGION        (default: us-central1)
#   DASHBOARD_SECRET  set to rotate the dashboard secret (default: reuse existing,
#                     or generate a new random one on first run)

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────
GCP_PROJECT="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID to your Google Cloud project ID}"
REGION="${GCP_REGION:-us-central1}"
TOPIC_NAME="gmail-cleaner-notifications"
SUBSCRIPTION_NAME="gmail-cleaner-push-sub"
SERVICE_NAME="gmail-cleaner-dashboard"
JOB_NAME="gmail-cleaner"
REPO_NAME="gmail-cleaner"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO_NAME}/gmailcleaner-web:latest"
BUCKET="${GCP_PROJECT}-gmailcleaner-data"
SA_NAME="gmail-cleaner-sa"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
SECRET_TOKEN="gmail-cleaner-token"
SECRET_GEMINI="gmail-cleaner-gemini-key"
SECRET_DASHBOARD="gmail-cleaner-dashboard-secret"
SCHEDULER_WATCH_JOB="gmail-cleaner-watch-renew"
FULL_TOPIC="projects/${GCP_PROJECT}/topics/${TOPIC_NAME}"
# ───────────────────────────────────────────────────────────────────────────

cd "$(dirname "$0")/.."

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  GmailCleaner — Dashboard & real-time deployment"
echo "  Project : ${GCP_PROJECT}"
echo "  Region  : ${REGION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

gcloud config set project "${GCP_PROJECT}" --quiet

if ! gcloud iam service-accounts describe "${SA_EMAIL}" --quiet >/dev/null 2>&1; then
  echo "  ✗ Service account ${SA_EMAIL} not found. Run ./deploy/deploy_job.sh first."
  exit 1
fi

# ── Pub/Sub topic ──────────────────────────────────────────────────────────
echo "▸ Pub/Sub topic ${TOPIC_NAME}..."
gcloud services enable pubsub.googleapis.com --quiet
if ! gcloud pubsub topics describe "${TOPIC_NAME}" --quiet >/dev/null 2>&1; then
  gcloud pubsub topics create "${TOPIC_NAME}" --quiet
  echo "  ✓ Topic created"
else
  echo "  · Topic exists"
fi

# Gmail publishes push notifications as this Google-managed account.
gcloud pubsub topics add-iam-policy-binding "${TOPIC_NAME}" \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher" \
  --quiet >/dev/null
echo "  ✓ Gmail granted Pub/Sub Publisher on topic"

# Pub/Sub must be able to mint OIDC tokens for the push subscription.
PROJECT_NUMBER=$(gcloud projects describe "${GCP_PROJECT}" --format="value(projectNumber)")
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator" \
  --quiet >/dev/null
echo "  ✓ Pub/Sub service agent can sign push tokens"

# ── Dashboard secret ───────────────────────────────────────────────────────
echo ""
echo "▸ Dashboard secret..."
if [ -n "${DASHBOARD_SECRET:-}" ]; then
  SECRET_SOURCE="provided"
elif gcloud secrets describe "${SECRET_DASHBOARD}" --quiet >/dev/null 2>&1; then
  DASHBOARD_SECRET=$(gcloud secrets versions access latest --secret="${SECRET_DASHBOARD}")
  SECRET_SOURCE="existing"
else
  DASHBOARD_SECRET=$(openssl rand -hex 24)
  SECRET_SOURCE="generated"
fi

if ! gcloud secrets describe "${SECRET_DASHBOARD}" --quiet >/dev/null 2>&1; then
  printf '%s' "${DASHBOARD_SECRET}" | gcloud secrets create "${SECRET_DASHBOARD}" \
    --replication-policy=automatic --data-file=- --quiet
elif [ "${SECRET_SOURCE}" = "provided" ]; then
  printf '%s' "${DASHBOARD_SECRET}" | gcloud secrets versions add "${SECRET_DASHBOARD}" \
    --data-file=- --quiet
fi
gcloud secrets add-iam-policy-binding "${SECRET_DASHBOARD}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet >/dev/null
echo "  ✓ Dashboard secret ready (${SECRET_SOURCE})"

SECRETS="GOOGLE_TOKEN_JSON=${SECRET_TOKEN}:latest,DASHBOARD_SECRET=${SECRET_DASHBOARD}:latest"
if gcloud secrets describe "${SECRET_GEMINI}" --quiet >/dev/null 2>&1; then
  SECRETS="${SECRETS},GEMINI_API_KEY=${SECRET_GEMINI}:latest"
fi

# ── Build & deploy service ─────────────────────────────────────────────────
echo ""
echo "▸ Building and pushing dashboard image..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
docker build -t "${IMAGE}" -f deploy/Dockerfile.web .
docker push "${IMAGE}"
echo "  ✓ Pushed ${IMAGE}"

echo ""
echo "▸ Deploying Cloud Run Service ${SERVICE_NAME}..."
# The dashboard enforces its own token auth and the webhook verifies Pub/Sub's
# OIDC JWT, so the service accepts unauthenticated traffic at the IAM layer.
# max-instances=1 keeps a single writer on the shared SQLite file.
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --set-secrets="${SECRETS}" \
  --set-env-vars="DATABASE_PATH=/app/data/gmailcleaner.db,PENDING_ACTIONS_PATH=/app/data/pending_actions.json,SQLITE_JOURNAL_MODE=DELETE,PUBSUB_TOPIC=${FULL_TOPIC},PUBSUB_SERVICE_ACCOUNT=${SA_EMAIL}" \
  --add-volume="name=state,type=cloud-storage,bucket=${BUCKET},mount-options=uid=1000;gid=1000" \
  --add-volume-mount="volume=state,mount-path=/app/data" \
  --execution-environment=gen2 \
  --allow-unauthenticated \
  --memory=512Mi \
  --cpu=1 \
  --min-instances=0 \
  --max-instances=1 \
  --timeout=300 \
  --port=8080 \
  --quiet

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" --format="value(status.url)")
WEBHOOK_URL="${SERVICE_URL}/webhook/pubsub"

# The push JWT's audience is the webhook URL — tell the service to require it.
gcloud run services update "${SERVICE_NAME}" \
  --region="${REGION}" \
  --update-env-vars="PUBSUB_AUDIENCE=${WEBHOOK_URL}" \
  --quiet >/dev/null
echo "  ✓ Service deployed: ${SERVICE_URL}"

# ── Push subscription ──────────────────────────────────────────────────────
echo ""
echo "▸ Pub/Sub push subscription..."
if ! gcloud pubsub subscriptions describe "${SUBSCRIPTION_NAME}" --quiet >/dev/null 2>&1; then
  gcloud pubsub subscriptions create "${SUBSCRIPTION_NAME}" \
    --topic="${TOPIC_NAME}" \
    --push-endpoint="${WEBHOOK_URL}" \
    --push-auth-service-account="${SA_EMAIL}" \
    --push-auth-token-audience="${WEBHOOK_URL}" \
    --ack-deadline=60 \
    --message-retention-duration=1h \
    --quiet
  echo "  ✓ Created → ${WEBHOOK_URL}"
else
  gcloud pubsub subscriptions modify-push-config "${SUBSCRIPTION_NAME}" \
    --push-endpoint="${WEBHOOK_URL}" \
    --push-auth-service-account="${SA_EMAIL}" \
    --push-auth-token-audience="${WEBHOOK_URL}" \
    --quiet
  echo "  ✓ Updated → ${WEBHOOK_URL}"
fi

# ── Daily watch renewal ────────────────────────────────────────────────────
echo ""
echo "▸ Watch renewal scheduler job..."
RENEW_ARGS=(
  --location="${REGION}"
  --schedule="0 3 * * *"
  --time-zone="Etc/UTC"
  --uri="${SERVICE_URL}/api/watch/renew"
  --http-method=POST
  --quiet
)
if ! gcloud scheduler jobs describe "${SCHEDULER_WATCH_JOB}" \
     --location="${REGION}" --quiet >/dev/null 2>&1; then
  gcloud scheduler jobs create http "${SCHEDULER_WATCH_JOB}" "${RENEW_ARGS[@]}" \
    --headers="Authorization=Bearer ${DASHBOARD_SECRET}"
  echo "  ✓ Created (daily 03:00 UTC)"
else
  gcloud scheduler jobs update http "${SCHEDULER_WATCH_JOB}" "${RENEW_ARGS[@]}" \
    --update-headers="Authorization=Bearer ${DASHBOARD_SECRET}"
  echo "  ✓ Updated (daily 03:00 UTC)"
fi

# ── Register the watch (via the service, so state lands in the shared DB) ─
echo ""
echo "▸ Registering Gmail watch..."
if curl -fsS -X POST -H "Authorization: Bearer ${DASHBOARD_SECRET}" \
     "${SERVICE_URL}/api/watch/renew" >/dev/null; then
  echo "  ✓ Gmail watch registered"
else
  echo "  ⚠ Watch registration failed. Retry with:"
  echo "    gcloud scheduler jobs run ${SCHEDULER_WATCH_JOB} --location=${REGION}"
fi

# ── Link the digest to the dashboard ───────────────────────────────────────
if gcloud run jobs describe "${JOB_NAME}" --region="${REGION}" --quiet >/dev/null 2>&1; then
  gcloud run jobs update "${JOB_NAME}" \
    --region="${REGION}" \
    --update-env-vars="DASHBOARD_URL=${SERVICE_URL}" \
    --quiet >/dev/null
  echo "  ✓ Digest emails now link to the dashboard"
fi

# ── Done ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ Dashboard & real-time processing deployed"
echo ""
echo "  Dashboard:     ${SERVICE_URL}"
echo "  Webhook:       ${WEBHOOK_URL}"
echo "  Pub/Sub topic: ${FULL_TOPIC}"
echo ""
echo "  Open the dashboard once with your secret to sign in (a cookie keeps you signed in):"
echo "    ${SERVICE_URL}/?token=<secret>"
echo "  Read the secret with:"
echo "    gcloud secrets versions access latest --secret=${SECRET_DASHBOARD}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
