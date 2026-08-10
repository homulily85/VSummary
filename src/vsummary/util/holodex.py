from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

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
    return 2 ** (retry_count - 1)


def is_ignored(topic_id: str | None) -> bool:
    return topic_id in IGNORED_TOPICS


def is_transcript_ready(available_at: datetime) -> bool:
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
    id: str
    name: str


@dataclass(frozen=True)
class HolodexVideo:
    id: str
    title: str
    topic_id: str | None
    available_at: datetime
    channel_name: str

    @property
    def video_id(self) -> str:
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
        self._retries = max(0, retries)
        self._client = httpx.AsyncClient(
            base_url=HOLODEX_API_URL,
            headers={"referer": REFERER, "user-agent": USER_AGENT},
            transport=transport,
            timeout=httpx.Timeout(timeout),
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def close(self):
        await self.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
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
                await asyncio.sleep(2**attempt)
                continue
            if response.status_code >= 500:
                if attempt >= self._retries:
                    raise TransientHolodexError(
                        f"Holodex returned HTTP {response.status_code}"
                    )
                await asyncio.sleep(2**attempt)
                continue
            return response
        raise AssertionError("unreachable")

    async def fetch_channel(self, channel_id: str) -> HolodexChannel:
        response = await self._request("GET", f"/channels/{channel_id}")
        if response.status_code == 404:
            raise ChannelNotFoundError(f"Channel '{channel_id}' not found on Holodex.")
        try:
            response.raise_for_status()
            data = response.json()
            return HolodexChannel(id=str(data["id"]), name=str(data["name"]))
        except (httpx.HTTPStatusError, KeyError, TypeError, ValueError) as exc:
            raise MalformedHolodexResponse(
                f"Invalid channel response for '{channel_id}'"
            ) from exc

    async def fetch_channel_videos(
        self, channel_id: str, limit: int = 1
    ) -> list[HolodexVideo]:
        response = await self._request(
            "GET",
            f"/channels/{channel_id}/videos",
            params={"type": "stream", "limit": limit, "offset": 0, "status": "past"},
        )
        try:
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise TypeError("expected a list")
            return [_parse_video(video) for video in payload]
        except (httpx.HTTPStatusError, KeyError, TypeError, ValueError) as exc:
            raise MalformedHolodexResponse(
                f"Invalid videos response for '{channel_id}'"
            ) from exc

    async def get_channel(self, channel_id: str) -> HolodexChannel | None:
        """Legacy helper returning ``None`` for ordinary HTTP failures."""
        try:
            return await self.fetch_channel(channel_id)
        except ChannelNotFoundError:
            raise
        except (PermanentHolodexError, TransientHolodexError) as exc:
            logger.error("Error fetching channel %s: %s", channel_id, exc)
            return None

    async def get_channel_videos(
        self, channel_id: str, limit: int = 1
    ) -> list[HolodexVideo]:
        """Legacy helper returning an empty list for ordinary HTTP failures."""
        try:
            return await self.fetch_channel_videos(channel_id, limit)
        except (PermanentHolodexError, TransientHolodexError) as exc:
            logger.error("Error fetching videos for channel %s: %s", channel_id, exc)
            return []


class HolodexGateway:
    """Strict typed gateway used by workflows."""

    def __init__(self, client: HolodexClient | None = None, **client_kwargs):
        self.client = client or HolodexClient(**client_kwargs)

    async def close(self) -> None:
        await self.client.aclose()

    async def get_channel(self, channel_id: str) -> HolodexChannel:
        return await self.client.fetch_channel(channel_id)

    async def get_channel_videos(
        self, channel_id: str, limit: int = 1
    ) -> list[HolodexVideo]:
        return await self.client.fetch_channel_videos(channel_id, limit)


def _parse_video(video: dict) -> HolodexVideo:
    try:
        channel = video["channel"]
        return HolodexVideo(
            id=str(video["id"]),
            title=str(video["title"]),
            topic_id=video.get("topic_id"),
            available_at=_parse_iso(str(video["available_at"])),
            channel_name=str(channel["name"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MalformedHolodexResponse("Invalid video item") from exc


def _parse_iso(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise MalformedHolodexResponse(f"Invalid timestamp: {value}") from exc
    if parsed.tzinfo is None:
        raise MalformedHolodexResponse("Holodex timestamps must include a timezone")
    return parsed.astimezone(UTC)
