"""Small Render-hosted Discord dispatcher for GitHub Actions encode jobs.

Secrets are read only from the service environment. Never place tokens in the Pages UI.
"""
from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
from urllib.parse import urlparse

import aiohttp
import discord
from aiohttp import web
from discord import app_commands

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ubagen.dispatcher")

REPOSITORY = os.getenv("GITHUB_REPOSITORY", "ubaparator/UBAgen-v0.01")
REF = os.getenv("GITHUB_REF", "main")
PAGES_ORIGIN = os.getenv("PAGES_ORIGIN", "https://ubaparator.github.io").rstrip("/")
ADMIN_TOKEN = os.getenv("UBAGEN_ADMIN_TOKEN", "")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")
ADMIN_IDS = {part.strip() for part in os.getenv("UBAGEN_DISCORD_ADMIN_IDS", "").split(",") if part.strip()}
API_ROOT = "https://api.github.com"
HTTP_SESSION: aiohttp.ClientSession | None = None


def validate_public_url(value: str, field: str) -> str:
    value = (value or "").strip()
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(field + " must be a direct http(s) URL.")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        raise ValueError(field + " must point to a public host.")
    try:
        ip = ipaddress.ip_address(host)
        if not ip.is_global:
            raise ValueError(field + " must point to a public host.")
    except ValueError as exc:
        if "must point" in str(exc):
            raise
        # A hostname (rather than a literal IP) is expected here.
    return value


def require_admin(request: web.Request) -> bool:
    supplied = request.headers.get("X-Admin-Key", "")
    return bool(ADMIN_TOKEN) and hmac.compare_digest(supplied, ADMIN_TOKEN)


def cors_headers(request: web.Request) -> dict[str, str]:
    origin = request.headers.get("Origin", "")
    allowed = PAGES_ORIGIN
    headers = {"Vary": "Origin", "Access-Control-Allow-Headers": "Content-Type, X-Admin-Key",
               "Access-Control-Allow-Methods": "GET, POST, OPTIONS"}
    if origin == allowed:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


async def github_request(method: str, path: str, **kwargs):
    if not GITHUB_TOKEN:
        raise RuntimeError("Render GITHUB_TOKEN secret is not configured.")
    assert HTTP_SESSION is not None
    headers = {
        "Authorization": "Bearer " + GITHUB_TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    async with HTTP_SESSION.request(method, API_ROOT + path, headers=headers, **kwargs) as response:
        body = await response.text()
        if response.status >= 400:
            raise RuntimeError("GitHub API returned " + str(response.status) + ": " + body[:500])
        if not body:
            return None
        return json.loads(body)


async def dispatch_encode(source_url: str, subtitle_url: str, codec: str) -> None:
    source_url = validate_public_url(source_url, "Video URL")
    if subtitle_url:
        subtitle_url = validate_public_url(subtitle_url, "Subtitle URL")
    if codec not in {"h264", "h265"}:
        raise ValueError("Codec must be h264 or h265.")
    payload = {
        "ref": REF,
        "inputs": {
            "source_url": source_url,
            "subtitle_url": subtitle_url,
            "codec": codec,
        },
    }
    await github_request(
        "POST",
        "/repos/" + REPOSITORY + "/actions/workflows/encode-job.yml/dispatches",
        json=payload,
    )


async def recent_runs(limit: int = 10) -> list[dict]:
    data = await github_request(
        "GET",
        "/repos/" + REPOSITORY + "/actions/runs",
        params={"per_page": min(max(limit, 1), 20)},
    )
    return [
        {
            "id": run.get("id"),
            "name": run.get("name") or run.get("path") or "GitHub Actions",
            "status": run.get("status"),
            "conclusion": run.get("conclusion"),
            "created_at": run.get("created_at"),
            "html_url": run.get("html_url"),
            "event": run.get("event"),
        }
        for run in data.get("workflow_runs", [])
    ]


async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "ubagen-dispatcher"})


async def options_handler(request: web.Request) -> web.Response:
    return web.Response(status=204, headers=cors_headers(request))


async def api_runs(request: web.Request) -> web.Response:
    headers = cors_headers(request)
    if not require_admin(request):
        return web.json_response({"error": "Unauthorized. Enter the Render UBAGEN_ADMIN_TOKEN in the dashboard."},
                                 status=401, headers=headers)
    try:
        return web.json_response({"runs": await recent_runs(15)}, headers=headers)
    except Exception as exc:
        log.exception("Could not load GitHub Actions runs")
        return web.json_response({"error": str(exc)}, status=502, headers=headers)


async def api_dispatch(request: web.Request) -> web.Response:
    headers = cors_headers(request)
    if not require_admin(request):
        return web.json_response({"error": "Unauthorized. Check the dashboard admin key."},
                                 status=401, headers=headers)
    try:
        data = await request.json()
        await dispatch_encode(
            str(data.get("source_url", "")),
            str(data.get("subtitle_url", "")),
            str(data.get("codec", "h264")),
        )
        return web.json_response({
            "ok": True,
            "message": "GitHub Actions encode job queued.",
            "workflow_url": "https://github.com/" + REPOSITORY + "/actions/workflows/encode-job.yml",
        }, status=202, headers=headers)
    except (ValueError, json.JSONDecodeError) as exc:
        return web.json_response({"error": str(exc)}, status=400, headers=headers)
    except Exception as exc:
        log.exception("Could not dispatch encode job")
        return web.json_response({"error": str(exc)}, status=502, headers=headers)


async def start_http_server() -> web.AppRunner:
    app = web.Application(client_max_size=64 * 1024)
    app.router.add_get("/health", health)
    app.router.add_route("OPTIONS", "/api/runs", options_handler)
    app.router.add_get("/api/runs", api_runs)
    app.router.add_route("OPTIONS", "/api/dispatch", options_handler)
    app.router.add_post("/api/dispatch", api_dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()
    log.info("HTTP health/admin API listening on port %s", os.getenv("PORT", "10000"))
    return runner


def make_bot() -> discord.Client:
    intents = discord.Intents.none()
    bot = discord.Client(intents=intents)
    tree = app_commands.CommandTree(bot)

    @bot.event
    async def on_ready():
        log.info("Dispatcher Discord bot connected as %s", bot.user)

    @tree.command(name="encodeal", description="Start an encode job on GitHub Actions")
    @app_commands.describe(source_url="Direct public video URL", codec="Output video codec",
                           subtitle_url="Optional direct .ass or .srt URL")
    @app_commands.choices(codec=[
        app_commands.Choice(name="H.264", value="h264"),
        app_commands.Choice(name="H.265 / HEVC", value="h265"),
    ])
    async def encodeal(interaction: discord.Interaction, source_url: str,
                       codec: app_commands.Choice[str], subtitle_url: str = ""):
        if not ADMIN_IDS or str(interaction.user.id) not in ADMIN_IDS:
            await interaction.response.send_message(
                "Bu komut için UBAGEN_DISCORD_ADMIN_IDS ayarında yetkili kullanıcı ID'n bulunmalı.",
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await dispatch_encode(source_url, subtitle_url, codec.value)
            await interaction.followup.send(
                "Encode işi GitHub Actions'a gönderildi. Durum: https://github.com/"
                + REPOSITORY + "/actions/workflows/encode-job.yml",
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Discord command could not dispatch encode")
            await interaction.followup.send("İş başlatılamadı: " + str(exc)[:1200], ephemeral=True)

    @tree.command(name="encodeisleri", description="Show recent GitHub Actions jobs")
    async def encodeisleri(interaction: discord.Interaction):
        if not ADMIN_IDS or str(interaction.user.id) not in ADMIN_IDS:
            await interaction.response.send_message("Bu komut için yetkin bulunmuyor.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            runs = await recent_runs(8)
            if not runs:
                message = "Henüz workflow çalıştırması yok."
            else:
                lines = []
                for run in runs:
                    state = run["conclusion"] or run["status"] or "unknown"
                    lines.append("• " + str(state) + " — " + str(run["created_at"])[:16]
                                 + "\n" + str(run["html_url"]))
                message = "\n".join(lines)
            await interaction.followup.send(message[:1900], ephemeral=True)
        except Exception as exc:
            await interaction.followup.send("İşler okunamadı: " + str(exc)[:1000], ephemeral=True)

    @bot.event
    async def setup_hook():
        await tree.sync()
        log.info("Dispatcher slash commands synced")

    return bot


async def main() -> None:
    global HTTP_SESSION
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_BOT_TOKEN secret is required.")
    if not GITHUB_TOKEN:
        raise RuntimeError("GITHUB_TOKEN secret is required.")
    HTTP_SESSION = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
    runner = await start_http_server()
    bot = make_bot()
    try:
        await bot.start(DISCORD_TOKEN)
    finally:
        await bot.close()
        await runner.cleanup()
        await HTTP_SESSION.close()


if __name__ == "__main__":
    asyncio.run(main())