"""Prepare one public yt-dlp media item for NotebookLM."""

import asyncio
import shutil
import socket
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import yt_dlp

from vsummary.util.video import VideoRef, build_video_url, normalize_public_url
from vsummary.util.video_metadata import VideoMetadata

MAX_AUDIO_BYTES = 190 * 1024 * 1024


class GenericMediaError(RuntimeError):
    """A temporary failure while preparing media."""


class GenericMediaUnavailableError(GenericMediaError):
    """The requested URL cannot produce one uploadable recording."""


def _options(directory: str | None = None) -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
    }
    if directory is not None:
        options["outtmpl"] = str(Path(directory) / "audio.%(ext)s")
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "64",
            }
        ]
    return options


def _ensure_public_host(url: str) -> None:
    """Reject hostnames that resolve to non-public network addresses."""
    normalized = normalize_public_url(url)
    parsed = urlsplit(normalized)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = socket.getaddrinfo(parsed.hostname, port)
    except OSError as exc:
        raise GenericMediaError("Could not resolve the media host.") from exc
    if not addresses or any(
        not ip_address(entry[4][0]).is_global for entry in addresses
    ):
        raise GenericMediaUnavailableError(
            "Private network media URLs are not supported."
        )


def _require_one_finished_media(info: object) -> dict:
    if not isinstance(info, dict):
        raise GenericMediaUnavailableError("The URL does not contain accessible media.")
    if info.get("_type") in {"playlist", "multi_video"} or "entries" in info:
        raise GenericMediaUnavailableError(
            "Provide a URL for one media item, not a playlist."
        )
    if (
        info.get("live_status") in {"is_live", "is_upcoming", "post_live"}
        or info.get("is_live") is True
    ):
        raise GenericMediaUnavailableError("Only completed recordings are supported.")
    if not info.get("id"):
        raise GenericMediaUnavailableError("The URL does not contain accessible media.")
    return info


def inspect_media(url: str) -> VideoMetadata | None:
    """Validate one accessible recording and return available display metadata."""
    _ensure_public_host(url)
    try:
        with yt_dlp.YoutubeDL(_options()) as downloader:
            info = _require_one_finished_media(
                downloader.extract_info(url, download=False)
            )
    except GenericMediaUnavailableError:
        raise
    except yt_dlp.utils.DownloadError as exc:
        raise GenericMediaUnavailableError(f"Could not access media: {exc}") from exc
    except Exception as exc:
        raise GenericMediaError("Could not inspect this media URL.") from exc
    title = info.get("title")
    channel = info.get("channel") or info.get("uploader") or info.get("artist")
    if isinstance(title, str) and title.strip():
        return VideoMetadata(
            title=title.strip(),
            channel_name=channel.strip()
            if isinstance(channel, str) and channel.strip()
            else None,
        )
    return None


async def inspect_generic_media(ref: VideoRef) -> VideoMetadata | None:
    """Check media without blocking the Discord event loop."""
    return await asyncio.to_thread(inspect_media, build_video_url(ref))


def _download(url: str, directory: str) -> Path:
    _ensure_public_host(url)
    if shutil.which("ffmpeg") is None:
        raise GenericMediaUnavailableError("ffmpeg is required to process media audio.")
    try:
        with yt_dlp.YoutubeDL(_options(directory)) as downloader:
            info = _require_one_finished_media(
                downloader.extract_info(url, download=False)
            )
            downloader.process_info(info)
    except GenericMediaUnavailableError:
        raise
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.PostProcessingError) as exc:
        raise GenericMediaError(
            f"Media audio download or conversion failed: {exc}"
        ) from exc
    paths = list(Path(directory).glob("*.m4a"))
    if len(paths) != 1:
        raise GenericMediaError("Media download did not produce one M4A file.")
    if paths[0].stat().st_size > MAX_AUDIO_BYTES:
        raise GenericMediaUnavailableError(
            "Media audio exceeds NotebookLM's upload limit."
        )
    return paths[0]


@asynccontextmanager
async def download_generic_audio(ref: VideoRef) -> AsyncIterator[Path]:
    """Download one public media item into a temporary M4A file."""
    if ref.source != "yt_dlp":
        raise ValueError("Generic audio requires a yt-dlp reference.")
    with tempfile.TemporaryDirectory(prefix="vsummary-media-") as directory:
        path = await asyncio.to_thread(_download, build_video_url(ref), directory)
        yield path
