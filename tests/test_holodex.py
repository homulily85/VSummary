from datetime import UTC, datetime, timedelta

import httpx
import pytest

from vsummary.util.holodex import (
    HOLODEX_API_URL,
    MAX_RETRIES,
    REFERER,
    USER_AGENT,
    ChannelNotFoundError,
    HolodexClient,
    MalformedHolodexResponse,
    TransientHolodexError,
    exponential_backoff_hours,
    is_ignored,
    is_transcript_ready,
)

CHANNEL_ID = "UCQ0UDLQCjY0rmuxCDE38FGg"
VIDEO_ID = "abcdefghijk"


@pytest.mark.parametrize(
    "retry_count, expected", [(1, 1), (2, 2), (3, 4), (4, 8), (5, 16)]
)
def test_exponential_backoff_hours(retry_count, expected):
    assert exponential_backoff_hours(retry_count) == expected


def test_max_retries_matches_documented_backoff_window():
    assert exponential_backoff_hours(MAX_RETRIES) == 16


@pytest.mark.parametrize(
    "topic_id", ["Original_Song", "membersonly", "shorts", "Music_Cover"]
)
def test_is_ignored_for_excluded_topics(topic_id):
    assert is_ignored(topic_id)


@pytest.mark.parametrize("topic_id", [None, "normal", "Gaming"])
def test_is_ignored_for_other_topics(topic_id):
    assert not is_ignored(topic_id)


def test_is_transcript_ready_after_two_hours():
    assert is_transcript_ready(datetime.now(UTC) - timedelta(hours=3))


def test_is_transcript_not_ready_within_two_hours():
    assert not is_transcript_ready(datetime.now(UTC) - timedelta(hours=1))


@pytest.mark.asyncio
async def test_get_channel_parses_response_and_sends_browser_headers():
    seen = {}

    async def handler(request):
        seen["request"] = request
        return httpx.Response(200, json={"id": CHANNEL_ID, "name": "Test Channel"})

    async with HolodexClient(transport=httpx.MockTransport(handler)) as client:
        channel = await client.get_channel(CHANNEL_ID)

    assert channel.id == CHANNEL_ID
    assert channel.name == "Test Channel"
    assert seen["request"].url == httpx.URL(f"{HOLODEX_API_URL}/channels/{CHANNEL_ID}")
    assert seen["request"].headers["referer"] == REFERER
    assert seen["request"].headers["user-agent"] == USER_AGENT


@pytest.mark.asyncio
async def test_get_channel_raises_for_not_found():
    async def handler(request):
        return httpx.Response(404)

    async with HolodexClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ChannelNotFoundError):
            await client.get_channel(CHANNEL_ID)


@pytest.mark.asyncio
async def test_get_channel_returns_none_for_other_http_errors():
    async def handler(request):
        return httpx.Response(500)

    async with HolodexClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.get_channel(CHANNEL_ID) is None


@pytest.mark.asyncio
async def test_get_channel_videos_parses_items_and_query_parameters():
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
                    "channel": {"name": "Test Channel"},
                }
            ],
        )

    async with HolodexClient(transport=httpx.MockTransport(handler)) as client:
        videos = await client.get_channel_videos(CHANNEL_ID, limit=7)

    assert len(videos) == 1
    assert videos[0].id == VIDEO_ID
    assert videos[0].title == "Test Stream"
    assert videos[0].topic_id == "stream"
    assert videos[0].available_at == datetime.fromisoformat(available_at)
    assert videos[0].channel_name == "Test Channel"
    assert dict(seen["request"].url.params) == {
        "type": "stream",
        "limit": "7",
        "offset": "0",
        "status": "past",
    }


@pytest.mark.asyncio
async def test_get_channel_videos_returns_empty_list_for_http_errors():
    async def handler(request):
        return httpx.Response(503)

    async with HolodexClient(transport=httpx.MockTransport(handler)) as client:
        assert await client.get_channel_videos(CHANNEL_ID) == []


@pytest.mark.asyncio
async def test_strict_gateway_raises_typed_timeout_error():
    async def handler(request):
        raise httpx.ReadTimeout("timed out")

    async with HolodexClient(
        transport=httpx.MockTransport(handler), retries=0
    ) as client:
        with pytest.raises(TransientHolodexError):
            await client.fetch_channel(CHANNEL_ID)


@pytest.mark.asyncio
async def test_strict_gateway_rejects_invalid_timestamps():
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

    async with HolodexClient(
        transport=httpx.MockTransport(handler), retries=0
    ) as client:
        with pytest.raises(MalformedHolodexResponse):
            await client.fetch_channel_videos(CHANNEL_ID)
