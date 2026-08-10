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
    """Hours to wait before the next attempt.

    Retry 1 waits 1 hour, retry 2 waits 2 hours, retry 3 waits 4 hours,
    retry 4 waits 8 hours, and retry 5 waits 16 hours.
    """
    return 2 ** (retry_count - 1)


def is_ignored(topic_id: str | None) -> bool:
    """True when a video's topic_id should be skipped for auto-summaries."""
    return topic_id in IGNORED_TOPICS


def is_transcript_ready(available_at: datetime) -> bool:
    """True when a transcript is expected to be available (>2h after available_at)."""
    now = datetime.now(UTC)
    return now - available_at > TRANSCRIPT_READY_DELAY


class HolodexError(Exception):
    """Base error for Holodex API failures."""


class ChannelNotFoundError(HolodexError):
    """Raised when the Holodex channel endpoint returns 404."""


@dataclass(frozen=True)
class HolodexChannel:
    id: str
    name: str


@dataclass(frozen=True)
class HolodexVideo:
    id: str
    title: str
    topic_id: str
    available_at: datetime
    channel_name: str


class HolodexClient:
    """Async client for the Holodex v2 API."""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._client = httpx.AsyncClient(
            base_url=HOLODEX_API_URL,
            headers={
                "referer": REFERER,
                "user-agent": USER_AGENT,
            },
            transport=transport,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.aclose()

    async def aclose(self):
        await self._client.aclose()

    async def get_channel(self, channel_id: str) -> HolodexChannel | None:
        response = await self._client.get(f"/channels/{channel_id}")
        if response.status_code == 404:
            raise ChannelNotFoundError(f"Channel '{channel_id}' not found on Holodex.")

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(f"Error fetching videos for channel {channel_id}: {exc}")
            return None

        data = response.json()
        return HolodexChannel(id=data["id"], name=data["name"])

    async def get_channel_videos(
        self, channel_id: str, limit: int = 1
    ) -> list[HolodexVideo]:
        response = await self._client.get(
            f"/channels/{channel_id}/videos",
            params={
                "type": "stream",
                "limit": limit,
                "offset": 0,
                "status": "past",
            },
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            logger.error(f"Error fetching videos for channel {channel_id}: {exc}")
            return []
        return [
            HolodexVideo(
                id=video["id"],
                title=video["title"],
                topic_id=video.get("topic_id"),
                available_at=_parse_iso(video["available_at"]),
                channel_name=video["channel"]["name"],
            )
            for video in response.json()
        ]


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed
