"""
UBAgen — encoder_worker.py
Production Cloud Run Job Worker & Discord Encoder Slash Commands.

Responsibilities:
1. Standalone Cloud Run Job Worker (`python3 encoder_worker.py --job-id <JOB_ID>` or `UBAGEN_JOB_ID`):
   - Downloads video from magnet URI using `aria2c` in an isolated temporary scratch directory.
   - Extracts or prepares `.ass` / `.srt` subtitles (preserving ASS/SSA styling or cleaning SRT markup).
   - Processes optional intro clip (`intro_source`), normalizing resolution/frame rate/audio and concatenating.
   - Encodes video using FFmpeg with user-selected `H.264` (`libx264`) or `H.265` (`libx265`) codec.
   - Parses real-time FFmpeg `-progress pipe:1` telemetry (`frame`, `fps`, `out_time_us`, `speed`) to compute
     accurate `percent`, `processed_frames`, `total_frames`, `fps`, and `eta_seconds`.
   - Uploads final video to Google Cloud Storage (`UBAGEN_GCS_BUCKET`) and verifies remote object existence
     and byte size BEFORE marking the job `completed` or exposing any download URL.
   - Enforces a 7200-second (2-hour) output retention window (`expires_at = completed_at + 7200`).
   - Updates Discord messages in real time and cleans up all temporary worker files in `finally:`.
2. Cloud Run Job Dispatcher (`dispatch_encode_job`):
   - Triggers a Google Cloud Run Job execution via Cloud Run v2 REST API when `UBAGEN_CLOUD_RUN_JOB_NAME`
     is configured, or runs an isolated worker subprocess/task when operating in local/dev environments.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Optional
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    import discord
    from discord import app_commands
except ImportError:
    discord = None
    app_commands = None

from job_store import (
    DEFAULT_RETENTION_SECONDS,
    EncodeJob,
    FirestoreUnavailableError,
    GCSOutputStorage,
    JobNotFoundError,
    PersistentJobStore,
    detect_cloud_run_environment_details,
    discover_and_apply_runtime_config,
    get_gcp_access_token,
    get_job_store,
    normalize_video_codec,
)
from stats import redact_sensitive_text
from storage_manager import format_storage_bytes, get_storage_manager
from subtitle_extract import (
    BITMAP_SUBTITLE_CODECS,
    build_aria2c_command,
    build_ffmpeg_extract_command,
    clean_srt_file,
    ensure_runtime_binaries,
    find_video_files,
    normalize_language_code,
    probe_subtitle_streams,
    select_subtitle_stream,
    subtitle_format,
    validate_magnet_uri,
)

logger = logging.getLogger("ubagen.encoder")

ENCODE_COMMAND_NAME = "encode"
ENCODEAL_COMMAND_NAME = "encodeal"
JOB_STATUS_COMMAND_NAME = "is_durumu"
DISCORD_RESTART_COMMAND_NAME = "discord"

ALLOWED_SUBTITLE_EXTENSIONS = {".ass", ".ssa", ".srt"}
ALLOWED_INTRO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
MAX_SUBTITLE_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB
MAX_INTRO_ATTACHMENT_BYTES = 200 * 1024 * 1024  # 200 MB

DISALLOWED_SUBTITLE_MIME_PREFIXES = (
    "video/",
    "audio/",
    "image/",
    "application/x-msdownload",
    "application/x-executable",
    "application/x-dosexec",
    "application/x-sh",
)
ALLOWED_INTRO_MIME_PREFIXES = (
    "video/",
    "application/octet-stream",
)


def sanitize_process_output(raw_text: Optional[str], max_chars: int = 400) -> str:
    """
    Sanitize subprocess (FFmpeg / aria2c / ffprobe) stderr/stdout before logging or persisting:
    redacts magnet hashes, query tokens, signatures, and secrets while keeping diagnostic lines.
    """
    if not raw_text:
        return ""
    cleaned = redact_sensitive_text(str(raw_text))
    cleaned = re.sub(r"magnet:\?[^\s\"']+", "magnet:?[REDACTED]", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"(https?://[^\s\"'?]+)\?[^\s\"']+", r"\1?[REDACTED_QUERY]", cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.strip()
    if len(cleaned) > max_chars:
        return cleaned[-max_chars:]
    return cleaned


def sanitize_untrusted_filename(
    raw_filename: Optional[str],
    *,
    allowed_extensions: set[str],
    kind_label: str,
) -> tuple[bool, str, str]:
    """
    Treat attachment filenames as untrusted input:
    - Strip directory traversal components (`../`, `/`, `\\`).
    - Enforce allowed file extension.
    - Replace unsafe characters with `_` and bound length.
    Returns `(is_valid, sanitized_filename_or_error, lowercase_extension)`.
    """
    raw = (raw_filename or "").strip()
    if not raw or "\x00" in raw:
        return False, f"{kind_label} dosya adı geçersiz veya boş.", ""

    normalized_slashes = raw.replace("\\", "/")
    if ".." in Path(normalized_slashes).parts:
        return False, f"{kind_label} dosya adında güvenli olmayan yol karakterleri (`..`) tespit edildi.", ""


    base_name = Path(normalized_slashes).name.strip()
    if not base_name or base_name.startswith("."):
        return False, f"{kind_label} dosya adı geçersiz.", ""

    ext = Path(base_name).suffix.lower()
    if not ext or ext not in allowed_extensions:
        allowed_str = ", ".join(sorted(allowed_extensions))
        return (
            False,
            f"Geçersiz {kind_label} dosya uzantısı (`{ext or 'yok'}`). Yalnızca şu uzantılar desteklenir: {allowed_str}",
            ext,
        )

    stem = Path(base_name).stem
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")[:60] or "file"
    return True, f"{safe_stem}{ext}", ext


def validate_discord_attachment(attachment: Any, *, kind: str) -> tuple[bool, str, str]:
    """
    Validate a Discord attachment object (`altyazi` or `intro`) for `/encodeal`:
    checks untrusted filename, extension, byte size limit, and MIME type (`content_type`).
    Returns `(is_valid, sanitized_filename_or_error_msg, normalized_content_type)`.
    """
    if attachment is None:
        return False, "Ek dosya bulunamadı.", ""

    if kind == "subtitle":
        allowed_exts = ALLOWED_SUBTITLE_EXTENSIONS
        max_bytes = MAX_SUBTITLE_ATTACHMENT_BYTES
        label = "Altyazı"
    elif kind == "intro":
        allowed_exts = ALLOWED_INTRO_EXTENSIONS
        max_bytes = MAX_INTRO_ATTACHMENT_BYTES
        label = "İntro"
    else:
        return False, f"Bilinmeyen ek dosya türü: {kind}", ""

    raw_filename = getattr(attachment, "filename", None)
    ok_name, safe_name_or_err, ext = sanitize_untrusted_filename(
        raw_filename,
        allowed_extensions=allowed_exts,
        kind_label=label,
    )
    if not ok_name:
        return False, safe_name_or_err, ""

    size_val = getattr(attachment, "size", None)
    if size_val is not None:
        try:
            size_int = int(size_val)
        except (TypeError, ValueError):
            return False, f"{label} dosya boyutu doğrulanamadı.", ""
        if size_int <= 0:
            return False, f"{label} dosyası boş (0 byte) olamaz.", ""
        if size_int > max_bytes:
            max_mb = max_bytes // (1024 * 1024)
            return False, f"{label} dosyası çok büyük ({format_storage_bytes(size_int)}). Maksimum sınır: {max_mb} MB.", ""

    raw_ct = str(getattr(attachment, "content_type", "") or "").strip().lower()
    mime_base = raw_ct.split(";", 1)[0].strip() if raw_ct else ""

    if kind == "subtitle" and mime_base:
        if any(mime_base.startswith(prefix) for prefix in DISALLOWED_SUBTITLE_MIME_PREFIXES):
            return (
                False,
                f"Altyazı eki için geçersiz MIME türü (`{mime_base}`). Yalnızca metin tabanlı `.ass` veya `.srt` yükleyin.",
                "",
            )
    elif kind == "intro" and mime_base:
        if not any(mime_base.startswith(prefix) for prefix in ALLOWED_INTRO_MIME_PREFIXES):
            return (
                False,
                f"İntro eki için geçersiz MIME türü (`{mime_base}`). Yalnızca geçerli bir video dosyası yükleyin.",
                "",
            )

    effective_ct = mime_base or ("text/plain; charset=utf-8" if kind == "subtitle" else "video/mp4")
    return True, safe_name_or_err, effective_ct


async def stage_discord_attachment_to_gcs(
    attachment: Any,
    *,
    user_id: str,
    job_id: str,
    kind: str,
    gcs_storage: GCSOutputStorage,
) -> dict[str, Any]:
    """
    Validate, download, and upload a Discord attachment into a unique, user/job-scoped
    GCS staging object (`gs://<bucket>/staging/<user_id>/<job_id>/...`).
    Never leaves ephemeral Discord CDN URLs in the job queue.
    """
    ok_att, safe_name_or_err, content_type = validate_discord_attachment(attachment, kind=kind)
    if not ok_att:
        raise ValueError(safe_name_or_err)

    max_bytes = MAX_SUBTITLE_ATTACHMENT_BYTES if kind == "subtitle" else MAX_INTRO_ATTACHMENT_BYTES
    label = "Altyazı" if kind == "subtitle" else "İntro"

    data: Optional[bytes] = None
    read_fn = getattr(attachment, "read", None)
    if callable(read_fn):
        maybe_coro = read_fn()
        if asyncio.iscoroutine(maybe_coro):
            data = await maybe_coro
        elif isinstance(maybe_coro, (bytes, bytearray)):
            data = bytes(maybe_coro)

    if data is None:
        att_url = str(getattr(attachment, "url", "") or "").strip()
        if not att_url.startswith(("https://", "http://")):
            raise RuntimeError(f"{label} eki okunamadı (geçerli veri veya kaynak bulunamadı).")

        def _download_bounded() -> bytes:
            req = urllib.request.Request(att_url, headers={"User-Agent": "UBAgen-AttachmentStager/1.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                buf = resp.read(max_bytes + 1)
                if len(buf) > max_bytes:
                    raise ValueError(f"{label} dosyası izin verilen maksimum boyutu aşıyor.")
                return buf

        data = await asyncio.to_thread(_download_bounded)

    if not data or len(data) <= 0:
        raise ValueError(f"{label} dosyası boş (0 byte).")
    if len(data) > max_bytes:
        raise ValueError(f"{label} dosyası izin verilen maksimum boyutu aşıyor.")

    if kind == "subtitle":
        if b"\x00" in data[:512]:
            raise ValueError("Altyazı dosyası ikili (binary) veri içeriyor; geçerli bir `.ass` veya `.srt` metin dosyası yükleyin.")

    upload_res = await asyncio.to_thread(
        gcs_storage.upload_staging_bytes,
        data,
        user_id=user_id,
        job_id=job_id,
        kind=kind,
        original_filename=safe_name_or_err,
        content_type=content_type,
    )
    return {
        **upload_res,
        "sanitized_filename": safe_name_or_err,
        "content_type": content_type,
    }


STAGE_LABELS_TR: dict[str, str] = {
    "queued": "Sırada Bekliyor",
    "downloading": "Torrent İndiriliyor (aria2c)",
    "preparing_subtitles": "Altyazı Hazırlanıyor (ASS/SRT)",
    "processing_intro": "İntro Hazırlanıyor",
    "encoding": "Video Encode Ediliyor (FFmpeg)",
    "uploading": "Google Cloud Storage'a Yükleniyor",
    "completed": "Tamamlandı (GCS Doğrulandı)",
    "failed": "Başarısız Oldu",
    "cancelled": "İptal Edildi",
    "expired": "Süresi Doldu (GCS'den Silindi)",
}


def escape_ffmpeg_filter_path(path: Path | str) -> str:
    """
    Escape a filesystem path for safe inclusion inside an FFmpeg `subtitles='...'` or `ass='...'` filter.
    Handles backslashes, colons, single quotes, and brackets per FFmpeg filtergraph escaping rules.
    """
    raw = str(Path(path).resolve()).replace("\\", "/")
    raw = raw.replace(":", "\\:")
    raw = raw.replace("'", "'\\\\\\''")
    raw = raw.replace("[", "\\[").replace("]", "\\]")
    raw = raw.replace(",", "\\,")
    raw = raw.replace(";", "\\;")
    return raw


def probe_video_metadata(video_path: Path, timeout: int = 30) -> dict[str, Any]:
    """
    Inspect video stream duration, frame count, FPS, width, height, and audio presence via `ffprobe`.
    """
    proc = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames,duration",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe video analizi başarısız: {(proc.stderr or '').strip()[-300:]}")

    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams") or []
    fmt = data.get("format") or {}

    video_stream: Optional[dict[str, Any]] = None
    has_audio = False
    for st in streams:
        ctype = st.get("codec_type")
        if ctype == "video" and video_stream is None:
            video_stream = st
        elif ctype == "audio":
            has_audio = True

    if video_stream is None:
        raise RuntimeError(f"Video akışı bulunamadı: {video_path.name}")

    width = int(video_stream.get("width") or 1280)
    height = int(video_stream.get("height") or 720)
    # Ensure even dimensions for yuv420p encoding
    width = max(2, width - (width % 2))
    height = max(2, height - (height % 2))

    fps_val = 24.0
    for fps_key in ("avg_frame_rate", "r_frame_rate"):
        raw_rate = str(video_stream.get(fps_key) or "").strip()
        if "/" in raw_rate:
            num_s, den_s = raw_rate.split("/", 1)
            with suppress(ValueError, ZeroDivisionError):
                n = float(num_s)
                d = float(den_s)
                if n > 0 and d > 0:
                    fps_val = n / d
                    break
        elif raw_rate:
            with suppress(ValueError):
                val = float(raw_rate)
                if val > 0:
                    fps_val = val
                    break

    duration_sec = 0.0
    for dur_candidate in (video_stream.get("duration"), fmt.get("duration")):
        if dur_candidate is not None:
            with suppress(ValueError):
                d = float(dur_candidate)
                if d > 0:
                    duration_sec = d
                    break

    total_frames: Optional[int] = None
    raw_nb = video_stream.get("nb_frames")
    if raw_nb is not None and str(raw_nb).isdigit() and int(raw_nb) > 0:
        total_frames = int(raw_nb)
    elif duration_sec > 0 and fps_val > 0:
        total_frames = max(1, int(round(duration_sec * fps_val)))

    return {
        "width": width,
        "height": height,
        "fps": round(fps_val, 3),
        "duration_sec": round(duration_sec, 3),
        "total_frames": total_frames,
        "has_audio": has_audio,
    }


def parse_ffmpeg_progress_block(
    progress_kv: dict[str, str],
    total_frames: Optional[int],
    total_duration_sec: float,
) -> dict[str, Any]:
    """
    Compute real `processed_frames`, `total_frames`, `fps`, `percent`, and `eta_seconds`
    from an FFmpeg `-progress pipe:1` key-value snapshot.
    """
    raw_frame = progress_kv.get("frame", "0").strip()
    processed_frames = int(raw_frame) if raw_frame.isdigit() else 0

    raw_fps = progress_kv.get("fps", "0").strip()
    try:
        fps = max(0.0, float(raw_fps))
    except ValueError:
        fps = 0.0

    out_time_sec = 0.0
    raw_us = progress_kv.get("out_time_us") or progress_kv.get("out_time_ms")
    if raw_us and raw_us.lstrip("-").isdigit():
        val_us = int(raw_us)
        if val_us > 0:
            out_time_sec = val_us / 1_000_000.0
    elif "out_time" in progress_kv:
        # Format HH:MM:SS.mmmmmm
        m = re.match(r"^(\d+):(\d+):(\d+(?:\.\d+)?)$", progress_kv["out_time"].strip())
        if m:
            out_time_sec = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))

    speed_factor = 0.0
    raw_speed = progress_kv.get("speed", "").strip().rstrip("x")
    if raw_speed and raw_speed != "N/A":
        with suppress(ValueError):
            speed_factor = max(0.0, float(raw_speed))

    is_end = progress_kv.get("progress", "").strip() == "end"

    # Calculate percentage
    percent = 0.0
    if is_end:
        percent = 100.0
    elif total_frames and total_frames > 0 and processed_frames > 0:
        percent = min(99.9, (processed_frames / float(total_frames)) * 100.0)
    elif total_duration_sec > 0 and out_time_sec > 0:
        percent = min(99.9, (out_time_sec / float(total_duration_sec)) * 100.0)

    # Calculate ETA in seconds
    eta_seconds: Optional[int] = None
    if is_end:
        eta_seconds = 0
    elif total_frames and total_frames > processed_frames and fps > 0.01:
        remaining_frames = max(0, total_frames - processed_frames)
        eta_seconds = max(0, int(math.ceil(remaining_frames / fps)))
    elif total_duration_sec > out_time_sec and speed_factor > 0.001:
        remaining_sec = max(0.0, total_duration_sec - out_time_sec)
        eta_seconds = max(0, int(math.ceil(remaining_sec / speed_factor)))

    return {
        "processed_frames": processed_frames,
        "total_frames": total_frames,
        "fps": round(fps, 2),
        "percent": round(percent, 1),
        "eta_seconds": eta_seconds,
        "out_time_sec": round(out_time_sec, 2),
        "is_end": is_end,
    }


def build_ffmpeg_encode_command(
    main_video_path: Path,
    output_path: Path,
    *,
    codec: str = "h264",
    subtitle_path: Optional[Path] = None,
    intro_video_path: Optional[Path] = None,
    main_meta: Optional[dict[str, Any]] = None,
    intro_meta: Optional[dict[str, Any]] = None,
    preset: Optional[str] = None,
) -> list[str]:
    """
    Construct the FFmpeg command for burning `.ass` / `.srt` subtitles, optionally prepending
    an `intro_video_path`, and encoding with `libx264` (H.264) or `libx265` (H.265) while emitting
    machine-parsable progress to `pipe:1`.
    """
    ok_codec, canonical_codec, video_encoder = normalize_video_codec(codec)
    if not ok_codec:
        raise ValueError(video_encoder)

    effective_preset = preset or os.getenv("UBAGEN_FFMPEG_PRESET", "").strip() or (
        "veryfast" if video_encoder == "libx264" else "fast"
    )
    crf_value = "22" if video_encoder == "libx264" else "24"

    cmd: list[str] = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-nostats",
        "-y",
    ]

    sub_filter = ""
    if subtitle_path is not None:
        escaped_sub = escape_ffmpeg_filter_path(subtitle_path)
        if subtitle_path.suffix.lower() in (".ass", ".ssa"):
            sub_filter = f"ass='{escaped_sub}'"
        else:
            sub_filter = f"subtitles='{escaped_sub}'"

    if intro_video_path is not None:
        m_meta = main_meta or probe_video_metadata(main_video_path)
        i_meta = intro_meta or probe_video_metadata(intro_video_path)
        w = int(m_meta.get("width") or 1280)
        h = int(m_meta.get("height") or 720)
        fps = float(m_meta.get("fps") or 24.0)
        main_has_audio = bool(m_meta.get("has_audio", True))
        intro_has_audio = bool(i_meta.get("has_audio", True))

        cmd.extend(["-i", str(intro_video_path), "-i", str(main_video_path)])

        intro_v_chain = (
            f"[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[v0]"
        )
        main_v_base = f"[1:v]scale={w}:{h},setsar=1,fps={fps},format=yuv420p"
        if sub_filter:
            main_v_chain = f"{main_v_base},{sub_filter}[v1]"
        else:
            main_v_chain = f"{main_v_base}[v1]"

        if main_has_audio or intro_has_audio:
            if intro_has_audio:
                intro_a_chain = "[0:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a0]"
            else:
                intro_dur = max(0.1, float(i_meta.get("duration_sec") or 1.0))
                intro_a_chain = f"aevalsrc=0:d={intro_dur}:s=48000:c=stereo[a0]"

            if main_has_audio:
                main_a_chain = "[1:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a1]"
            else:
                main_dur = max(0.1, float(m_meta.get("duration_sec") or 1.0))
                main_a_chain = f"aevalsrc=0:d={main_dur}:s=48000:c=stereo[a1]"

            filter_complex = (
                f"{intro_v_chain};{main_v_chain};{intro_a_chain};{main_a_chain};"
                f"[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]"
            )
            cmd.extend([
                "-filter_complex",
                filter_complex,
                "-map",
                "[vout]",
                "-map",
                "[aout]",
            ])
        else:
            filter_complex = f"{intro_v_chain};{main_v_chain};[v0][v1]concat=n=2:v=1:a=0[vout]"
            cmd.extend([
                "-filter_complex",
                filter_complex,
                "-map",
                "[vout]",
            ])
    else:
        cmd.extend(["-i", str(main_video_path)])
        if sub_filter:
            cmd.extend(["-vf", sub_filter])
        cmd.extend(["-map", "0:v:0", "-map", "0:a:0?"])

    cmd.extend([
        "-c:v",
        video_encoder,
        "-preset",
        effective_preset,
        "-crf",
        crf_value,
        "-pix_fmt",
        "yuv420p",
    ])
    if video_encoder == "libx265":
        cmd.extend(["-tag:v", "hvc1"])

    cmd.extend([
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-movflags",
        "+faststart",
        str(output_path),
    ])
    return cmd


def format_eta_human(eta_seconds: Optional[int]) -> str:
    """Format ETA seconds into human-readable Turkish duration string."""
    if eta_seconds is None or eta_seconds < 0:
        return "Hesaplanıyor..."
    if eta_seconds == 0:
        return "0sn"
    mins, secs = divmod(int(eta_seconds), 60)
    hrs, mins = divmod(mins, 60)
    if hrs > 0:
        return f"{hrs}sa {mins}dk {secs}sn"
    if mins > 0:
        return f"{mins}dk {secs}sn"
    return f"{secs}sn"


def format_discord_job_message(job: EncodeJob) -> str:
    """
    Build a detailed Discord status message reflecting real job progress, frames, FPS, ETA,
    codec, error state, and 7200-second expiring GCS download link.
    """
    stage_label = STAGE_LABELS_TR.get(job.stage, job.stage)
    pct = max(0.0, min(100.0, float(job.percent or 0.0)))
    filled = int(round(pct / 10.0))
    bar = "█" * filled + "░" * max(0, 10 - filled)

    frames_str = (
        f"{job.processed_frames} / {job.total_frames}"
        if job.total_frames
        else f"{job.processed_frames}"
    )
    fps_str = f"{job.fps:.1f} FPS" if job.fps else "0.0 FPS"
    eta_str = format_eta_human(job.eta_seconds)
    codec_label = f"{job.codec.upper()} (`{job.video_encoder}`)"

    header_icon = (
        "✅"
        if job.status == "completed"
        else "🛑"
        if job.status == "cancelled"
        else "❌"
        if job.status == "failed"
        else "⌛"
        if job.status == "expired"
        else "🎬"
    )

    lines = [
        f"{header_icon} **UBAgen Cloud Encoder — İş Durumu (`{job.job_id}`)**",
        f"• **Kullanıcı:** <@{job.user_id}>",
        f"• **Aşama:** `{stage_label}`",
        f"• **İlerleme:** `[{bar}] %{pct:.1f}`",
        f"• **Kare (Frame):** `{frames_str}` | **Hız:** `{fps_str}` | **Kalan Süre (ETA):** `{eta_str}`",
        f"• **Codec:** {codec_label} | **Altyazı:** `{job.subtitle_lang}` ({job.subtitle_format or 'otomatik'})"
        + (" | **İntro:** `Aktif`" if job.has_intro else ""),
    ]

    if job.status == "completed" and job.gcs_upload_verified and job.download_url:
        exp_ts = int(job.expires_at or (time.time() + DEFAULT_RETENTION_SECONDS))
        size_label = format_storage_bytes(job.output_size_bytes)
        lines.extend([
            f"• **Çıktı Boyutu:** `{size_label}` (`{job.output_filename or 'video.mp4'}`)",
            f"• **Son Kullanma (7200 sn / 2 Saat):** <t:{exp_ts}:F> (<t:{exp_ts}:R>)",
            f"🔗 **İndirme Bağlantısı (Yalnızca <@{job.user_id}> için yetkili):**\n{job.download_url}",
            "⚠️ *Not: Bu bağlantı 7200 saniye (2 saat) sonra otomatik olarak iptal edilir ve video Google Cloud Storage'dan kalıcı olarak silinir.*",
        ])
    elif job.status == "cancelled":
        lines.append(f"🛑 **Durum:** {job.error or 'İşlem iptal edildi.'}")
    elif job.status == "failed" or job.error:
        lines.append(f"❌ **Hata:** {job.error or 'Bilinmeyen işlem hatası.'}")
    elif job.status == "expired" or job.deleted_from_gcs:
        lines.append("🗑️ **Durum:** 7200 saniyelik saklama süresi dolduğu için erişim iptal edildi ve dosya GCS'den silindi.")

    return "\n".join(lines)


def notify_discord_webhook_sync(job: EncodeJob) -> bool:
    """
    Update the original Discord interaction message via Discord Webhook REST API
    (`PATCH /api/v10/webhooks/{application_id}/{interaction_token}/messages/@original`).
    Allows the separate Cloud Run Job worker to push live progress directly to Discord!
    """
    if not job.application_id or not job.interaction_token:
        return False

    url = (
        f"https://discord.com/api/v10/webhooks/"
        f"{urllib.parse.quote(str(job.application_id))}/"
        f"{urllib.parse.quote(str(job.interaction_token))}/messages/@original"
    )
    body = json.dumps({"content": format_discord_job_message(job)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="PATCH",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "UBAgen-EncoderWorker/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:
        logger.debug("Discord interaction webhook PATCH skipped/failed: %s", exc)
        return False


def _materialize_input_file(
    source: str,
    dest_dir: Path,
    prefix: str,
    default_suffix: str,
    *,
    gcs_storage: Optional[GCSOutputStorage] = None,
    expected_user_id: Optional[str] = None,
    expected_job_id: Optional[str] = None,
) -> Path:
    """
    Resolve a GCS staging URI (`gs://bucket/staging/<user_id>/<job_id>/...`),
    local path, or HTTP/HTTPS URL into `dest_dir` for subtitle or intro inputs.
    Enforces ownership verification on GCS staging paths.
    """
    clean = source.strip()
    if clean.startswith("gs://"):
        without_scheme = clean[5:]
        if "/" not in without_scheme:
            raise ValueError(f"Geçersiz GCS URI formatı: {clean}")
        bucket_name, object_name = without_scheme.split("/", 1)
        clean_obj = object_name.lstrip("/")
        if ".." in Path(clean_obj).parts:
            raise ValueError("GCS nesne yolunda güvenli olmayan karakterler tespit edildi.")

        if clean_obj.startswith("staging/") and expected_user_id and expected_job_id:
            safe_user = re.sub(r"[^A-Za-z0-9._-]+", "_", expected_user_id.strip())[:64]
            safe_job = re.sub(r"[^A-Za-z0-9._-]+", "_", expected_job_id.strip())[:64]
            expected_prefix = f"staging/{safe_user}/{safe_job}/"
            if not clean_obj.startswith(expected_prefix):
                raise PermissionError(
                    "Yetkisiz GCS staging nesnesi erişimi reddedildi (kullanıcı veya iş kimliği uyuşmuyor)."
                )

        ext = Path(clean_obj).suffix.lower() or default_suffix
        target = dest_dir / f"{prefix}_{uuid.uuid4().hex[:6]}{ext}"
        active_gcs = gcs_storage or get_job_store().gcs
        return active_gcs.download_to_path(bucket_name, clean_obj, target)

    if clean.startswith(("http://", "https://")):
        parsed = urllib.parse.urlparse(clean)
        ext = Path(parsed.path).suffix.lower() or default_suffix
        target = dest_dir / f"{prefix}_{uuid.uuid4().hex[:6]}{ext}"
        req = urllib.request.Request(clean, headers={"User-Agent": "UBAgen-Worker/1.0"})
        with urllib.request.urlopen(req, timeout=120) as resp, open(target, "wb") as out_f:
            shutil.copyfileobj(resp, out_f)
        if not target.is_file() or target.stat().st_size == 0:
            raise RuntimeError(f"Harici kaynak indirilemedi veya boş: {prefix}")
        return target

    local_p = Path(clean).resolve()
    if not local_p.is_file() or local_p.stat().st_size == 0:
        raise FileNotFoundError(f"Girdi dosyası bulunamadı veya boş: {Path(clean).name}")
    target = dest_dir / f"{prefix}_{local_p.name}"
    if local_p != target:
        shutil.copy2(local_p, target)
    return target



def execute_encode_job_sync(
    job_id: str,
    *,
    job_store: Optional[PersistentJobStore] = None,
    progress_callback: Optional[Callable[[EncodeJob], None]] = None,
    download_runner: Optional[Callable[[str, Path], subprocess.CompletedProcess[str]]] = None,
    encode_timeout: int = 3600,
    download_timeout: int = 900,
) -> EncodeJob:
    """
    Execute the complete Cloud Run Job encoder workflow synchronously:
    1. Load job from persistent `JobStore`.
    2. Create isolated temporary worker workspace (`worker_temp/job_<id>/download` & `encode`).
    3. Download video from magnet via `aria2c` (or injected `download_runner`).
    4. Extract or prepare ASS/SRT subtitle file.
    5. Prepare optional intro video if specified.
    6. Run FFmpeg with `libx264` or `libx265`, parsing `-progress pipe:1` in real time for
       `processed_frames`, `total_frames`, `fps`, `percent`, and `eta_seconds`.
    7. Upload output `.mp4` to Google Cloud Storage and verify remote object existence & size.
    8. Set 7200-second expiration and mark job `completed` ONLY after GCS verification succeeds.
    9. Clean up temporary worker directory in `finally:`.
    """
    store = job_store or get_job_store()
    task_attempt = 0
    with suppress(ValueError):
        task_attempt = int(os.getenv("CLOUD_RUN_TASK_ATTEMPT", "0") or 0)

    should_run, job, prep_state = store.prepare_job_for_worker_execution(
        job_id,
        is_task_retry=(task_attempt > 0),
    )
    if not should_run:
        logger.info("Job '%s' skipped worker execution (state=%s).", job_id, prep_state)
        return job

    execution_name = (
        os.getenv("CLOUD_RUN_EXECUTION", "").strip()
        or f"worker-pid-{os.getpid()}"
    )

    def _emit_update(updated_job: Optional[EncodeJob]) -> None:
        if updated_job is None:
            return
        notify_discord_webhook_sync(updated_job)
        if progress_callback is not None:
            with suppress(Exception):
                progress_callback(updated_job)

    def _abort_if_cancelled() -> Optional[EncodeJob]:
        if store.is_job_cancelled(job_id):
            cancelled_job = store.get_job(job_id) or job
            _emit_update(cancelled_job)
            return cancelled_job
        return None

    # Validate magnet URI and codec before starting heavy work
    valid_mag, mag_or_err = validate_magnet_uri(job.magnet)
    if not valid_mag:
        failed = store.mark_job_failed(job_id, mag_or_err)
        _emit_update(failed)
        return failed or job

    ok_codec, canonical_codec, video_encoder = normalize_video_codec(job.codec)
    if not ok_codec:
        failed = store.mark_job_failed(job_id, video_encoder)
        _emit_update(failed)
        return failed or job

    # Ensure aria2c, ffmpeg, and ffprobe exist
    if shutil.which("aria2c") is None or shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        ensure_runtime_binaries(auto_install=True)

    if download_runner is None and shutil.which("aria2c") is None:
        failed = store.mark_job_failed(job_id, "Worker ortamında `aria2c` bulunamadı.")
        _emit_update(failed)
        return failed or job

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        failed = store.mark_job_failed(job_id, "Worker ortamında `ffmpeg` veya `ffprobe` bulunamadı.")
        _emit_update(failed)
        return failed or job

    storage_mgr = get_storage_manager()
    space_ok, space_msg = storage_mgr.check_space_for_job(512 * 1024 * 1024)
    if not space_ok:
        failed = store.mark_job_failed(job_id, space_msg)
        _emit_update(failed)
        return failed or job

    # Create isolated temporary worker directories (strictly separated from persistent outputs)
    worker_root = Path(
        os.getenv("UBAGEN_WORKER_TEMP_DIR", "").strip()
        or (storage_mgr.download_temp_dir / "worker_jobs")
    ).resolve()
    worker_root.mkdir(parents=True, exist_ok=True)
    job_temp_dir = Path(tempfile.mkdtemp(prefix=f"worker_{job.job_id}_", dir=str(worker_root)))
    download_dir = job_temp_dir / "download"
    encode_dir = job_temp_dir / "encode"
    download_dir.mkdir(parents=True, exist_ok=True)
    encode_dir.mkdir(parents=True, exist_ok=True)

    try:
        if cancelled := _abort_if_cancelled():
            return cancelled

        # STAGE 1: Downloading torrent via aria2c
        job = store.update_job_progress(
            job_id,
            stage="downloading",
            status="running",
            percent=2.0,
            worker_execution_name=execution_name,
        ) or job
        _emit_update(job)

        if download_runner is not None:
            dl_res = download_runner(mag_or_err, download_dir)
        else:
            aria_cmd = build_aria2c_command(mag_or_err, download_dir)
            dl_res = subprocess.run(
                aria_cmd,
                capture_output=True,
                text=True,
                timeout=download_timeout,
                check=False,
            )

        if cancelled := _abort_if_cancelled():
            return cancelled

        videos = find_video_files(download_dir)
        if dl_res.returncode != 0 and not videos:
            err_tail = sanitize_process_output(dl_res.stderr or dl_res.stdout or "")
            msg = f"Torrent indirilemedi (aria2c kod {dl_res.returncode}). Magnet bağlantısını ve seeder durumunu kontrol edin."
            if err_tail:
                logger.warning("Job %s aria2c failure: %s", job_id, err_tail)
            failed = store.mark_job_failed(job_id, msg)
            _emit_update(failed)
            return failed or job


        if not videos:
            failed = store.mark_job_failed(
                job_id,
                "İndirilen torrent içeriğinde desteklenen bir video dosyası (.mkv, .mp4 vb.) bulunamadı.",
            )
            _emit_update(failed)
            return failed or job

        source_video = videos[0]
        main_meta = probe_video_metadata(source_video)

        # STAGE 2: Preparing ASS/SRT Subtitles
        job = store.update_job_progress(
            job_id,
            stage="preparing_subtitles",
            percent=10.0,
            total_frames=main_meta.get("total_frames"),
        ) or job
        _emit_update(job)

        subtitle_file: Optional[Path] = None
        detected_sub_format: Optional[str] = None

        if job.subtitle_source and str(job.subtitle_source).strip():
            subtitle_file = _materialize_input_file(
                str(job.subtitle_source),
                encode_dir,
                "external_sub",
                ".srt",
                gcs_storage=store.gcs,
                expected_user_id=job.user_id,
                expected_job_id=job.job_id,
            )
            ext_lower = subtitle_file.suffix.lower()
            if ext_lower not in (".ass", ".ssa", ".srt"):
                raise RuntimeError("Harici altyazı dosyası yalnızca `.ass` veya `.srt` formatında olmalıdır.")
            detected_sub_format = "ass" if ext_lower in (".ass", ".ssa") else "srt"
            if detected_sub_format == "srt":
                clean_srt_file(subtitle_file)

        else:
            # Extract embedded subtitle stream matching requested language
            streams = probe_subtitle_streams(source_video)
            if not streams:
                raise RuntimeError(
                    f"`{source_video.name}` içinde gömülü altyazı akışı bulunamadı ve harici ASS/SRT dosyası verilmedi."
                )
            selected_stream, _ = select_subtitle_stream(streams, job.subtitle_lang)
            if selected_stream is None:
                raise RuntimeError(f"`{source_video.name}` içinde uygun altyazı akışı seçilemedi.")

            sub_codec = str(selected_stream.get("codec_name") or "").strip().lower()
            if sub_codec in BITMAP_SUBTITLE_CODECS:
                raise RuntimeError(
                    f"Seçilen altyazı akışı (`{sub_codec}`) görüntü tabanlıdır; metin tabanlı ASS/SRT altyazı gerekiyor."
                )
            sub_idx = selected_stream.get("index")
            if sub_idx is None:
                raise RuntimeError("Altyazı akış indeksi okunamadı.")

            sub_suffix, sub_enc = subtitle_format(sub_codec)
            detected_sub_format = "ass" if sub_suffix == ".ass" else "srt"
            subtitle_file = encode_dir / f"subtitle_{sub_idx}{sub_suffix}"
            ext_cmd = build_ffmpeg_extract_command(
                source_video,
                int(sub_idx),
                sub_enc,
                subtitle_file,
            )
            ext_proc = subprocess.run(
                ext_cmd,
                capture_output=True,
                text=True,
                timeout=180,
                check=False,
            )
            if ext_proc.returncode != 0 or not subtitle_file.is_file() or subtitle_file.stat().st_size == 0:
                raise RuntimeError("Gömülü altyazı FFmpeg ile ayıklanamadı.")
            if sub_suffix == ".srt":
                clean_srt_file(subtitle_file)

        if cancelled := _abort_if_cancelled():
            return cancelled

        # STAGE 3: Optional Intro Preparation
        intro_file: Optional[Path] = None
        intro_meta: Optional[dict[str, Any]] = None
        total_duration_sec = float(main_meta.get("duration_sec") or 0.0)
        total_frames = main_meta.get("total_frames")

        if job.intro_source and str(job.intro_source).strip():
            job = store.update_job_progress(
                job_id,
                stage="processing_intro",
                percent=15.0,
                subtitle_format=detected_sub_format,
            ) or job
            _emit_update(job)

            intro_file = _materialize_input_file(
                str(job.intro_source),
                encode_dir,
                "intro_clip",
                ".mp4",
                gcs_storage=store.gcs,
                expected_user_id=job.user_id,
                expected_job_id=job.job_id,
            )

            intro_meta = probe_video_metadata(intro_file)
            intro_dur = float(intro_meta.get("duration_sec") or 0.0)
            total_duration_sec += intro_dur
            main_fps = float(main_meta.get("fps") or 24.0)
            if total_Duration := total_duration_sec:
                if main_fps > 0:
                    total_frames = max(1, int(round(total_Duration * main_fps)))

        if cancelled := _abort_if_cancelled():
            return cancelled

        # STAGE 4: Encoding Video (libx264 / libx265) with real-time progress parsing
        safe_stem = re.sub(r"[^\w.\-]+", "_", source_video.stem).strip("._")[:60] or "video"
        output_filename = f"{safe_stem}_{canonical_codec}_{job.subtitle_lang}.mp4"
        encoded_output_path = encode_dir / output_filename

        ffmpeg_cmd = build_ffmpeg_encode_command(
            source_video,
            encoded_output_path,
            codec=canonical_codec,
            subtitle_path=subtitle_file,
            intro_video_path=intro_file,
            main_meta=main_meta,
            intro_meta=intro_meta,
        )

        job = store.update_job_progress(
            job_id,
            stage="encoding",
            status="running",
            percent=18.0,
            processed_frames=0,
            total_frames=total_frames,
            fps=0.0,
            eta_seconds=None,
            subtitle_format=detected_sub_format,
        ) or job
        _emit_update(job)

        progress_kv: dict[str, str] = {}
        last_emit_time = 0.0
        start_encode_ts = time.monotonic()

        with subprocess.Popen(
            ffmpeg_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        ) as proc:
            try:
                assert proc.stdout is not None
                for raw_line in proc.stdout:
                    if (time.monotonic() - start_encode_ts) > encode_timeout:
                        proc.kill()
                        raise subprocess.TimeoutExpired(ffmpeg_cmd, encode_timeout)

                    line = raw_line.strip()
                    if not line or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    progress_kv[k.strip()] = v.strip()

                    if k.strip() == "progress":
                        if cancelled := _abort_if_cancelled():
                            proc.kill()
                            return cancelled

                        parsed = parse_ffmpeg_progress_block(
                            progress_kv,
                            total_frames=total_frames,
                            total_duration_sec=total_duration_sec,
                        )
                        now_mono = time.monotonic()
                        if parsed["is_end"] or (now_mono - last_emit_time) >= 1.5:
                            # Scale encode progress smoothly between 18% and 92% (or 95% on end before upload)
                            raw_pct = float(parsed["percent"])
                            scaled_pct = 18.0 + (raw_pct * 0.77)
                            job = store.update_job_progress(
                                job_id,
                                stage="encoding",
                                status="running",
                                percent=round(min(95.0, scaled_pct), 1),
                                processed_frames=parsed["processed_frames"],
                                total_frames=parsed["total_frames"],
                                fps=parsed["fps"],
                                eta_seconds=parsed["eta_seconds"],
                            ) or job
                            _emit_update(job)
                            last_emit_time = now_mono
                        progress_kv.clear()

                stderr_out = proc.stderr.read() if proc.stderr else ""
                retcode = proc.wait(timeout=30)
            except Exception:
                if proc.poll() is None:
                    with suppress(Exception):
                        proc.kill()
                raise

        if cancelled := _abort_if_cancelled():
            return cancelled

        if retcode != 0 or not encoded_output_path.is_file() or encoded_output_path.stat().st_size == 0:
            err_tail = sanitize_process_output(stderr_out or "")
            if err_tail:
                logger.error("Job %s FFmpeg encode failure (code %s): %s", job_id, retcode, err_tail)
            raise RuntimeError(f"FFmpeg encode işlemi başarısız oldu (kod {retcode}): {err_tail}")


        # STAGE 5: Uploading final video to Google Cloud Storage & Verifying Object
        job = store.update_job_progress(
            job_id,
            stage="uploading",
            status="running",
            percent=96.0,
            eta_seconds=5,
        ) or job
        _emit_update(job)

        gcs_object_name = f"outputs/{job.job_id}/{output_filename}"
        upload_res = store.gcs.upload_file(
            encoded_output_path,
            gcs_object_name,
            content_type="video/mp4",
            metadata={
                "job_id": job.job_id,
                "user_id": job.user_id,
                "codec": canonical_codec,
                "retention_seconds": str(job.retention_seconds),
            },
        )

        # Strictly verify upload before marking completed
        if not upload_res.get("verified") or int(upload_res.get("size_bytes") or 0) <= 0:
            raise RuntimeError(
                "GCS nesne doğrulaması başarısız oldu; çıktı yüklemesi teyit edilemediği için bağlantı üretilmedi."
            )

        completed_job = store.mark_job_completed(
            job_id,
            gcs_bucket=str(upload_res["bucket"]),
            gcs_object_name=str(upload_res["object_name"]),
            output_filename=output_filename,
            output_size_bytes=int(upload_res["size_bytes"]),
            gcs_upload_verified=True,
        )
        _emit_update(completed_job)
        return completed_job

    except subprocess.TimeoutExpired:
        logger.warning("Encode job %s timed out.", job_id)
        failed = store.mark_job_failed(
            job_id,
            "İşlem zaman aşımına uğradı (Torrent indirme veya FFmpeg encode süresi sınırı aşıldı).",
        )
        _emit_update(failed)
        return failed or job
    except Exception as exc:
        if cancelled := _abort_if_cancelled():
            return cancelled
        safe_err = sanitize_process_output(str(exc), max_chars=500) or "Encode işlemi sırasında beklenmeyen hata oluştu."
        logger.exception("Encode job %s failed: %s", job_id, safe_err)
        failed = store.mark_job_failed(job_id, safe_err)
        _emit_update(failed)
        return failed or job
    finally:
        # Guaranteed cleanup of isolated worker scratch directory and any leftover GCS staging objects
        if job_temp_dir.exists():
            with suppress(Exception):
                shutil.rmtree(job_temp_dir, ignore_errors=True)
        with suppress(Exception):
            latest_job = store.get_job(job_id) or job
            store.gcs.cleanup_job_staging_objects(latest_job)


# ------------------------------------------------------------------------------
# Cloud Run Job Dispatcher & Controlled Service Revision Restart
# ------------------------------------------------------------------------------
def trigger_cloud_run_job_execution(job_id: str) -> dict[str, Any]:
    """
    Trigger a dedicated Google Cloud Run Job execution for `job_id` using the
    Cloud Run v2 REST API (`projects.locations.jobs.run`), passing `UBAGEN_JOB_ID`.
    Auto-discovers `UBAGEN_CLOUD_RUN_JOB_NAME`, `UBAGEN_GCP_PROJECT_ID`, and `UBAGEN_GCP_REGION`
    when not explicitly set.
    """
    discover_and_apply_runtime_config()
    env_details = detect_cloud_run_environment_details()
    job_name = env_details.get("cloud_run_job_name")
    project_id = env_details.get("project_id")
    region = env_details.get("region") or "europe-west2"
    sa_email = env_details.get("service_account_email") or "varsayılan servis hesabı"

    if not job_name or not project_id:
        return {
            "dispatched": False,
            "mode": "unconfigured",
            "reason": "UBAGEN_CLOUD_RUN_JOB_NAME veya UBAGEN_GCP_PROJECT_ID tanımlı değil.",
        }

    token = get_gcp_access_token()
    if not token:
        return {
            "dispatched": False,
            "mode": "auth_unavailable",
            "reason": "Cloud Run Job tetiklemek için GCP Service Account OAuth2 token alınamadı.",
        }

    api_url = (
        f"https://{region}-run.googleapis.com/v2/projects/"
        f"{urllib.parse.quote(project_id)}/locations/{urllib.parse.quote(region)}/"
        f"jobs/{urllib.parse.quote(job_name)}:run"
    )
    overrides_payload = {
        "overrides": {
            "containerOverrides": [
                {
                    "env": [
                        {"name": "UBAGEN_JOB_ID", "value": job_id},
                    ]
                }
            ],
            "taskCount": 1,
        }
    }
    req = urllib.request.Request(
        api_url,
        data=json.dumps(overrides_payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))
            exec_name = resp_data.get("name") or resp_data.get("metadata", {}).get("name") or f"cloud-run-job:{job_name}"
            get_job_store().update_job_progress(job_id, worker_execution_name=str(exec_name))
            return {
                "dispatched": True,
                "mode": "cloud_run_job",
                "execution_name": exec_name,
            }
    except urllib.error.HTTPError as exc:
        err_body = sanitize_process_output(exc.read().decode("utf-8", errors="replace"), max_chars=300)
        logger.warning("Cloud Run Job API HTTP %s error: %s", exc.code, err_body)
        if exc.code == 403:
            reason = (
                f"Cloud Run Job (`{job_name}`, proje: `{project_id}`, bölge: `{region}`) tetikleme yetkisi reddedildi (HTTP 403). "
                f"`{sa_email}` servis hesabına `roles/run.developer` rolü verilmelidir. Detay: {err_body}"
            )
        elif exc.code == 404:
            reason = (
                f"Cloud Run Job (`{job_name}`) `{project_id}` projesinin `{region}` bölgesinde bulunamadı (HTTP 404). "
                f"`cloud-run-encoder-job.yaml` ile Job dağıtımını tamamlayın. Detay: {err_body}"
            )
        else:
            reason = f"Cloud Run Job API HTTP {exc.code}: {err_body}"
        return {
            "dispatched": False,
            "mode": "cloud_run_job_error",
            "http_status": exc.code,
            "reason": reason,
        }
    except Exception as exc:
        safe_exc = sanitize_process_output(str(exc), max_chars=300)
        logger.warning("Cloud Run Job API request failed: %s", safe_exc)
        return {
            "dispatched": False,
            "mode": "cloud_run_job_error",
            "reason": safe_exc,
        }


def _is_local_worker_fallback_allowed() -> bool:
    """
    Determine whether spawning a local subprocess worker inside the current container is permitted.
    In production / Cloud Run Service environments, heavy video encoding must NOT run inside the
    Discord bot web service container unless explicitly enabled for local development/testing.
    """
    if os.getenv("UBAGEN_DISABLE_LOCAL_WORKER", "").strip().lower() in ("1", "true", "yes"):
        return False
    explicit_allow = os.getenv("UBAGEN_ALLOW_LOCAL_WORKER_FALLBACK", "").strip().lower()
    if explicit_allow in ("1", "true", "yes"):
        return True
    if explicit_allow in ("0", "false", "no"):
        return False

    # If running on a managed Cloud Run Service instance (`K_SERVICE` set) or in production mode,
    # do NOT silently run heavy FFmpeg jobs inside the web service container.
    k_service = os.getenv("K_SERVICE", "").strip()
    env_mode = os.getenv("ENVIRONMENT", "production").strip().lower()
    if k_service or env_mode == "production":
        return False
    return env_mode in ("development", "dev", "local", "test")


def _stream_worker_subprocess_logs(proc: subprocess.Popen[str], job_id: str) -> None:
    """
    Read stdout/stderr from a dev-mode local worker subprocess and log sanitized lines
    so FFmpeg and aria2c errors are never swallowed by `subprocess.DEVNULL`.
    """
    if proc.stdout is None:
        return
    try:
        for raw_line in proc.stdout:
            line = sanitize_process_output(raw_line, max_chars=500)
            if line:
                logger.info("[worker:%s] %s", job_id, line)
    except Exception:
        pass
    finally:
        with suppress(Exception):
            proc.stdout.close()


def dispatch_encode_job_background(job_id: str, *, allow_retry: bool = False) -> dict[str, Any]:
    """
    Dispatch `job_id` to the dedicated Google Cloud Run Job worker (`UBAGEN_CLOUD_RUN_JOB_NAME`).
    - Prevents launching the same `job_id` multiple times concurrently via `acquire_dispatch_claim`.
    - Auto-discovers `UBAGEN_CLOUD_RUN_JOB_NAME`, `UBAGEN_GCP_PROJECT_ID`, and `UBAGEN_GCP_REGION`
      from `cloud-run-encoder-job.yaml` and the GCP Metadata Server.
    - Never falls back to running heavy encodes inside the production Cloud Run Service instance.
    """
    store = get_job_store()
    can_dispatch, claimed_job, claim_reason = store.acquire_dispatch_claim(job_id, allow_retry=allow_retry)
    if not can_dispatch:
        return {
            "dispatched": False,
            "mode": "duplicate_or_terminal",
            "reason": claim_reason,
            "worker_execution_name": claimed_job.worker_execution_name if claimed_job else None,
        }

    discover_and_apply_runtime_config()
    env_details = detect_cloud_run_environment_details()
    if env_details.get("cloud_run_job_configured") and not _is_local_worker_fallback_allowed():
        res = trigger_cloud_run_job_execution(job_id)
        if res.get("dispatched"):
            return res
        fail_reason = f"Cloud Run Job başlatılamadı: {res.get('reason')}"
        store.mark_job_failed(job_id, fail_reason)
        return res

    if env_details.get("cloud_run_job_configured") and os.getenv("UBAGEN_CLOUD_RUN_JOB_NAME", "").strip():
        # If running on Cloud Run or when Cloud Run Job API token is available, dispatch via Cloud Run Job
        if os.getenv("K_SERVICE", "").strip() or os.getenv("UBAGEN_FORCE_CLOUD_RUN_JOB", "").strip().lower() in ("1", "true", "yes"):
            res = trigger_cloud_run_job_execution(job_id)
            if res.get("dispatched"):
                return res
            fail_reason = f"Cloud Run Job başlatılamadı: {res.get('reason')}"
            store.mark_job_failed(job_id, fail_reason)
            return res

    if not _is_local_worker_fallback_allowed():
        reason = (
            "Cloud Run Job (`UBAGEN_CLOUD_RUN_JOB_NAME`) veya GCP proje kimliği (`UBAGEN_GCP_PROJECT_ID`) doğrulanamadı. "
            "Üretim ortamında bot/web servisinin kararlılığını korumak için yerel worker fallback devre dışıdır."
        )
        store.mark_job_failed(job_id, reason)
        return {"dispatched": False, "mode": "disabled", "reason": reason}

    worker_script = Path(__file__).resolve()
    proc = subprocess.Popen(
        [sys.executable, "-B", str(worker_script), "--job-id", job_id],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    log_thread = threading.Thread(
        target=_stream_worker_subprocess_logs,
        args=(proc, job_id),
        name=f"ubagen-worker-log-{job_id}",
        daemon=True,
    )
    log_thread.start()
    store.update_job_progress(job_id, worker_execution_name=f"local-worker-pid-{proc.pid}")
    return {
        "dispatched": True,
        "mode": "isolated_subprocess_worker",
        "pid": proc.pid,
    }


# ------------------------------------------------------------------------------
# Discord Slash Commands (/encode, /encodeal, /is_durumu, /discord)
# ------------------------------------------------------------------------------
async def submit_encode_job_from_interaction(
    interaction: Any,
    *,
    magnet: str,
    codec: str = "h264",
    dil: str = "tur",
    altyazi_url: Optional[str] = None,
    intro_url: Optional[str] = None,
    altyazi_attachment: Optional[Any] = None,
    intro_attachment: Optional[Any] = None,
    command_source: str = "encode",
) -> Optional[EncodeJob]:
    """
    Shared backend pipeline used by BOTH `/encode` and `/encodeal`:
    1. Immediately defers the Discord interaction (`thinking=True`) so it never times out.
    2. Validates user identity, magnet link, codec (`h264`/`h265`), language, and any attachments.
    3. Creates the persistent job record in `PersistentJobStore` (Firestore / SQLite WAL).
    4. If Discord file attachments (`altyazi_attachment`, `intro_attachment`) are provided,
       validates and uploads them to unique, user/job-scoped GCS staging objects
       (`gs://<bucket>/staging/<user_id>/<job_id>/...`) — never queuing ephemeral Discord CDN URLs.
    5. Displays the real `job_id` and initial status in the Discord interaction message.
    6. Dispatches the Cloud Run Job worker (`UBAGEN_JOB_ID=<job_id>`).
    """
    if hasattr(interaction, "response") and hasattr(interaction.response, "defer"):
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)

    user_obj = getattr(interaction, "user", None)
    user_id = str(getattr(user_obj, "id", "") or "").strip()
    user_name = str(getattr(user_obj, "name", "") or "") or None
    if not user_id:
        if hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(content="❌ Kullanıcı kimliği doğrulanamadı.")
        return None

    valid_mag, mag_or_err = validate_magnet_uri(magnet)
    if not valid_mag:
        if hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(content=f"❌ {mag_or_err}")
        return None

    ok_codec, canonical_codec, video_encoder = normalize_video_codec(codec)
    if not ok_codec:
        if hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(content=f"❌ {video_encoder}")
        return None

    ok_lang, lang_or_err = normalize_language_code(dil)
    if not ok_lang:
        if hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(content=f"❌ {lang_or_err}")
        return None

    # Pre-validate attachments before creating job in database
    if altyazi_attachment is not None:
        ok_sub, sub_err, _ = validate_discord_attachment(altyazi_attachment, kind="subtitle")
        if not ok_sub:
            if hasattr(interaction, "edit_original_response"):
                await interaction.edit_original_response(content=f"❌ {sub_err}")
            return None

    if intro_attachment is not None:
        ok_intro, intro_err, _ = validate_discord_attachment(intro_attachment, kind="intro")
        if not ok_intro:
            if hasattr(interaction, "edit_original_response"):
                await interaction.edit_original_response(content=f"❌ {intro_err}")
            return None

    store = get_job_store()
    sub_hint = (
        getattr(altyazi_attachment, "filename", None)
        if altyazi_attachment is not None
        else ((altyazi_url or "").strip() or None)
    )
    intro_hint = (
        getattr(intro_attachment, "filename", None)
        if intro_attachment is not None
        else ((intro_url or "").strip() or None)
    )

    try:
        active_dup = await asyncio.to_thread(
            store.find_active_duplicate_job,
            user_id=user_id,
            magnet=mag_or_err,
            codec=canonical_codec,
            subtitle_lang=lang_or_err,
            subtitle_source=sub_hint,
            intro_source=intro_hint,
        )
        if active_dup is not None:
            if hasattr(interaction, "edit_original_response"):
                dup_msg = (
                    f"ℹ️ **Aynı parametrelerle devam eden aktif bir encode işiniz zaten var (`{active_dup.job_id}`).**\n"
                    f"Aynı işin birden fazla kez başlatılması engellendi; mevcut işinizin durumu aşağıdadır:\n\n"
                    + format_discord_job_message(active_dup)
                )
                await interaction.edit_original_response(content=dup_msg)
            return active_dup

        job = await asyncio.to_thread(
            store.create_job,
            user_id=user_id,
            user_name=user_name,
            magnet=mag_or_err,
            codec=canonical_codec,
            subtitle_lang=lang_or_err,
            subtitle_source=(altyazi_url or "").strip() or None,
            intro_source=(intro_url or "").strip() or None,
            guild_id=str(getattr(interaction, "guild_id", "") or "") or None,
            channel_id=str(getattr(interaction, "channel_id", "") or "") or None,
            application_id=str(getattr(interaction, "application_id", "") or "") or None,
            interaction_token=str(getattr(interaction, "token", "") or "") or None,
            command_source=command_source,
        )
    except FirestoreUnavailableError as exc:
        logger.error("Failed to create job in Firestore: %s", exc)
        if hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(
                content=f"❌ Kalıcı iş veritabanı (Firestore) hatası nedeniyle iş oluşturulamadı: {exc}"
            )
        return None
    except Exception as exc:
        logger.error("Failed to create job: %s", exc)
        if hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(content=f"❌ İş kaydı oluşturulamadı: {exc}")
        return None

    # Stage Discord attachments into GCS under `staging/{user_id}/{job.job_id}/...`
    staged_objects: list[str] = []
    if altyazi_attachment is not None or intro_attachment is not None:
        try:
            if altyazi_attachment is not None:
                sub_stage = await stage_discord_attachment_to_gcs(
                    altyazi_attachment,
                    user_id=user_id,
                    job_id=job.job_id,
                    kind="subtitle",
                    gcs_storage=store.gcs,
                )
                job.subtitle_source = str(sub_stage["gcs_uri"])
                staged_objects.append(str(sub_stage["object_name"]))
                ext_s = Path(str(sub_stage["sanitized_filename"])).suffix.lower()
                job.subtitle_format = "ass" if ext_s in (".ass", ".ssa") else "srt"

            if intro_attachment is not None:
                intro_stage = await stage_discord_attachment_to_gcs(
                    intro_attachment,
                    user_id=user_id,
                    job_id=job.job_id,
                    kind="intro",
                    gcs_storage=store.gcs,
                )
                job.intro_source = str(intro_stage["gcs_uri"])
                job.has_intro = True
                staged_objects.append(str(intro_stage["object_name"]))

            job.staged_gcs_objects = staged_objects
            job = await asyncio.to_thread(store.save_job, job)
        except Exception as exc:
            job.staged_gcs_objects = staged_objects
            fail_msg = f"Ek dosya GCS staging alanına yüklenemedi: {sanitize_process_output(str(exc), max_chars=250)}"
            failed_job = await asyncio.to_thread(store.mark_job_failed, job.job_id, fail_msg)
            if hasattr(interaction, "edit_original_response"):
                await interaction.edit_original_response(
                    content=format_discord_job_message(failed_job or job)
                )
            return failed_job or job

    if hasattr(interaction, "edit_original_response"):
        with suppress(Exception):
            await interaction.edit_original_response(content=format_discord_job_message(job))

    dispatch_info = await asyncio.to_thread(dispatch_encode_job_background, job.job_id)
    if not dispatch_info.get("dispatched"):
        updated = await asyncio.to_thread(store.get_job, job.job_id)
        if updated and hasattr(interaction, "edit_original_response"):
            with suppress(Exception):
                await interaction.edit_original_response(content=format_discord_job_message(updated))
        return updated or job

    return job


async def handle_encode_slash_command(
    interaction: Any,
    magnet: str,
    codec: str = "h264",
    dil: str = "tur",
    altyazi_url: Optional[str] = None,
    intro_url: Optional[str] = None,
) -> Optional[EncodeJob]:
    """
    Asynchronous handler for `/encode` slash command.
    Delegates directly to the shared `submit_encode_job_from_interaction` pipeline.
    """
    return await submit_encode_job_from_interaction(
        interaction,
        magnet=magnet,
        codec=codec,
        dil=dil,
        altyazi_url=altyazi_url,
        intro_url=intro_url,
        command_source=ENCODE_COMMAND_NAME,
    )


async def handle_encodeal_slash_command(
    interaction: Any,
    magnet: str,
    altyazi: Optional[Any] = None,
    intro: Optional[Any] = None,
    codec: str = "h264",
    dil: str = "tur",
) -> Optional[EncodeJob]:
    """
    Asynchronous handler for `/encodeal` slash command.
    Accepts optional `.ass`/`.srt` subtitle and intro video attachments, stages them safely to GCS,
    and reuses the exact same `submit_encode_job_from_interaction` pipeline as `/encode`.
    """
    return await submit_encode_job_from_interaction(
        interaction,
        magnet=magnet,
        codec=codec,
        dil=dil,
        altyazi_attachment=altyazi,
        intro_attachment=intro,
        command_source=ENCODEAL_COMMAND_NAME,
    )



async def handle_job_status_slash_command(interaction: Any, is_id: str) -> None:
    """Handler for `/is_durumu` slash command to inspect live encode job progress."""
    if hasattr(interaction, "response") and hasattr(interaction.response, "defer"):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

    user_obj = getattr(interaction, "user", None)
    requester_id = str(getattr(user_obj, "id", "") or "").strip()

    store = get_job_store()
    job = await asyncio.to_thread(store.get_job, is_id.strip())
    if job is None:
        if hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(content=f"❌ `{is_id}` numaralı bir encode işi bulunamadı.")
        return

    # Enforce cross-user protection on download link visibility
    if requester_id != job.user_id:
        redacted_copy = EncodeJob.from_dict(job.to_dict())
        redacted_copy.download_url = None
        msg = (
            format_discord_job_message(redacted_copy)
            + "\n🔒 *Bu iş başka bir kullanıcıya aittir; indirme bağlantısı yalnızca iş sahibine gösterilir.*"
        )
    else:
        msg = format_discord_job_message(job)

    if hasattr(interaction, "edit_original_response"):
        await interaction.edit_original_response(content=msg)


# ------------------------------------------------------------------------------
# /discord Controlled Bot & Cloud Run Service Restart Command
# ------------------------------------------------------------------------------
async def is_user_authorized_for_discord_restart(interaction: Any, bot: Optional[Any] = None) -> bool:
    """
    Verify whether `interaction.user` is authorized to execute `/discord` service restart:
    1. Explicit Discord User ID allowlist (`UBAGEN_DISCORD_ADMIN_IDS` or `DISCORD_OWNER_ID`).
    2. Discord Application Owner (`await bot.is_owner(user)` or `bot.owner_id`).
    3. Server Administrator (`user.guild_permissions.administrator == True`) inside a guild.
    Never allows arbitrary unprivileged users to restart the bot.
    """
    user_obj = getattr(interaction, "user", None)
    user_id = str(getattr(user_obj, "id", "") or "").strip()
    if not user_id:
        return False

    # 1. Check explicit admin/owner IDs from environment
    allowed_ids: set[str] = set()
    for env_key in ("UBAGEN_DISCORD_ADMIN_IDS", "DISCORD_OWNER_ID"):
        raw_ids = os.getenv(env_key, "").strip()
        if raw_ids:
            for part in raw_ids.replace(";", ",").split(","):
                clean_part = part.strip()
                if clean_part.isdigit():
                    allowed_ids.add(clean_part)
    if user_id in allowed_ids:
        return True

    # 2. Check bot owner
    active_bot = bot or getattr(interaction, "client", None)
    if active_bot is not None:
        owner_id = getattr(active_bot, "owner_id", None)
        if owner_id and str(owner_id) == user_id:
            return True
        owner_ids = getattr(active_bot, "owner_ids", None)
        if owner_ids and any(str(oid) == user_id for oid in owner_ids):
            return True
        is_owner_fn = getattr(active_bot, "is_owner", None)
        if callable(is_owner_fn):
            with suppress(Exception):
                maybe_coro = is_owner_fn(user_obj)
                if asyncio.iscoroutine(maybe_coro):
                    if await maybe_coro:
                        return True
                elif bool(maybe_coro):
                    return True

    # 3. Check Guild Administrator permission when invoked in a server
    guild_perms = getattr(user_obj, "guild_permissions", None)
    if guild_perms is not None and bool(getattr(guild_perms, "administrator", False)):
        return True

    return False


def trigger_cloud_run_service_revision_restart(
    service_name: Optional[str] = None,
    project_id: Optional[str] = None,
    region: Optional[str] = None,
) -> dict[str, Any]:
    """
    Trigger a controlled new revision rollout on the Cloud Run Service via Cloud Run Admin v2 API
    (`GET` + `PATCH https://{region}-run.googleapis.com/v2/projects/{project}/locations/{region}/services/{service}`)
    by updating `template.annotations['ubagen.dev/restart-requested-at']`.
    Does NOT touch or stop any running Cloud Run Jobs (`ubagen-encoder-worker`).
    """
    env_details = detect_cloud_run_environment_details()
    svc = (
        service_name
        or os.getenv("UBAGEN_CLOUD_RUN_SERVICE_NAME", "").strip()
        or env_details.get("k_service")
        or ""
    ).strip()
    proj = (project_id or env_details.get("project_id") or "").strip()
    reg = (region or env_details.get("region") or "europe-west2").strip()

    if not svc or not proj:
        return {
            "triggered": False,
            "verified_complete": False,
            "mode": "cloud_run_revision_unconfigured",
            "reason": "K_SERVICE / UBAGEN_CLOUD_RUN_SERVICE_NAME veya UBAGEN_GCP_PROJECT_ID tanımlı değil.",
        }

    token = get_gcp_access_token()
    if not token:
        return {
            "triggered": False,
            "verified_complete": False,
            "mode": "cloud_run_auth_unavailable",
            "reason": "Cloud Run Admin API için GCP OAuth2 erişim anahtarı alınamadı.",
        }

    svc_url = (
        f"https://{reg}-run.googleapis.com/v2/projects/"
        f"{urllib.parse.quote(proj)}/locations/{urllib.parse.quote(reg)}/"
        f"services/{urllib.parse.quote(svc)}"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    try:
        get_req = urllib.request.Request(svc_url, headers=headers, method="GET")
        with urllib.request.urlopen(get_req, timeout=10) as get_resp:
            svc_data = json.loads(get_resp.read().decode("utf-8", errors="replace"))

        template = svc_data.get("template") or {}
        annotations = dict(template.get("annotations") or {})
        annotations["ubagen.dev/restart-requested-at"] = datetime.now(timezone.utc).isoformat()
        template["annotations"] = annotations
        svc_data["template"] = template

        patch_url = f"{svc_url}?updateMask=template.annotations"
        patch_req = urllib.request.Request(
            patch_url,
            data=json.dumps(svc_data).encode("utf-8"),
            headers=headers,
            method="PATCH",
        )
        with urllib.request.urlopen(patch_req, timeout=15) as patch_resp:
            op_data = json.loads(patch_resp.read().decode("utf-8", errors="replace"))
            op_name = op_data.get("name") or f"projects/{proj}/locations/{reg}/operations/restart"
            op_done = bool(op_data.get("done", False))
            return {
                "triggered": True,
                "verified_complete": op_done,
                "mode": "cloud_run_revision",
                "operation_name": op_name,
                "service_name": svc,
            }
    except urllib.error.HTTPError as exc:
        err_body = sanitize_process_output(exc.read().decode("utf-8", errors="replace"), max_chars=240)
        logger.warning("Cloud Run Service revision PATCH HTTP %s: %s", exc.code, err_body)
        return {
            "triggered": False,
            "verified_complete": False,
            "mode": "cloud_run_revision_error",
            "http_status": exc.code,
            "reason": f"Cloud Run API HTTP {exc.code}: {err_body}",
        }
    except Exception as exc:
        safe_exc = sanitize_process_output(str(exc), max_chars=240)
        logger.warning("Cloud Run Service revision PATCH error: %s", safe_exc)
        return {
            "triggered": False,
            "verified_complete": False,
            "mode": "cloud_run_revision_error",
            "reason": safe_exc,
        }


async def handle_discord_restart_command(
    interaction: Any,
    bot: Optional[Any] = None,
    bot_manager: Optional[Any] = None,
) -> tuple[int, dict[str, Any]]:
    """
    Asynchronous handler for `/discord` slash command:
    1. Immediately defers the interaction (`ephemeral=True, thinking=True`) so it never times out.
    2. Verifies caller authorization (Bot Owner, `UBAGEN_DISCORD_ADMIN_IDS`, or Guild Administrator).
       Rejects unauthorized users with HTTP 403 and a clear message.
    3. Sends an immediate acknowledgement that the restart request was received.
    4. Never calls `sys.exit()` or `os.kill()` and never interrupts running Cloud Run Job encode tasks.
    5. Executes a controlled restart via `BotLifecycleManager.restart_bot()` (and optional Cloud Run
       revision rollout when `UBAGEN_DISCORD_RESTART_MODE=cloud_run_revision`), reporting accurately
       whether the restart request was accepted (`202`), verified complete (`200`), throttled (`429`),
       or failed (`500`/`503`).
    """
    if hasattr(interaction, "response") and hasattr(interaction.response, "defer"):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)

    active_bot = bot or getattr(interaction, "client", None)
    authorized = await is_user_authorized_for_discord_restart(interaction, bot=active_bot)
    if not authorized:
        user_id = str(getattr(getattr(interaction, "user", None), "id", "unknown"))
        logger.warning("Unauthorized /discord restart attempt rejected for user_id=%s", user_id)
        msg = (
            "🚫 **Yetkisiz İşlem (403):** `/discord` bot yeniden başlatma komutunu yalnızca "
            "bot sahibi veya sunucu yöneticileri (`Administrator`) çalıştırabilir."
        )
        if hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(content=msg)
        return 403, {"status": "forbidden", "error": msg}

    # Acknowledge immediately before starting the restart sequence
    if hasattr(interaction, "edit_original_response"):
        with suppress(Exception):
            await interaction.edit_original_response(
                content=(
                    "⏳ **Yeniden başlatma isteği kabul edildi.** "
                    "Yetki doğrulandı; Discord bot servisi güvenli şekilde yeniden başlatılıyor..."
                )
            )

    # Resolve BotLifecycleManager from bot or FastAPI state
    mgr = bot_manager or getattr(active_bot, "lifecycle_manager", None)
    restart_mode = os.getenv("UBAGEN_DISCORD_RESTART_MODE", "gateway").strip().lower()
    revision_info: Optional[dict[str, Any]] = None

    if restart_mode == "cloud_run_revision":
        revision_info = await asyncio.to_thread(trigger_cloud_run_service_revision_restart)
        if revision_info.get("triggered"):
            if revision_info.get("verified_complete"):
                msg = (
                    f"✅ **Cloud Run Servis Yeniden Dağıtımı Tamamlandı (`{revision_info.get('service_name')}`)**\n"
                    f"• **İşlem:** `{revision_info.get('operation_name')}`\n"
                    "• Bağımsız çalışan Cloud Run Job encode görevleri kesintiye uğramadan devam etmektedir."
                )
                if hasattr(interaction, "edit_original_response"):
                    await interaction.edit_original_response(content=msg)
                return 200, {"status": "completed", "revision": revision_info}

            msg = (
                f"🔄 **Cloud Run Yeni Revision İsteği Kabul Edildi (`{revision_info.get('service_name')}`)**\n"
                f"• **Operation:** `{revision_info.get('operation_name')}` (Dağıtım arka planda devam ediyor, henüz tamamlandığı doğrulanmadı)\n"
                "• Bağımsız Cloud Run Job encode işlemleri etkilenmeden çalışmaya devam eder."
            )
            if hasattr(interaction, "edit_original_response"):
                await interaction.edit_original_response(content=msg)
            return 202, {"status": "accepted", "revision": revision_info}

    if mgr is not None and hasattr(mgr, "restart_bot") and callable(mgr.restart_bot):
        status_code, payload = await mgr.restart_bot()
    else:
        return_err = (
            "Bot yaşam döngüsü yöneticisi (`BotLifecycleManager`) bağlı değil"
            + (f" ve Cloud Run revision API hatası: {revision_info.get('reason')}" if revision_info else ".")
        )
        if hasattr(interaction, "edit_original_response"):
            await interaction.edit_original_response(
                content=f"❌ **Yeniden Başlatma Başarısız (503):** {return_err}"
            )
        return 503, {"status": "unavailable", "error": return_err}

    if status_code in (200, 202):
        rev_note = ""
        if revision_info and not revision_info.get("triggered"):
            rev_note = (
                f"\n• *Not: Cloud Run Revision API kullanılamadı (`{revision_info.get('reason')}`), "
                "güvenli süreç içi Gateway yeniden başlatma uygulandı.*"
            )
        msg = (
            "🔄 **Bot Yeniden Başlatma İsteği Kabul Edildi (202 Accepted)**\n"
            "• Mevcut Discord Gateway oturumu güvenli şekilde kapatıldı ve yeni bot örneğiyle yeniden bağlantı başlatıldı.\n"
            "• Web izleme paneli ve devam eden **Cloud Run Job** video encode işlemleri kesintiye uğramadan çalışmaya devam etmektedir."
            f"{rev_note}"
        )
        if hasattr(interaction, "edit_original_response"):
            with suppress(Exception):
                await interaction.edit_original_response(content=msg)
        return status_code, payload

    if status_code == 429:
        retry_after = payload.get("retry_after", 15)
        msg = (
            f"⏳ **Çok Sık İstek (429):** Yakın zamanda bir yeniden başlatma isteği gönderildi "
            f"veya işlem halen devam ediyor. Lütfen `{retry_after}` saniye sonra tekrar deneyin."
        )
        if hasattr(interaction, "edit_original_response"):
            with suppress(Exception):
                await interaction.edit_original_response(content=msg)
        return 429, payload

    err_detail = payload.get("error") or "Bilinmeyen yeniden başlatma hatası."
    msg = f"❌ **Yeniden Başlatma Başarısız ({status_code}):** {err_detail}"
    if hasattr(interaction, "edit_original_response"):
        with suppress(Exception):
            await interaction.edit_original_response(content=msg)
    return status_code, payload


def register_discord_restart_command(tree: Any, bot: Optional[Any] = None) -> Optional[Any]:
    """
    Register the `/discord` controlled bot restart slash command on `tree` if not already registered.
    """
    if discord is None or app_commands is None or tree is None:
        return None
    if tree.get_command(DISCORD_RESTART_COMMAND_NAME) is not None:
        return None

    @tree.command(
        name=DISCORD_RESTART_COMMAND_NAME,
        description="Bot servisini güvenli şekilde kapatıp yeniden başlatır (Yalnızca yetkili yönetici).",
    )
    async def discord_restart_command(interaction: discord.Interaction) -> None:
        active_bot = bot or getattr(interaction, "client", None)
        mgr = getattr(active_bot, "lifecycle_manager", None) if active_bot is not None else None
        await handle_discord_restart_command(interaction, bot=active_bot, bot_manager=mgr)

    return discord_restart_command


def register_encoder_commands(tree: Any, bot: Optional[Any] = None) -> list[Any]:
    """
    Register `/encode`, `/encodeal`, `/is_durumu`, and `/discord` slash commands on the Discord CommandTree.
    All command and parameter descriptions strictly adhere to Discord's <= 100 character limit.
    Idempotent: never registers a command twice if it already exists on `tree`.
    """
    if discord is None or app_commands is None or tree is None:
        return []

    registered: list[Any] = []

    if tree.get_command(ENCODE_COMMAND_NAME) is None:
        @tree.command(
            name=ENCODE_COMMAND_NAME,
            description="Magnetten videoyu indirir, ASS/SRT altyazı ve intro ile H.264/H.265 encode eder.",
        )
        @app_commands.describe(
            magnet="Zorunlu torrent magnet bağlantısı (magnet:?xt=urn:btih:...)",
            codec="Video codec seçimi: h264 (libx264) veya h265 (libx265)",
            dil="Altyazı dil kodu (varsayılan: tur)",
            altyazi_url="İsteğe bağlı harici .ass veya .srt altyazı dosyası URL'si",
            intro_url="İsteğe bağlı intro video dosyası URL'si (.mp4/.mkv)",
        )
        @app_commands.choices(
            codec=[
                app_commands.Choice(name="H.264 (libx264 — Yüksek Uyumluluk)", value="h264"),
                app_commands.Choice(name="H.265 / HEVC (libx265 — Yüksek Sıkıştırma)", value="h265"),
            ]
        )
        async def encode_command(
            interaction: discord.Interaction,
            magnet: str,
            codec: str = "h264",
            dil: str = "tur",
            altyazi_url: Optional[str] = None,
            intro_url: Optional[str] = None,
        ) -> None:
            await handle_encode_slash_command(
                interaction,
                magnet=magnet,
                codec=codec,
                dil=dil,
                altyazi_url=altyazi_url,
                intro_url=intro_url,
            )

        registered.append(encode_command)

    if tree.get_command(ENCODEAL_COMMAND_NAME) is None:
        @tree.command(
            name=ENCODEAL_COMMAND_NAME,
            description="Magnet, isteğe bağlı ASS/SRT altyazı ve intro dosya ekiyle H.264/H.265 encode eder.",
        )
        @app_commands.describe(
            magnet="Zorunlu torrent magnet bağlantısı (magnet:?xt=urn:btih:...)",
            altyazi="İsteğe bağlı .ass veya .srt altyazı dosyası eki (GCS staging'e yüklenir)",
            intro="İsteğe bağlı intro video dosyası eki (.mp4/.mkv, GCS staging'e yüklenir)",
            codec="Video codec seçimi: h264 (libx264) veya h265 (libx265)",
            dil="Altyazı dil kodu (varsayılan: tur)",
        )
        @app_commands.choices(
            codec=[
                app_commands.Choice(name="H.264 (libx264 — Yüksek Uyumluluk)", value="h264"),
                app_commands.Choice(name="H.265 / HEVC (libx265 — Yüksek Sıkıştırma)", value="h265"),
            ]
        )
        async def encodeal_command(
            interaction: discord.Interaction,
            magnet: str,
            altyazi: Optional[discord.Attachment] = None,
            intro: Optional[discord.Attachment] = None,
            codec: str = "h264",
            dil: str = "tur",
        ) -> None:
            await handle_encodeal_slash_command(
                interaction,
                magnet=magnet,
                altyazi=altyazi,
                intro=intro,
                codec=codec,
                dil=dil,
            )

        registered.append(encodeal_command)

    if tree.get_command(JOB_STATUS_COMMAND_NAME) is None:
        @tree.command(
            name=JOB_STATUS_COMMAND_NAME,
            description="Bir video encode işinin gerçek zamanlı ilerleme, FPS, ETA ve indirme durumunu sorgular.",
        )
        @app_commands.describe(is_id="Sorgulanacak iş kimliği (örnek: job_...)")
        async def job_status_command(interaction: discord.Interaction, is_id: str) -> None:
            await handle_job_status_slash_command(interaction, is_id=is_id)

        registered.append(job_status_command)

    discord_cmd = register_discord_restart_command(tree, bot=bot)
    if discord_cmd is not None:
        registered.append(discord_cmd)

    return registered



# ------------------------------------------------------------------------------
# CLI Entrypoint for Cloud Run Job Worker
# ------------------------------------------------------------------------------
def main_cli(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="UBAgen Cloud Run Job Video Encoder Worker")
    parser.add_argument(
        "--job-id",
        default=os.getenv("UBAGEN_JOB_ID", "").strip(),
        help="Encode Job ID to execute (defaults to UBAGEN_JOB_ID env var).",
    )
    parser.add_argument(
        "--cleanup-expired",
        action="store_true",
        help="Run server-side 7200s retention cleanup on expired GCS outputs and exit.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    store = get_job_store()
    if args.cleanup_expired:
        summary = store.cleanup_expired_jobs()
        logger.info("Expired job cleanup finished: %s", json.dumps(summary))
        return 0

    job_id = (args.job_id or "").strip()
    if not job_id:
        logger.error("No job ID provided (--job-id or UBAGEN_JOB_ID). Exiting with code 2.")
        return 2

    logger.info("Starting Cloud Run Job Encoder Worker for job_id=%s", job_id)
    result_job = execute_encode_job_sync(job_id, job_store=store)
    if result_job.status == "completed" and result_job.gcs_upload_verified:
        logger.info(
            "Encode job %s completed and verified in GCS (%s, expires_at=%s).",
            job_id,
            result_job.gcs_uri,
            result_job.expires_at,
        )
        return 0

    logger.error("Encode job %s failed: %s", job_id, result_job.error)
    return 1


if __name__ == "__main__":
    raise SystemExit(main_cli())
