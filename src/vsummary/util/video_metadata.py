"""Read display titles and channel names for supported sources."""

import asyncio
from dataclasses import dataclass

import yt_dlp

from vsummary.util.video import VideoRef, build_video_url


class VideoMetadataError(RuntimeError):
    """Signal missing or unusable display metadata from yt-dlp."""


@dataclass(frozen=True)
class VideoMetadata:
    """Carry display metadata for a recording.

    Attributes:
        title: Non-blank recording title.
        channel_name: Display name for the channel, uploader, or artist when available.
    """

    title: str
    channel_name: str | None


async def get_video_metadata(ref: VideoRef) -> VideoMetadata:
    """Look up a video's title and channel in a worker thread.

    Args:
        ref: Video reference whose URL can be opened by yt-dlp.

    Returns:
        Recording title and channel or uploader name.

    Raises:
        UnsupportedVideoSource: If the reference cannot be turned into a URL.
        VideoMetadataError: If extraction fails or either required display field is
            absent.
    """
    return await asyncio.to_thread(_extract_video_metadata, build_video_url(ref))


def _extract_video_metadata(url: str) -> VideoMetadata:
    """Read required display fields with yt-dlp without downloading media.

    Args:
        url: Recording URL to inspect.

    Returns:
        Metadata with a trimmed title and channel name, using the uploader as a
            fallback.

    Raises:
        VideoMetadataError: If extraction fails, the result is invalid, or the title and
            channel cannot both be obtained.
    """
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
    except Exception as exc:
        raise VideoMetadataError(f"Could not load video metadata: {exc}") from exc

    if not isinstance(info, dict):
        raise VideoMetadataError("Could not load video metadata: invalid response")
    title = _nonempty_text(info.get("title"))
    channel_name = _nonempty_text(info.get("channel")) or _nonempty_text(
        info.get("uploader")
    )
    if title is None or channel_name is None:
        raise VideoMetadataError(
            "Could not load video metadata: title or channel missing"
        )
    return VideoMetadata(title=title, channel_name=channel_name)


def _nonempty_text(value: object) -> str | None:
    """Normalize optional display text.

    Args:
        value: Raw metadata field.

    Returns:
        Stripped text if it contains a non-blank string, otherwise ``None``.
    """
    return value.strip() if isinstance(value, str) and value.strip() else None
