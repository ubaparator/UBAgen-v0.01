"""
UBAgen — health_api.py
Lightweight FastAPI application providing HTTP health check and real-time statistics endpoints.

Exposes:
- GET /api/stats : Complete real-time host, container, and Discord bot hardware metrics.
- GET /stats     : Alias for /api/stats for convenience.
- GET /health    : Lightweight health probe for container orchestrators (Docker, Render, Railway, Fly.io).
- GET /          : Serves React dashboard if built (dist/), otherwise service identification JSON.

Configured with CORS support and zero secret leakage.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import os
import time
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from stats import collect_all_stats

SERVICE_START_TIME = time.time()


def create_health_app(bot: Any = None) -> FastAPI:
    """
    Factory creating the FastAPI application.
    Mounts CORS, /health, /api/stats, and static files when present.
    """
    app = FastAPI(
        title="UBAgen System Monitor & Health API",
        description="Lightweight HTTP monitoring service for UBAgen Discord Bot backend",
        version="1.0.0",
        docs_url=None,  # Disabled in production to conserve memory
        redoc_url=None,
    )

    # Enable CORS for cross-origin dashboard requests
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET", "OPTIONS"],
        allow_headers=["*"],
    )

    # Store bot instance in application state
    app.state.bot = bot

    @app.get("/api/stats", summary="Real-time Hardware & Bot Statistics")
    @app.get("/stats", summary="Real-time Hardware & Bot Statistics (Alias)")
    async def get_stats_api() -> JSONResponse:
        """
        Returns full live hardware statistics (CPU, RAM, Disk, Uptime, Process, Toolchain, Bot).
        Runs non-blocking inside asyncio.to_thread to avoid blocking gateway loop.
        """
        current_bot = getattr(app.state, "bot", None)
        stats = await asyncio.to_thread(collect_all_stats, current_bot)
        return JSONResponse(
            content=stats,
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
            },
        )

    @app.get("/health", summary="Backend Health Check")
    async def health_check() -> JSONResponse:
        """
        Lightweight health check endpoint for container orchestrators.
        Always returns HTTP 200 while HTTP service is alive, detailing bot gateway status.
        """
        current_bot = getattr(app.state, "bot", None)
        tracker = getattr(current_bot, "tracker", None) if current_bot else None

        if tracker:
            bot_is_ready = tracker.is_ready
            bot_connected = tracker.connected
            latency_ms = tracker.latency_ms
            bot_user = tracker.user
            diagnostic_msg = tracker.diagnostic_message
            bot_state = tracker.status
        else:
            bot_is_ready = bool(current_bot and hasattr(current_bot, "is_ready") and current_bot.is_ready())
            bot_connected = bot_is_ready
            latency_ms = (
                round(current_bot.latency * 1000, 2)
                if (bot_is_ready and current_bot and getattr(current_bot, "latency", None) is not None)
                else None
            )
            bot_user = str(current_bot.user) if (bot_is_ready and current_bot and getattr(current_bot, "user", None)) else None
            bot_state = "connected" if bot_is_ready else "disconnected"
            diagnostic_msg = "Bot is connected." if bot_is_ready else "Bot is disconnected."

        uptime_sec = int(time.time() - SERVICE_START_TIME)

        payload = {
            "status": "healthy",
            "service": "ubagen-backend",
            "http": "alive",
            "discord_bot": {
                "status": bot_state,
                "connected": bot_connected,
                "ready": bot_is_ready,
                "latency_ms": latency_ms,
                "user": bot_user,
                "diagnostic_message": diagnostic_msg,
            },
            "uptime_seconds": uptime_sec,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        return JSONResponse(content=payload, status_code=200)

    # Mount static frontend files if dist/ directory exists (production container deployment)
    if os.path.isdir("dist"):
        app.mount("/", StaticFiles(directory="dist", html=True), name="static")
    else:
        @app.get("/", summary="Root Status")
        async def root_index() -> dict[str, Any]:
            return {
                "service": "UBAgen System Monitor Backend",
                "status": "online",
                "endpoints": {
                    "stats": "/api/stats",
                    "health": "/health",
                },
                "version": "1.0.0",
            }

    return app
