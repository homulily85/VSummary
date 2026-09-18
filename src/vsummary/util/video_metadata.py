"""Metadata lookup for supported video sources."""

import asyncio
from dataclasses import dataclass

import yt_dlp

from vsummary.util.video import VideoRef, build_video_url


class VideoMetadataError(RuntimeError):
    """Video title or channel metadata could not be loaded."""


@dataclass(frozen=True)
class VideoMetadata:
    title: str
    channel_name: str


async def get_video_metadata(ref: VideoRef) -> VideoMetadata:
    """Load a supported video's title and channel name without downloading it."""
    return await asyncio.to_thread(_extract_video_metadata, build_video_url(ref))


def _extract_video_metadata(url: str) -> VideoMetadata:
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
    return value.strip() if isinstance(value, str) and value.strip() else None
