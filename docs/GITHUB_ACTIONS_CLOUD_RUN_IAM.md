# GitHub Actions → Cloud Run encoder IAM

This workflow lets the repository repair the encoder Job's runtime execution binding without storing a Google service-account key in GitHub. It uses Workload Identity Federation (WIF).

**It does not bypass Google Cloud IAM.** A project administrator must perform the one-time bootstrap below. The GitHub Actions deployer identity is deliberately limited to reading/updating Cloud Run Job IAM policies; it is not a general project owner.

## One-time setup (run as a Google Cloud administrator)

Open Cloud Shell while signed in to an account that can create Workload Identity pools/providers, service accounts, custom roles, and IAM bindings. Set the project ID to the project that contains the encoder Job:

```bash
export PROJECT_ID="ais-europe-west2-4471ce5b85c94"
export REGION="europe-west2"
export REPO="ubaparator/UBAgen-v0.01"
export POOL_ID="ubagen-github-pool"
export PROVIDER_ID="github-provider"
export DEPLOYER_SA="ubagen-github-deployer"

gcloud config set project "$PROJECT_ID"
export PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

gcloud services enable iam.googleapis.com run.googleapis.com --project="$PROJECT_ID"

gcloud iam workload-identity-pools create "$POOL_ID" \
  --project="$PROJECT_ID" \
  --location="global" \
  --display-name="UBAgen GitHub Actions"

gcloud iam workload-identity-pools providers create-oidc "$PROVIDER_ID" \
  --project="$PROJECT_ID" \
  --location="global" \
  --workload-identity-pool="$POOL_ID" \
  --display-name="UBAgen repository" \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition="assertion.repository == '$REPO'"

gcloud iam service-accounts create "$DEPLOYER_SA" \
  --project="$PROJECT_ID" \
  --display-name="UBAgen GitHub IAM deployer"

# A small custom role for only the IAM policy operations the workflow needs.
gcloud iam roles create ubagenJobIamEditor \
  --project="$PROJECT_ID" \
  --title="UBAgen Cloud Run Job IAM editor" \
  --description="Read and update Cloud Run Job IAM policies for UBAgen automation" \
  --permissions="run.jobs.getIamPolicy,run.jobs.setIamPolicy" \
  --stage="GA"

gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:$DEPLOYER_SA@$PROJECT_ID.iam.gserviceaccount.com" \
  --role="projects/$PROJECT_ID/roles/ubagenJobIamEditor"

gcloud iam service-accounts add-iam-policy-binding \
  "$DEPLOYER_SA@$PROJECT_ID.iam.gserviceaccount.com" \
  --project="$PROJECT_ID" \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL_ID/attribute.repository/$REPO"

echo "WIF provider: projects/$PROJECT_NUMBER/locations/global/workloadIdentityPools/$POOL_ID/providers/$PROVIDER_ID"
echo "Deployer service account: $DEPLOYER_SA@$PROJECT_ID.iam.gserviceaccount.com"
```

If any command fails, stop and resolve that error with the project administrator before continuing. Do not paste service-account keys or other credentials into GitHub.

## Configure GitHub repository variables

In GitHub, open **Settings → Secrets and variables → Actions → Variables** for this repository and add these repository variables (they are identifiers, not secrets):

| Variable | Value |
|---|---|
| `GCP_PROJECT_ID` | `ais-europe-west2-4471ce5b85c94` |
| `GCP_REGION` | `europe-west2` |
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | The full `WIF provider` resource printed above |
| `GCP_DEPLOYER_SERVICE_ACCOUNT` | The full `Deployer service account` email printed above |
| `UBAGEN_RUNTIME_SERVICE_ACCOUNT` | `ais-sandbox@ais-europe-west2-4471ce5b85c94.iam.gserviceaccount.com` |
| `UBAGEN_CLOUD_RUN_JOB_NAME` | `ubagen-encoder-worker` |

## Run the workflow

1. Merge the pull request that adds `.github/workflows/repair-cloud-run-iam.yml`.
2. Open **Actions → Repair Cloud Run encoder IAM → Run workflow**.
3. Type `APPLY` into the confirmation field and start the workflow.
4. Confirm the run succeeds, then retry an encode request in UBAgen.

The workflow only adds `roles/run.jobsExecutor` to the runtime service account on the named Job. It does not deploy a new image or change the running bot. If the Job does not exist in the configured project/region, deployment must be completed separately first.
