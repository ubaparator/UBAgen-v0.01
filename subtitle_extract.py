"""Discord slash command for extracting embedded text subtitles from video files."""
import asyncio
import json
import logging
import subprocess
import tempfile
from pathlib import Path

try:
    import discord
except ImportError:
    discord = None

logger = logging.getLogger("ubagen.subtitles")
MAX_INPUT = 100 * 1024 * 1024
MAX_OUTPUT = 8 * 1024 * 1024
VIDEO_SUFFIXES = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts"}


def _run(args, timeout):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def register_subtitle_extract_command(tree):
    if discord is None or tree is None:
        logger.warning("discord.py unavailable; /altyazi_ayikla was not registered.")
        return
    if tree.get_command("altyazi_ayikla") is not None:
        return

    @tree.command(name="altyazi_ayikla", description="Videonun içine gömülü altyazıyı çıkarır.")
    @discord.app_commands.describe(video="Altyazı çıkarılacak video", altyazi_sirasi="0 ilk altyazıdır")
    async def altyazi_ayikla(
        interaction: discord.Interaction,
        video: discord.Attachment,
        altyazi_sirasi: discord.app_commands.Range[int, 0, 20] = 0,
    ):
        if Path(video.filename).suffix.lower() not in VIDEO_SUFFIXES:
            await interaction.response.send_message("Desteklenmeyen video dosyası.", ephemeral=True)
            return
        if not 0 < video.size <= MAX_INPUT:
            await interaction.response.send_message("Video boş olamaz ve en fazla 100 MB olabilir.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            with tempfile.TemporaryDirectory(prefix="ubagen-subtitle-") as directory:
                source = Path(directory) / ("input" + Path(video.filename).suffix.lower())
                await video.save(source)
                probe = await asyncio.to_thread(
                    _run,
                    ["ffprobe", "-v", "error", "-select_streams", "s",
                     "-show_entries", "stream=index,codec_name:stream_tags=language",
                     "-of", "json", str(source)],
                    30,
                )
                if probe.returncode:
                    await interaction.followup.send("Video okunamadı.")
                    return
                streams = json.loads(probe.stdout).get("streams", [])
                if not streams:
                    await interaction.followup.send("Videoda gömülü altyazı bulunamadı.")
                    return
                if altyazi_sirasi >= len(streams):
                    listing = "\n".join(
                        f"{i}: {s.get('codec_name', 'bilinmiyor')} / {s.get('tags', {}).get('language', 'und')}"
                        for i, s in enumerate(streams[:20])
                    )
                    await interaction.followup.send(f"Geçersiz sıra. Altyazı akışları:\n{listing}")
                    return

                stream = streams[altyazi_sirasi]
                codec = str(stream.get("codec_name", "")).lower()
                if codec in {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}:
                    await interaction.followup.send(
                        "Bu altyazı görüntü tabanlı; metne dönüştürmek için OCR gerekir."
                    )
                    return

                suffix = ".ass" if codec in {"ass", "ssa"} else ".srt"
                output = Path(directory) / f"subtitle{suffix}"
                result = await asyncio.to_thread(
                    _run,
                    ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                     "-i", str(source), "-map", f"0:{stream['index']}", "-vn", "-an",
                     "-c:s", "ass" if suffix == ".ass" else "srt", str(output)],
                    120,
                )
                if result.returncode or not output.is_file() or output.stat().st_size == 0:
                    logger.warning("Subtitle extraction failed: %s", result.stderr[-1000:])
                    await interaction.followup.send("Altyazı çıkarılamadı; akış desteklenmiyor olabilir.")
                    return
                if output.stat().st_size > MAX_OUTPUT:
                    await interaction.followup.send("Çıktı 8 MB sınırını aşıyor.")
                    return

                language = stream.get("tags", {}).get("language", "und")
                filename = f"{Path(video.filename).stem[:80]}_{language}_{altyazi_sirasi}{suffix}"
                await interaction.followup.send(
                    f"Altyazı çıkarıldı (akış {altyazi_sirasi}, {codec or 'bilinmiyor'}).",
                    file=discord.File(str(output), filename=filename),
                )
        except subprocess.TimeoutExpired:
            await interaction.followup.send("İşlem zaman aşımına uğradı; daha küçük bir video dene.")
        except Exception:
            logger.exception("Error in /altyazi_ayikla")
            if not interaction.is_expired():
                await interaction.followup.send("Altyazı işlenirken hata oluştu; bot loglarını kontrol edin.")
