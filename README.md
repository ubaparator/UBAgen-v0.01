# UBAgen — System Monitor & Discord Bot Backend

Production-ready backend and live hardware monitoring dashboard for the **UBAgen** Discord bot, built with **Python 3.12**, **discord.py**, **FastAPI**, **Express**, **React**, and **Docker**.

Provides real-time host and container telemetry through:
1. **Live Web Dashboard** (`UBAgen — System Monitor`) polling `GET /api/stats` every 2 seconds with pause/resume and manual refresh.
2. **Discord Slash Commands**:
   - `/stats` returning a rich diagnostic embed to any Discord server without blocking the event loop.
   - `/altyazı_ayıklama` (`magnet`, `dil`) downloading torrents via `aria2c` into an isolated `download_temp` workspace and extracting/cleaning embedded `.srt`/`.ass` subtitles via `ffprobe`/`ffmpeg`.
   - `/encode` (`magnet`, `codec` (`h264`/`h265`), `dil`, `altyazi_url`, `intro_url`) creating a persistent encode job, dispatching it to the dedicated **Cloud Run Job worker** (`encoder_worker.py`), updating Discord progress messages with real-time `frame/total_frames`, `FPS`, `ETA`, and percentage, uploading the final output to **Google Cloud Storage (GCS)**, and issuing a 7200-second (2-hour) owner-authorized download link.
   - `/encodeal` (`magnet`, `altyazi` attachment, `intro` attachment, `codec` (`h264`/`h265`), `dil`) reusing the exact same shared backend pipeline as `/encode`, validating and uploading `.ass`/`.srt` and intro attachments into isolated, user/job-scoped GCS staging objects (`gs://<bucket>/staging/<user_id>/<job_id>/...`), and automatically cleaning up staging objects after completion or failure.
   - `/is_durumu` (`is_id`) inspecting persistent encode job state, progress, and expiration countdown across Cloud Run instance restarts with cross-user download link redaction.
   - `/discord` executing an authorized, cooldown-protected controlled restart of the Discord bot service (`BotLifecycleManager.restart_bot()` or Cloud Run Service revision rollout) without interrupting running Cloud Run Job encode tasks.
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
├── server.ts                   # Full-stack Node/Express server running Vite, companion worker & API proxy
├── main.py                     # Python service coordinating Discord bot, FastAPI & 7200s retention sweeper
├── encoder_worker.py           # Cloud Run Job video encoder worker (aria2c + FFmpeg libx264/libx265 + GCS)
├── job_store.py                # Persistent job state store (Firestore / SQLite WAL) & GCS output lifecycle
├── storage_manager.py          # Runtime storage inspector & persistent vs worker scratch separation
├── subtitle_extract.py         # /altyazı_ayıklama slash command & ASS/SRT stream extraction
├── stats.py                    # Hardware statistics measurement engine (psutil, cgroups, /stats embed)
├── health_api.py               # FastAPI application serving /api/stats, /api/storage, /api/jobs, /api/logs
├── cloud-run-encoder-job.yaml  # Declarative Google Cloud Run Job manifest (4 vCPU, 8 GiB, 3600s timeout)
├── Dockerfile                  # Multi-stage production container (React build + Python 3.12 + FFmpeg + aria2)
├── requirements.txt            # Python dependencies (discord.py, fastapi, google-cloud-storage, firestore)
├── package.json                # Frontend & full-stack dependencies
├── .env.example                # Clean environment variables template
└── src/
    └── App.tsx                 # Live dark telemetry dashboard (CPU, RAM, Storage Separation, Jobs, Bot)
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
| `DISCORD_BOT_TOKEN` | Optional (Required for Bot) | *Empty* | Server-side Discord Bot token from the Discord Developer Portal. If left empty, UBAgen runs in web-only monitoring mode. Never expose in client/frontend code. |
| `UBAGEN_ADMIN_TOKEN` | Required for Bot Restart | *Empty* | Server-side private bearer token required by `POST /api/admin/restart-bot` (used by the dashboard's **Restart Bot** button). Must be configured only in server-side Secrets/environment; never put it in `VITE_*` variables, frontend source code, or `HOST`. |
| `DISCORD_GUILD_ID` | Optional | *Empty* | Your Discord Server (Guild) ID for **instant** slash command registration during testing. If omitted, global sync is used. |
| `PORT` | Optional | `8080` (or `3000` in Node dev) | Primary HTTP server port. Injected automatically by cloud hosting platforms and used by `Dockerfile` (`8080`). |
| `UBAGEN_HTTP_PORT` | Optional | *Empty* (falls back to `PORT`) | Optional port override for the Python FastAPI server (set to `5050` automatically when `server.ts` spawns `main.py` as a companion worker). |
| `HOST` | Optional | `0.0.0.0` | Network bind interface address (e.g. `0.0.0.0` or `127.0.0.1`). **Do not** place admin tokens or URLs in `HOST`. |
| `ENVIRONMENT` | Optional | `production` | Informational label displayed in logs and monitoring cards. |
| `GOOGLE_DRIVE_CREDENTIALS` | Optional | *Empty* | Optional Google Drive credentials JSON or file path (tested safely without leaking contents). |
| `UBAGEN_STORAGE_DIR` | Optional | `.` | Root storage directory for separated `video_outputs` and `download_temp` workspaces. Mount a persistent disk or Cloud Storage FUSE volume here in production for persistence across restarts. |
| `UBAGEN_OUTPUT_DIR` | Optional | `<UBAGEN_STORAGE_DIR>/video_outputs` | Dedicated directory for completed video/subtitle output files served via `/api/outputs/{filename}`. |
| `UBAGEN_TEMP_DIR` | Optional | `<UBAGEN_STORAGE_DIR>/download_temp` | Dedicated temporary directory for `aria2c` torrent downloads (`job_*`) and `ffmpeg` intermediate encodes (`encode_temp/encode_*`). Cleaned up in `finally` and on startup. |
| `UBAGEN_STORAGE_MAX_GB` | Optional | `200` | Maximum storage quota ceiling (GB), bounded automatically by actual available space minus `UBAGEN_MIN_FREE_RESERVE_GB`. |
| `UBAGEN_OUTPUT_RETENTION_HOURS` | Optional | `24` | Retention TTL (hours) before expired files in local `video_outputs` are automatically purged. |
| `UBAGEN_GCS_BUCKET` | Recommended (Production) | *Empty* | Google Cloud Storage bucket name for persistent encoded video outputs and `/encodeal` attachment staging (`staging/{user_id}/{job_id}/...`). |
| `UBAGEN_CLOUD_RUN_JOB_NAME` | Recommended (Production) | *Empty* | Dedicated Google Cloud Run Job name (`ubagen-encoder-worker`) triggered via Cloud Run v2 `jobs.run` API (`UBAGEN_JOB_ID=<job_id>`). |
| `UBAGEN_GCP_PROJECT_ID` | Recommended (Production) | Auto-discovered on GCP | Google Cloud Project ID hosting Cloud Run Job, Firestore, and GCS. Auto-discovered from Metadata Server or `GOOGLE_CLOUD_PROJECT` when available. |
| `UBAGEN_GCP_REGION` | Optional | `europe-west2` (or Metadata Server) | Google Cloud region for Cloud Run Job and Service operations. |
| `UBAGEN_USE_FIRESTORE` | Optional | `false` | Set to `true` to use Google Cloud Firestore as the authoritative job store across Cloud Run Service and Job worker instances (never silently falls back to local SQLite when `true`). |
| `UBAGEN_FIRESTORE_DATABASE` | Optional | `(default)` | Firestore database ID. |
| `UBAGEN_FIRESTORE_COLLECTION` | Optional | `ubagen_encode_jobs` | Firestore collection storing encode jobs across Cloud Run Service and Cloud Run Job worker instances. |
| `UBAGEN_ENCODE_RETENTION_SECONDS` | Optional | `7200` | Exact output retention TTL in seconds (`7200` = 2 hours). After expiry, download access is revoked (`410 Gone`) and the object is deleted from GCS. |
| `UBAGEN_ALLOW_LOCAL_WORKER_FALLBACK` | Optional | `false` | Permits spawning a local worker subprocess only in local dev/test environments when Cloud Run Job is not configured. Disabled in production to protect bot stability. |
| `UBAGEN_DISCORD_ADMIN_IDS` | Optional | *Empty* (Bot Owner & Server Admins) | Optional comma-separated Discord User IDs authorized to execute the `/discord` bot restart command. |
| `UBAGEN_DISCORD_RESTART_MODE` | Optional | `gateway` | Controls `/discord` restart strategy: `gateway` (safe in-process gateway session reconnect) or `cloud_run_revision` (Cloud Run Admin v2 API revision rollout). |
| `UBAGEN_ENCODER_MODE` | Optional | `cloud_run_job` (or `service_worker`) | Set to `service_worker` (or `in_process` / `local`) to execute FFmpeg encode jobs inside the running service without requiring Cloud Run Job `run.jobs.run` IAM permissions. |
| `DISCORD_PUBLIC_KEY` | Optional (Required for HTTP Interactions) | *Empty* | 64-character hex Ed25519 application public key from Discord Developer Portal. Enables `POST /api/discord/interactions` for signed HTTP slash commands and automatic Wake-on-Command when the Gateway bot is offline. |

> 🔒 **Security Notice**: Never commit secret tokens or `.env` files to Git, and never place `UBAGEN_ADMIN_TOKEN` or `DISCORD_BOT_TOKEN` in client-exposed `VITE_*` environment variables. Set them securely through your hosting platform's server-side secret manager.

---

## API Endpoints

- **`GET /api/stats`** (and alias **`GET /stats`**):
  Returns complete live hardware, storage persistence audit, and bot status in JSON format.
  Headers: `Cache-Control: no-store, no-cache, max-age=0`.
- **`GET /api/storage`**:
  Returns the runtime storage decision tree (`A_PERSISTENT_DISK`, `B_EXTERNAL_CLOUD_STORAGE`, or `B_EPHEMERAL_CONTAINER_STORAGE`), explicit separation between **Persistent Output Storage (GCS)**, **Ephemeral Worker Scratch Disk**, and **Persistent Job State DB**, real mount/filesystem metrics (`os.statvfs`, `/proc/mounts`, write probe), and active encode jobs.
- **`GET /api/jobs`** & **`POST /api/jobs`**:
  Lists or creates persistent video encode jobs (`magnet`, `codec` (`h264`/`h265`), `user_id`, `burn_subtitles`, `subtitle_lang`, `intro_path`, `crf`, `preset`). Never stores job state exclusively in Cloud Run instance memory.
- **`GET /api/jobs/{job_id}`**:
  Returns persistent job progress (`stage`, `percent`, `processed_frames`, `total_frames`, `fps`, `eta_seconds`, `codec`, `error`, `output_expires_at`, `output_remaining_seconds`). Only includes `download_url` when the requester is authorized.
- **`GET /api/jobs/{job_id}/download`**:
  Owner-authorized download endpoint requiring matching `user_id` and `token`. Verifies `7200`s retention window, revokes access (`410 Gone`) and deletes the GCS object immediately upon expiry, and blocks cross-user access (`403 Forbidden`).
- **`POST /api/jobs/cleanup-expired`**:
  Server-side retention sweep endpoint suitable for **Cloud Scheduler** (`*/5 * * * *`) as well as the internal background task and worker CLI (`python3 encoder_worker.py --cleanup-expired`). Deletes expired GCS objects and recovers crashed/timed-out workers.
- **`GET /api/outputs/{filename}`**:
  Serves stored output files from `video_outputs` with path-traversal protection and HTTP `Range` (`206 Partial Content`) support.
- **`GET /api/logs`**:
  Returns recent redacted backend log entries from the thread-safe ring buffer without exposing tokens or secrets.
- **`GET /health`**:
  Returns lightweight JSON status (`{"status":"healthy","service":"ubagen-backend","http":"alive","http_alive":true,"discord_gateway_connected":...,"discord_bot_ready":...,"discord_bot":{...}}`) separating HTTP liveness, Gateway WebSocket state, and full Bot Readiness.
- **`POST /api/discord/interactions`** (and alias **`POST /discord/interactions`**):
  Discord HTTP Interactions Endpoint with Ed25519 signature verification (`X-Signature-Ed25519` + `X-Signature-Timestamp` verified against `DISCORD_PUBLIC_KEY`). Handles `PING` (`type=1` → `{"type": 1}`) and `APPLICATION_COMMAND` (`type=2` for `/encode`, `/encodeal`, `/is_durumu`, `/stats`, `/discord`), automatically waking the Discord Gateway bot worker on demand when offline and responding within Discord's 3-second callback deadline.
- **`POST /api/discord/wake`**:
  Authenticated wake-on-command control endpoint (`Authorization: Bearer <UBAGEN_ADMIN_TOKEN>`). Wakes an offline or backoff-waiting Discord bot worker (`202 Accepted`) with single-flight deduplication and cooldown protection, or returns `200 OK` without disrupting the session if the bot is already ready.
- **`POST /api/admin/restart-bot`**:
  Supported identically in both the **Python/FastAPI** (`health_api.py` / `main.py` Docker deployment) and **Node/Express** (`server.ts` full-stack) runtime paths.
  - Requires `Authorization: Bearer <UBAGEN_ADMIN_TOKEN>` (validated using constant-time comparison).
  - Returns `503` when `UBAGEN_ADMIN_TOKEN` or `DISCORD_BOT_TOKEN` is not configured, `401` when the Bearer token is missing or invalid, `405` for non-POST HTTP methods, `429` when rate-limited (15s cooldown / concurrency lock), and `202` (`{"status":"restarting","message":"..."}`) when accepted.
  - Cleanly closes the current Discord gateway client, recreates the bot instance, re-registers slash commands and event handlers, and reconnects using `DISCORD_BOT_TOKEN` while keeping the FastAPI/Express HTTP monitoring server (`/api/stats`, `/health`) online without interruption.

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
     -e UBAGEN_ADMIN_TOKEN="set_a_long_random_private_value" \
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

### Google AI Studio Run, GitHub sync, and publishing

This project is designed to run as a full-stack AI Studio app in development mode: `npm run dev` starts `server.ts`; Express starts the Vite dashboard and spawns `python3 main.py` as a companion worker. That Python process registers the Discord slash commands, runs the Discord gateway client when `DISCORD_BOT_TOKEN` is configured, and serves its internal FastAPI endpoints on port 5050.

**A GitHub commit alone does not guarantee that an already-running AI Studio Run session or the published `*.ai.studio` app has loaded the new code.** After external commits or merging a pull request:

1. Open the same app in Google AI Studio Build mode.
2. Open **Settings → GitHub** and use the repository sync/pull action to bring the latest repository changes into the AI Studio project. Review/resolve any conflicts before proceeding.
3. Run the app again in AI Studio to test the current source.
4. To update the public `https://your-app.ai.studio` URL, use **Publish/Republish** in AI Studio. AI Studio's published apps are deployed through its managed Cloud Run integration; publishing is separate from the GitHub Actions CI workflow.

GitHub Actions validates the code and builds the frontend/Docker image. It does not stop or restart the AI Studio Run session and does not publish the AI Studio app. Do not configure a fictitious `DEPLOY_WEBHOOK_URL` for this workflow.

On every process start, `main.py` registers the bot's slash commands and syncs them with Discord. Guild-scoped copies are attempted for servers the bot has joined; global commands are also synced. Global Discord command propagation can take up to an hour. The bot must be invited with the `applications.commands` scope.

---

## 🔗 Discord Kullanıcıları İçin Google Drive OAuth 2.0 Entegrasyonu

UBAgen, her Discord kullanıcısının kendi Google Drive hesabını resmi **Google OAuth 2.0 Authorization Code Flow + PKCE (S256)** ile bağımsız ve güvenli şekilde bağlamasını destekler. En az yetki ilkesi gereği yalnızca `https://www.googleapis.com/auth/drive.file` kapsamı istenir (tüm Drive dosyalarına erişim istenmez).

### Eklenen Discord Slash Komutları

| Komut | Açıklama |
| :--- | :--- |
| `/drive_bagla` | Kullanıcının Discord kimliğine bağlı, tek kullanımlık (10 dk geçerli) ve CSRF (`state`) korumalı Google OAuth 2.0 yetkilendirme bağlantısı üretir (ephemeral/gizli yanıt). |
| `/drive_durum` | Kullanıcının bağlı Google Drive hesabını, izin kapsamını (`drive.file`) ve bağlantı durumunu gösterir. |
| `/drive_ayir` | Kullanıcının Google OAuth erişim iznini iptal eder (`oauth2.googleapis.com/revoke`) ve şifreli token kayıtlarını veritabanından siler. |
| `/drive_yardim` | Hesap bağlama adımlarını, güvenlik ilkelerini ve olası OAuth hata/sorun giderme çözümlerini gösterir. |

### Gerekli Ortam Değişkenleri (Secrets)

| Değişken | Zorunluluk | Açıklama |
| :--- | :--- | :--- |
| `GOOGLE_CLIENT_ID` | Drive OAuth için Zorunlu | Google Cloud Console üzerinde oluşturulan **Web application** türündeki OAuth 2.0 Client ID değeri. |
| `GOOGLE_CLIENT_SECRET` | Drive OAuth için Zorunlu | Google Cloud Console OAuth 2.0 Client Secret değeri (**Gizli tutulmalıdır**). |
| `GOOGLE_REDIRECT_URI` | Drive OAuth için Zorunlu | OAuth geri dönüş adresi. Örn. Yerel: `http://localhost:8080/api/oauth/google/callback`, Üretim: `https://<sizin-servis-adresiniz>/api/oauth/google/callback` |
| `TOKEN_ENCRYPTION_KEY` | Drive OAuth için Zorunlu | Refresh ve access token'ların veritabanında şifreli (`Fernet` / `PBKDF2-HMAC-SHA256`) saklanması için kullanılan şifreleme anahtarı. |

### Adım Adım Google Cloud Console ve Secret Kurulum Rehberi

1. **Google Cloud Console'da Proje Oluşturma veya Mevcut Projeyi Seçme:**
   - [Google Cloud Console — Proje Seçici](https://console.cloud.google.com/projectselector2/home/dashboard) sayfasına gidin.
   - Mevcut projenizi seçin veya **Create Project** ile yeni bir proje oluşturun.
2. **Google Drive API'yi Etkinleştirme:**
   - [Google Drive API Kütüphane Sayfası](https://console.cloud.google.com/apis/library/drive.googleapis.com) adresini açın ve **Enable (Etkinleştir)** düğmesine tıklayın.
3. **OAuth Consent Screen ve Test Kullanıcılarını Yapılandırma:**
   - [Google Auth Platform / OAuth Consent Screen](https://console.cloud.google.com/apis/credentials/consent) sayfasına gidin.
   - **External** (veya kurum içi ise **Internal**) seçeneğini belirleyin; uygulama adını, destek e-postasını ve geliştirici iletişim bilgilerini girin.
   - **Scopes (Kapsamlar)** adımında yalnızca `https://www.googleapis.com/auth/drive.file` kapsamını ekleyin ([Google Drive API Scopes Dokümantasyonu](https://developers.google.com/drive/api/guides/api-specific-auth)).
   - Uygulama **Testing** (Test) modundayken **Test users** bölümüne Google Drive hesabını bağlayacak Discord kullanıcılarının Google e-posta adreslerini ekleyin.
4. **OAuth Client ID Oluşturma ve Doğru Uygulama Türünü Seçme:**
   - [Google Cloud Console — Credentials (Kimlik Bilgileri)](https://console.cloud.google.com/apis/credentials) sayfasına gidin.
   - **+ Create Credentials** -> **OAuth client ID** seçeneğine tıklayın.
   - **Application type** olarak **Web application** seçin ([Web Server OAuth 2.0 Dokümantasyonu](https://developers.google.com/identity/protocols/oauth2/web-server)).
5. **Authorized Redirect URI Yapılandırma:**
   - Oluşturduğunuz OAuth istemcisinin **Authorized redirect URIs** listesine tam geri dönüş adresinizi ekleyin:
     - Yerel geliştirme için: `http://localhost:8080/api/oauth/google/callback` (veya port 3000 kullanıyorsanız `http://localhost:3000/api/oauth/google/callback`)
     - Cloud Run / Production için: `https://<sizin-cloud-run-veya-alan-adiniz>/api/oauth/google/callback`
6. **Client ID ve Client Secret Değerlerini Ortama Ekleme:**
   - Google Cloud Console'un verdiği `Client ID` ve `Client Secret` değerlerini kopyalayın; **asla Git deposuna veya kaynak koda yazmayın**.
7. **Token Şifreleme Anahtarını (`TOKEN_ENCRYPTION_KEY`) Güvenli Şekilde Oluşturma:**
   - Terminalde aşağıdaki komutla 32 baytlık kriptografik anahtar üretin:
     ```bash
     python3 -c "import secrets, base64; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())"
     ```
   - Üretilen değeri `TOKEN_ENCRYPTION_KEY` olarak kaydedin.
8. **Ortam Değişkenlerini Yerel Geliştirme ve Production Ortamında Ayarlama:**
   - **Yerel geliştirme (`.env`):** `.env.example` dosyasını `.env` olarak kopyalayın (`.gitignore` içinde olduğundan Git'e eklenmez) ve `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REDIRECT_URI`, `TOKEN_ENCRYPTION_KEY` alanlarını doldurun.
   - **Google Cloud Run / Secret Manager:** [Google Cloud Secret Manager](https://console.cloud.google.com/security/secret-manager) ve [Cloud Run Services](https://console.cloud.google.com/run) üzerinden ilgili servisin *Variables & Secrets* sekmesine bu 4 değişkeni ekleyin.
   - **GitHub Actions Secrets (İsteğe Bağlı CI/CD):** Repo içindeki `Settings -> Secrets and variables -> Actions` sayfasına ekleyebilirsiniz.
9. **OAuth Test Modu, Test Kullanıcıları ve Production'a Geçiş Kısıtları:**
   - Uygulama *Publishing status: Testing* durumundayken yalnızca *Test users* listesine eklenen Google hesapları (en fazla 100 test kullanıcısı) `/drive_bagla` ile giriş yapabilir; listede olmayan kullanıcılar `Error 403: access_denied` alır.
   - Ayrıca Google OAuth dokümantasyonuna göre *Testing* modundaki uygulamaların verdiği refresh token'lar 7 gün sonra sona erebilir.
   - Herkese açık kullanıma geçmek için *OAuth consent screen* sayfasında **Publish App** düğmesiyle uygulamayı *In production* durumuna geçirin (`drive.file` kapsamı hassas olmayan/önerilen kapsam kategorisinde yer aldığı için tam Drive erişimine kıyasla doğrulama süreci çok daha basittir).

