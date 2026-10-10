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


def _degraded_stats(reason: str) -> dict[str, Any]:
    """Return the same public schema without inventing unavailable measurements."""
    return {
        "status": "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "diagnostic_message": reason,
        "bot": {
            "status": "unavailable", "connected": False, "is_ready": False,
            "diagnostic_message": "Bot statistics are temporarily unavailable.",
            "latency_ms": None, "user": "Unavailable", "guild_count": 0,
            "uptime_seconds": None, "uptime_str": "Unavailable",
            "sync_status": None, "client_id": None, "invite_url": None,
        },
        "compute": {
            "cpu_usage_pct": None, "logical_cores": None, "physical_cores": None,
            "os_name": "Unavailable", "python_version": "Unavailable", "python_impl": "Unavailable",
            "system_uptime_sec": None, "system_uptime_str": "Unavailable",
            "backend_uptime_sec": None, "backend_uptime_str": "Unavailable",
        },
        "memory": {
            "system_total_bytes": None, "system_total_str": "Unavailable",
            "system_used_bytes": None, "system_used_str": "Unavailable",
            "system_available_bytes": None, "system_available_str": "Unavailable",
            "system_free_bytes": None, "system_free_str": "Unavailable", "system_percent": None,
            "container_limit": {
                "is_limited": False, "memory_limited": False, "cpu_limited": False,
                "limit_bytes": None, "limit_str": "Unavailable", "usage_bytes": None,
                "usage_str": None, "cpu_quota_cores": None, "cpu_quota_str": "Unavailable",
                "type": "Unavailable", "description": "Container metrics unavailable",
            },
            "process_rss_bytes": None, "process_rss_str": "Unavailable", "process_percent": None,
        },
        "disk": {
            "total_bytes": None, "total_str": "Unavailable", "used_bytes": None,
            "used_str": "Unavailable", "free_bytes": None, "free_str": "Unavailable", "percent": None,
        },
        "binaries": {
            "ffmpeg": {"installed": False, "version": "Unavailable", "path": None},
            "aria2": {"installed": False, "version": "Unavailable", "path": None},
        },
        "env_vars": [],
    }


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

    # Store bot instance and last valid snapshot in application state.
    app.state.bot = bot
    app.state.last_stats = None

    @app.get("/api/stats", summary="Real-time Hardware & Bot Statistics")
    @app.get("/stats", summary="Real-time Hardware & Bot Statistics (Alias)")
    async def get_stats_api() -> JSONResponse:
        """
        Returns full live hardware statistics (CPU, RAM, Disk, Uptime, Process, Toolchain, Bot).
        Runs non-blocking inside asyncio.to_thread to avoid blocking gateway loop.
        """
        current_bot = getattr(app.state, "bot", None)
        try:
            stats = await asyncio.to_thread(collect_all_stats, current_bot)
            app.state.last_stats = stats
        except Exception as exc:
            # Keep a last known good snapshot if possible; otherwise return a typed
            # degraded payload with HTTP 200 so every frontend can render/retry cleanly.
            import logging
            logging.getLogger("ubagen.health").exception("Statistics collection failed")
            stats = getattr(app.state, "last_stats", None) or _degraded_stats(
                f"Statistics temporarily unavailable ({type(exc).__name__})."
            )
        return JSONResponse(
            content=stats,
            status_code=200,
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
