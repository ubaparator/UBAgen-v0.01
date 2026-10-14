# UBAgen: GitHub Actions + Pages + Render dispatcher

This is an opt-in first integration path. It does not replace the existing UBAgen bot or remove the older runtime files. The new dispatcher bot starts a separate GitHub Actions workflow for direct video URLs.

## 1. Create the GitHub Actions secrets

In **Settings → Secrets and variables → Actions → Secrets**, add:

- `RCLONE_CONFIG_B64`: base64-encoded contents of a valid rclone.conf containing the remote you intend to use.
- `RCLONE_DESTINATION`: destination such as `drive:UBAgen/outputs` or `mega:UBAgen/outputs`, matching a remote in that config.

Generate the encoded config locally, not in a chat or commit. On Linux/macOS use `base64 < rclone.conf | tr -d '\n'`; on Windows PowerShell use `[Convert]::ToBase64String([IO.File]::ReadAllBytes("rclone.conf"))`.

**Security:** do not commit `rclone.conf`, OAuth tokens, passwords, or any actual secret. If a live rclone credential was ever committed to a public repository, rotate that account password/token and remove the exposed credential from repository history.

## 2. Deploy the Render dispatcher

1. In Render, create a new **Blueprint** from this repository and use `render.yaml`.
2. Add these environment variables to the service:
   - `DISCORD_BOT_TOKEN`: token for the *separate dispatcher bot*.
   - `GITHUB_TOKEN`: fine-grained GitHub token scoped to this repository with **Actions: Read and write** and **Contents: Read** if required by your token policy.
   - `UBAGEN_ADMIN_TOKEN`: a long random admin secret used by the Pages dashboard API.
   - `UBAGEN_DISCORD_ADMIN_IDS`: comma-separated Discord user IDs allowed to use the dispatcher commands. If empty, slash commands deny everyone.
3. Keep `GITHUB_REPOSITORY=ubaparator/UBAgen-v0.01`, `GITHUB_REF=main`, and `PAGES_ORIGIN=https://ubaparator.github.io`.
4. After deployment, check `https://YOUR-RENDER-SERVICE.onrender.com/health`. It should return JSON with `"ok": true`.

The new bot registers `/encodeal` and `/encodeisleri`. This is a *second bot application*; it needs its own Discord application/token and must be invited to your server with bot and application-command scopes.

## 3. Publish GitHub Pages

1. Merge the reviewed changes into `main`.
2. Open **Settings → Pages → Build and deployment → Source** and select **GitHub Actions**.
3. Run the **Deploy UBAgen Management Pages** workflow once if it does not start automatically.
4. The expected project-site URL is `https://ubaparator.github.io/UBAgen-v0.01/`.

The page is static. It never contains a GitHub token. To connect, enter your Render service URL and the `UBAGEN_ADMIN_TOKEN` value. The page keeps the key only in the current browser tab's `sessionStorage`; do not use it on a shared device.

## 4. Best-effort Render health pings

The `Render Dispatcher Health Ping` workflow runs every 10 minutes and requests the service's `/health` endpoint. Add a repository **Actions variable** named `RENDER_HEALTH_URL` with the full health URL, for example `https://YOUR-RENDER-SERVICE.onrender.com/health`.

This may reduce idle sleep, but it cannot guarantee uptime. Scheduled Actions can be delayed, Render can restart/suspend free services, and Render's free tier is not intended for production uptime.

## 5. What the encode workflow currently supports

- Starts one separate Actions run per request.
- Downloads a direct HTTP/HTTPS video URL and optional direct ASS/SRT subtitle URL.
- Encodes H.264 or H.265 using FFmpeg, hard-subbing when a subtitle URL is provided.
- Uploads the output using the configured rclone remote.
- Logs a public link if the selected backend supports `rclone link`; otherwise logs the remote path.

It does not yet accept Discord attachment uploads, automatically remove outputs after two hours, or reproduce every feature of the existing `encoder_worker.py`. Actions jobs have runtime, disk, and monthly quota limits; the workflow stops before the six-hour maximum.

## Troubleshooting

- **401 from dashboard:** verify `UBAGEN_ADMIN_TOKEN` in Render and the key entered on Pages match.
- **GitHub API 401/403:** refresh `GITHUB_TOKEN` and confirm Actions write permission.
- **Workflow dispatch 404:** confirm workflow file exists on `main` and `GITHUB_REPOSITORY` is exact.
- **Upload failure:** verify `RCLONE_CONFIG_B64` decodes to a working config and `RCLONE_DESTINATION` matches a remote/path.
- **Pages shows 404:** enable GitHub Actions as Pages source, merge the workflow, and check its deployment run.
- **Render service fails to start:** check Render logs and confirm the new Discord bot token and GitHub token are set as service secrets.