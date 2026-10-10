# UBAgen — Backend Infrastructure & Diagnostic Service

Production-ready backend for the **UBAgen** Discord bot and cloud diagnostic health service, built with **Python 3.12**, **discord.py**, **FastAPI**, and **Docker**.

This backend implements the `/stats` slash command and a lightweight HTTP `/health` probe to verify that your cloud container, hosting limits, dependencies (FFmpeg, aria2), and environment variables are operating properly before rolling out media or processing workloads.

---

## Table of Contents

1. [Features & Capabilities](#features--capabilities)
2. [Project Architecture & File Tree](#project-architecture--file-tree)
3. [Discord Developer Portal & Bot Setup](#discord-developer-portal--bot-setup)
   - [Creating the Bot Application](#1-creating-the-bot-application)
   - [Configuring Bot Privileges & Intents](#2-configuring-bot-privileges--intents)
   - [Generating the Invite URL & Adding to Your Server](#3-generating-the-invite-url--adding-to-your-server)
4. [Environment Variables Reference](#environment-variables-reference)
5. [Setting Environment Variables in Hosting Dashboards](#setting-environment-variables-in-hosting-dashboards)
   - [Render](#render-web-service)
   - [Railway](#railway)
   - [Fly.io](#flyio)
   - [Docker & Docker Compose](#docker--docker-compose)
6. [Deployment & Startup Instructions](#deployment--startup-instructions)
   - [Method 1: Docker (Recommended)](#method-1-docker-recommended)
   - [Method 2: Docker Compose](#method-2-docker-compose)
   - [Method 3: Local Python Virtual Environment](#method-3-local-python-virtual-environment)
   - [Method 4: Linux VPS with Systemd](#method-4-linux-vps-with-systemd)
7. [Testing the `/stats` Command](#testing-the-stats-command)
8. [Testing the HTTP Health Endpoint](#testing-the-http-health-endpoint)
9. [Troubleshooting Guide](#troubleshooting-guide)
   - [Slash command `/stats` does not appear in Discord](#1-slash-command-stats-does-not-appear-in-discord)
   - [Bot login failure (`LoginFailure`)](#2-bot-login-failure-loginfailure)
   - [RAM or CPU metrics show "Unavailable" or differ from host](#3-ram-or-cpu-metrics-show-unavailable-or-differ-from-host)
   - [HTTP port binding errors in cloud platforms](#4-http-port-binding-errors-in-cloud-platforms)

---

## Features & Capabilities

- **Discord `/stats` Slash Command**:
  - **Bot Gateway Status**: Connection latency (ping in ms), account ID, guild count, and bot uptime.
  - **Host & Container Compute**: CPU usage percentage, logical & physical core count, operating system & kernel, Python version.
  - **Memory Differentiation**:
    - **System RAM**: Total, used, available, and percentage.
    - **Process RAM**: Exact Resident Set Size (RSS) used strictly by UBAgen.
    - **Container Quota**: Detects Linux cgroups v1 & cgroups v2 memory limits (e.g., 512 MB on Render/Fly.io) so you know your real OOM threshold.
  - **Disk Storage**: Total, used, available space and utilization percentage.
  - **Dependency Verification**: Real-time detection and version inspection of `ffmpeg` and `aria2c`.
  - **Safe Credential Audit**: Detects if Google Drive and Discord credentials are configured without exposing secret keys or tokens.
  - **Non-blocking Execution**: Uses `interaction.response.defer(thinking=True)` and `asyncio.to_thread()` to prevent Discord 3-second gateway timeouts.
- **Lightweight FastAPI Health Endpoint (`GET /health`)**:
  - Returns JSON with HTTP status and real-time Discord gateway connection state.
  - Ideal for Docker `HEALTHCHECK`, Render/Fly.io health probes, and Kubernetes readiness checks.
- **Unified Async Concurrency**:
  - Runs both `discord.py` and `uvicorn`/`FastAPI` concurrently in a single Python 3.12 event loop.
  - Clean OS signal handlers for graceful shutdown (`SIGTERM`, `SIGINT`).

---

## Project Architecture & File Tree

```
ubagen-backend/
├── Dockerfile          # Multi-stage production container with FFmpeg, aria2 & non-root user
├── .dockerignore       # Docker build exclusions
├── .env.example        # Environment variable template with documentation
├── requirements.txt    # Pinned Python package dependencies
├── main.py             # Application entrypoint coordinating Discord bot & FastAPI server
├── stats.py            # /stats slash command implementation & system telemetry collector
├── health_api.py       # FastAPI HTTP health check service (GET /health)
└── README.md           # Deployment and operational manual
```

---

## Discord Developer Portal & Bot Setup

Follow these exact steps to register your bot and obtain your token.

### 1. Creating the Bot Application
1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Click **New Application** in the top right corner.
3. Enter the name `UBAgen` and accept the Developer Terms of Service.
4. Navigate to the **Bot** tab on the left sidebar.
5. Click **Reset Token** (confirm with 2FA if prompted).
6. Click **Copy** to copy your bot token. **Keep this secret. This is your `DISCORD_BOT_TOKEN`.**

### 2. Configuring Bot Privileges & Intents
1. In the **Bot** tab, scroll down to the **Privileged Gateway Intents** section.
2. For UBAgen's `/stats` slash command, **NO privileged intents are required**!
   - `Presence Intent`: **OFF**
   - `Server Members Intent`: **OFF**
   - `Message Content Intent`: **OFF**
   *(Slash commands operate over Discord's interactions infrastructure, not standard message reading. Leaving privileged intents disabled allows your bot to run on any server without requiring verification).*

### 3. Generating the Invite URL & Adding to Your Server
1. In the Discord Developer Portal, navigate to **OAuth2** -> **URL Generator** on the left menu.
2. In the **Scopes** checklist, select:
   - `bot`
   - `applications.commands` *(CRITICAL: This scope is required for slash commands like `/stats` to appear!)*
3. In the **Bot Permissions** checklist that appears below, select:
   - `Send Messages`
   - `Embed Links`
   - `Attach Files`
   - `Read Message History`
   - `View Channels`
4. Copy the generated **OAuth2 URL** at the bottom of the page.
5. Paste the URL into your web browser, select your Discord server, and click **Authorize**.

---

## Environment Variables Reference

| Variable Name | Required | Default | Description |
| :--- | :---: | :---: | :--- |
| `DISCORD_BOT_TOKEN` | **Yes** | *None* | Secret bot token from the Discord Developer Portal. |
| `DISCORD_GUILD_ID` | No | *Empty* | Server ID for instantaneous slash command registration during development. If omitted, commands sync globally (takes up to 1 hr). |
| `PORT` | No | `8080` | Port for the HTTP health check service. Automatically injected by cloud providers like Render and Railway. |
| `HOST` | No | `0.0.0.0` | Host IP binding for the FastAPI server. |
| `GOOGLE_APPLICATION_CREDENTIALS` | No | *Empty* | Path to Google Service Account JSON file or raw JSON content. Audited safely in `/stats`. |
| `ENVIRONMENT` | No | `production` | Tag for logging (e.g. `production`, `development`, `docker`). |

---

## Setting Environment Variables in Hosting Dashboards

> ⚠️ **CRITICAL SECURITY RULE**: Never hardcode secrets in source code files, Git commits, or Dockerfiles. Use the hosting platform's web dashboard or CLI secret manager.

### Render (Web Service)
1. In the [Render Dashboard](https://dashboard.render.com/), select your UBAgen Web Service.
2. Click the **Environment** tab on the left.
3. Under **Environment Variables**, click **Add Environment Variable**:
   - Key: `DISCORD_BOT_TOKEN` | Value: `your_bot_token_here`
   - Key: `DISCORD_GUILD_ID` (Optional) | Value: `your_discord_server_id`
   - Note: Render automatically provides `PORT`. Do not override it unless necessary.
4. Click **Save Changes**. Render will automatically redeploy with the new credentials.

### Railway
1. Open your project on [Railway](https://railway.app/).
2. Click on the UBAgen service box.
3. Select the **Variables** tab.
4. Click **New Variable** (or **RAW Editor**):
   - Add `DISCORD_BOT_TOKEN=your_bot_token_here`
   - Add `DISCORD_GUILD_ID=your_discord_server_id`
5. Railway injects these into the container environment securely on startup.

### Fly.io
Use the Fly CLI secrets command so values are encrypted at rest:
```bash
fly secrets set DISCORD_BOT_TOKEN="your_bot_token_here" DISCORD_GUILD_ID="your_discord_server_id"
```

### Docker & Docker Compose
Create a local `.env` file from `.env.example`:
```bash
cp .env.example .env
nano .env  # or notepad .env
```
Docker will inject the contents of `.env` into the running container without committing secrets to repository history.

---

## Deployment & Startup Instructions

### Method 1: Docker (Recommended)

1. **Build the Docker container image**:
   ```bash
   docker build -t ubagen:latest .
   ```

2. **Run the container with your Discord token**:
   ```bash
   docker run -d \
     --name ubagen-service \
     --restart unless-stopped \
     -p 8080:8080 \
     -e DISCORD_BOT_TOKEN="your_discord_bot_token_here" \
     -e DISCORD_GUILD_ID="your_server_id_optional" \
     ubagen:latest
   ```

3. **Check container logs**:
   ```bash
   docker logs -f ubagen-service
   ```

### Method 2: Docker Compose

Create a `docker-compose.yml` in the project root:
```yaml
version: '3.8'

services:
  ubagen:
    build: .
    container_name: ubagen
    restart: unless-stopped
    ports:
      - "8080:8080"
    env_file:
      - .env
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3
```

Start the service:
```bash
docker compose up -d
docker compose logs -f
```

### Method 3: Local Python Virtual Environment

Requires **Python 3.12+**, **FFmpeg**, and **aria2** installed on your system.

1. **Create and activate a virtual environment**:
   ```bash
   python3.12 -m venv venv
   source venv/bin/activate       # On Linux/macOS
   # .\venv\Scripts\activate      # On Windows PowerShell
   ```

2. **Install Python dependencies**:
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

3. **Configure environment**:
   ```bash
   cp .env.example .env
   # Edit .env and paste your DISCORD_BOT_TOKEN
   ```

4. **Start the application**:
   ```bash
   python main.py
   ```

### Method 4: Linux VPS with Systemd

Create `/etc/systemd/system/ubagen.service`:
```ini
[Unit]
Description=UBAgen Discord Bot & Health Service
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/ubagen
EnvironmentFile=/opt/ubagen/.env
ExecStart=/opt/ubagen/venv/bin/python main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable ubagen
sudo systemctl start ubagen
sudo journalctl -u ubagen -f
```

---

## Testing the `/stats` Command

1. Open your Discord server where UBAgen has been authorized.
2. In any text channel the bot has permission to view, type `/stats`.
3. The Discord autocomplete pop-up will show:
   ```
   /stats — Displays backend infrastructure health, RAM, CPU, and dependencies.
   ```
4. Press **Enter**.
5. UBAgen will immediately show `"UBAgen is thinking..."` (confirming the deferral mechanism fired).
6. Within 1-2 seconds, UBAgen edits the message with the formatted embed titled **`UBAgen — Backend Stats`**:
   - **🤖 Bot Gateway Connection**: Online status, ping latency, uptime.
   - **🖥️ Compute & Platform**: CPU load, core count, OS distro, Python version.
   - **💾 Memory (RAM Allocation)**: System RAM vs Process RAM vs Container Cgroup quota.
   - **💿 Storage (Disk Volume)**: Disk total and used space.
   - **📦 Multimedia Toolchain**: Verification of FFmpeg and aria2.
   - **🔑 Environment & Credentials**: Safe audit of tokens and Google Drive configuration.

---

## Testing the HTTP Health Endpoint

You can test the health probe using `curl` or any browser:

```bash
curl -s http://localhost:8080/health | jq .
```

Expected Response:
```json
{
  "status": "healthy",
  "service": "ubagen-backend",
  "http": "alive",
  "discord_bot": {
    "connected": true,
    "latency_ms": 42.15,
    "user": "UBAgen#1234",
    "ready": true
  },
  "uptime_seconds": 320,
  "timestamp": "2026-10-10T17:35:00.000000+00:00"
}
```

---

## Troubleshooting Guide

### 1. Slash command `/stats` does not appear in Discord
- **Cause A: Global Sync Caching**: Discord caches global application commands across its edge network. Global slash commands can take **up to 1 hour** to propagate to your client.
  - **Solution**: Set `DISCORD_GUILD_ID="your_server_id"` in your `.env` or hosting dashboard and restart UBAgen. Guild-specific commands sync **instantly**.
  - *(To get your Server ID: Discord Settings -> Advanced -> Enable Developer Mode. Right-click your server icon -> Copy Server ID).*
- **Cause B: Missing OAuth Scope**: When inviting the bot, the `applications.commands` scope was not selected.
  - **Solution**: Re-invite the bot using the URL Generator in Discord Developer Portal with both `bot` and `applications.commands` checked.
- **Cause C: Discord Client Cache**: Sometimes the Discord desktop client holds stale command lists.
  - **Solution**: Press `Ctrl + R` (or `Cmd + R` on macOS) in Discord to refresh the client.

### 2. Bot login failure (`LoginFailure`)
- **Symptoms**: Logs show `CRITICAL: Failed to log in to Discord. The provided DISCORD_BOT_TOKEN is invalid or expired.`
- **Cause A: Reset Token**: If you clicked "Reset Token" in the developer portal after copying it, the previous token was instantly invalidated.
  - **Solution**: Copy the new token from the Discord Developer Portal and update your hosting environment variable.
- **Cause B: Whitespace or Quotes**: Accidental leading/trailing quotes or spaces in the environment variable.
  - **Solution**: Paste the raw token without extra quotes or spaces.

### 3. RAM or CPU metrics show "Unavailable" or differ from host
- **Explanation**: In Docker, Kubernetes, or cloud hosts (such as Render, Fly.io, or Railway), containers run inside isolation namespaces.
  - Standard `psutil.virtual_memory()` queries `/proc/meminfo`, which in some container setups reports the **underlying bare-metal host** (e.g. 64 GB RAM).
  - UBAgen's `/stats` command automatically inspects `/sys/fs/cgroup/memory.max` (cgroups v2) and `/sys/fs/cgroup/memory/memory.limit_in_bytes` (cgroups v1).
  - If a container quota is active (e.g., 512 MB or 1 GB), it is explicitly reported under **Container Quota**.
  - If running in a virtualized container where `/sys/fs/cgroup` is unmounted or restricted, UBAgen safely falls back to reporting `Unavailable` rather than hallucinating an inaccurate value.

### 4. HTTP port binding errors in cloud platforms
- **Symptoms**: Container crashes with `OSError: [Errno 98] Address already in use` or cloud platform reports unhealthy health checks.
- **Cause**: Cloud providers (like Render, Railway, or Google Cloud Run) dynamically assign a port using the `$PORT` environment variable.
- **Solution**: `main.py` is configured to automatically read `os.getenv("PORT", "8080")`. Do not hardcode port `80` or `8080` in cloud environments where the provider defines `$PORT`.
