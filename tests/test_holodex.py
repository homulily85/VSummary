import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

from vsummary.util import holodex
from vsummary.util.holodex import (
    HOLODEX_API_URL,
    REFERER,
    USER_AGENT,
    ChannelNotFoundError,
    HolodexClient,
    MalformedHolodexResponse,
    PermanentHolodexError,
    TransientHolodexError,
)

CHANNEL_ID = "UCQ0UDLQCjY0rmuxCDE38FGg"
VIDEO_ID = "abcdefghijk"


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def time(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay


@pytest.fixture
def clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(
        holodex,
        "asyncio",
        SimpleNamespace(
            Lock=asyncio.Lock, get_running_loop=lambda: clock, sleep=clock.sleep
        ),
    )
    return clock


@pytest.fixture
async def make_client(clock):
    clients = []

    def create(**kwargs):
        client = HolodexClient(**kwargs)
        clients.append(client)
        return client

    try:
        yield create
    finally:
        for client in clients:
            await client.aclose()


@pytest.mark.asyncio
async def test_fetch_channel_parses_response_and_sends_browser_headers(make_client):
    seen = {}

    async def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"id": CHANNEL_ID, "name": "Test Channel"})

    client = make_client(transport=httpx.MockTransport(handler))
    channel = await client.fetch_channel(CHANNEL_ID)

    assert channel.id == CHANNEL_ID
    assert channel.name == "Test Channel"
    assert seen["request"].url == httpx.URL(f"{HOLODEX_API_URL}/channels/{CHANNEL_ID}")
    assert seen["request"].headers["referer"] == REFERER
    assert seen["request"].headers["user-agent"] == USER_AGENT


@pytest.mark.asyncio
async def test_fetch_channel_raises_for_not_found(make_client):
    async def handler(request):
        return httpx.Response(404)

    client = make_client(transport=httpx.MockTransport(handler))
    with pytest.raises(ChannelNotFoundError):
        await client.fetch_channel(CHANNEL_ID)


@pytest.mark.asyncio
async def test_fetch_channel_raises_for_other_http_errors(make_client):
    async def handler(request):
        return httpx.Response(500)

    client = make_client(transport=httpx.MockTransport(handler))
    with pytest.raises(TransientHolodexError):
        await client.fetch_channel(CHANNEL_ID)


@pytest.mark.asyncio
async def test_fetch_channel_videos_parses_items_and_query_parameters(make_client):
    seen = {}
    available_at = "2026-08-09T12:30:00+00:00"

    async def handler(request):
        seen["request"] = request
        return httpx.Response(
            200,
            json=[
                {
                    "id": VIDEO_ID,
                    "title": "Test Stream",
                    "topic_id": "stream",
                    "available_at": available_at,
                    "duration": 5400,
                    "channel": {"name": "Test Channel"},
                }
            ],
        )

    client = make_client(transport=httpx.MockTransport(handler))
    videos = await client.fetch_channel_videos(CHANNEL_ID, limit=7)

    assert len(videos) == 1
    assert videos[0].id == VIDEO_ID
    assert videos[0].title == "Test Stream"
    assert videos[0].topic_id == "stream"
    assert videos[0].available_at == datetime.fromisoformat(available_at)
    assert videos[0].duration == 5400
    assert videos[0].channel_name == "Test Channel"
    assert dict(seen["request"].url.params) == {
        "type": "stream",
        "limit": "7",
        "offset": "0",
        "status": "past",
    }


@pytest.mark.asyncio
async def test_fetch_channel_videos_raises_for_http_errors(make_client):
    async def handler(request):
        return httpx.Response(503)

    client = make_client(transport=httpx.MockTransport(handler))
    with pytest.raises(TransientHolodexError):
        await client.fetch_channel_videos(CHANNEL_ID)


@pytest.mark.asyncio
async def test_get_video_topic_refreshes_one_video_from_holodex(make_client):
    seen = []

    async def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json={"id": VIDEO_ID, "topic_id": "shorts"})

    client = make_client(transport=httpx.MockTransport(handler))
    assert await client.get_video_topic(VIDEO_ID) == "shorts"

    assert seen == [httpx.URL(f"{HOLODEX_API_URL}/videos/{VIDEO_ID}")]


@pytest.mark.asyncio
async def test_get_video_topic_allows_missing_topic(make_client):
    async def handler(request):
        return httpx.Response(200, json={"id": VIDEO_ID, "topic_id": None})

    client = make_client(transport=httpx.MockTransport(handler))
    assert await client.get_video_topic(VIDEO_ID) is None


@pytest.mark.asyncio
async def test_concurrent_endpoints_share_one_send_interval(make_client, clock):
    starts = []

    async def handler(request):
        starts.append(clock.time())
        if request.url.path.endswith(CHANNEL_ID):
            await asyncio.sleep(0)
            assert len(starts) == 1
            clock.now += 1
            return httpx.Response(200, json={"id": CHANNEL_ID, "name": "Test Channel"})
        return httpx.Response(200, json={"id": VIDEO_ID, "topic_id": "shorts"})

    client = make_client(transport=httpx.MockTransport(handler))
    channel, topic = await asyncio.gather(
        client.fetch_channel(CHANNEL_ID), client.get_video_topic(VIDEO_ID)
    )

    assert channel.id == CHANNEL_ID
    assert topic == "shorts"
    assert starts == [0, 2]


@pytest.mark.asyncio
async def test_topic_lookup_makes_one_http_attempt_per_call(make_client):
    requests = []

    async def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"id": VIDEO_ID, "topic_id": "shorts"})

    client = make_client(transport=httpx.MockTransport(handler), retries=1)
    with pytest.raises(TransientHolodexError):
        await client.get_video_topic(VIDEO_ID)

    assert len(requests) == 1


@pytest.mark.asyncio
async def test_strict_gateway_raises_typed_timeout_error(make_client):
    async def handler(request):
        raise httpx.ReadTimeout("timed out")

    client = make_client(transport=httpx.MockTransport(handler), retries=0)
    with pytest.raises(TransientHolodexError):
        await client.fetch_channel(CHANNEL_ID)


@pytest.mark.asyncio
async def test_strict_gateway_rejects_invalid_timestamps(make_client):
    async def handler(request):
        return httpx.Response(
            200,
            json=[
                {
                    "id": VIDEO_ID,
                    "title": "Test",
                    "available_at": "not-a-timestamp",
                    "channel": {"name": "Channel"},
                }
            ],
        )

    client = make_client(transport=httpx.MockTransport(handler), retries=0)
    with pytest.raises(MalformedHolodexResponse):
        await client.fetch_channel_videos(CHANNEL_ID)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "server_error"])
async def test_transient_retry_uses_the_send_interval(make_client, clock, failure):
    starts = []

    async def handler(request):
        starts.append(clock.time())
        if len(starts) == 1:
            if failure == "timeout":
                raise httpx.ReadTimeout("timed out")
            return httpx.Response(503)
        return httpx.Response(200, json={"id": CHANNEL_ID, "name": "Test Channel"})

    client = make_client(transport=httpx.MockTransport(handler), retries=1)
    assert (await client.fetch_channel(CHANNEL_ID)).id == CHANNEL_ID
    assert starts == [0, 2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("retry_after", "retry_at"),
    [("3", 4), ("Sun, 09 Aug 2026 12:30:05 GMT", 6), ("0", 2)],
)
async def test_rate_limit_response_is_retried_using_retry_after_header(
    make_client, clock, monkeypatch, retry_after, retry_at
):
    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 8, 9, 12, 30, tzinfo=UTC)

    monkeypatch.setattr(holodex, "datetime", FixedDatetime)
    starts = []

    async def handler(request):
        starts.append(clock.time())
        if len(starts) == 1:
            clock.now += 1
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"id": CHANNEL_ID, "name": "Test Channel"})

    client = make_client(transport=httpx.MockTransport(handler), retries=1)
    channel = await client.fetch_channel(CHANNEL_ID)

    assert channel.id == CHANNEL_ID
    assert starts == [0, retry_at]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers", [{}, {"Retry-After": "invalid"}, {"Retry-After": "inf"}]
)
async def test_rate_limit_fallback_keeps_exponential_backoff(
    make_client, clock, headers
):
    starts = []

    async def handler(request):
        starts.append(clock.time())
        if len(starts) <= 3:
            return httpx.Response(429, headers=headers)
        return httpx.Response(200, json={"id": CHANNEL_ID, "name": "Test Channel"})

    client = make_client(transport=httpx.MockTransport(handler), retries=3)
    assert (await client.fetch_channel(CHANNEL_ID)).id == CHANNEL_ID
    assert starts == [0, 2, 4, 8]


@pytest.mark.asyncio
async def test_exhausted_rate_limit_delays_other_requests(make_client, clock):
    starts = []

    async def handler(request):
        starts.append(clock.time())
        if request.url.path.endswith(VIDEO_ID):
            await asyncio.sleep(0)
            return httpx.Response(429, headers={"Retry-After": "5"})
        return httpx.Response(200, json={"id": CHANNEL_ID, "name": "Test Channel"})

    client = make_client(transport=httpx.MockTransport(handler), retries=2)
    topic, channel = await asyncio.gather(
        client.get_video_topic(VIDEO_ID),
        client.fetch_channel(CHANNEL_ID),
        return_exceptions=True,
    )

    assert isinstance(topic, TransientHolodexError)
    assert channel.id == CHANNEL_ID
    assert starts == [0, 5]


@pytest.mark.asyncio
async def test_cancelling_waiting_request_releases_the_limiter(
    make_client, clock, monkeypatch
):
    starts = []
    waiting = asyncio.Event()

    async def handler(request):
        starts.append(clock.time())
        return httpx.Response(200, json={"id": CHANNEL_ID, "name": "Test Channel"})

    async def block_sleep(delay):
        waiting.set()
        await asyncio.Future()

    client = make_client(transport=httpx.MockTransport(handler))
    await client.fetch_channel(CHANNEL_ID)
    with monkeypatch.context() as patch:
        patch.setattr(holodex.asyncio, "sleep", block_sleep)
        pending = asyncio.create_task(client.fetch_channel(CHANNEL_ID))
        try:
            await asyncio.sleep(0)
            assert waiting.is_set()
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    assert pending.cancelled()
    await client.fetch_channel(CHANNEL_ID)
    assert starts == [0, 2]


@pytest.mark.asyncio
async def test_pagination_collects_all_videos_newer_than_followed_at(
    make_client, clock
):
    requests = []
    since = datetime(2026, 8, 9, 10, tzinfo=UTC)

    def video(video_id, hour):
        return {
            "id": video_id,
            "title": video_id,
            "available_at": datetime(2026, 8, 9, hour, tzinfo=UTC).isoformat(),
            "duration": 0,
            "channel": {"name": "Test Channel"},
        }

    async def handler(request):
        offset = int(request.url.params["offset"])
        requests.append((offset, clock.time()))
        pages = {
            0: [video("newest", 13), video("newer", 12)],
            2: [video("new", 11), video("old", 9)],
        }
        return httpx.Response(200, json=pages[offset])

    client = make_client(transport=httpx.MockTransport(handler))
    videos = await client.fetch_channel_videos_since(CHANNEL_ID, since, page_size=2)

    assert [video.id for video in videos] == ["newest", "newer", "new"]
    assert requests == [(0, 0), (2, 2)]


@pytest.mark.asyncio
async def test_non_retryable_client_error_is_permanent(make_client):
    async def handler(request):
        return httpx.Response(400)

    client = make_client(transport=httpx.MockTransport(handler), retries=1)
    with pytest.raises(PermanentHolodexError, match="HTTP 400"):
        await client.fetch_channel(CHANNEL_ID)
