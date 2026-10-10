"""
UBAgen — health_api.py
Lightweight FastAPI application providing HTTP health check endpoints.

Exposes:
- GET /health : Diagnostic status for container orchestration (Docker, Cloud Run, Render, Railway).
- GET /       : Service identification.

Ensures sensitive secrets (e.g. DISCORD_BOT_TOKEN) are never leaked.
"""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# Service start timestamp for uptime calculation
SERVICE_START_TIME = time.time()


def create_health_app(bot: Any = None) -> FastAPI:
    """
    Factory creating the FastAPI health check application.
    Accepts the Discord bot instance to query real-time gateway status.
    """
    app = FastAPI(
        title="UBAgen Health API",
        description="Lightweight HTTP health check service for UBAgen Discord Bot backend",
        version="1.0.0",
        docs_url=None,   # Disable interactive docs in production to save memory
        redoc_url=None,
    )

    # Store bot in application state
    app.state.bot = bot

    @app.get("/health", summary="Backend Health Check")
    async def health_check() -> JSONResponse:
        """
        Returns JSON status indicating whether HTTP server is alive
        and whether the Discord bot gateway connection is established.
        """
        current_bot = getattr(app.state, "bot", None)

        bot_is_ready = bool(current_bot and current_bot.is_ready())
        latency_ms = (
            round(current_bot.latency * 1000, 2)
            if (bot_is_ready and current_bot.latency is not None)
            else None
        )
        bot_user = str(current_bot.user) if (bot_is_ready and current_bot.user) else None

        # Calculate uptime
        uptime_sec = int(time.time() - SERVICE_START_TIME)

        # Determine overall health state
        # Note: During the initial 5-10 seconds of container boot, the bot may still be connecting
        if bot_is_ready:
            overall_status = "healthy"
            http_code = 200
        else:
            overall_status = "starting"
            http_code = 200  # Return 200 so orchestrator doesn't kill container during gateway handshake

        payload = {
            "status": overall_status,
            "service": "ubagen-backend",
            "http": "alive",
            "discord_bot": {
                "connected": bot_is_ready,
                "latency_ms": latency_ms,
                "user": bot_user,
                "ready": bot_is_ready,
            },
            "uptime_seconds": uptime_sec,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return JSONResponse(content=payload, status_code=http_code)

    @app.get("/", summary="Root Index")
    async def root_index() -> dict[str, Any]:
        """Root endpoint confirming service identity."""
        return {
            "service": "UBAgen Discord Bot Backend",
            "status": "online",
            "health_endpoint": "/health",
            "version": "1.0.0",
        }

    return app
