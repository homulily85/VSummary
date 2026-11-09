"""Validation, metadata lookup, and M4A extraction for generic public media."""

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

from vsummary.settings import RuntimeConfig, runtime_config
from vsummary.util.video import VideoRef, build_video_url, normalize_public_url
from vsummary.util.video_metadata import VideoMetadata


class GenericMediaError(RuntimeError):
    """Signal a host lookup, extraction, or preparation failure."""


class GenericMediaUnavailableError(GenericMediaError):
    """Signal media that cannot produce a supported, uploadable recording."""


def _options(
    directory: str | None = None, *, config: RuntimeConfig | None = None
) -> dict:
    """Build the yt-dlp options for inspection or audio conversion.

    Args:
        directory: Destination directory for M4A output, or ``None`` to omit conversion
            options.
        config: Operational settings for format selection and bitrate. Defaults to
            runtime settings.

    Returns:
        yt-dlp option mapping.
    """
    config = runtime_config(config)
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": config.audio_download_format,
    }
    if directory is not None:
        options["outtmpl"] = str(Path(directory) / "audio.%(ext)s")
        options["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": str(config.audio_bitrate_kbps),
            }
        ]
    return options


def _ensure_public_host(url: str) -> None:
    """Resolve the media hostname and require public addresses.

    Args:
        url: Candidate public HTTP(S) URL.

    Raises:
        UnsupportedVideoSource: If the URL fails local validation.
        GenericMediaError: If DNS resolution fails.
        GenericMediaUnavailableError: If no addresses resolve or any address is
            non-public.
    """
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
    """Validate that extracted metadata describes one completed recording.

    Args:
        info: Raw metadata returned by yt-dlp.

    Returns:
        Original metadata dictionary after validation.

    Raises:
        GenericMediaUnavailableError: If metadata lacks an ID, represents multiple
            items, or indicates an upcoming, live, or unfinished item.
    """
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
    """Check access to one public recording and extract its display metadata.

    Args:
        url: Public media URL to inspect without downloading audio.

    Returns:
        Trimmed title with an optional channel name, or ``None`` if the recording has no
            usable title.

    Raises:
        UnsupportedVideoSource: If the URL fails local validation.
        GenericMediaUnavailableError: If the host is non-public or the recording is
            inaccessible or unsupported.
        GenericMediaError: If DNS resolution or another inspection operation fails.
    """
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
    """Inspect a media reference in a worker thread.

    Args:
        ref: Reference that can be converted to a public media URL.

    Returns:
        Available display metadata, or ``None`` if the recording has no usable title.

    Raises:
        UnsupportedVideoSource: If the reference or its URL is unsupported.
        GenericMediaUnavailableError: If the recording cannot be accessed or used.
        GenericMediaError: If host resolution or inspection fails.
    """
    return await asyncio.to_thread(inspect_media, build_video_url(ref))


def _download(url: str, directory: str, *, config: RuntimeConfig | None = None) -> Path:
    """Download a single finished item and transcode its audio to M4A.

    Args:
        url: Public media URL.
        directory: Directory for the audio file.
        config: Download format, bitrate, and size limit. Defaults to runtime settings.

    Returns:
        Path to the single M4A file in the output directory.

    Raises:
        UnsupportedVideoSource: If the URL fails local validation.
        GenericMediaUnavailableError: If the host or recording is unsupported, FFmpeg is
            missing, or the size limit is exceeded.
        GenericMediaError: If host resolution, download, or conversion fails, or if
            output is missing or ambiguous.
        OSError: If the output cannot be inspected on disk.
    """
    config = runtime_config(config)
    _ensure_public_host(url)
    if shutil.which("ffmpeg") is None:
        raise GenericMediaUnavailableError("ffmpeg is required to process media audio.")
    try:
        with yt_dlp.YoutubeDL(_options(directory, config=config)) as downloader:
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
    if paths[0].stat().st_size > config.max_audio_mib * 1024 * 1024:
        raise GenericMediaUnavailableError(
            "Media audio exceeds NotebookLM's upload limit."
        )
    return paths[0]


@asynccontextmanager
async def download_generic_audio(
    ref: VideoRef, *, config: RuntimeConfig | None = None
) -> AsyncIterator[Path]:
    """Prepare generic media or YouTube fallback audio in a temporary directory.

    Args:
        ref: Generic or YouTube reference to download.
        config: Audio settings. Defaults to runtime settings.

    Yields:
        Path to the M4A file, available until the context exits.

    Raises:
        ValueError: If the reference is for a different source.
        UnsupportedVideoSource: If the reference lacks a supported URL.
        GenericMediaUnavailableError: If the media cannot be used, FFmpeg is missing, or
            the audio exceeds the size limit.
        GenericMediaError: If resolution or audio preparation fails.
        OSError: If a temporary-file operation fails.
    """
    if ref.source not in {"yt_dlp", "youtube"}:
        raise ValueError("Generic audio requires a yt-dlp or YouTube reference.")
    with tempfile.TemporaryDirectory(prefix="vsummary-media-") as directory:
        options = {"config": config} if config is not None else {}
        path = await asyncio.to_thread(
            _download, build_video_url(ref), directory, **options
        )
        yield path
