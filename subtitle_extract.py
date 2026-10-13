"""Discord slash command (/altyazı_ayıklama) for downloading torrents via aria2c and extracting embedded subtitles via FFmpeg."""

from __future__ import annotations

import asyncio
from contextlib import suppress
import html
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import uuid
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

try:
    import discord
except ImportError:
    discord = None

try:
    from stats import clear_binary_cache
except ImportError:
    def clear_binary_cache() -> None:
        pass

try:
    from storage_manager import get_storage_manager
except ImportError:
    get_storage_manager = None

logger = logging.getLogger("ubagen.subtitles")

COMMAND_NAME = "altyazı_ayıklama"
MAX_INPUT = 1_500_000_000  # 1.5 GB (decimal)
MAX_OUTPUT = 8 * 1024 * 1024  # 8 MB Discord standard attachment ceiling
DOWNLOAD_TIMEOUT = 600
FFPROBE_TIMEOUT = 30
FFMPEG_TIMEOUT = 180

DOWNLOAD_TEMP_DIR = Path("download_temp")
ENCODE_TEMP_DIR = DOWNLOAD_TEMP_DIR / "encode_temp"
VIDEO_OUTPUTS_DIR = Path("video_outputs")
PRIMARY_VIDEO_SUFFIXES = (".mkv", ".mp4")
VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts"}
BITMAP_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}

REQUIRED_BINARIES: tuple[tuple[str, str, str], ...] = (
    ("aria2c", "--version", "aria2"),
    ("ffmpeg", "-version", "ffmpeg"),
    ("ffprobe", "-version", "ffmpeg"),
)
_INSTALL_LOCK = threading.Lock()

# Colab torrent downloader tracker list (preserved for aria2c peer discovery)
COLAB_TRACKERS: tuple[str, ...] = (
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.tracker.cl:1337/announce",
    "udp://p4p.arenabg.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://tracker.dler.org:6969/announce",
    "udp://open.stealth.si:80/announce",
    "udp://ipv4.tracker.harry.lu:80/announce",
    "https://opentrackr.org:443/announce",
)
BT_TRACKER_LIST = ",".join(COLAB_TRACKERS)

LANGUAGE_ALIASES: dict[str, set[str]] = {
    "tur": {"tur", "tr", "turkish", "türkçe", "turkce"},
    "tr": {"tur", "tr", "turkish", "türkçe", "turkce"},
    "eng": {"eng", "en", "english"},
    "en": {"eng", "en", "english"},
    "jpn": {"jpn", "ja", "japanese"},
    "ja": {"jpn", "ja", "japanese"},
    "deu": {"deu", "ger", "de", "german", "deutsch"},
    "ger": {"deu", "ger", "de", "german", "deutsch"},
    "de": {"deu", "ger", "de", "german", "deutsch"},
    "fra": {"fra", "fre", "fr", "french"},
    "fre": {"fra", "fre", "fr", "french"},
    "fr": {"fra", "fre", "fr", "french"},
    "spa": {"spa", "es", "spanish", "espanol", "español"},
    "es": {"spa", "es", "spanish", "espanol", "español"},
    "ita": {"ita", "it", "italian"},
    "it": {"ita", "it", "italian"},
    "rus": {"rus", "ru", "russian"},
    "ru": {"rus", "ru", "russian"},
    "ara": {"ara", "ar", "arabic"},
    "ar": {"ara", "ar", "arabic"},
}


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    """Run an external command synchronously with captured stdout/stderr and timeout."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def check_runtime_binary(binary_name: str, version_arg: str = "--version") -> dict[str, Any]:
    """
    Verify that `binary_name` exists on PATH (`shutil.which`) and is executable.
    Returns diagnostic metadata with path, version, and executability state.
    """
    resolved_path = shutil.which(binary_name)
    if not resolved_path:
        return {
            "name": binary_name,
            "installed": False,
            "executable": False,
            "path": None,
            "version": "Not installed",
        }

    try:
        proc = subprocess.run(
            [resolved_path, version_arg],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        raw_out = (proc.stdout or proc.stderr or "").strip()
        first_line = raw_out.splitlines()[0].strip() if raw_out else "Installed"
        executable = proc.returncode == 0 or bool(first_line)
        return {
            "name": binary_name,
            "installed": True,
            "executable": executable,
            "path": resolved_path,
            "version": first_line,
        }
    except Exception as exc:
        return {
            "name": binary_name,
            "installed": True,
            "executable": False,
            "path": resolved_path,
            "version": f"Execution failed ({type(exc).__name__})",
        }


def ensure_runtime_binaries(auto_install: bool = True) -> dict[str, dict[str, Any]]:
    """
    Verify that `aria2c`, `ffmpeg`, and `ffprobe` are present and executable in the current runtime.
    If any required binary is missing and `auto_install=True`, attempts installation via the platform's
    official Debian/Ubuntu `apt-get` package manager when running with root privileges (such as AI Studio Run).
    Never downloads arbitrary binaries or executes untrusted remote shell scripts.
    """
    statuses: dict[str, dict[str, Any]] = {}
    missing_apt_packages: list[str] = []

    for bin_name, ver_arg, apt_pkg in REQUIRED_BINARIES:
        info = check_runtime_binary(bin_name, ver_arg)
        statuses[bin_name] = info
        if (not info["installed"] or not info["executable"]) and apt_pkg not in missing_apt_packages:
            missing_apt_packages.append(apt_pkg)

    if not missing_apt_packages or not auto_install:
        return statuses

    with _INSTALL_LOCK:
        # Re-check inside lock in case another caller already completed installation
        missing_apt_packages.clear()
        for bin_name, ver_arg, apt_pkg in REQUIRED_BINARIES:
            info = check_runtime_binary(bin_name, ver_arg)
            statuses[bin_name] = info
            if (not info["installed"] or not info["executable"]) and apt_pkg not in missing_apt_packages:
                missing_apt_packages.append(apt_pkg)

        if not missing_apt_packages:
            return statuses

        apt_get_path = shutil.which("apt-get")
        is_root = hasattr(os, "geteuid") and os.geteuid() == 0
        if apt_get_path and is_root:
            logger.info(
                "Missing system multimedia package(s) %s detected; installing via official apt-get...",
                ", ".join(missing_apt_packages),
            )
            env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
            try:
                update_res = subprocess.run(
                    [apt_get_path, "update", "-qq"],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=90,
                    check=False,
                )
                if update_res.returncode != 0:
                    logger.warning(
                        "apt-get update exited with code %s: %s",
                        update_res.returncode,
                        (update_res.stderr or update_res.stdout or "").strip()[-500:],
                    )
                install_res = subprocess.run(
                    [
                        apt_get_path,
                        "-o",
                        "Dpkg::Options::=--force-confdef",
                        "-o",
                        "Dpkg::Options::=--force-confold",
                        "install",
                        "-y",
                        "--no-install-recommends",
                        *missing_apt_packages,
                        "ca-certificates",
                        "curl",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=180,
                    check=False,
                )
                if install_res.returncode != 0:
                    logger.warning(
                        "apt-get install exited with code %s: %s",
                        install_res.returncode,
                        (install_res.stderr or install_res.stdout or "").strip()[-500:],
                    )
                clear_binary_cache()
            except Exception as exc:
                logger.warning("Automatic apt-get provisioning failed (%s): %s", type(exc).__name__, exc)
        elif not apt_get_path:
            logger.error(
                "Cannot auto-install missing multimedia tools %s: apt-get is unavailable in this runtime. "
                "Install aria2 and ffmpeg in the image/build configuration.",
                ", ".join(missing_apt_packages),
            )
        else:
            logger.error(
                "Cannot auto-install missing multimedia tools %s: apt-get requires root privileges, "
                "but the current process is not root (euid=%s). Install aria2 and ffmpeg during image build.",
                ", ".join(missing_apt_packages),
                os.geteuid() if hasattr(os, "geteuid") else "unknown",
            )

        for bin_name, ver_arg, _ in REQUIRED_BINARIES:
            statuses[bin_name] = check_runtime_binary(bin_name, ver_arg)

    return statuses


def log_runtime_binaries_status(auto_install: bool = True) -> dict[str, dict[str, Any]]:
    """Verify and log the paths and versions of `aria2c`, `ffmpeg`, and `ffprobe` at startup."""
    statuses = ensure_runtime_binaries(auto_install=auto_install)
    for bin_name, info in statuses.items():
        if info["installed"] and info["executable"]:
            logger.info(
                "Runtime binary verified: %-8s -> %s (%s)",
                bin_name,
                info["path"],
                info["version"],
            )
        else:
            logger.error(
                "Runtime binary MISSING or NOT EXECUTABLE: %-8s (status: %s)",
                bin_name,
                info["version"],
            )
    return statuses


def cleanup_stale_workdirs(base_dir: Optional[Path] = None) -> int:
    """Remove any leftover `job_*` and `encode_*` directories inside `download_temp` and run output retention cleanup."""
    root = Path(base_dir) if base_dir is not None else DOWNLOAD_TEMP_DIR
    removed = 0
    if root.exists():
        for child in root.iterdir():
            if child.is_dir() and (child.name.startswith("job_") or child.name.startswith("encode_")):
                with suppress(Exception):
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
        encode_root = root / "encode_temp"
        if encode_root.exists():
            for child in encode_root.iterdir():
                if child.is_dir() and child.name.startswith("encode_"):
                    with suppress(Exception):
                        shutil.rmtree(child, ignore_errors=True)
                        removed += 1
    if base_dir is None and get_storage_manager is not None:
        with suppress(Exception):
            mgr = get_storage_manager()
            removed += mgr.cleanup_stale_temp_dirs()
            mgr.cleanup_outputs()
    return removed


def _is_public_ip_address(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True strictly if `ip_obj` (including any IPv4-mapped IPv6 address) is a public routable IP."""
    mapped = getattr(ip_obj, "ipv4_mapped", None)
    if mapped is not None:
        return _is_public_ip_address(mapped)
    if (
        not ip_obj.is_global
        or ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    ):
        return False
    return True


def validate_video_source(source: str) -> tuple[bool, str]:
    """
    Accept a torrent magnet URI or a direct HTTP(S) media URL.
    Reject local/private literal addresses, metadata hosts, credentials, and non-HTTP schemes
    to prevent SSRF when fetching user-supplied URLs.
    """
    raw = (source or "").strip()
    if not raw:
        return False, "Video kaynağı boş olamaz. Lütfen bir magnet bağlantısı veya doğrudan HTTP/HTTPS medya URL'si girin."
    if raw.lower().startswith("magnet:"):
        # Preserve the existing torrent validation and compatibility.
        return validate_magnet_uri(raw)
    try:
        parsed = urlparse(raw)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False, "Video kaynağı magnet bağlantısı veya doğrudan HTTP/HTTPS medya URL'si olmalıdır."
        if parsed.username or parsed.password or "@" in (parsed.netloc or ""):
            return False, "Kimlik bilgisi içeren URL'ler kabul edilmiyor."
        host = parsed.hostname.rstrip(".").lower()
        if (
            host in {"localhost", "localhost.localdomain", "metadata.google.internal", "metadata", "instance-data"}
            or host.endswith(".localhost")
            or host.endswith(".local")
            or host.endswith(".internal")
        ):
            return False, "Yerel ağ/metadata adresleri video kaynağı olarak kabul edilmiyor."
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            ip = None
        if ip is not None and not _is_public_ip_address(ip):
            return False, "Özel veya yerel IP adresleri video kaynağı olarak kabul edilmiyor."
        if any(ch.isspace() for ch in raw) or len(raw) > 4096:
            return False, "Video URL'si geçersiz veya çok uzun."
        return True, raw
    except Exception:
        return False, "Video kaynağı URL formatı geçersiz."


def verify_url_resolved_ips(url: str) -> tuple[bool, str]:
    """
    Resolve the hostname of an HTTP(S) URL via DNS and verify that all resolved IPs are public
    (mitigates DNS rebinding against internal/metadata addresses during worker download).
    """
    ok, val_or_err = validate_video_source(url)
    if not ok:
        return False, val_or_err
    parsed = urlparse(val_or_err)
    host = (parsed.hostname or "").rstrip(".").lower()
    if not host:
        return False, "Video URL'sinde geçerli bir sunucu adı bulunamadı."
    try:
        ip_literal = ipaddress.ip_address(host)
        if not _is_public_ip_address(ip_literal):
            return False, "Özel veya yerel IP adresleri video kaynağı olarak kabul edilmiyor."
        return True, val_or_err
    except ValueError:
        pass

    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80), type=socket.SOCK_STREAM)
    except OSError:
        return False, f"Video kaynağı sunucu adresi çözümlenemedi (`{host}`)."

    if not infos:
        return False, f"Video kaynağı sunucu adresi çözümlenemedi (`{host}`)."

    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip_str = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, "Çözümlenen IP adresi geçersiz."
        if not _is_public_ip_address(ip_obj):
            return False, "Güvenlik engeli: Video URL sunucusu özel veya yerel bir IP adresine çözümlendi."

    return True, val_or_err


def validate_magnet_uri(magnet: Optional[str]) -> tuple[bool, str]:
    """Validate that a user-supplied magnet link is a well-formed BitTorrent magnet URI."""
    if not magnet or not magnet.strip():
        return False, "Magnet bağlantısı boş olamaz. Lütfen geçerli bir `magnet:?xt=urn:btih:...` bağlantısı girin."
    cleaned = magnet.strip()
    lower = cleaned.lower()
    if not lower.startswith("magnet:?"):
        return False, "Geçersiz bağlantı: Magnet bağlantısı `magnet:?` ile başlamalıdır."
    if "xt=urn:btih:" not in lower:
        return False, "Geçersiz magnet bağlantısı: `xt=urn:btih:` torrent hash bilgisi eksik."
    return True, cleaned


def normalize_language_code(dil: Optional[str]) -> tuple[bool, str]:
    """Validate and normalize the requested subtitle language code (default: 'tur')."""
    lang = (dil or "tur").strip().lower()
    if not lang:
        lang = "tur"
    if not re.fullmatch(r"[a-z0-9_-]{2,12}", lang):
        return False, "Dil kodu geçersiz (örnek: `tur`, `eng`, `jpn`, `deu`)."
    return True, lang


def create_job_workdir(base_dir: Optional[Path] = None) -> Path:
    """Create a unique, per-invocation isolated temporary working directory inside download_temp."""
    root = Path(base_dir) if base_dir is not None else DOWNLOAD_TEMP_DIR
    root.mkdir(parents=True, exist_ok=True)
    job_dir = tempfile.mkdtemp(prefix=f"job_{uuid.uuid4().hex[:8]}_", dir=str(root))
    return Path(job_dir)


def create_encode_workdir(base_dir: Optional[Path] = None) -> Path:
    """Create a unique, per-invocation isolated temporary directory for FFmpeg encoding."""
    root = Path(base_dir) if base_dir is not None else ENCODE_TEMP_DIR
    root.mkdir(parents=True, exist_ok=True)
    enc_dir = tempfile.mkdtemp(prefix=f"encode_{uuid.uuid4().hex[:8]}_", dir=str(root))
    return Path(enc_dir)


def build_aria2c_command(magnet: str, workdir: Path) -> list[str]:
    """
    Build the aria2c command preserving the Colab torrent configuration:
    - tracker list (--bt-tracker)
    - DHT enabled (--enable-dht=true) with isolated per-job DHT file (--dht-file-path)
    - 16 connections per server & 16 splits (--max-connection-per-server=16 --split=16)
    - zero seeding (--seed-time=0)
    - isolated output directory (-d / --dir)
    """
    dht_file = workdir / "dht.dat"
    return [
        "aria2c",
        "--seed-time=0",
        "--max-connection-per-server=16",
        "--split=16",
        "--enable-dht=true",
        f"--dht-file-path={dht_file}",
        "--bt-enable-lpd=true",
        "--enable-peer-exchange=true",
        "--bt-max-peers=200",
        f"--bt-tracker={BT_TRACKER_LIST}",
        "--bt-stop-timeout=180",
        "--summary-interval=0",
        "--console-log-level=warn",
        "--allow-overwrite=true",
        "--continue=true",
        "-d",
        str(workdir),
        magnet.strip(),
    ]


def subtitle_format(codec: str) -> tuple[str, str]:
    """Return (file suffix, FFmpeg subtitle encoder) without losing ASS/SSA styling."""
    normalized = (codec or "").strip().lower()
    if normalized in {"ass", "ssa"}:
        return ".ass", "ass"
    return ".srt", "srt"


def clean_srt_file(path: Path) -> None:
    """Remove HTML and inline override markup from SRT text while keeping subtitle timing and content."""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</?(?:i|b|u|font|span|ruby|rt|c)(?:\s+[^>]*)?>", "", raw)
    raw = re.sub(r"(?is)<[^>]+>", "", raw)
    raw = re.sub(r"\{[^}]*\}", "", raw)
    raw = html.unescape(raw).replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(raw, encoding="utf-8")


def find_video_files(root: Path) -> list[Path]:
    """
    Locate downloaded video files under `root`.
    Preserves Colab priority: checks .mkv and .mp4 files first (sorted largest-first),
    then falls back to other video extensions. Ignores .aria2 control files and empty files.
    """
    if not root.exists():
        return []

    all_files = [
        p
        for p in root.rglob("*")
        if p.is_file()
        and not p.name.endswith(".aria2")
        and p.suffix.lower() in VIDEO_SUFFIXES
        and p.stat().st_size > 0
    ]
    primary = sorted(
        (p for p in all_files if p.suffix.lower() in PRIMARY_VIDEO_SUFFIXES),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    secondary = sorted(
        (p for p in all_files if p.suffix.lower() not in PRIMARY_VIDEO_SUFFIXES),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )
    return primary + secondary


# Alias for backward compatibility
_find_videos = find_video_files


def probe_subtitle_streams(video_path: Path) -> list[dict[str, Any]]:
    """Inspect embedded subtitle streams in `video_path` using ffprobe."""
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "s",
            "-show_entries",
            "stream=index,codec_name:stream_tags=language,title",
            "-of",
            "json",
            str(video_path),
        ],
        FFPROBE_TIMEOUT,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Video ffprobe ile okunamadı: {result.stderr.strip()[-300:]}")
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError("ffprobe çıktısı geçerli JSON değil.") from exc
    streams = payload.get("streams", [])
    return streams if isinstance(streams, list) else []


_probe_subtitles = probe_subtitle_streams


def _stream_matches_language(stream: dict[str, Any], target_lang: str) -> bool:
    """Check whether a subtitle stream's language tag or title matches `target_lang`."""
    target = target_lang.strip().lower()
    accepted = LANGUAGE_ALIASES.get(target, {target, target.split("-")[0]})

    tags = stream.get("tags") or {}
    raw_lang = str(tags.get("language") or "").strip().lower()
    lang_base = raw_lang.split("-")[0].split("_")[0]
    if raw_lang in accepted or (lang_base and lang_base in accepted):
        return True

    raw_title = str(tags.get("title") or "").strip().lower()
    if raw_title:
        for alias in accepted:
            if len(alias) >= 3 and re.search(rf"\b{re.escape(alias)}\b", raw_title):
                return True
    return False


def _is_bitmap_stream(stream: dict[str, Any]) -> bool:
    codec = str(stream.get("codec_name") or "").strip().lower()
    return codec in BITMAP_SUBTITLE_CODECS


def select_subtitle_stream(
    streams: list[dict[str, Any]],
    target_lang: str = "tur",
) -> tuple[Optional[dict[str, Any]], bool]:
    """
    Select the best subtitle stream for `target_lang`.
    Returns `(stream_dict, matched_requested_language)`.
    Prefers text-based subtitle streams over bitmap/image streams.
    """
    if not streams:
        return None, False

    matching = [s for s in streams if _stream_matches_language(s, target_lang)]
    if matching:
        text_matching = [s for s in matching if not _is_bitmap_stream(s)]
        return (text_matching[0] if text_matching else matching[0]), True

    text_all = [s for s in streams if not _is_bitmap_stream(s)]
    return (text_all[0] if text_all else streams[0]), False


def build_ffmpeg_extract_command(
    video_path: Path,
    stream_index: int,
    subtitle_encoder: str,
    output_path: Path,
) -> list[str]:
    """Build the FFmpeg command to extract a specific subtitle stream index."""
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video_path),
        "-map",
        f"0:{stream_index}",
        "-vn",
        "-an",
        "-c:s",
        subtitle_encoder,
        str(output_path),
    ]


async def _update_progress(interaction: Any, message: str) -> None:
    """Safely update the deferred interaction response with progressive status."""
    if hasattr(interaction, "edit_original_response"):
        with suppress(Exception):
            await interaction.edit_original_response(content=message)
            return
    if hasattr(interaction, "followup") and hasattr(interaction.followup, "send"):
        with suppress(Exception):
            await interaction.followup.send(message)


async def _send_final_error(interaction: Any, message: str) -> None:
    """Send or update the interaction with a user-friendly error message."""
    if hasattr(interaction, "edit_original_response"):
        try:
            await interaction.edit_original_response(content=f"❌ {message}")
            return
        except Exception:
            pass
    if hasattr(interaction, "followup") and hasattr(interaction.followup, "send"):
        with suppress(Exception):
            await interaction.followup.send(f"❌ {message}")


async def execute_subtitle_extraction(
    interaction: Any,
    magnet: str,
    dil: str = "tur",
    base_temp_dir: Optional[Path] = None,
) -> None:
    """
    Core asynchronous handler for `/altyazı_ayıklama`.
    1. Immediately defers the interaction to prevent Discord's 3-second timeout.
    2. Validates magnet URI, language parameter, and required system binaries (aria2c, ffmpeg, ffprobe).
    3. Downloads the torrent into an isolated per-job folder under `download_temp`.
    4. Locates the downloaded video (.mkv/.mp4 prioritized), probes subtitle streams, and extracts the requested language.
    5. Cleans SRT markup (or preserves ASS styling) and sends the subtitle file to Discord.
    6. Always cleans up the isolated job folder in `finally:`.
    """
    # 1. Immediately defer interaction
    if hasattr(interaction, "response") and hasattr(interaction.response, "defer"):
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)

    await _update_progress(
        interaction,
        "⏳ **İşlem başlatıldı:** Magnet bağlantısı ve sistem araçları kontrol ediliyor...",
    )

    # 2. Validate inputs & binaries
    valid_magnet, magnet_or_error = validate_magnet_uri(magnet)
    if not valid_magnet:
        await _send_final_error(interaction, magnet_or_error)
        return

    valid_lang, lang_or_error = normalize_language_code(dil)
    if not valid_lang:
        await _send_final_error(interaction, lang_or_error)
        return
    lang = lang_or_error

    if shutil.which("aria2c") is None or shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        await asyncio.to_thread(ensure_runtime_binaries, True)

    if shutil.which("aria2c") is None:
        await _send_final_error(
            interaction,
            "Torrent indirmek için sunucuda `aria2c` bulunamadı. Lütfen Docker imajını veya sunucu paketlerini kontrol edin.",
        )
        return

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        await _send_final_error(
            interaction,
            "Altyazı ayıklamak için sunucuda `ffmpeg` / `ffprobe` bulunamadı.",
        )
        return

    if base_temp_dir is None and get_storage_manager is not None:
        space_ok, space_msg = await asyncio.to_thread(
            get_storage_manager().check_space_for_job,
            256 * 1024 * 1024,
        )
        if not space_ok:
            await _send_final_error(interaction, space_msg)
            return

    workdir: Optional[Path] = None
    try:
        workdir = create_job_workdir(base_temp_dir)

        # 3. Torrent download stage via aria2c
        await _update_progress(
            interaction,
            f"📥 **Torrent indiriliyor (`aria2c`)...**\n"
            f"• Hedef dil: `{lang}`\n"
            f"• Bağlantı: 16 split / DHT + Colab tracker listesi aktif",
        )

        aria2_cmd = build_aria2c_command(magnet_or_error, workdir)
        download = await asyncio.to_thread(_run, aria2_cmd, DOWNLOAD_TIMEOUT)

        videos = await asyncio.to_thread(find_video_files, workdir)
        if download.returncode != 0 and not videos:
            stderr_tail = (download.stderr or download.stdout or "").strip()[-1000:]
            logger.warning("aria2c download failed (code %s): %s", download.returncode, stderr_tail)
            await _send_final_error(
                interaction,
                "Torrent indirilemedi. Magnet bağlantısının geçerli olduğundan ve aktif seeder bulunduğundan emin olun.",
            )
            return

        if not videos:
            await _send_final_error(
                interaction,
                "İndirilen torrent içeriğinde desteklenen bir video dosyası (`.mkv`, `.mp4` vb.) bulunamadı.",
            )
            return

        # 4. Probe subtitle streams across downloaded videos
        await _update_progress(
            interaction,
            f"🔍 **Video bulundu:** `{videos[0].name}`\n"
            f"Altyazı akışları (`ffprobe`) taranıyor (aranan dil: `{lang}`)...",
        )

        selected_video: Optional[Path] = None
        selected_stream: Optional[dict[str, Any]] = None
        matched_lang = False
        probe_errors: list[str] = []

        for candidate in videos:
            try:
                streams = await asyncio.to_thread(probe_subtitle_streams, candidate)
            except Exception as exc:
                probe_errors.append(str(exc))
                continue

            if not streams:
                continue

            stream, is_exact_match = select_subtitle_stream(streams, lang)
            if stream is not None:
                selected_video = candidate
                selected_stream = stream
                matched_lang = is_exact_match
                if is_exact_match:
                    break

        if selected_video is None or selected_stream is None:
            if probe_errors and not selected_video:
                logger.warning("ffprobe errors while inspecting downloaded videos: %s", "; ".join(probe_errors))
            await _send_final_error(
                interaction,
                f"İndirilen video dosyasında (`{videos[0].name}`) gömülü altyazı akışı bulunamadı (aranan dil: `{lang}`).",
            )
            return

        codec = str(selected_stream.get("codec_name") or "").strip().lower()
        stream_index = selected_stream.get("index")
        stream_lang = str((selected_stream.get("tags") or {}).get("language") or "und").strip()

        if codec in BITMAP_SUBTITLE_CODECS:
            await _send_final_error(
                interaction,
                f"Seçilen altyazı akışı (`{codec}`) görüntü tabanlıdır (PGS/VobSub); metin tabanlı `.srt`/`.ass` olarak doğrudan çıkarılamaz (OCR gerekir).",
            )
            return

        if stream_index is None:
            await _send_final_error(interaction, "Altyazı akış numarası (stream index) okunamadı.")
            return

        # 5. Extract subtitle stream via FFmpeg
        suffix, subtitle_encoder = subtitle_format(codec)
        output_path = workdir / f"extracted_{stream_index}{suffix}"

        await _update_progress(
            interaction,
            f"⚙️ **Altyazı ayıklanıyor (`ffmpeg`)...**\n"
            f"• Video: `{selected_video.name}`\n"
            f"• Akış: `0:{stream_index}` (`{codec or 'unknown'}`, dil etiketi: `{stream_lang}`)",
        )

        ffmpeg_cmd = build_ffmpeg_extract_command(
            selected_video,
            int(stream_index),
            subtitle_encoder,
            output_path,
        )
        extract_res = await asyncio.to_thread(_run, ffmpeg_cmd, FFMPEG_TIMEOUT)

        if extract_res.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
            logger.warning("FFmpeg subtitle extraction failed: %s", (extract_res.stderr or "")[-1000:])
            await _send_final_error(
                interaction,
                "Altyazı FFmpeg ile çıkarılamadı; seçilen akış bozuk veya desteklenmeyen bir formatta olabilir.",
            )
            return

        # Only strip HTML/override tags on .srt outputs; preserve .ass/.ssa styling intact
        if suffix == ".srt":
            await asyncio.to_thread(clean_srt_file, output_path)

        file_size = output_path.stat().st_size
        if file_size == 0:
            await _send_final_error(interaction, "Çıkarılan altyazı dosyası temizleme sonrasında boş kaldı.")
            return

        # 6. Prepare output filename and store/deliver
        safe_stem = re.sub(r"[^\w.\-]+", "_", selected_video.stem).strip("_")[:70] or "subtitle"
        effective_lang_label = lang if matched_lang else stream_lang
        out_filename = f"{safe_stem}_{effective_lang_label}{suffix}"

        if file_size > MAX_OUTPUT:
            stored_info: Optional[dict[str, Any]] = None
            if get_storage_manager is not None:
                with suppress(Exception):
                    stored_info = await asyncio.to_thread(
                        get_storage_manager().store_output_file,
                        output_path,
                        out_filename,
                    )
            if stored_info:
                persistence_note = (
                    "Kalıcı sunucu diskine kaydedildi."
                    if stored_info.get("is_persistent")
                    else "Geçici sunucu depolamasına kaydedildi (konteyner yeniden başlatılırsa silinir)."
                )
                await _send_final_error(
                    interaction,
                    f"Çıkarılan altyazı dosyası 8 MB Discord gönderim sınırını aşıyor (`{stored_info['size_str']}`). "
                    f"Dosya sunucunun `video_outputs` dizinine alındı: `{stored_info['download_url']}` ({persistence_note})",
                )
            else:
                await _send_final_error(
                    interaction,
                    "Çıkarılan altyazı dosyası 8 MB Discord gönderim sınırını aşıyor.",
                )
            return

        match_note = (
            f"Hedef dil (`{lang}`) eşleşti."
            if matched_lang
            else f"`{lang}` dili bulunamadı; mevcut ilk uygun akış (`{stream_lang}`) seçildi."
        )
        summary_msg = (
            f"✅ **Altyazı başarıyla ayıklandı!**\n"
            f"• **Video:** `{selected_video.name}`\n"
            f"• **Akış:** `0:{stream_index}` (`{codec or 'bilinmiyor'}`)\n"
            f"• **Dil:** {match_note}"
        )

        if discord is not None and hasattr(interaction, "followup") and hasattr(interaction.followup, "send"):
            await _update_progress(interaction, "✅ **Altyazı hazırlandı, dosya yükleniyor...**")
            discord_file = discord.File(str(output_path), filename=out_filename)
            try:
                await interaction.followup.send(
                    content=summary_msg,
                    file=discord_file,
                )
                await _update_progress(interaction, summary_msg)
            finally:
                with suppress(Exception):
                    discord_file.close()
        else:
            await _update_progress(interaction, summary_msg)

    except subprocess.TimeoutExpired:
        logger.warning("Timeout expired during /%s execution.", COMMAND_NAME)
        await _send_final_error(
            interaction,
            "İşlem zaman aşımına uğradı. Torrent çok büyük veya seeder sayısı yetersiz olabilir.",
        )
    except Exception:
        logger.exception("Unexpected error in /%s", COMMAND_NAME)
        await _send_final_error(
            interaction,
            "Altyazı ayıklama sırasında beklenmeyen bir hata oluştu; lütfen bot loglarını kontrol edin.",
        )
    finally:
        if workdir is not None and workdir.exists():
            with suppress(Exception):
                shutil.rmtree(workdir, ignore_errors=True)


def register_subtitle_extract_command(tree: Any) -> Any:
    """Register the `/altyazı_ayıklama` slash command on the given discord.app_commands.CommandTree."""
    if discord is None or tree is None:
        logger.warning("discord.py unavailable; /%s command was not registered.", COMMAND_NAME)
        return None

    existing = tree.get_command(COMMAND_NAME)
    if existing is not None:
        return existing

    @tree.command(
        name=COMMAND_NAME,
        description="Magnet bağlantısından torrent videosunu indirir ve gömülü altyazıyı ayıklar.",
    )
    @discord.app_commands.describe(
        magnet="Zorunlu torrent magnet bağlantısı (magnet:?xt=urn:btih:...)",
        dil="İsteğe bağlı altyazı dili kodu (varsayılan: tur)",
    )
    async def altyazi_ayiklama_command(
        interaction: discord.Interaction,
        magnet: str,
        dil: str = "tur",
    ) -> None:
        await execute_subtitle_extraction(interaction, magnet=magnet, dil=dil)

    return altyazi_ayiklama_command
