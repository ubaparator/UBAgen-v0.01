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
from contextlib import suppress
from datetime import datetime, timezone
import hmac
import logging
import os
import time
from typing import Any, Optional

from fastapi import FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from encoder_worker import dispatch_encode_job_background
from job_store import (
    FirestoreUnavailableError,
    discover_and_apply_runtime_config,
    get_job_store,
    normalize_video_codec,
)
from stats import collect_all_stats, get_recent_logs, install_log_buffer_handler
from storage_manager import get_storage_manager
from subtitle_extract import normalize_language_code, validate_magnet_uri

SERVICE_START_TIME = time.time()
logger = logging.getLogger("ubagen.health")
install_log_buffer_handler()


def verify_admin_token(authorization_header: Optional[str], expected_token: str) -> bool:
    """
    Validate 'Authorization: Bearer <token>' against expected_token using constant-time comparison.
    Never logs or exposes tokens.
    """
    clean_expected = (expected_token or "").strip()
    if not clean_expected or not authorization_header:
        return False

    parts = authorization_header.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return False

    supplied_token = parts[1].strip()
    if not supplied_token:
        return False

    return hmac.compare_digest(
        supplied_token.encode("utf-8"),
        clean_expected.encode("utf-8"),
    )


def _degraded_stats(reason: str) -> dict[str, Any]:
    """Return the same public schema without inventing unavailable measurements."""
    return {
        "status": "degraded",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "epoch_time": time.time(),
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
            "mount_point": "/", "fs_type": "Unavailable", "writable": False,
            "is_persistent": False, "persistence_verified": False,
            "storage_mode": "unavailable", "decision_branch": "UNAVAILABLE", "summary": reason,
        },
        "storage": None,
        "binaries": {
            "ffmpeg": {"installed": False, "version": "Unavailable", "path": None},
            "ffprobe": {"installed": False, "version": "Unavailable", "path": None},
            "aria2": {"installed": False, "version": "Unavailable", "path": None},
        },
        "env_vars": [],
    }


def create_health_app(
    bot: Any = None,
    bot_manager: Any = None,
    restart_cooldown_seconds: float = 15.0,
) -> FastAPI:
    """
    Factory creating the FastAPI application.
    Mounts CORS, /health, /api/stats, /api/admin/restart-bot, and static files when present.
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
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Origin", "X-Requested-With", "Content-Type", "Accept", "Authorization", "*"],
    )

    # Store bot instance, lifecycle manager, and rate-limiting state in application state.
    app.state.bot = bot
    app.state.bot_manager = bot_manager
    app.state.last_stats = None
    app.state.restart_lock = asyncio.Lock()
    app.state.is_restarting = False
    app.state.last_restart_at = 0.0
    app.state.restart_cooldown_seconds = restart_cooldown_seconds
    app.state.restart_bot = None

    if bot_manager is not None and hasattr(bot_manager, "attach_health_app"):
        bot_manager.attach_health_app(app)

    async def _fallback_restart_bot() -> tuple[int, dict[str, Any]]:
        """Fallback in-process restart handler when create_health_app is used without BotLifecycleManager."""
        cooldown = float(getattr(app.state, "restart_cooldown_seconds", 15.0))
        if getattr(app.state, "is_restarting", False) or app.state.restart_lock.locked():
            retry_after = max(1, int(round(cooldown)))
            return 429, {
                "error": f"A bot restart is already in progress. Wait {retry_after} seconds before trying again.",
                "retry_after": retry_after,
            }

        now = time.monotonic()
        last_at = float(getattr(app.state, "last_restart_at", 0.0))
        if last_at > 0 and (now - last_at) < cooldown:
            remaining = max(1, int(round(cooldown - (now - last_at))))
            return 429, {
                "error": f"A bot restart was requested recently. Wait {remaining} seconds before trying again.",
                "retry_after": remaining,
            }

        current_bot = getattr(app.state, "bot", None)
        bot_token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        token_configured = bool(bot_token) and bot_token not in (
            "your_discord_bot_token_here",
            "MY_DISCORD_BOT_TOKEN",
            "CHANGE_ME",
        )
        if current_bot is None and not token_configured:
            return 503, {
                "error": "Cannot restart Discord bot: DISCORD_BOT_TOKEN is not configured on the server.",
            }

        async with app.state.restart_lock:
            now = time.monotonic()
            last_at = float(getattr(app.state, "last_restart_at", 0.0))
            if last_at > 0 and (now - last_at) < cooldown:
                remaining = max(1, int(round(cooldown - (now - last_at))))
                return 429, {
                    "error": f"A bot restart was requested recently. Wait {remaining} seconds before trying again.",
                    "retry_after": remaining,
                }

            app.state.is_restarting = True
            try:
                if current_bot is not None:
                    tracker = getattr(current_bot, "tracker", None)
                    if tracker is not None:
                        tracker.connected = False
                        tracker.is_ready = False
                        tracker.status = "connecting"
                        tracker.diagnostic_message = "Bot restart requested. Reconnecting to Discord Gateway..."
                    if hasattr(current_bot, "close") and callable(current_bot.close):
                        res = current_bot.close()
                        if asyncio.iscoroutine(res):
                            await asyncio.wait_for(res, timeout=5.0)
                    if hasattr(current_bot, "restart") and callable(current_bot.restart):
                        res = current_bot.restart()
                        if asyncio.iscoroutine(res):
                            await res

                app.state.last_stats = None
                app.state.last_restart_at = time.monotonic()
                return 202, {
                    "status": "restarting",
                    "message": "Bot restart requested. The worker should reconnect shortly.",
                }
            except Exception as exc:
                logger.exception("Error while restarting Discord bot (%s).", type(exc).__name__)
                return 500, {
                    "error": "Could not restart the Discord bot due to an internal error.",
                }
            finally:
                app.state.is_restarting = False

    @app.api_route(
        "/api/admin/restart-bot",
        methods=["POST", "GET", "PUT", "DELETE", "PATCH"],
        summary="Authenticated Discord Bot Restart",
    )
    async def restart_bot_api(
        request: Request = None,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        """
        Authenticated endpoint to restart the Discord bot connection in-process without
        stopping the FastAPI web monitor.
        Requires 'Authorization: Bearer <UBAGEN_ADMIN_TOKEN>'.
        """
        method = request.method.upper() if (request is not None and getattr(request, "method", None)) else "POST"
        if method != "POST":
            return JSONResponse(
                content={"error": "Method not allowed. Use POST /api/admin/restart-bot."},
                status_code=405,
                headers={"Allow": "POST, OPTIONS", "Cache-Control": "no-store"},
            )

        # UBAGEN_ADMIN_TOKEN is strictly required on the server; never fall back to HOST or other vars.
        expected_token = os.getenv("UBAGEN_ADMIN_TOKEN", "").strip()
        if not expected_token:
            return JSONResponse(
                content={
                    "error": "Bot restart is disabled: UBAGEN_ADMIN_TOKEN is not configured on the server."
                },
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

        auth_header: Optional[str] = None
        if isinstance(authorization, str):
            auth_header = authorization
        elif request is not None and hasattr(request, "headers"):
            auth_header = request.headers.get("authorization")

        if not verify_admin_token(auth_header, expected_token):
            return JSONResponse(
                content={"error": "Unauthorized. The admin token is missing or invalid."},
                status_code=401,
                headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
            )

        restart_fn = getattr(app.state, "restart_bot", None) or _fallback_restart_bot
        status_code, payload = await restart_fn()
        headers = {"Cache-Control": "no-store"}
        if status_code == 429 and "retry_after" in payload:
            headers["Retry-After"] = str(payload["retry_after"])
        return JSONResponse(content=payload, status_code=status_code, headers=headers)

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
            logger.exception("Statistics collection failed")
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
            if current_bot is not None:
                try:
                    if hasattr(current_bot, "is_ready") and callable(current_bot.is_ready) and current_bot.is_ready():
                        raw_lat = getattr(current_bot, "latency", None)
                        if isinstance(raw_lat, (int, float)) and raw_lat >= 0 and raw_lat == raw_lat and raw_lat != float("inf"):
                            tracker.latency_ms = round(float(raw_lat) * 1000, 1)
                except Exception:
                    pass
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

    @app.get("/api/storage", summary="Runtime Storage Infrastructure & Output Files Audit")
    async def get_storage_api() -> JSONResponse:
        """Return detailed runtime storage decision tree, mount info, persistent vs worker scratch separation, and stored output files."""
        mgr = get_storage_manager()
        store = get_job_store()
        with suppress(Exception):
            await asyncio.to_thread(store.cleanup_expired_jobs)
        inspection = await asyncio.to_thread(mgr.inspect)
        outputs = await asyncio.to_thread(mgr.list_output_files)
        jobs = await asyncio.to_thread(store.list_jobs, None, None, 25)
        return JSONResponse(
            content={
                "status": "online",
                "storage": inspection,
                "outputs": outputs,
                "jobs": [j.to_public_dict() for j in jobs],
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/jobs", summary="List Persistent Video Encode Jobs")
    async def list_jobs_api(
        user_id: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 50,
        x_user_id: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        """Return persistent encode jobs from Firestore/SQLite store."""
        store = get_job_store()
        try:
            with suppress(Exception):
                await asyncio.to_thread(store.cleanup_expired_jobs)
            requester = (x_user_id or user_id or "").strip() or None
            jobs = await asyncio.to_thread(store.list_jobs, user_id, status, limit)
        except FirestoreUnavailableError as exc:
            return JSONResponse(
                content={"status": "firestore_unavailable", "error": str(exc)},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            content={
                "status": "online",
                "backend": store.get_backend_status(),
                "count": len(jobs),
                "jobs": [j.to_public_dict(requester_user_id=requester) for j in jobs],
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/jobs", summary="Create and Optionally Dispatch an Encode Job")
    async def create_job_api(request: Request) -> JSONResponse:
        """Create a persistent encode job and optionally dispatch it to the Cloud Run Job worker."""
        try:
            body = await request.json()
        except Exception:
            return JSONResponse(
                content={"error": "Geçersiz JSON gövdesi."},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        user_id = str(body.get("user_id") or "").strip()
        magnet = str(body.get("magnet") or "").strip()
        codec = str(body.get("codec") or "h264").strip()
        subtitle_lang = str(body.get("subtitle_lang") or body.get("dil") or "tur").strip()
        subtitle_source = body.get("subtitle_source")
        intro_source = body.get("intro_source")
        should_dispatch = bool(body.get("dispatch", False))

        if not user_id:
            return JSONResponse(
                content={"error": "Kullanıcı kimliği (`user_id`) zorunludur."},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        ok_mag, mag_or_err = validate_magnet_uri(magnet)
        if not ok_mag:
            return JSONResponse(
                content={"error": mag_or_err},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        ok_codec, canonical_codec, enc_or_err = normalize_video_codec(codec)
        if not ok_codec:
            return JSONResponse(
                content={"error": enc_or_err},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        ok_lang, lang_or_err = normalize_language_code(subtitle_lang)
        if not ok_lang:
            return JSONResponse(
                content={"error": lang_or_err},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )

        store = get_job_store()
        try:
            job = await asyncio.to_thread(
                store.create_job,
                user_id=user_id,
                magnet=mag_or_err,
                codec=canonical_codec,
                subtitle_lang=lang_or_err,
                subtitle_source=subtitle_source,
                intro_source=intro_source,
                user_name=body.get("user_name"),
            )
            dispatch_result = None
            if should_dispatch:
                dispatch_result = await asyncio.to_thread(dispatch_encode_job_background, job.job_id)
                job = await asyncio.to_thread(store.get_job, job.job_id) or job
        except FirestoreUnavailableError as exc:
            return JSONResponse(
                content={"status": "firestore_unavailable", "error": str(exc)},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

        return JSONResponse(
            content={
                "status": "created",
                "job": job.to_public_dict(requester_user_id=user_id),
                "dispatch": dispatch_result,
            },
            status_code=201,
            headers={"Cache-Control": "no-store"},
        )

    @app.post("/api/jobs/cleanup-expired", summary="Server-Side 7200s Retention Cleanup & Stale Worker Recovery")
    async def cleanup_expired_jobs_api(
        request: Request = None,
        authorization: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        """
        Trigger server-side 7200-second expiration cleanup (deleting expired objects from GCS and revoking access)
        and recover any crashed/stale running jobs. Can be called by Cloud Scheduler or internal timers.
        """
        expected_token = os.getenv("UBAGEN_ADMIN_TOKEN", "").strip()
        auth_header = authorization
        if not auth_header and request is not None and hasattr(request, "headers"):
            auth_header = request.headers.get("authorization")

        # If UBAGEN_ADMIN_TOKEN is configured and caller supplies an Authorization header, verify it
        if expected_token and auth_header and not verify_admin_token(auth_header, expected_token):
            return JSONResponse(
                content={"error": "Unauthorized cleanup trigger."},
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )

        store = get_job_store()
        try:
            cleanup_res = await asyncio.to_thread(store.cleanup_expired_jobs)
            recovered = await asyncio.to_thread(store.recover_stale_running_jobs)
        except FirestoreUnavailableError as exc:
            return JSONResponse(
                content={"status": "firestore_unavailable", "error": str(exc)},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        return JSONResponse(
            content={
                "status": "ok",
                "cleanup": cleanup_res,
                "recovered_crashed_jobs": [j.job_id for j in recovered],
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/jobs/{job_id}", summary="Get Single Encode Job Progress & Telemetry")
    async def get_job_detail_api(
        job_id: str,
        user_id: Optional[str] = None,
        x_user_id: Optional[str] = Header(default=None),
    ) -> JSONResponse:
        """Retrieve a single job record from persistent storage."""
        store = get_job_store()
        try:
            job = await asyncio.to_thread(store.get_job, job_id)
            if job is None:
                return JSONResponse(
                    content={"error": f"Encode işi bulunamadı: {job_id}"},
                    status_code=404,
                    headers={"Cache-Control": "no-store"},
                )
            if job.is_expired() and not job.deleted_from_gcs:
                await asyncio.to_thread(store.cleanup_expired_jobs)
                job = await asyncio.to_thread(store.get_job, job_id) or job
        except FirestoreUnavailableError as exc:
            return JSONResponse(
                content={"status": "firestore_unavailable", "error": str(exc)},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )

        requester = (x_user_id or user_id or "").strip() or None
        return JSONResponse(
            content={
                "status": "online",
                "job": job.to_public_dict(requester_user_id=requester),
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/jobs/{job_id}/download", summary="Authorized & 7200s-Bounded GCS Video Download")
    async def download_job_output_api(
        job_id: str,
        user_id: Optional[str] = None,
        token: Optional[str] = None,
        x_user_id: Optional[str] = Header(default=None),
        authorization: Optional[str] = Header(default=None),
    ):
        """
        Serve or stream a completed video output from Google Cloud Storage:
        - Enforces per-user ownership (`user_id` + `token`) -> returns 403 Forbidden on cross-user access.
        - Enforces 7200-second retention -> actively deletes expired object from GCS and returns 410 Gone.
        - Verifies `gcs_upload_verified` is True before serving -> returns 409 if not verified.
        """
        store = get_job_store()
        try:
            job = await asyncio.to_thread(store.get_job, job_id)
        except FirestoreUnavailableError as exc:
            return JSONResponse(
                content={"status": "firestore_unavailable", "error": str(exc)},
                status_code=503,
                headers={"Cache-Control": "no-store"},
            )
        if job is None:
            return JSONResponse(
                content={"error": "İstenen encode işi bulunamadı."},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )

        effective_user = (user_id or x_user_id or "").strip()
        effective_token = (token or "").strip()
        if not effective_token and authorization and authorization.lower().startswith("bearer "):
            effective_token = authorization.split(None, 1)[1].strip()

        if not job.can_user_access(effective_user, effective_token):
            return JSONResponse(
                content={
                    "error": "Erişim reddedildi (403): Bu video çıktısını yalnızca işi başlatan kullanıcı indirebilir."
                },
                status_code=403,
                headers={"Cache-Control": "no-store"},
            )

        if job.is_expired():
            # Actively purge the object from GCS if it hasn't been deleted yet
            await asyncio.to_thread(store.cleanup_expired_jobs)
            return JSONResponse(
                content={
                    "error": "Çıktının 7200 saniyelik (2 saat) saklama süresi dolduğu için erişim iptal edildi ve dosya GCS'den silindi."
                },
                status_code=410,
                headers={"Cache-Control": "no-store"},
            )

        if job.status != "completed" or not job.gcs_upload_verified or not job.gcs_bucket or not job.gcs_object_name:
            return JSONResponse(
                content={
                    "error": "Video çıktısı henüz tamamlanmadı veya Google Cloud Storage yüklemesi doğrulanmadı."
                },
                status_code=409,
                headers={"Cache-Control": "no-store"},
            )

        local_file, raw_bytes = await asyncio.to_thread(
            store.gcs.open_download_stream_or_path,
            job.gcs_bucket,
            job.gcs_object_name,
        )
        filename = job.output_filename or f"{job.job_id}.mp4"
        if local_file is not None and local_file.is_file():
            return FileResponse(
                path=str(local_file),
                filename=filename,
                media_type="video/mp4",
                headers={"Cache-Control": "no-store"},
            )
        if raw_bytes is not None:
            return Response(
                content=raw_bytes,
                media_type="video/mp4",
                headers={
                    "Content-Disposition": f'attachment; filename="{filename}"',
                    "Cache-Control": "no-store",
                },
            )

        return JSONResponse(
            content={"error": "GCS nesnesine erişilemedi veya dosya silinmiş."},
            status_code=404,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/logs", summary="Redacted Real-Time Backend Logs")
    async def get_logs_api(limit: int = 100) -> JSONResponse:
        """Return recent redacted backend log entries from the in-memory ring buffer."""
        entries = get_recent_logs(limit=limit)
        return JSONResponse(
            content={
                "status": "online",
                "count": len(entries),
                "logs": entries,
            },
            status_code=200,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/outputs/{filename}", summary="Download Stored Video or Subtitle Output")
    async def download_output_file(filename: str):
        """Serve a stored file from video_outputs with path-traversal protection and HTTP Range support."""
        mgr = get_storage_manager()
        resolved = await asyncio.to_thread(mgr.resolve_output_file, filename)
        if resolved is None:
            return JSONResponse(
                content={"error": "Requested output file was not found or has expired."},
                status_code=404,
                headers={"Cache-Control": "no-store"},
            )
        return FileResponse(
            path=str(resolved),
            filename=resolved.name,
            media_type="application/octet-stream",
        )

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
