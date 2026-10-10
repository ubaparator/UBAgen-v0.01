"""Discord slash command for extracting embedded text subtitles from videos or magnet downloads."""
import asyncio
import html
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

try:
    import discord
except ImportError:
    discord = None

logger = logging.getLogger("ubagen.subtitles")
MAX_INPUT = 100 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
DOWNLOAD_TIMEOUT = 600
VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts"}


def _run(args, timeout):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def subtitle_format(codec: str) -> tuple[str, str]:
    """Return (file suffix, FFmpeg subtitle encoder) without losing ASS styling."""
    normalized = codec.strip().lower()
    if normalized in {"ass", "ssa"}:
        return ".ass", "ass"
    return ".srt", "srt"


def clean_srt_file(path: Path) -> None:
    """Remove markup from SRT text while keeping subtitle timing and content."""
    raw = path.read_text(encoding="utf-8-sig", errors="replace")
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</?(?:i|b|u|font|span|ruby|rt|c)(?:\s+[^>]*)?>", "", raw)
    raw = re.sub(r"(?is)<[^>]+>", "", raw)
    raw = re.sub(r"\{[^}]*\}", "", raw)
    raw = html.unescape(raw).replace("\r\n", "\n").replace("\r", "\n")
    path.write_text(raw, encoding="utf-8")


def _find_videos(root: Path):
    return sorted(
        (p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_SUFFIXES),
        key=lambda p: p.stat().st_size,
        reverse=True,
    )


def _probe_subtitles(video_path: Path):
    result = _run(
        [
            "ffprobe", "-v", "error", "-select_streams", "s",
            "-show_entries", "stream=index,codec_name:stream_tags=language",
            "-of", "json", str(video_path),
        ],
        30,
    )
    if result.returncode:
        raise RuntimeError("Video could not be read by ffprobe.")
    return json.loads(result.stdout).get("streams", [])


def register_subtitle_extract_command(tree):
    if discord is None or tree is None:
        logger.warning("discord.py unavailable; subtitle command was not registered.")
        return
    if tree.get_command("altyazi_ayikla") is not None:
        return

    @tree.command(
        name="altyazi_ayikla",
        description="Videodan veya magnet indirmesinden seçilen dildeki altyazıyı çıkarır.",
    )
    @discord.app_commands.describe(
        video="İstersen doğrudan video dosyası yükle (en fazla 100 MB)",
        magnet_uri="İsteğe bağlı magnet bağlantısı; torrent içindeki videoyu indirir",
        dil="Altyazı dili kodu (varsayılan: tur)",
        altyazi_sirasi="Video dosyasında akış seçimi; 0 ilk altyazı akışıdır (yalnızca video yüklerken)",
    )
    async def altyazi_ayikla(
        interaction: discord.Interaction,
        video: Optional[discord.Attachment] = None,
        magnet_uri: Optional[str] = None,
        dil: str = "tur",
        altyazi_sirasi: discord.app_commands.Range[int, 0, 20] = 0,
    ):
        if (video is None) == (not magnet_uri):
            await interaction.response.send_message(
                "Yalnızca birini ver: video dosyası veya magnet_uri bağlantısı.",
                ephemeral=True,
            )
            return
        if magnet_uri and not magnet_uri.strip().lower().startswith("magnet:?"):
            await interaction.response.send_message("Geçerli bir magnet:? bağlantısı gir.", ephemeral=True)
            return
        if video is not None:
            if Path(video.filename).suffix.lower() not in VIDEO_SUFFIXES:
                await interaction.response.send_message("Desteklenmeyen video dosyası.", ephemeral=True)
                return
            if not 0 < video.size <= MAX_INPUT:
                await interaction.response.send_message("Video boş olamaz ve en fazla 100 MB olabilir.", ephemeral=True)
                return
        lang = (dil or "tur").strip().lower()
        if not re.fullmatch(r"[a-z0-9]{2,3}", lang):
            await interaction.response.send_message("Dil kodu 2-3 harf/rakam olmalı (ör. tur, eng, deu).", ephemeral=True)
            return
        if magnet_uri and shutil.which("aria2c") is None:
            await interaction.response.send_message(
                "Magnet indirmek için sunucuda aria2c bulunamadı. Docker imajını yeniden oluştur.",
                ephemeral=True,
            )
            return
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            await interaction.response.send_message("Sunucuda ffmpeg/ffprobe kurulu değil.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            with tempfile.TemporaryDirectory(prefix="ubagen-subtitle-") as directory:
                workdir = Path(directory)
                if video is not None:
                    source = workdir / ("input" + Path(video.filename).suffix.lower())
                    await video.save(source)
                    videos = [source]
                else:
                    download = await asyncio.to_thread(
                        _run,
                        [
                            "aria2c", "--seed-time=0", "--max-connection-per-server=16",
                            "--split=16", "--enable-dht=true", "--bt-max-peers=200",
                            "--summary-interval=2", "--console-log-level=warn",
                            "--dir", str(workdir), "--continue=true", "--allow-overwrite=true",
                            magnet_uri.strip(),
                        ],
                        DOWNLOAD_TIMEOUT,
                    )
                    videos = _find_videos(workdir)
                    if download.returncode != 0 and not videos:
                        logger.warning("aria2c download failed: %s", download.stderr[-1200:])
                        await interaction.followup.send(
                            "Torrent indirilemedi. Magnet bağlantısını, seed durumunu ve sunucu ağ erişimini kontrol et."
                        )
                        return
                    if not videos:
                        await interaction.followup.send("İndirilen torrent içinde desteklenen video bulunamadı.")
                        return

                selected = None
                selected_stream = None
                for candidate in videos:
                    streams = await asyncio.to_thread(_probe_subtitles, candidate)
                    if not streams:
                        continue
                    if video is not None:
                        if altyazi_sirasi >= len(streams):
                            listing = "\n".join(
                                f"{i}: {s.get('codec_name', 'unknown')} / {s.get('tags', {}).get('language', 'und')}"
                                for i, s in enumerate(streams[:20])
                            )
                            await interaction.followup.send(f"Geçersiz altyazı sırası. Akışlar:\n{listing}")
                            return
                        selected, selected_stream = candidate, streams[altyazi_sirasi]
                        break
                    matches = [
                        s for s in streams
                        if str(s.get("tags", {}).get("language", "")).lower().split("-")[0] == lang
                    ]
                    selected, selected_stream = candidate, (matches[0] if matches else streams[0])
                    if matches:
                        break

                if selected is None or selected_stream is None:
                    await interaction.followup.send(
                        f"İndirilen videolarda altyazı akışı bulunamadı (aranan dil: {lang})."
                    )
                    return

                codec = str(selected_stream.get("codec_name", "")).lower()
                if codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}:
                    await interaction.followup.send(
                        "Seçilen altyazı görüntü tabanlı; metne dönüştürmek için OCR gerekir."
                    )
                    return
                suffix, subtitle_encoder = subtitle_format(codec)
                output = workdir / f"extracted{suffix}"
                result = await asyncio.to_thread(
                    _run,
                    [
                        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                        "-i", str(selected), "-map", f"0:{selected_stream['index']}",
                        "-vn", "-an", "-c:s", subtitle_encoder, str(output),
                    ],
                    120,
                )
                if result.returncode or not output.is_file() or output.stat().st_size == 0:
                    logger.warning("Subtitle extraction failed: %s", result.stderr[-1000:])
                    await interaction.followup.send("Altyazı çıkarılamadı; akış desteklenmiyor olabilir.")
                    return
                # ASS/SSA override tags and style metadata are meaningful; only
                # sanitize SRT text, never strip formatting from ASS output.
                if suffix == ".srt":
                    clean_srt_file(output)
                if output.stat().st_size > MAX_OUTPUT:
                    await interaction.followup.send("Çıktı 8 MB sınırını aşıyor.")
                    return
                if output.stat().st_size == 0:
                    await interaction.followup.send("Çıkarılan altyazı boş.")
                    return

                filename = f"{selected.stem[:70]}_{lang}{suffix}"
                await interaction.followup.send(
                    f"Altyazı çıkarıldı: {selected.name} (hedef dil: {lang}; akış kodu: {codec or 'bilinmiyor'}).",
                    file=discord.File(str(output), filename=filename),
                )
        except subprocess.TimeoutExpired:
            await interaction.followup.send("İşlem zaman aşımına uğradı. Daha küçük bir torrent/video dene.")
        except Exception:
            logger.exception("Error in /altyazi_ayikla")
            if not interaction.is_expired():
                await interaction.followup.send("Altyazı işlenirken hata oluştu; bot loglarını kontrol edin.")
