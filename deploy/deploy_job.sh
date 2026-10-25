#!/usr/bin/env bash
# GmailCleaner — Deploy the scheduled scan (Cloud Run Job + Cloud Scheduler)
#
# Idempotent: safe to run multiple times. Re-running redeploys the latest code.
#
# What this script does:
#   1. Enables the required Google Cloud APIs
#   2. Creates a service account, Artifact Registry repo and a Cloud Storage
#      bucket that holds the shared SQLite DB and pending actions report
#   3. Stores the Gmail OAuth token (and Gemini API key, if set) in Secret Manager
#   4. Builds and pushes the job image, then deploys the Cloud Run Job
#   5. Creates/updates the Cloud Scheduler trigger
#
# Prerequisites:
#   - gcloud CLI installed and authenticated (gcloud auth login), Docker running
#   - Billing enabled on the GCP project
#   - config/token.json generated locally (run: python main.py info)
#
# Usage:
#   GCP_PROJECT_ID=my-project GEMINI_API_KEY=... ./deploy/deploy_job.sh
#
# Optional environment variables:
#   GCP_REGION      (default: us-central1)
#   SCAN_SCHEDULE   cron expression (default: "0 7 * * *" — 07:00 daily)
#   SCAN_TIMEZONE   IANA timezone for the schedule (default: Etc/UTC)
#   GEMINI_API_KEY  enables AI triage + digest in the cloud job

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────────────
GCP_PROJECT="${GCP_PROJECT_ID:?Set GCP_PROJECT_ID to your Google Cloud project ID}"
REGION="${GCP_REGION:-us-central1}"
JOB_NAME="gmail-cleaner"
SCHEDULER_JOB_NAME="gmail-cleaner-daily"
REPO_NAME="gmail-cleaner"
IMAGE="${REGION}-docker.pkg.dev/${GCP_PROJECT}/${REPO_NAME}/gmailcleaner:latest"
CRON_SCHEDULE="${SCAN_SCHEDULE:-0 7 * * *}"
TIMEZONE="${SCAN_TIMEZONE:-Etc/UTC}"
BUCKET="${GCP_PROJECT}-gmailcleaner-data"
SECRET_TOKEN="gmail-cleaner-token"
SECRET_GEMINI="gmail-cleaner-gemini-key"
SA_NAME="gmail-cleaner-sa"
SA_EMAIL="${SA_NAME}@${GCP_PROJECT}.iam.gserviceaccount.com"
# ───────────────────────────────────────────────────────────────────────────

cd "$(dirname "$0")/.."

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  GmailCleaner — Scheduled scan deployment"
echo "  Project : ${GCP_PROJECT}"
echo "  Region  : ${REGION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

if [ ! -f "config/token.json" ]; then
  echo "  ✗ config/token.json not found."
  echo "    Run: python main.py info  (this runs the OAuth consent flow and saves the token)"
  exit 1
fi

gcloud config set project "${GCP_PROJECT}" --quiet

# ── Enable APIs ────────────────────────────────────────────────────────────
echo "▸ Enabling required APIs..."
gcloud services enable \
  gmail.googleapis.com \
  run.googleapis.com \
  cloudscheduler.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  storage.googleapis.com \
  --quiet
echo "  ✓ APIs enabled"

# ── Service account ────────────────────────────────────────────────────────
echo ""
echo "▸ Service account..."
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --quiet >/dev/null 2>&1; then
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="GmailCleaner Service Account" --quiet
  echo "  ✓ Created ${SA_EMAIL}"
else
  echo "  · Exists: ${SA_EMAIL}"
fi

# Lets Cloud Scheduler (running as this SA) start the job.
gcloud projects add-iam-policy-binding "${GCP_PROJECT}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.invoker" \
  --condition=None --quiet >/dev/null
echo "  ✓ roles/run.invoker granted"

# ── Cloud Storage bucket (shared state) ────────────────────────────────────
echo ""
echo "▸ State bucket gs://${BUCKET}..."
if ! gcloud storage buckets describe "gs://${BUCKET}" --quiet >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention \
    --quiet
  echo "  ✓ Bucket created"
else
  echo "  · Bucket exists"
fi
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectUser" \
  --quiet >/dev/null
echo "  ✓ roles/storage.objectUser granted on bucket"

# ── Artifact Registry ──────────────────────────────────────────────────────
echo ""
echo "▸ Artifact Registry repository..."
if ! gcloud artifacts repositories describe "${REPO_NAME}" \
     --location="${REGION}" --quiet >/dev/null 2>&1; then
  gcloud artifacts repositories create "${REPO_NAME}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="GmailCleaner container images" \
    --quiet
  echo "  ✓ Repository created"
else
  echo "  · Repository exists"
fi
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

# ── Secrets ────────────────────────────────────────────────────────────────
upsert_secret() {  # upsert_secret NAME FILE
  if ! gcloud secrets describe "$1" --quiet >/dev/null 2>&1; then
    gcloud secrets create "$1" --replication-policy=automatic --data-file="$2" --quiet
  else
    gcloud secrets versions add "$1" --data-file="$2" --quiet
  fi
  gcloud secrets add-iam-policy-binding "$1" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" \
    --quiet >/dev/null
}

echo ""
echo "▸ Secret Manager..."
upsert_secret "${SECRET_TOKEN}" "config/token.json"
echo "  ✓ OAuth token stored in ${SECRET_TOKEN}"

SECRETS="GOOGLE_TOKEN_JSON=${SECRET_TOKEN}:latest"
if [ -n "${GEMINI_API_KEY:-}" ]; then
  printf '%s' "${GEMINI_API_KEY}" > /tmp/gmailcleaner-gemini.$$
  upsert_secret "${SECRET_GEMINI}" "/tmp/gmailcleaner-gemini.$$"
  rm -f /tmp/gmailcleaner-gemini.$$
  echo "  ✓ Gemini API key stored in ${SECRET_GEMINI}"
fi
if gcloud secrets describe "${SECRET_GEMINI}" --quiet >/dev/null 2>&1; then
  SECRETS="${SECRETS},GEMINI_API_KEY=${SECRET_GEMINI}:latest"
else
  echo "  · No Gemini key — AI triage and the digest are skipped in the cloud job"
fi

# ── Build & push ───────────────────────────────────────────────────────────
echo ""
echo "▸ Building and pushing job image..."
docker build -t "${IMAGE}" -f deploy/Dockerfile .
docker push "${IMAGE}"
echo "  ✓ Pushed ${IMAGE}"

# ── Cloud Run Job ──────────────────────────────────────────────────────────
echo ""
echo "▸ Deploying Cloud Run Job ${JOB_NAME}..."
gcloud run jobs deploy "${JOB_NAME}" \
  --image="${IMAGE}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --set-secrets="${SECRETS}" \
  --set-env-vars="DATABASE_PATH=/app/data/gmailcleaner.db,PENDING_ACTIONS_PATH=/app/data/pending_actions.json,SQLITE_JOURNAL_MODE=DELETE" \
  --add-volume="name=state,type=cloud-storage,bucket=${BUCKET},mount-options=uid=1000;gid=1000" \
  --add-volume-mount="volume=state,mount-path=/app/data" \
  --memory=512Mi \
  --cpu=1 \
  --max-retries=1 \
  --task-timeout=900 \
  --quiet
echo "  ✓ Job deployed"

# ── Cloud Scheduler ────────────────────────────────────────────────────────
echo ""
echo "▸ Cloud Scheduler trigger..."
SCHEDULER_URI="https://run.googleapis.com/v2/projects/${GCP_PROJECT}/locations/${REGION}/jobs/${JOB_NAME}:run"
SCHEDULER_ARGS=(
  --location="${REGION}"
  --schedule="${CRON_SCHEDULE}"
  --time-zone="${TIMEZONE}"
  --uri="${SCHEDULER_URI}"
  --http-method=POST
  --oauth-service-account-email="${SA_EMAIL}"
  --quiet
)
if ! gcloud scheduler jobs describe "${SCHEDULER_JOB_NAME}" \
     --location="${REGION}" --quiet >/dev/null 2>&1; then
  gcloud scheduler jobs create http "${SCHEDULER_JOB_NAME}" "${SCHEDULER_ARGS[@]}"
  echo "  ✓ Created: ${CRON_SCHEDULE} (${TIMEZONE})"
else
  gcloud scheduler jobs update http "${SCHEDULER_JOB_NAME}" "${SCHEDULER_ARGS[@]}"
  echo "  ✓ Updated: ${CRON_SCHEDULE} (${TIMEZONE})"
fi

# ── Done ───────────────────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ✓ Scheduled scan deployed"
echo ""
echo "  Job:       ${JOB_NAME} (${REGION})"
echo "  Schedule:  ${CRON_SCHEDULE} (${TIMEZONE})"
echo "  State:     gs://${BUCKET}"
echo ""
echo "  Run now:       gcloud run jobs execute ${JOB_NAME} --region=${REGION}"
echo "  Execute confirmed actions in the cloud:"
echo "                 gcloud run jobs execute ${JOB_NAME} --region=${REGION} --args=execute,--yes"
echo "  Logs:          gcloud logging read 'resource.type=cloud_run_job AND resource.labels.job_name=${JOB_NAME}' --limit=50"
echo ""
echo "  Next: ./deploy/deploy_dashboard.sh for the web dashboard + real-time Pub/Sub"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
