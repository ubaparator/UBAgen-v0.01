# UBAgen — System Monitor & Discord Bot Backend

Production-ready backend and live hardware monitoring dashboard for the **UBAgen** Discord bot, built with **Python 3.12**, **discord.py**, **FastAPI**, **Express**, **React**, and **Docker**.

Provides real-time host and container telemetry through:
1. **Live Web Dashboard** (`UBAgen — System Monitor`) polling `GET /api/stats` every 2 seconds with pause/resume and manual refresh.
2. **Discord Slash Command** (`/stats`) returning a rich diagnostic embed to any Discord server without blocking the event loop.
3. **HTTP Health Probes** (`GET /health`) for container orchestrators (Render, Railway, Fly.io, Cloud Run, Docker).

---

## Key Features & Resilient Architecture

- **Independent Monitoring Service**: The HTTP API (`/api/stats`, `/health`) and web dashboard start immediately and function reliably **with or without** a Discord bot token.
- **Resilient Authentication & Bot Lifecycle**:
  - Distinguishes **missing token** (`not_configured`), **invalid token** (`LoginFailure`), **network errors**, **Discord API errors**, and **command synchronization errors**.
  - If Discord authentication fails or a token is rejected, the HTTP server and dashboard remain online and display safe diagnostic guidance.
  - Recovers automatically from temporary gateway network interruptions without spawning duplicate bot instances.
  - Safe slash command registration prevents duplicate command registration errors.
  - Instant command sync with `DISCORD_GUILD_ID` catches permission errors (such as 403 Missing Access when the bot is missing the `applications.commands` scope) and gracefully falls back to global sync.
- **Zero Secret Exposure**: Tokens, keys, and private credentials are never logged, displayed in the dashboard, or exposed in API responses.
- **Real Hardware Telemetry**: All metrics (CPU, RAM, process RSS, storage, cgroup limits) are measured directly from the host and container using `psutil`, `/proc`, and `cgroups` (never fabricated or mocked).

---

## Architecture Overview

```
ubagen/
├── server.ts           # Full-stack Node/Express server running Vite, companion worker & /api/stats
├── main.py             # Python service coordinating Discord bot & FastAPI server with signal handling
├── stats.py            # Hardware statistics measurement engine (psutil, cgroups, Linux /proc, /stats embed)
├── health_api.py       # FastAPI application serving GET /api/stats and GET /health
├── Dockerfile          # Multi-stage production container (React build + Python 3.12 + FFmpeg + aria2)
├── requirements.txt    # Python dependencies (discord.py, fastapi, uvicorn, psutil, aiohttp, python-dotenv)
├── package.json        # Frontend & full-stack dependencies
├── .env.example        # Clean environment variables template
└── src/
    └── App.tsx         # Live dark telemetry dashboard (CPU, RAM, Disks, Uptime, Bot, Cgroups, Toolchain)
```

---

## Real-Time Hardware Metrics Reported

Every metric is measured directly from the host and container running the backend:

- **CPU Compute**: Real-time CPU usage percentage, logical core count, physical core count.
- **System Memory (RAM)**: Total RAM, used RAM, available RAM, and percentage visible to the host node.
- **Process Memory (RSS)**: Exact RAM used exclusively by the UBAgen process and percentage of total system RAM.
- **Container Quota Detection**: Automatically inspects Linux **cgroups v1** (`memory.limit_in_bytes`, `cpu.cfs_quota_us`) and **cgroups v2** (`memory.max`, `cpu.max`) to detect container limits (e.g. 512 MB or 4 GB) so you know your Out-Of-Memory (OOM) ceiling.
- **Disk Storage**: Total, used, and free storage on root (`/`), with utilization percentage.
- **Discord Gateway State**: Real-time connection status (Connected, Auth Failed, Connecting, Network Reconnecting, Web Monitor Mode), bot account tag, gateway ping latency (ms), connected server count, diagnostic message, and slash sync status.
- **Toolchain Verification**: Detection and version inspection of `ffmpeg` and `aria2c`.
- **Environment & Credentials Audit**: Safe verification of configured environment variables (including Google Drive credential detection without revealing values).
- **Operating System & Python**: Kernel, distro (`/etc/os-release`), Python version, and implementation.
- **Uptime**: Host system uptime and backend process uptime.

---

## Environment Variables Configuration

Configure these variables in your hosting provider's dashboard (Render, Railway, Fly.io, etc.) or `.env` file:

| Variable | Required | Default | Purpose |
| :--- | :---: | :---: | :--- |
| `DISCORD_BOT_TOKEN` | Optional | *Empty* | Your bot token from the Discord Developer Portal. If left empty, UBAgen runs in web-only monitoring mode. |
| `DISCORD_GUILD_ID` | Optional | *Empty* | Your Discord Server (Guild) ID for **instant** slash command registration during testing. If omitted, global sync is used. |
| `PORT` | Optional | `8080` (or `3000`) | HTTP server port. Injected automatically by hosting platforms. |
| `HOST` | Optional | `0.0.0.0` | Network binding interface. |
| `ENVIRONMENT` | Optional | `production` | Informational label displayed in logs and monitoring cards. |
| `GOOGLE_DRIVE_CREDENTIALS` | Optional | *Empty* | Optional Google Drive credentials JSON or file path (tested safely without leaking contents). |

> 🔒 **Security Notice**: Never commit secret tokens or `.env` files to Git. Set them securely through your hosting platform's web dashboard or secret manager.

---

## API Endpoints

- **`GET /api/stats`** (and alias **`GET /stats`**):
  Returns complete live hardware and bot status in JSON format.
  Headers: `Cache-Control: no-store, no-cache, max-age=0`.
- **`GET /health`**:
  Returns lightweight JSON status (`{"status":"healthy","service":"ubagen-backend","http":"alive","discord_bot":{...}}`) for container health checks.

---

## Deployment Instructions

### Method 1: Docker (Recommended for Render, Railway, Fly.io, Cloud Run, VPS)

1. **Build the container image**:
   ```bash
   docker build -t ubagen:latest .
   ```

2. **Run the container**:
   ```bash
   docker run -d \
     --name ubagen-monitor \
     --restart unless-stopped \
     -p 8080:8080 \
     -e DISCORD_BOT_TOKEN="your_discord_bot_token" \
     -e DISCORD_GUILD_ID="your_server_id_optional" \
     ubagen:latest
   ```

3. **Access the Dashboard**:
   Open `http://localhost:8080` in your browser. You will see the live hardware dashboard, and UBAgen will connect to Discord.

### Method 2: Local Development (Full-Stack)

1. Install frontend and backend dependencies:
   ```bash
   npm install
   pip install -r requirements.txt
   ```

2. Run the development server (runs full-stack Express + Vite on port 3000):
   ```bash
   npm run dev
   ```

3. Open `http://localhost:3000` in your browser to view the live dashboard.

### Automatic redeploy after GitHub updates

The repository includes a GitHub Actions workflow that validates the Python source and builds the frontend on pushes and pull requests. To trigger a real redeploy/restart after each push to `main`, configure a repository Actions secret named `DEPLOY_WEBHOOK_URL` with the deployment provider's deploy-hook URL. Optionally set the repository Actions variable `DEPLOY_WEBHOOK_METHOD` to `GET` or `POST` if the provider requires a specific HTTP method (defaults to `POST`). The workflow sends the hook only after a successful validation run on `main`; without this provider URL, GitHub can validate the code but cannot restart an external running service by itself.

On every process start, `main.py` registers the bot's slash commands and syncs them with Discord. Guild-scoped commands are attempted for servers the bot has joined; global commands are also synced. Global Discord command propagation can take up to an hour. The bot must be invited with the `applications.commands` scope.
