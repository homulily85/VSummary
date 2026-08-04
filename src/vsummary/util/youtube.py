import re
from urllib.parse import parse_qs, urlparse


class InvalidYouTubeInputError(ValueError):
    """Raised when the user provides something that is neither a valid YouTube
    video link nor a valid YouTube video ID."""


_VIDEO_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{11}$")


def to_video_url(value: str) -> str:
    """Normalize a YouTube video link or bare video ID into a full watch URL.

    Supported inputs:
      - A bare video ID (e.g. ``dQw4w9WgXcQ``)
      - A standard watch URL (``https://youtube.com/watch?v=VIDEO_ID`` or
        ``https://www.youtube.com/watch?v=VIDEO_ID``, matching the ``v`` query
        parameter regardless of query ordering)
      - ``youtu.be``, ``shorts``, ``embed``, ``live``, and ``<something>.youtube.com``
        URLs that encode the video ID in their path
    """
    stripped = value.strip()
    if not stripped:
        raise InvalidYouTubeInputError("Input cannot be empty.")

    if not re.match(r"^https?://", stripped, re.IGNORECASE):
        return _build_watch_url(_require_video_id(stripped))

    return _build_watch_url(_extract_video_id_from_url(stripped))


def _build_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _require_video_id(video_id: str) -> str:
    if not _VIDEO_ID_PATTERN.fullmatch(video_id):
        raise InvalidYouTubeInputError(
            f"'{video_id}' is not a valid YouTube video ID or link."
        )
    return video_id


def _extract_video_id_from_url(url: str) -> str:
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
