# ==============================================================================
# UBAgen Discord Bot Backend Dockerfile
# Base: Python 3.12 Slim (Debian Bookworm)
# ==============================================================================
FROM python:3.12-slim

# Set environment variables to prevent bytecode generation and ensure unbuffered output
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    PORT=8080 \
    HOST=0.0.0.0

# Install runtime dependencies:
# - ffmpeg: Required for multimedia tools (reported in /stats)
# - aria2: High-speed download utility (reported in /stats)
# - curl: For container health checks
# - procps & ca-certificates: For process monitoring and secure HTTPS connections
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    aria2 \
    curl \
    procps \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Set standard working directory
WORKDIR /app

# Create a dedicated non-root system user and group for security hardening
RUN groupadd -r ubagen && useradd -r -g ubagen -d /app -s /sbin/nologin ubagen

# Copy dependency definition and install Python packages
COPY requirements.txt /app/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY . /app/

# Set ownership of the application directory to the non-root user
RUN chown -R ubagen:ubagen /app

# Switch execution context to non-root user
USER ubagen

# Expose HTTP health check port
EXPOSE 8080

# Container healthcheck targeting the FastAPI /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

# Launch the combined Discord bot and FastAPI server
CMD ["python", "main.py"]
