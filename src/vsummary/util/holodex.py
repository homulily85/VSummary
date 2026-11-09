"""Typed Holodex API access with request pacing and bounded retries."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite

import httpx

from vsummary.logging import log_event

logger = logging.getLogger(__name__)

HOLODEX_BASE_URL = "https://holodex.net"
HOLODEX_API_URL = f"{HOLODEX_BASE_URL}/api/v2"
_REQUEST_INTERVAL_SECONDS = 2.0
REFERER = f"{HOLODEX_BASE_URL}"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
DEFAULT_IGNORED_TOPICS = {
    "Original_Song",
    "membersonly",
    "shorts",
    "Music_Cover",
}


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
    """Async Holodex client with shared request pacing and bounded retries."""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        timeout: float = 15.0,
        retries: int = 2,
    ):
        """Create a client with a single timeout and retry policy for every request."""
        self._retries = max(0, retries)
        self._request_lock = asyncio.Lock()
        self._next_request_at = float("-inf")
        self._client = httpx.AsyncClient(
            base_url=HOLODEX_API_URL,
            headers={"referer": REFERER, "user-agent": USER_AGENT},
            transport=transport,
            timeout=httpx.Timeout(timeout),
        )

    async def aclose(self):
        """Close the underlying reusable HTTP connection pool."""
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, retries: int | None = None, **kwargs
    ) -> httpx.Response:
        """Request Holodex, retrying transient transport and server failures."""
        retry_limit = self._retries if retries is None else max(0, retries)
        clock = asyncio.get_running_loop().time
        for attempt in range(retry_limit + 1):
            rate_limit_delay = None
            try:
                async with self._request_lock:
                    while (wait := self._next_request_at - clock()) > 0:
                        await asyncio.sleep(wait)
                    self._next_request_at = clock() + _REQUEST_INTERVAL_SECONDS
                    response = await self._client.request(method, path, **kwargs)
                    if response.status_code == 429:
                        rate_limit_delay = _retry_delay(response, attempt)
                        self._next_request_at = max(
                            self._next_request_at, clock() + rate_limit_delay
                        )
            except (
                httpx.TimeoutException,
                httpx.NetworkError,
                httpx.ConnectError,
            ) as exc:
                if attempt >= retry_limit:
                    raise TransientHolodexError(str(exc)) from exc
                delay = 2**attempt
                log_event(
                    logger,
                    logging.WARNING,
                    "Holodex request failed; retrying %s %s.",
                    method,
                    path,
                    attempt=attempt + 1,
                    retry_limit=retry_limit + 1,
                    retry_delay_seconds=delay,
                    error_type=type(exc).__name__,
                )
                await asyncio.sleep(delay)
                continue
            if response.status_code in {408, 429} or response.status_code >= 500:
                if attempt >= retry_limit:
                    raise TransientHolodexError(
                        f"Holodex returned HTTP {response.status_code}"
                    )
                delay = (
                    _retry_delay(response, attempt)
                    if rate_limit_delay is None
                    else rate_limit_delay
                )
                log_event(
                    logger,
                    logging.WARNING,
                    "Holodex returned HTTP %s; retrying %s %s.",
                    response.status_code,
                    method,
                    path,
                    attempt=attempt + 1,
                    retry_limit=retry_limit + 1,
                    retry_delay_seconds=delay,
                    http_status=response.status_code,
                )
                if rate_limit_delay is None:
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

    async def get_video_topic(self, video_id: str) -> str | None:
        """Fetch the current topic of one video, preserving API failures."""
        response = await self._request("GET", f"/videos/{video_id}", retries=0)
        if response.status_code == 404:
            raise PermanentHolodexError(f"Video '{video_id}' not found on Holodex")
        if response.is_error:
            raise PermanentHolodexError(
                f"Holodex returned HTTP {response.status_code} for video '{video_id}'"
            )
        try:
            data = response.json()
            if not isinstance(data, dict) or str(data.get("id")) != video_id:
                raise ValueError("invalid video response")
            topic_id = data.get("topic_id")
            if topic_id is None:
                return None
            if not isinstance(topic_id, str):
                raise TypeError("invalid topic ID")
            return topic_id.strip() or None
        except (TypeError, ValueError) as exc:
            raise MalformedHolodexResponse(
                f"Invalid topic response for video '{video_id}'"
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


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    """Honor a valid Retry-After response header before exponential backoff."""
    value = response.headers.get("Retry-After")
    if value:
        try:
            delay = float(value)
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())
            except (OverflowError, TypeError, ValueError):
                pass
        else:
            if isfinite(delay) and delay >= 0:
                return delay
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
