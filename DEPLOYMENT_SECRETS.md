# UBAgen secrets for Google AI Studio

Do not commit a real `.env` file or live credentials. This repository is run from Google AI Studio Build mode, and the public `*.ai.studio` URL is published separately from GitHub Actions.

## Required for the live Discord bot & Restart Bot feature

- `DISCORD_BOT_TOKEN` — the bot token from Discord Developer Portal → Applications → your application → Bot. Add it as a server-side secret in the **same Google AI Studio app's Secrets panel** (or Docker/Cloud host environment). Do not put it in browser/frontend code, `VITE_*` variables, a commit, the README, or a public issue.
- `UBAGEN_ADMIN_TOKEN` — strong private bearer token required on the server for `POST /api/admin/restart-bot` (used by the dashboard's **Restart Bot** button). Add it as a server-side secret only. Never place it in `HOST`, frontend source code, or client-side `VITE_*` variables.
- If using the local development workflow, put these secrets in a local `.env` file copied from `.env.example`; keep it untracked.

## Optional runtime configuration

- `DISCORD_GUILD_ID` — numeric ID of your test Discord server. Add it to the same AI Studio app's server-side secrets/environment settings if you want faster guild-scoped slash-command sync.
- `PORT` — HTTP server port provided by the hosting environment in production (default `8080` in Docker/FastAPI, `3000` in Node/Express dev mode).
- `UBAGEN_HTTP_PORT` — optional port override for the Python FastAPI companion server (`5050` when spawned by `server.ts`).
- `HOST` — network bind interface address (defaults to `0.0.0.0`). Do **not** put admin tokens or URLs into `HOST`.
- `BACKEND_URL` — only needed if the Node server should query a separate Python backend. In the normal AI Studio Run path, `server.ts` starts its own Python worker on port 5050, so leave this unset.

## Which system to use

- **Google AI Studio → your app → Secrets:** put `DISCORD_BOT_TOKEN` here for the Run/published app's server-side environment.
- **GitHub → Settings → Secrets and variables → Actions:** no deploy webhook is required for the normal AI Studio workflow. GitHub Actions validates source code; it does not restart AI Studio Run or publish the public app.
- **GitHub repository:** source code only; never commit actual credentials.
- **AI Studio Run:** tests the current code in the Build workspace.
- **AI Studio Publish/Republish:** updates the public `*.ai.studio` app after you sync the intended code into AI Studio and test it.

## Correct update flow after a GitHub commit

1. Merge the reviewed pull request into the intended branch.
2. In the same AI Studio Build project, open Settings → GitHub and sync/pull the latest repository changes into the workspace. Review conflicts if prompted.
3. Run the app in AI Studio and confirm the Discord bot connects.
4. Use Publish/Republish to update the public `*.ai.studio` deployment.

A GitHub commit alone does not guarantee that an already-running Run session or the public deployment has restarted.

## Video upload limitation & Storage Persistence

The bot's 1.5 GB limit is an application-side validation limit. It does not override Discord's own upload cap (8 MB standard) for interaction attachments.
- Temporary torrent downloads (`download_temp/job_*`) and intermediate FFmpeg files (`download_temp/encode_temp/encode_*`) are strictly separated from completed outputs (`video_outputs/`) and are deleted automatically in `finally` after every job.
- **Important Persistence Notice**: In Google AI Studio Run and published Cloud Run containers, `/` and `/tmp` are `overlay` / `tmpfs` filesystems. Although `os.statvfs("/")` may report ~378 GiB on the host overlay, container overlay storage is **ephemeral** and is wiped when a container instance restarts, scales down, or is redeployed.
- **SQLite & Rclone Configuration Persistence**:
  - `ubagen_jobs.sqlite3` (storing encode jobs and pending 7200s deletions) and `rclone.conf` (storing encrypted MEGA & Google Drive remote credentials) must be mounted on a persistent volume in production.
  - **Docker / VPS**: Mount a host directory to `/app/storage` (e.g. `-v /var/data/ubagen:/app/storage`) and set `UBAGEN_STORAGE_DIR=/app/storage`. Both the SQLite database (`/app/storage/ubagen_jobs.sqlite3`) and rclone configuration (`/app/storage/rclone.conf`) will be persisted across container rebuilds and restarts.
  - **Cloud Run / Managed Container**: Attach a Cloud Storage or NFS volume at `/mnt/storage` and configure `UBAGEN_STORAGE_DIR=/mnt/storage`. Never assume root overlay filesystem (`/`) is persistent across container instance lifecycles.
