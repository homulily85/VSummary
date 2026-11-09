"""Validation and canonical URL construction for YouTube videos."""

import re
from urllib.parse import parse_qs, urlparse


class InvalidYouTubeInputError(ValueError):
    """Signal input without a valid YouTube video ID or recognized video URL."""


_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


def to_video_url(value: str) -> str:
    """Convert a YouTube video link or bare ID to a canonical watch URL.

    Accept watch URLs with a ``v`` query parameter, ``youtu.be`` links, and recognized
    YouTube path forms such as ``shorts``, ``embed``, ``live``, and ``v``.

    Args:
        value: Candidate URL or 11-character video ID, optionally surrounded by
            whitespace.

    Returns:
        HTTPS watch URL on ``www.youtube.com`` with the validated ID.

    Raises:
        InvalidYouTubeInputError: If the input is blank, has an unrecognized URL shape,
            or contains an invalid ID.
    """
    stripped = value.strip()
    if not stripped:
        raise InvalidYouTubeInputError("Input cannot be empty.")

    if not re.match(r"^https?://", stripped, re.IGNORECASE):
        return _build_watch_url(_require_video_id(stripped))

    return _build_watch_url(_extract_video_id_from_url(stripped))


def _build_watch_url(video_id: str) -> str:
    """Format the canonical watch URL.

    Args:
        video_id: Previously validated YouTube video ID.

    Returns:
        HTTPS watch URL for that ID.
    """
    return f"https://www.youtube.com/watch?v={video_id}"


def _require_video_id(video_id: str) -> str:
    """Validate the length and character set of a YouTube video ID.

    Args:
        video_id: Candidate ID.

    Returns:
        The unchanged ID when valid.

    Raises:
        InvalidYouTubeInputError: If the ID fails the 11-character pattern.
    """
    if not _VIDEO_ID_PATTERN.fullmatch(video_id):
        raise InvalidYouTubeInputError(
            f"'{video_id}' is not a valid YouTube video ID or link."
        )
    return video_id


def _extract_video_id_from_url(url: str) -> str:
    """Extract the ID from a recognized YouTube URL.

    Args:
        url: Candidate YouTube URL.

    Returns:
        Validated video ID from the path or watch query.

    Raises:
        InvalidYouTubeInputError: If the URL, host, path, or video ID is invalid.
    """
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        raise InvalidYouTubeInputError(f"'{url}' is not a valid URL.") from exc

    hostname = (parsed.hostname or "").lower()
    is_youtube = (
        hostname == "youtube.com"
        or hostname.endswith(".youtube.com")
        or hostname == "youtu.be"
    )
    if not is_youtube:
        raise InvalidYouTubeInputError(f"'{url}' is not a YouTube link.")

    if hostname == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/", 1)[0]
        return _require_video_id(video_id)

    if parsed.path.startswith("/watch"):
        video_id = parse_qs(parsed.query).get("v", [None])[0]
        if video_id is None:
            raise InvalidYouTubeInputError(f"'{url}' does not contain a video ID.")
        return _require_video_id(video_id)

    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) >= 2 and segments[0] in {"shorts", "embed", "live", "v"}:
        return _require_video_id(segments[1])

    raise InvalidYouTubeInputError(f"'{url}' is not a recognized YouTube link.")
