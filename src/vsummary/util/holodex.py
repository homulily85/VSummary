"""Typed Holodex API access with bounded HTTP retries and response validation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

import httpx

from vsummary.logging import log_event

logger = logging.getLogger(__name__)

HOLODEX_BASE_URL = "https://holodex.net"
HOLODEX_API_URL = f"{HOLODEX_BASE_URL}/api/v2"
REFERER = f"{HOLODEX_BASE_URL}/channel/UCQ0UDLQCjY0rmuxCDE38FGg"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
IGNORED_TOPICS = {"Original_Song", "membersonly", "shorts", "Music_Cover"}
TRANSCRIPT_READY_DELAY = timedelta(hours=2)
MAX_RETRIES = 5


def exponential_backoff_hours(retry_count: int) -> int:
    """Return the one-based exponential source-job retry delay in hours."""
    return 2 ** (retry_count - 1)


def is_ignored(topic_id: str | None) -> bool:
    """Return whether Holodex classifies a video as intentionally unsupported."""
    return topic_id in IGNORED_TOPICS


def is_transcript_ready(available_at: datetime) -> bool:
    """Return whether a stream has passed the transcript-availability delay."""
    if available_at.tzinfo is None:
        raise ValueError("available_at must be timezone-aware")
    return datetime.now(UTC) - available_at > TRANSCRIPT_READY_DELAY


class HolodexError(Exception):
    """Base error for Holodex API failures."""


class ChannelNotFoundError(HolodexError):
    """Raised when the Holodex channel endpoint returns 404."""


class TransientHolodexError(HolodexError):
    """Raised for timeouts, connection failures, and server errors."""


class PermanentHolodexError(HolodexError):
    """Raised for non-retryable API responses."""


class MalformedHolodexResponse(PermanentHolodexError):
    """Raised when Holodex returns a response with an invalid shape."""


@dataclass(frozen=True)
class HolodexChannel:
    """Minimal channel data needed to create an automatic subscription."""

    id: str
    name: str


@dataclass(frozen=True)
class HolodexVideo:
    """Normalized past-stream data returned by Holodex channel endpoints."""

    id: str
    title: str
    topic_id: str | None
    available_at: datetime
    channel_name: str
    duration: int = 0

    @property
    def video_id(self) -> str:
        """Expose ``id`` under the naming used by summary models and workflows."""
        return self.id


class HolodexClient:
    """Async Holodex client with a single timeout and retry policy."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        timeout: float = 15.0,
        retries: int = 2,
    ):
        """Create a client with a single timeout and retry policy for every request."""
        self._retries = max(0, retries)
        self._client = httpx.AsyncClient(
            base_url=HOLODEX_API_URL,
            headers={"referer": REFERER, "user-agent": USER_AGENT},
            transport=transport,
            timeout=httpx.Timeout(timeout),
        )

    async def __aenter__(self):
        """Return this client for use as an asynchronous context manager."""
        return self

    async def __aexit__(self, exc_type, exc, tb):
        """Close the underlying HTTP client when its context exits."""
        await self.aclose()

    async def aclose(self):
        """Close the underlying reusable HTTP connection pool."""
        await self._client.aclose()

    async def close(self):
        """Provide a compatibility alias for :meth:`aclose`."""
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Request Holodex, retrying transient transport and server failures."""
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(method, path, **kwargs)
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.ConnectError,
            ) as exc:
                if attempt >= self._retries:
                    raise TransientHolodexError(str(exc)) from exc
                delay = 2**attempt
                log_event(
                    logger,
                    logging.WARNING,
                    "Holodex request failed; retrying %s %s.",
                    method,
                    path,
                    attempt=attempt + 1,
                    retry_limit=self._retries + 1,
                    retry_delay_seconds=delay,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code in {408, 429} or response.status_code >= 500:
                if attempt >= self._retries:
                    raise TransientHolodexError(
                        f"Holodex returned HTTP {response.status_code}"
                    )
                delay = _retry_delay(response, attempt)
                log_event(
                    logger,
                    logging.WARNING,
                    "Holodex returned HTTP %s; retrying %s %s.",
                    response.status_code,
                    method,
                    path,
                    attempt=attempt + 1,
                    retry_limit=self._retries + 1,
                    retry_delay_seconds=delay,
                    http_status=response.status_code,
                )
                await asyncio.sleep(delay)
                continue
            return response
        raise AssertionError("unreachable")

    async def fetch_channel(self, channel_id: str) -> HolodexChannel:
        """Fetch one channel or raise a typed error for its response status."""
        response = await self._request("GET", f"/channels/{channel_id}")
        if response.status_code == 404:
            raise ChannelNotFoundError(f"Channel '{channel_id}' not found on Holodex.")
        if response.is_error:
            raise PermanentHolodexError(
                f"Holodex returned HTTP {response.status_code} for channel '{channel_id}'"
            )
        try:
            data = response.json()
            return HolodexChannel(id=str(data["id"]), name=str(data["name"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedHolodexResponse(
                f"Invalid channel response for '{channel_id}'"
            ) from exc

    async def fetch_channel_videos(
        self, channel_id: str, limit: int = 1, offset: int = 0
    ) -> list[HolodexVideo]:
        """Fetch and validate one newest-first page of past channel streams."""
        response = await self._request(
            "GET",
            f"/channels/{channel_id}/videos",
            params={
                "type": "stream",
                "limit": limit,
                "offset": offset,
                "status": "past",
            },
        )
        if response.is_error:
            raise PermanentHolodexError(
                f"Holodex returned HTTP {response.status_code} for videos of "
                f"channel '{channel_id}'"
            )
        try:
            payload = response.json()
            if not isinstance(payload, list):
                raise TypeError("expected a list")
            return [_parse_video(video) for video in payload]
        except (KeyError, TypeError, ValueError) as exc:
            raise MalformedHolodexResponse(
                f"Invalid videos response for '{channel_id}'"
            ) from exc

    async def fetch_channel_videos_since(
        self,
        channel_id: str,
        since: datetime,
        *,
        page_size: int = 50,
    ) -> list[HolodexVideo]:
        """Return every past stream published after ``since``.

        Holodex returns newest videos first. Pagination stops as soon as the
        first older item is encountered, avoiding a full channel history scan.
        """
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        if page_size <= 0:
            raise ValueError("page_size must be positive")

        videos: list[HolodexVideo] = []
        offset = 0
        while True:
            page = await self.fetch_channel_videos(
                channel_id, limit=page_size, offset=offset
            )
            newer = [video for video in page if video.available_at > since]
            videos.extend(newer)
            if len(page) < page_size or len(newer) != len(page):
                return videos
            offset += len(page)

    async def get_channel(self, channel_id: str) -> HolodexChannel | None:
        """Legacy helper returning ``None`` for ordinary HTTP failures."""
        try:
            return await self.fetch_channel(channel_id)
        except ChannelNotFoundError:
            raise
        except (PermanentHolodexError, TransientHolodexError) as exc:
            log_event(
                logger,
                logging.ERROR,
                "Error fetching channel %s: %s",
                channel_id,
                exc,
                channel_id=channel_id,
                error_type=type(exc).__name__,
            )
            return None

    async def get_channel_videos(
        self, channel_id: str, limit: int = 1, offset: int = 0
    ) -> list[HolodexVideo]:
        """Legacy helper returning an empty list for ordinary HTTP failures."""
        try:
            return await self.fetch_channel_videos(channel_id, limit, offset)
        except (PermanentHolodexError, TransientHolodexError) as exc:
            log_event(
                logger,
                logging.ERROR,
                "Error fetching videos for channel %s: %s",
                channel_id,
                exc,
                channel_id=channel_id,
                error_type=type(exc).__name__,
            )
            return []


class HolodexGateway:
    """Strict typed gateway used by workflows."""

    def __init__(self, client: HolodexClient | None = None, **client_kwargs):
        """Wrap a client, creating one when the caller does not provide it."""
        self.client = client or HolodexClient(**client_kwargs)

    async def close(self) -> None:
        """Close the gateway's underlying HTTP client."""
        await self.client.aclose()

    async def get_channel(self, channel_id: str) -> HolodexChannel:
        """Return a channel while preserving strict typed client failures."""
        return await self.client.fetch_channel(channel_id)

    async def get_channel_videos(
        self, channel_id: str, limit: int = 1, offset: int = 0
    ) -> list[HolodexVideo]:
        """Return one page of validated past streams for a channel."""
        return await self.client.fetch_channel_videos(channel_id, limit, offset)

    async def get_channel_videos_since(
        self, channel_id: str, since: datetime, *, page_size: int = 50
    ) -> list[HolodexVideo]:
        """Return all validated past streams newer than a UTC lower bound."""
        return await self.client.fetch_channel_videos_since(
            channel_id, since, page_size=page_size
        )


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honor a valid Retry-After response header before exponential backoff."""
    value = response.headers.get("Retry-After")
    if value:
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (TypeError, ValueError):
                pass
    return float(2**attempt)


def _parse_video(video: dict) -> HolodexVideo:
    """Map a raw Holodex video object to the strictly typed workflow shape."""
    try:
        channel = video["channel"]
        return HolodexVideo(
            id=str(video["id"]),
            title=str(video["title"]),
            topic_id=video.get("topic_id"),
            available_at=_parse_iso(str(video["available_at"])),
            channel_name=str(channel["name"]),
            duration=_parse_duration(video["duration"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedHolodexResponse("Invalid video item") from exc


def _parse_duration(value) -> int:
    """Parse a non-negative Holodex duration expressed in seconds."""
    try:
        duration = int(value)
    except (TypeError, ValueError) as exc:
        raise MalformedHolodexResponse("Invalid video duration") from exc
    if duration < 0:
        raise MalformedHolodexResponse("Video duration cannot be negative")
    return duration


def _parse_iso(value: str) -> datetime:
    """Parse a timezone-aware ISO timestamp and normalize it to UTC."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MalformedHolodexResponse(f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise MalformedHolodexResponse("Holodex timestamps must include a timezone")
    return parsed.astimezone(UTC)
