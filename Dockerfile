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
# - ffmpeg: Required for multimedia tools (monitored in dashboard & /stats)
# - aria2: High-speed download utility (monitored in dashboard & /stats)
# - curl: For container health checks
# - procps & ca-certificates: For process monitoring and SSL
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    curl \
    procps \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

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

# Secure ownership
RUN chown -R ubagen:ubagen /app

USER ubagen

EXPOSE 8080

# Health check targeting the FastAPI /health probe
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Launch the combined backend and dashboard server
CMD ["python", "main.py"]
