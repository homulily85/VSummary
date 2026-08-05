from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from vsummary.util.youtube import InvalidYouTubeInputError, to_video_url


class UnsupportedVideoSource(ValueError):
    """Raised when a video reference comes from an unsupported source."""


@dataclass(frozen=True)
class VideoRef:
    """A normalized video reference split into a source and its ID within that
    source (e.g. source="youtube", id="dQw4w9WgXcQ")."""

    source: str
    id: str


def parse_video_source(value: str) -> VideoRef:
    """Split a video URL (or bare ID) into a ``VideoRef``.

    Only YouTube is supported today, but this function is the single entry
    point for adding other services later (Vimeo, Twitch, etc.) — each new
    source just needs a branch that produces its own ``VideoRef``.
    """
    if not value.strip():
        raise UnsupportedVideoSource("Video reference cannot be empty.")

    try:
        watch_url = to_video_url(value)
    except InvalidYouTubeInputError as exc:
        raise UnsupportedVideoSource(
            f"'{value}' is not a supported video source."
        ) from exc

    video_id = parse_qs(urlparse(watch_url).query)["v"][0]
    return VideoRef(source="youtube", id=video_id)


def build_video_url(ref: VideoRef) -> str:
    """Reconstruct the full URL for a ``VideoRef``.

    This is the counterpart to :func:`parse_video_ref` and should grow a
    branch per supported source.
    """
    if ref.source == "youtube":
        return f"https://www.youtube.com/watch?v={ref.id}"

    raise UnsupportedVideoSource(f"Unsupported video source: '{ref.source}'")
