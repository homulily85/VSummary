"""Asynchronous Holodex requests, response validation, and retry pacing."""

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
    """Base class for errors reported by the Holodex client."""


class ChannelNotFoundError(HolodexError):
    """Signal that Holodex returned HTTP 404 for a channel lookup."""


class TransientHolodexError(HolodexError):
    """Signal exhausted retries for a transport failure or retryable HTTP response."""


class PermanentHolodexError(HolodexError):
    """Signal a Holodex failure that the client cannot resolve by retrying."""


class MalformedHolodexResponse(PermanentHolodexError):
    """Signal that a Holodex response could not be decoded or validated."""


@dataclass(frozen=True)
class HolodexChannel:
    """Carry the Holodex channel fields needed for a subscription.

    Attributes:
        id: Holodex channel identifier.
        name: Channel display name.
    """

    id: str
    name: str


@dataclass(frozen=True)
class HolodexVideo:
    """Carry a past stream returned by a Holodex channel listing.

    Attributes:
        id: YouTube video identifier.
        title: Video display title.
        topic_id: Holodex topic identifier, if supplied.
        available_at: Publication timestamp normalized to UTC.
        channel_name: Channel display name.
        duration: Stream duration in seconds.
    """

    id: str
    title: str
    topic_id: str | None
    available_at: datetime
    channel_name: str
    duration: int = 0

    @property
    def video_id(self) -> str:
        """YouTube video identifier under the name used by summary workflows."""
        return self.id


class HolodexClient:
    """Reuse one asynchronous HTTP client for Holodex requests.

    Serialize requests within the client, space their starts, and honor rate limits
    across callers.
    """

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        timeout: float = 15.0,
        retries: int = 2,
    ):
        """Initialize the request pool, rate limiter, and retry budget.

        Args:
            transport: Optional HTTPX transport. By default, use HTTPX networking.
            timeout: Per-request HTTPX timeout in seconds.
            retries: Additional attempts for transient failures. Negative values act as
                zero.
        """
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
        """Close the shared asynchronous HTTP connection pool."""
        await self._client.aclose()

    async def _request(
        self, method: str, path: str, *, retries: int | None = None, **kwargs
    ) -> httpx.Response:
        """Send a paced request and retry transient failures within the budget.

        Args:
            method: HTTP request method.
            path: Endpoint path relative to the Holodex API base URL.
            retries: Additional attempts, or ``None`` to use the client's budget.
            **kwargs: HTTPX request options, such as query parameters.

        Returns:
            HTTP response after handling retryable status codes. Endpoint methods
                interpret other statuses.

        Raises:
            TransientHolodexError: If transport failures or HTTP 408, 429, or 5xx
                responses exhaust the budget.
        """
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
        """Fetch the ID and display name for one Holodex channel.

        Args:
            channel_id: Holodex channel identifier.

        Returns:
            Channel details used when following the channel.

        Raises:
            ChannelNotFoundError: If the channel endpoint returns HTTP 404.
            TransientHolodexError: If transient request failures exhaust retries.
            PermanentHolodexError: If another non-retryable HTTP error is returned.
            MalformedHolodexResponse: If the response lacks usable channel fields.
        """
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
        """Read one page of past streams in the order returned by Holodex.

        Args:
            channel_id: Channel whose past streams to query.
            limit: Maximum number of streams requested in the page.
            offset: Number of results to skip from the start of the listing.

        Returns:
            Validated stream records in the endpoint's newest-first order.

        Raises:
            TransientHolodexError: If transient request failures exhaust retries.
            PermanentHolodexError: If a non-retryable HTTP error is returned.
            MalformedHolodexResponse: If the response is not a valid list of stream
                records.
        """
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
        """Read the current topic of a video in one Holodex request.

        Args:
            video_id: YouTube ID expected in the Holodex response.

        Returns:
            Trimmed topic ID, or ``None`` if it is missing or blank.

        Raises:
            TransientHolodexError: If the request fails with a transient error.
            PermanentHolodexError: If the video is missing or another non-retryable HTTP
                error occurs.
            MalformedHolodexResponse: If the response has an unexpected ID or invalid
                topic data.
        """
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
        """Page through past streams until reaching the publication cutoff.

        Args:
            channel_id: Channel to scan in newest-first order.
            since: Exclusive, timezone-aware lower bound on publication time.
            page_size: Positive maximum number of streams to request per page.

        Returns:
            Streams published strictly after the cutoff, in newest-first order.

        Raises:
            ValueError: If the cutoff lacks a timezone or the page size is non-positive.
            TransientHolodexError: If a page request exhausts its retry budget.
            PermanentHolodexError: If a page request returns a non-retryable HTTP error.
            MalformedHolodexResponse: If a page cannot be decoded or validated.
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
    """Choose the retry delay from the response header or exponential backoff.

    Args:
        response: HTTP response that may specify a ``Retry-After`` delay or date.
        attempt: Zero-based index of the failed attempt.

    Returns:
        Non-negative delay in seconds, falling back to two raised to the attempt index.
    """
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
    """Decode one Holodex stream item into the workflow's video record.

    Args:
        video: Raw JSON object from a channel listing.

    Returns:
        Video record with UTC publication time and a duration in seconds.

    Raises:
        MalformedHolodexResponse: If required fields are missing or cannot be parsed.
    """
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
    """Convert the Holodex duration field to non-negative seconds.

    Args:
        value: Raw duration field, convertible to an integer.

    Returns:
        Duration as an integer number of seconds.

    Raises:
        MalformedHolodexResponse: If conversion fails with a type or value error, or the
            result is negative.
        OverflowError: If the input is numeric infinity.
    """
    try:
        duration = int(value)
    except (TypeError, ValueError) as exc:
        raise MalformedHolodexResponse("Invalid video duration") from exc
    if duration < 0:
        raise MalformedHolodexResponse("Video duration cannot be negative")
    return duration


def _parse_iso(value: str) -> datetime:
    """Convert an ISO timestamp from Holodex to UTC.

    Args:
        value: ISO timestamp with an explicit timezone offset.

    Returns:
        Timezone-aware datetime in UTC.

    Raises:
        MalformedHolodexResponse: If the timestamp cannot be parsed or lacks a timezone.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MalformedHolodexResponse(f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise MalformedHolodexResponse("Holodex timestamps must include a timezone")
    return parsed.astimezone(UTC)
