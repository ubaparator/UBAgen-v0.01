# UBAgen encoder migration: no Cloud Run Job IAM

## Goal

Remove the runtime dependency on calling the Cloud Run Jobs API (jobs.run / run.jobs.run). The /encode request uses a direct HTTP(S) media URL and executes through the existing UBAgen service's worker mode instead of dispatching to ubagen-encoder-worker.

## Repository findings

- encoder_worker.py owns slash-command registration, persistent job dispatch, the synchronous encode pipeline, FFmpeg progress reporting, and GCS upload verification.
- dispatch_encode_job_background() currently claims a job and calls trigger_cloud_run_job_execution() when a Cloud Run Job is configured. A 403 marks the job failed before the worker starts.
- health_api.py currently validates magnet as a torrent magnet in POST /api/jobs.
- job_store.py already stores jobs persistently and supports Firestore/GCS, so that data model can be retained.

## Planned rollout (PR #12)

1. Add an explicit UBAGEN_ENCODER_MODE=service_worker mode that skips Cloud Run Job API calls and launches an isolated local worker process in the same service container.
2. Accept direct HTTP/HTTPS video URLs in /encode and POST /api/jobs, while preserving magnet URI compatibility.
3. Keep job claims, progress updates, FFmpeg behavior, Firestore job state, GCS output verification, and 7200-second output retention.
4. Add tests for URL validation and service-worker dispatch selection.
5. Leave the old Cloud Run Job mode available as a backwards-compatible fallback until the service-worker mode is explicitly configured and tested.

## Operational limits

This removes the Cloud Run Job execution IAM dependency, not all IAM. The running service account still needs existing Firestore and GCS permissions. The service-worker mode shares the service's CPU, RAM, and ephemeral disk with Discord/API traffic. A service restart can interrupt an active worker; use a bounded worker concurrency limit and persist state so interrupted jobs can be retried. For production reliability, verify that the chosen Cloud Run Service CPU allocation and request/instance lifecycle support background work before enabling this mode.

## Validation workflow

The accompanying GitHub Actions workflow runs the Python test suite, frontend build, and TypeScript check. It does not modify Google Cloud IAM or deploy to Cloud Run.

## Rollback

Unset UBAGEN_ENCODER_MODE or set it to the existing cloud_run_job mode to return to the previous dispatcher. Do not delete the Cloud Run Job until the new mode has passed an end-to-end encode test.
