"""Download public Twitch VOD audio for NotebookLM uploads."""

import asyncio
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import yt_dlp

from vsummary.util.video import VideoRef, build_video_url

MAX_AUDIO_BYTES = 190 * 1024 * 1024


class TwitchAudioError(RuntimeError):
    """A Twitch VOD could not be made into an uploadable audio source."""


class TwitchAudioUnavailableError(TwitchAudioError):
    """A VOD is private, restricted, too long, or otherwise unsupported."""


def _download(url: str, directory: str, max_duration_seconds: int) -> Path:
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
            duration = info.get("duration")
            if (
                not isinstance(duration, (int, float))
                or duration > max_duration_seconds
            ):
                raise TwitchAudioUnavailableError(
                    f"Twitch VODs must be no longer than {max_duration_seconds // 3600} hours"
                )
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
    return paths[0]


@asynccontextmanager
async def download_twitch_audio(
    ref: VideoRef, max_duration_seconds: int
) -> AsyncIterator[Path]:
    """Download a public Twitch VOD to a temporary M4A audio file."""
    if ref.source != "twitch":
        raise ValueError("Twitch audio can only be downloaded for Twitch references")
    with tempfile.TemporaryDirectory(prefix="vsummary-twitch-") as directory:
        path = await asyncio.to_thread(
            _download, build_video_url(ref), directory, max_duration_seconds
        )
        yield path
