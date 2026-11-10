"""Prepare public Twitch VOD audio as M4A for NotebookLM."""

import asyncio
import logging
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import yt_dlp

from vsummary.util.audio import MAX_AUDIO_BYTES, audio_options
from vsummary.util.video import VideoRef, build_video_url

logger = logging.getLogger(__name__)


class TwitchAudioError(RuntimeError):
    """Signal a failure while preparing an uploadable Twitch audio file."""


class TwitchAudioUnavailableError(TwitchAudioError):
    """Signal inaccessible Twitch audio or an unsupported conversion or size."""


def _download(url: str, directory: str) -> Path:
    """Download a Twitch VOD and validate its converted M4A file.

    Args:
        url: Public Twitch VOD URL.
        directory: Directory for the audio file.

    Returns:
        Path to the single M4A file in the output directory.

    Raises:
        TwitchAudioUnavailableError: If FFmpeg is missing, yt-dlp cannot access or
            convert the recording, or the audio exceeds the size limit.
        TwitchAudioError: If the expected single M4A output is missing or ambiguous.
        OSError: If the file cannot be inspected on disk.
    """
    if shutil.which("ffmpeg") is None:
        raise TwitchAudioUnavailableError("ffmpeg is required to process Twitch audio")
    options = audio_options(Path(directory) / "%(id)s.%(ext)s")
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
    """Prepare a public Twitch VOD in a temporary directory.

    Args:
        ref: Normalized Twitch VOD reference.

    Yields:
        Path to the converted M4A file, available until the context exits.

    Raises:
        ValueError: If the reference is for another source.
        TwitchAudioUnavailableError: If the audio cannot be accessed or converted, or
            exceeds the size limit.
        TwitchAudioError: If the downloader does not produce one M4A file.
        OSError: If a temporary-file operation fails.
    """
    if ref.source != "twitch":
        raise ValueError("Twitch audio can only be downloaded for Twitch references")
    with tempfile.TemporaryDirectory(prefix="vsummary-twitch-") as directory:
        path = await asyncio.to_thread(_download, build_video_url(ref), directory)
        yield path
