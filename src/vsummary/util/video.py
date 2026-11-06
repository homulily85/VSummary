"""Source-agnostic parsing and URL construction for supported video inputs."""

from dataclasses import dataclass, field
from hashlib import sha256
from ipaddress import ip_address
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

from vsummary.util.youtube import InvalidYouTubeInputError, to_video_url


class UnsupportedVideoSource(ValueError):
    """Raised when a video reference comes from an unsupported source."""


@dataclass(frozen=True)
class VideoRef:
    """A normalized video reference identified by source and video_id."""

    source: str
    video_id: str
    url: str | None = field(default=None, kw_only=True)

    def __post_init__(self):
        """Reject references without a source-local video ID."""
        if not self.video_id:
            raise ValueError("video_id is required")


def parse_non_space_video_source(value: str) -> VideoRef:
    """Parse a Twitch, YouTube, or generic public media reference."""
    if not value.strip():
        raise UnsupportedVideoSource("Video reference cannot be empty.")

    parsed = urlparse(value.strip())
    hostname = (parsed.hostname or "").lower()
    twitch_segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        parsed.scheme in {"http", "https"}
        and hostname in {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
        and len(twitch_segments) == 2
        and twitch_segments[0] == "videos"
        and twitch_segments[1].isdigit()
    ):
        return VideoRef(source="twitch", video_id=twitch_segments[1])

    try:
        watch_url = to_video_url(value)
    except InvalidYouTubeInputError as exc:
        try:
            url = normalize_public_url(value)
        except UnsupportedVideoSource:
            raise UnsupportedVideoSource(
                f"'{value}' is not a supported video source."
            ) from exc
        return VideoRef(
            source="yt_dlp",
            video_id=sha256(url.encode("utf-8")).hexdigest(),
            url=url,
        )

    video_id = parse_qs(urlparse(watch_url).query)["v"][0]
    return VideoRef(source="youtube", video_id=video_id)


def build_video_url(ref: VideoRef) -> str:
    """Build the public URL for a normalized video reference."""
    if ref.source == "youtube":
        return f"https://www.youtube.com/watch?v={ref.video_id}"
    if ref.source == "twitch":
        return f"https://www.twitch.tv/videos/{ref.video_id}"
    if ref.source == "x_space":
        return f"https://x.com/i/spaces/{ref.video_id}"
    if ref.source == "yt_dlp" and ref.url:
        return ref.url

    raise UnsupportedVideoSource(f"Unsupported video source: '{ref.source}'")


def normalize_public_url(value: str) -> str:
    """Normalize an HTTP media URL and reject local or credentialed addresses."""
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise UnsupportedVideoSource("Invalid media URL.") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise UnsupportedVideoSource("A public HTTP(S) media URL is required.")
    host = host.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local")):
        raise UnsupportedVideoSource("Private network media URLs are not supported.")
    try:
        address = ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise UnsupportedVideoSource("Private network media URLs are not supported.")
    host_text = f"[{host}]" if ":" in host else host
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    netloc = (
        host_text if port is None or port == default_port else f"{host_text}:{port}"
    )
    return urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, "")
    )
