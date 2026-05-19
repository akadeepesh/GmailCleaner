#!/usr/bin/env bash
# GmailCleaner — GCP Setup & Deployment Script
#
# Idempotent: safe to run multiple times. Already-existing resources are skipped.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated (gcloud auth login)
#   - Billing enabled on the GCP project
#   - OAuth credentials JSON downloaded to config/credentials.json
#   - Gmail OAuth token generated locally (run: python main.py info)
#
# Usage:
#   chmod +x deploy/scheduler.sh
#   ./deploy/scheduler.sh

set -euo pipefail

# ── Configuration — edit these ─────────────────────────────────────────────
GCP_PROJECT="${GCP_PROJECT_ID:-gmailcleaner-496510}"
REGION="${GCP_REGION:-us-central1}"
JOB_NAME="gmail-cleaner"
SCHEDULER_JOB_NAME="gmail-cleaner-daily"
REPO_NAME="gmail-cleaner"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO_NAME}/gmailcleaner:latest"
# Cron: 7:00 AM IST = 01:30 UTC daily.
CRON_SCHEDULE="30 1 * * *"
TIMEZONE="Asia/Kolkata"
# Secret Manager secret names.
SECRET_CREDENTIALS="gmail-cleaner-credentials"
SECRET_TOKEN="gmail-cleaner-token"
# Service account for Cloud Run.
SA_NAME="gmail-cleaner-sa"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
# ───────────────────────────────────────────────────────────────────────────

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  GmailCleaner — GCP Setup"
echo "  Project : ${GCP_PROJECT}"
echo "  Region  : ${REGION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

gcloud config set project "${GCP_PROJECT}"

# ── Enable APIs ────────────────────────────────────────────────────────────
echo "▸ Enabling required APIs..."
gcloud services enable \
  gmail.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  --quiet

echo "  ✓ APIs enabled"

# ── Service account ────────────────────────────────────────────────────────
echo ""
echo "▸ Creating service account..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --quiet 2>/dev/null; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="GmailCleaner Service Account" \
    --quiet
  echo "  ✓ Service account created: ${SA_EMAIL}"
else
  echo "  · Service account already exists: ${SA_EMAIL}"
fi

# Grant Secret Manager access.
gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/secretmanager.secretAccessor" \
  --quiet > /dev/null

echo "  ✓ IAM binding set (secretmanager.secretAccessor)"

# ── Artifact Registry ──────────────────────────────────────────────────────
echo ""
echo "▸ Creating Artifact Registry repository..."
if ! gcloud artifacts repositories describe "${REPO_NAME}" \
     --location="${REGION}" --quiet 2>/dev/null; then
  gcloud artifacts repositories create "${REPO_NAME}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="GmailCleaner container images" \
    --quiet
  echo "  ✓ Repository created: ${REPO_NAME}"
else
  echo "  · Repository already exists: ${REPO_NAME}"
fi

gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# ── Secret Manager — OAuth credentials ────────────────────────────────────
echo ""
echo "▸ Uploading secrets to Secret Manager..."

if [ ! -f "config/credentials.json" ]; then
  echo "  ✗ config/credentials.json not found."
  echo "    Download it from Google Cloud Console → APIs & Services → Credentials"
  exit 1
fi

if ! gcloud secrets describe "${SECRET_CREDENTIALS}" --quiet 2>/dev/null; then
  gcloud secrets create "${SECRET_CREDENTIALS}" \
    --data-file="config/credentials.json" \
    --quiet
  echo "  ✓ Credentials secret created"
else
  gcloud secrets versions add "${SECRET_CREDENTIALS}" \
    --data-file="config/credentials.json" \
    --quiet
  echo "  ✓ Credentials secret updated"
fi

if [ ! -f "config/token.json" ]; then
  echo "  ✗ config/token.json not found."
  echo "    Run: python main.py info  (this triggers the OAuth flow and saves the token)"
  exit 1
fi

if ! gcloud secrets describe "${SECRET_TOKEN}" --quiet 2>/dev/null; then
  gcloud secrets create "${SECRET_TOKEN}" \
    --data-file="config/token.json" \
    --quiet
  echo "  ✓ Token secret created"
else
  gcloud secrets versions add "${SECRET_TOKEN}" \
    --data-file="config/token.json" \
    --quiet
  echo "  ✓ Token secret updated"
fi

# ── Build & push container ─────────────────────────────────────────────────
echo ""
echo "▸ Building and pushing container image..."
docker build -t "${IMAGE}" -f deploy/Dockerfile .
docker push "${IMAGE}"
echo "  ✓ Image pushed: ${IMAGE}"

# ── Cloud Run Job ──────────────────────────────────────────────────────────
echo ""
echo "▸ Creating/updating Cloud Run Job..."
gcloud run jobs create "${JOB_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --set-secrets="GOOGLE_CREDENTIALS_JSON=${SECRET_CREDENTIALS}:latest,GOOGLE_TOKEN_JSON=${SECRET_TOKEN}:latest" \
  --memory=512Mi \
  --cpu=1 \
  --max-retries=1 \
  --task-timeout=600 \
  --quiet 2>/dev/null \
|| gcloud run jobs update "${JOB_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --quiet

echo "  ✓ Cloud Run Job: ${JOB_NAME}"

# ── Cloud Scheduler ────────────────────────────────────────────────────────
echo ""
echo "▸ Creating/updating Cloud Scheduler job..."

# Scheduler needs the Cloud Run invoker role on the SA.
gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --quiet > /dev/null

SCHEDULER_URI="https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${GCP_PROJECT}/jobs/${JOB_NAME}:run"

if ! gcloud scheduler jobs describe "${SCHEDULER_JOB_NAME}" \
     --location="${REGION}" --quiet 2>/dev/null; then
  gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" \
    --location="${REGION}" \
    --schedule="${CRON_SCHEDULE}" \
    --time-zone="${TIMEZONE}" \
    --uri="${SCHEDULER_URI}" \
    --http-method=POST \
    --oauth-service-account-email="${SA_EMAIL}" \
    --quiet
  echo "  ✓ Scheduler job created: ${CRON_SCHEDULE} ${TIMEZONE}"
else
  echo "  · Scheduler job already exists: ${SCHEDULER_JOB_NAME}"
fi

# ── Done ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ GmailCleaner deployed successfully"
echo ""
echo "  Cloud Run Job:   ${JOB_NAME} (${REGION})"
echo "  Schedule:        ${CRON_SCHEDULE} (${TIMEZONE})"
echo ""
echo "  To trigger a manual run:"
echo "  gcloud run jobs execute ${JOB_NAME} --region=${REGION}"
echo ""
echo "  To view logs:"
echo "  gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME}' --limit=50 --format='value(textPayload)'"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""