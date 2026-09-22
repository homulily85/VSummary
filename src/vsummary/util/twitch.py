"""Download public Twitch VOD audio for NotebookLM uploads."""

import asyncio
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import yt_dlp

from vsummary.util.video import VideoRef, build_video_url

MAX_AUDIO_BYTES = 190 * 1024 * 1024
logger = logging.getLogger(__name__)


class TwitchAudioError(RuntimeError):
    """A Twitch VOD could not be made into an uploadable audio source."""


class TwitchAudioUnavailableError(TwitchAudioError):
    """A VOD is private, restricted, or otherwise unsupported."""


def _download(url: str, directory: str) -> Path:
    """Download, transcode, size-check, and return one temporary Twitch M4A file."""
    if shutil.which("ffmpeg") is None:
        raise TwitchAudioUnavailableError("ffmpeg is required to process Twitch audio")
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(Path(directory) / "%(id)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "64",
            }
        ],
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise TwitchAudioUnavailableError(
                    "Twitch VOD is unavailable or cannot be accessed"
                )
            vod_id = info.get("id")
            if not isinstance(vod_id, str) or not vod_id:
                vod_id = url.rstrip("/").rsplit("/", maxsplit=1)[-1]
            logger.info("Downloading Twitch VOD audio for %s.", vod_id)
            downloader.download([url])
    except TwitchAudioUnavailableError:
        raise
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.PostProcessingError) as exc:
        raise TwitchAudioUnavailableError(
            f"Twitch audio download or conversion failed: {exc}"
        ) from exc
    paths = list(Path(directory).glob("*.m4a"))
    if len(paths) != 1:
        raise TwitchAudioError("Twitch audio download did not produce an M4A file")
    if paths[0].stat().st_size > MAX_AUDIO_BYTES:
        raise TwitchAudioUnavailableError(
            "Twitch VOD audio exceeds NotebookLM's upload limit"
        )
    logger.info("Prepared Twitch VOD audio for %s.", paths[0].stem)
    return paths[0]


@asynccontextmanager
async def download_twitch_audio(ref: VideoRef) -> AsyncIterator[Path]:
    """Download a public Twitch VOD to a temporary M4A audio file."""
    if ref.source != "twitch":
        raise ValueError("Twitch audio can only be downloaded for Twitch references")
    with tempfile.TemporaryDirectory(prefix="vsummary-twitch-") as directory:
        path = await asyncio.to_thread(_download, build_video_url(ref), directory)
        yield path
