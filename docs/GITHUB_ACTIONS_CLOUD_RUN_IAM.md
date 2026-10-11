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

## Discord Wake-on-Command & Cloud Run Lifecycle Architecture

### Why Gateway-Only Bots Appear Offline on Serverless Platforms

Discord Gateway bots maintain an outbound WebSocket connection (`wss://gateway.discord.gg`). When a bot disconnects or when a serverless container (such as Google Cloud Run) scales down to zero or throttles CPU between HTTP requests:

1. **Discord cannot wake an offline Gateway bot without an HTTP endpoint**: If no WebSocket session is active and no **Interactions Endpoint URL** is configured in the Discord Developer Portal, Discord rejects slash commands in the client and delivers nothing to the backend.
2. **Scale-to-zero instances require an inbound HTTP trigger**: A Cloud Run service scaled to `0` instances has no running Python process until an inbound HTTP request arrives at the Cloud Run ingress URL.

### Two-Layer Wake-on-Command Architecture

1. **Signed Discord HTTP Interactions Endpoint (`POST /api/discord/interactions` & `/discord/interactions`)**:
   - Verifies incoming Discord webhook requests using **Ed25519 signature verification** (`X-Signature-Ed25519` + `X-Signature-Timestamp` against `DISCORD_PUBLIC_KEY`) with 300-second replay protection.
   - Responds to Discord's verification `PING` (`type=1`) with `{"type": 1}` (`200 OK`).
   - When a user runs `/encode`, `/encodeal`, `/is_durumu`, `/stats`, or `/discord` (`type=2` `APPLICATION_COMMAND`):
     - An incoming HTTP request automatically wakes a scaled-to-zero Cloud Run instance.
     - If the in-process Discord Gateway worker is disconnected, in backoff, or not yet started, `BotLifecycleManager.ensure_bot_awake()` wakes it immediately with single-flight locking and cooldown protection.
     - Responds within Discord's 3-second deadline (`type=5` deferred or `type=4` immediate) and executes the command asynchronously, updating the original interaction message via Discord's Interaction Webhook REST API.
2. **Authenticated External Wake Control Endpoint (`POST /api/discord/wake`) & Gateway `on_interaction` Hook**:
   - External health checks, Cloud Scheduler jobs, or administrators can send `POST /api/discord/wake` with `Authorization: Bearer <UBAGEN_ADMIN_TOKEN>`.
   - Returns `200 OK` (`{"status": "ready", "woke": false}`) when already ready, `202 Accepted` (`{"status": "waking", "woke": true}`) when waking an offline worker, and `429 Too Many Requests` when rate-limited by cooldown.
   - Separates **HTTP Alive**, **Discord Gateway Connected**, and **Bot Ready** states across `/health`, `/api/stats`, and the web dashboard so the bot is never reported as ready until `on_ready` completes.

### Cloud Run Scaling & CPU Allocation Requirements

- **For scale-to-zero wake-on-command via Discord slash commands**:
  1. Set `DISCORD_PUBLIC_KEY` on the Cloud Run service to the 64-character hex public key from **Discord Developer Portal → General Information → Public Key**.
  2. Set **Interactions Endpoint URL** in the Discord Developer Portal to:
     `https://<your-cloud-run-service-url>/api/discord/interactions`
- **For uninterrupted Gateway WebSocket presence and `service_worker` FFmpeg execution**:
  - Enable `--no-cpu-throttling` (CPU always allocated) on the Cloud Run service so background Gateway heartbeats and FFmpeg workers are not throttled after HTTP responses return.
  - Set `--min-instances=1` if you require continuous 24/7 green presence in the Discord member list without cold-start delay.

