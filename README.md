# UBAgen — System Monitor & Discord Bot Backend

Production-ready backend and live hardware monitoring dashboard for the **UBAgen** Discord bot, built with **Python 3.12**, **discord.py**, **FastAPI**, **Express**, **React**, and **Docker**.

Provides real-time host and container telemetry through:
1. **Live Web Dashboard** (`UBAgen — System Monitor`) polling `GET /api/stats` every 2 seconds.
2. **Discord Slash Command** (`/stats`) returning a rich diagnostic embed to any Discord server.
3. **HTTP Health Probes** (`GET /health`) for container orchestrators (Render, Railway, Fly.io, Docker).

---

## Architecture Overview

```
ubagen/
├── server.ts           # Full-stack Node/Express server serving React & /api/stats (dev & preview)
├── main.py             # Python entrypoint coordinating Discord bot & FastAPI server (production)
├── stats.py            # Hardware statistics measurement engine (psutil, cgroups, Linux /proc)
├── health_api.py       # FastAPI application serving GET /api/stats and GET /health
├── Dockerfile          # Multi-stage production container (React build + Python 3.12 + FFmpeg + aria2)
├── requirements.txt    # Python dependencies (discord.py, fastapi, uvicorn, psutil, aiohttp)
├── package.json        # Frontend & full-stack dependencies
├── .env.example        # Clean environment variables template
└── src/
    └── App.tsx         # Live dark telemetry dashboard (CPU, RAM, Disks, Uptime, Bot, Cgroups)
```

---

## Real-Time Hardware Metrics Reported

Every metric is measured directly from the host and container running the backend (never simulated or faked):

- **CPU Compute**: Real-time CPU usage percentage, logical core count, physical core count.
- **System Memory (RAM)**: Total RAM, used RAM, available RAM, and percentage visible to the node.
- **Process Memory (RSS)**: Exact RAM used exclusively by the UBAgen process and percentage of total system RAM.
- **Container Quota Detection**: Automatically inspects Linux **cgroups v1** (`memory.limit_in_bytes`) and **cgroups v2** (`memory.max`) to detect whether the container is limited (e.g. 512 MB on Render/Railway/Fly.io) so you know your true Out-Of-Memory (OOM) ceiling.
- **Disk Storage**: Total, used, and free storage on root (`/`), with utilization percentage.
- **Discord Gateway State**: Real-time connection status (Connected / Offline), bot username, ping latency (ms), connected server count, and slash command status.
- **Toolchain Verification**: Detection and version inspection of `ffmpeg` and `aria2c`.
- **Operating System & Python**: Kernel, distro (`/etc/os-release`), Python version, and implementation.
- **Uptime**: Host system uptime and backend process uptime.

---

## Required Environment Variables

Configure these variables in your hosting provider's dashboard (Render, Railway, Fly.io, etc.):

| Variable | Required | Default | Purpose |
| :--- | :---: | :---: | :--- |
| `DISCORD_BOT_TOKEN` | **Yes** (for Discord) | *None* | Your bot token from the Discord Developer Portal. If left empty, UBAgen runs in web-only monitoring mode. |
| `DISCORD_GUILD_ID` | Optional | *Empty* | Your Discord Server (Guild) ID for **instant** slash command registration during testing. If omitted, global commands take ~1 hour to propagate. |
| `PORT` | Optional | `8080` (or `3000`) | HTTP server port. Injected automatically by Render, Railway, Fly.io, and Cloud Run. |
| `HOST` | Optional | `0.0.0.0` | Network binding interface. |
| `ENVIRONMENT` | Optional | `production` | Informational label displayed in logs and monitoring cards. |

> 🔒 **Security Notice**: Never commit secret tokens or `.env` files to GitHub. Set them securely through your hosting platform's web dashboard or secret manager. Google Drive credentials are no longer required for this hardware monitoring version.

---

## API Endpoints

- **`GET /api/stats`** (and alias **`GET /stats`**):
  Returns the complete live hardware and bot status in JSON format.
  Headers: `Cache-Control: no-store, no-cache, max-age=0`.
- **`GET /health`**:
  Returns lightweight JSON status (`{"status":"healthy","service":"ubagen-backend","http":"alive"}`) for container health checks.

---

## Deployment Instructions

### Method 1: Docker (Recommended for Render, Railway, Fly.io, VPS)

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

1. Install frontend dependencies:
   ```bash
   npm install
   ```

2. Run the development server (runs full-stack Express + Vite on port 3000):
   ```bash
   npm run dev
   ```

3. Open `http://localhost:3000` in your browser to view the live dashboard.
