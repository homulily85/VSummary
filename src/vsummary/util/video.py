from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from vsummary.util.youtube import InvalidYouTubeInputError, to_video_url


class UnsupportedVideoSource(ValueError):
    """Raised when a video reference comes from an unsupported source."""


@dataclass(frozen=True, init=False)
class VideoRef:
    """A normalized video reference split into a source and its ID within that
    source (e.g. source="youtube", id="dQw4w9WgXcQ")."""

    source: str
    video_id: str

    def __init__(
        self,
        source: str,
        video_id: str | None = None,
        *,
        id: str | None = None,
    ):
        resolved_id = video_id or id
        if not resolved_id:
            raise ValueError("video_id is required")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "video_id", resolved_id)

    @property
    def id(self) -> str:
        return self.video_id


def parse_video_source(value: str) -> VideoRef:
    """Split a video URL (or bare ID) into a ``VideoRef``.

    Only YouTube is supported today, but this function is the single entry
    point for adding other services later (Vimeo, Twitch, etc.) — each new
    source just needs a branch that produces its own ``VideoRef``.
    """
    if not value.strip():
        raise UnsupportedVideoSource("Video reference cannot be empty.")

    parsed = urlparse(value.strip())
    hostname = (parsed.hostname or "").lower()
    twitch_segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        parsed.scheme in {"http", "https"}
        and hostname in {"twitch.tv", "www.twitch.tv"}
        and len(twitch_segments) == 2
        and twitch_segments[0] == "videos"
        and twitch_segments[1].isdigit()
    ):
        return VideoRef(source="twitch", video_id=twitch_segments[1])

    try:
        watch_url = to_video_url(value)
    except InvalidYouTubeInputError as exc:
        raise UnsupportedVideoSource(
            f"'{value}' is not a supported video source."
        ) from exc

    video_id = parse_qs(urlparse(watch_url).query)["v"][0]
    return VideoRef(source="youtube", video_id=video_id)


def build_video_url(ref: VideoRef) -> str:
    """Reconstruct the full URL for a ``VideoRef``.

    This is the counterpart to :func:`parse_video_source` and should grow a
    branch per supported source.
    """
    if ref.source == "youtube":
        return f"https://www.youtube.com/watch?v={ref.video_id}"
    if ref.source == "twitch":
        return f"https://www.twitch.tv/videos/{ref.video_id}"

    raise UnsupportedVideoSource(f"Unsupported video source: '{ref.source}'")
