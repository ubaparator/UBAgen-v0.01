# ==============================================================================
# UBAgen System Monitor & Discord Bot Dockerfile
# Stage 1: Build modern React frontend dashboard
# Stage 2: Production Python 3.12 runtime with FFmpeg, aria2 & FastAPI
# ==============================================================================

# --- Stage 1: Build Frontend Dashboard ---
FROM node:20-slim AS frontend-builder
WORKDIR /build

COPY package.json ./
RUN npm install

COPY . ./
RUN npm run build

# --- Stage 2: Production Runtime ---
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8080 \
    HOST=0.0.0.0

# Install runtime dependencies:
# - ffmpeg & ffprobe: Required for multimedia tools (monitored in dashboard & /stats)
# - aria2 (aria2c): High-speed download utility (monitored in dashboard & /stats)
# - rclone: Official release with native MEGA and Google Drive backends
# - curl & unzip: For container health checks, binary installation and verification
# - procps & ca-certificates: For process monitoring and SSL
# - fontconfig & fonts-dejavu-core: For subtitle rendering
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    curl \
    unzip \
    procps \
    ca-certificates \
    fontconfig \
    fonts-dejavu-core \
    && curl -fsSL https://rclone.org/install.sh | bash \
    && rm -rf /var/lib/apt/lists/*

# Strict Docker build verification for all four required encode & transfer tools:
# aria2c, ffmpeg, ffprobe, and rclone MUST be installed and executable on PATH.
# If any tool fails or is missing, the Docker image build will fail immediately.
RUN echo "=== [Build Verification 1/4] aria2c ===" && aria2c --version \
    && echo "=== [Build Verification 2/4] ffmpeg ===" && ffmpeg -version \
    && echo "=== [Build Verification 3/4] ffprobe ===" && ffprobe -version \
    && echo "=== [Build Verification 4/4] rclone ===" && rclone version \
    && echo "=== [Build Verification] rclone backends ===" \
    && rclone help backends | grep -qi "mega" \
    && rclone help backends | grep -qi "drive"

WORKDIR /app

# Create dedicated non-root user
RUN groupadd -r ubagen && useradd -r -g ubagen -d /app -s /sbin/nologin ubagen

# Install Python packages
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy backend Python code
COPY . /app/

# Copy built frontend assets from Stage 1 into /app/dist
COPY --from=frontend-builder /build/dist /app/dist

# Pre-create separated temporary worker and output directories and secure ownership for non-root user
RUN mkdir -p /app/download_temp/encode_temp /app/download_temp/worker_jobs /app/video_outputs && \
    chown -R ubagen:ubagen /app

USER ubagen

# Verify non-root execution capability on PATH for all four tools
RUN aria2c --version >/dev/null \
    && ffmpeg -version >/dev/null \
    && ffprobe -version >/dev/null \
    && rclone version >/dev/null

EXPOSE 8080

# Health check targeting the FastAPI /health probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Launch the combined backend and dashboard server
CMD ["python", "main.py"]
