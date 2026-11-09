"""Read display titles and channel names for supported sources."""

import asyncio
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

import yt_dlp

from vsummary.util.video import VideoRef, build_video_url


class VideoMetadataError(RuntimeError):
    """Signal missing or unusable display metadata from yt-dlp."""


@dataclass(frozen=True)
class VideoMetadata:
    """Carry display and verification metadata for a recording.

    Attributes:
        title: Non-blank recording title.
        channel_name: Display name for the channel, uploader, or artist when available.
        channel_id: Validated YouTube channel ID, when available.
        channel_url: Channel profile URL reported by the extractor.
        description: Bounded copy of the video description.
        links: Deduplicated URLs found in the description.
        aired_at: UTC release time for a recording identified as a past livestream.
        published_on: Upload date reported by the extractor.
    """

    title: str
    channel_name: str | None
    channel_id: str | None = None
    channel_url: str | None = None
    description: str = ""
    links: tuple[str, ...] = ()
    aired_at: datetime | None = None
    published_on: date | None = None

    @property
    def content_date(self) -> date | None:
        """Return the livestream date, or the upload date if it is unknown."""
        return self.aired_at.date() if self.aired_at else self.published_on


async def get_video_metadata(ref: VideoRef) -> VideoMetadata:
    """Look up video metadata in a worker thread.

    Args:
        ref: Video reference whose URL can be opened by yt-dlp.

    Returns:
        Recording title and channel or uploader name, plus optional verification metadata.

    Raises:
        UnsupportedVideoSource: If the reference cannot be turned into a URL.
        VideoMetadataError: If extraction fails or either required display field is
            absent.
    """
    return await asyncio.to_thread(_extract_video_metadata, build_video_url(ref))


def _extract_video_metadata(url: str) -> VideoMetadata:
    """Read display and verification metadata with yt-dlp without downloading media.

    Args:
        url: Recording URL to inspect.

    Returns:
        Metadata with a trimmed title and channel name, using the uploader as a
            fallback. Optional description, profile, and date fields are included
            when available.

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
    description = (_nonempty_text(info.get("description")) or "")[:20000]
    channel_id = _nonempty_text(info.get("channel_id"))
    if not channel_id or not re.fullmatch(r"UC[A-Za-z0-9_-]{22}", channel_id):
        channel_id = None
    published = _nonempty_text(info.get("upload_date"))
    try:
        published_on = (
            datetime.strptime(published, "%Y%m%d").date() if published else None
        )
    except ValueError:
        published_on = None
    aired_at = None
    stamp = info.get("release_timestamp")
    if info.get("was_live") and type(stamp) in {float, int}:
        try:
            aired_at = datetime.fromtimestamp(stamp, tz=UTC)
        except (OverflowError, OSError, ValueError):
            pass
    return VideoMetadata(
        title=title,
        channel_name=channel_name,
        channel_id=channel_id,
        channel_url=_nonempty_text(info.get("channel_url")),
        description=description,
        links=tuple(dict.fromkeys(re.findall(r'https?://[^\s<>"\x27]+', description))),
        aired_at=aired_at,
        published_on=published_on,
    )


def _nonempty_text(value: object) -> str | None:
    """Normalize optional display text.

    Args:
        value: Raw metadata field.

    Returns:
        Stripped text if it contains a non-blank string, otherwise ``None``.
    """
    return value.strip() if isinstance(value, str) and value.strip() else None
