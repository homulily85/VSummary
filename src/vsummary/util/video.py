"""Normalize media identifiers and resolve them back to public URLs."""

from dataclasses import dataclass, field
from hashlib import sha256
from ipaddress import ip_address
from urllib.parse import parse_qs, urlparse, urlsplit, urlunsplit

from vsummary.util.youtube import InvalidYouTubeInputError, to_video_url


class UnsupportedVideoSource(ValueError):
    """Signal media input that cannot be represented by a supported source."""


@dataclass(frozen=True)
class VideoRef:
    """Identify media by source and a source-specific key.

    Attributes:
        source: Source key such as ``youtube``, ``twitch``, ``x_space``, or ``yt_dlp``.
        video_id: Non-empty source-local ID, or the normalized URL hash for generic
            media.
        url: Stored URL when required to reopen the media, as with generic sources.
    """

    source: str
    video_id: str
    url: str | None = field(default=None, kw_only=True)

    def __post_init__(self):
        """Require the source-local identifier when constructing a reference.

        Raises:
            ValueError: If the video ID is empty.
        """
        if not self.video_id:
            raise ValueError("video_id is required")


def parse_non_space_video_source(value: str) -> VideoRef:
    """Classify a Twitch VOD, YouTube video, or generic HTTP(S) URL locally.

    Args:
        value: User input containing a supported URL or a bare YouTube video ID.

    Returns:
        Source reference. Generic media uses the SHA-256 hash of its normalized URL as
            the ID and retains that URL.

    Raises:
        UnsupportedVideoSource: If the input is empty or cannot be normalized as a
            supported reference.
        ValueError: If initial URL parsing rejects a malformed host.
    """
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
    """Construct a public URL from a normalized media reference.

    Args:
        ref: Reference with a supported source and its source-local ID or stored URL.

    Returns:
        Canonical platform URL, or the stored URL for generic media.

    Raises:
        UnsupportedVideoSource: If the source is unknown or a generic reference lacks
            its URL.
    """
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
    """Normalize a generic HTTP(S) media URL without performing DNS resolution.

    Args:
        value: Candidate URL, optionally surrounded by whitespace.

    Returns:
        URL with a lowercase scheme and host, default ports and fragment removed, and
            the query preserved.

    Raises:
        UnsupportedVideoSource: If the URL is malformed, lacks a supported scheme or
            host, contains credentials, or names a local host or non-public IP address.
    """
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
