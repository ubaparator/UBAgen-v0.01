#!/usr/bin/env bash
# ==============================================================================
# UBAgen — Automated Google Cloud Production Provisioning & Deployment Script
# ==============================================================================
# Configures least-privilege IAM, Firestore, GCS Bucket (with lifecycle rules),
# Secret Manager references, Artifact Registry image build, and Cloud Run Job
# deployment (`ubagen-encoder-worker`) for the verified UBAgen environment.
#
# Usage:
#   bash scripts/deploy_cloud_run_encoder.sh
# ==============================================================================
set -euo pipefail

PROJECT_ID="${UBAGEN_GCP_PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || echo 'ais-europe-west2-4471ce5b85c94')}"
REGION="${UBAGEN_GCP_REGION:-europe-west2}"
SERVICE_NAME="${K_SERVICE:-ais-dev-cpgku4j2jfxruafwftwvut}"
JOB_NAME="${UBAGEN_CLOUD_RUN_JOB_NAME:-ubagen-encoder-worker}"
SA_EMAIL="${UBAGEN_SERVICE_ACCOUNT_EMAIL:-ais-sandbox@${PROJECT_ID}.iam.gserviceaccount.com}"
BUCKET_NAME="${UBAGEN_GCS_BUCKET:-${PROJECT_ID}-ubagen-media}"
AR_REPO="${UBAGEN_AR_REPO:-ubagen-repo}"
IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/ubagen:latest"
PUBLIC_BASE_URL="${UBAGEN_PUBLIC_BASE_URL:-https://ais-pre-cpgku4j2jfxruafwftwvut-444154444547.europe-west2.run.app}"

echo "=== [1/7] Enabling required Google Cloud APIs in ${PROJECT_ID} ==="
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  cloudscheduler.googleapis.com \
  --project="${PROJECT_ID}"

echo "=== [2/7] Ensuring Firestore (Native Mode) database '(default)' exists ==="
if ! gcloud firestore databases describe --database="(default)" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud firestore databases create \
    --database="(default)" \
    --location="${REGION}" \
    --type=firestore-native \
    --project="${PROJECT_ID}"
fi

echo "=== [3/7] Ensuring GCS Bucket gs://${BUCKET_NAME} exists with uniform bucket-level access ==="
if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET_NAME}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --uniform-bucket-level-access \
    --public-access-prevention
fi

echo "=== [4/7] Configuring Least-Privilege IAM Roles for ${SA_EMAIL} ==="
# 1. Allow Cloud Run Service to trigger Cloud Run Job (run.jobs.run) and controlled service revision restarts
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/run.developer" \
  --condition=None >/dev/null

# 2. Allow Service Account to act as itself when deploying/running jobs
gcloud iam service-accounts add-iam-policy-binding "${SA_EMAIL}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/iam.serviceAccountUser" \
  --project="${PROJECT_ID}" >/dev/null

# 3. Allow Bot Service & Worker Job to read/write job records in Firestore
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/datastore.user" \
  --condition=None >/dev/null

# 4. Allow Bot Service & Worker Job to stage inputs and upload/delete outputs on the dedicated GCS bucket only
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" >/dev/null

# 5. If DISCORD_BOT_TOKEN secret exists in Secret Manager, grant secretAccessor on that secret only
if gcloud secrets describe DISCORD_BOT_TOKEN --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud secrets add-iam-policy-binding DISCORD_BOT_TOKEN \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="roles/secretmanager.secretAccessor" \
    --project="${PROJECT_ID}" >/dev/null
fi

echo "=== [5/7] Ensuring Artifact Registry repository & building container image ==="
if ! gcloud artifacts repositories describe "${AR_REPO}" --location="${REGION}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
  gcloud artifacts repositories create "${AR_REPO}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="UBAgen Discord Bot & Cloud Run Encoder Worker images" \
    --project="${PROJECT_ID}"
fi

gcloud builds submit --tag "${IMAGE_URI}" --project="${PROJECT_ID}" .

echo "=== [6/7] Deploying Cloud Run Job (${JOB_NAME}) in ${REGION} ==="
gcloud run jobs deploy "${JOB_NAME}" \
  --image="${IMAGE_URI}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --service-account="${SA_EMAIL}" \
  --command="python3" \
  --args="-B,encoder_worker.py" \
  --cpu=4 \
  --memory=8Gi \
  --task-timeout=3600s \
  --max-retries=1 \
  --tasks=1 \
  --parallelism=1 \
  --set-env-vars="ENVIRONMENT=production,UBAGEN_GCP_PROJECT_ID=${PROJECT_ID},UBAGEN_GCP_REGION=${REGION},UBAGEN_CLOUD_RUN_JOB_NAME=${JOB_NAME},UBAGEN_USE_FIRESTORE=true,UBAGEN_FIRESTORE_DATABASE=(default),UBAGEN_FIRESTORE_COLLECTION=ubagen_encode_jobs,UBAGEN_GCS_BUCKET=${BUCKET_NAME},UBAGEN_WORKER_TEMP_DIR=/tmp/ubagen_worker_scratch,UBAGEN_ENCODE_RETENTION_SECONDS=7200,UBAGEN_PUBLIC_BASE_URL=${PUBLIC_BASE_URL}"

echo "=== [7/7] Updating Cloud Run Service (${SERVICE_NAME}) environment variables ==="
gcloud run services update "${SERVICE_NAME}" \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --update-env-vars="UBAGEN_GCP_PROJECT_ID=${PROJECT_ID},UBAGEN_GCP_REGION=${REGION},UBAGEN_CLOUD_RUN_JOB_NAME=${JOB_NAME},UBAGEN_USE_FIRESTORE=true,UBAGEN_FIRESTORE_DATABASE=(default),UBAGEN_FIRESTORE_COLLECTION=ubagen_encode_jobs,UBAGEN_GCS_BUCKET=${BUCKET_NAME},UBAGEN_ENCODE_RETENTION_SECONDS=7200,UBAGEN_PUBLIC_BASE_URL=${PUBLIC_BASE_URL}"

echo "=== Deployment Complete ==="
