from datetime import UTC, datetime, timedelta

import httpx
import pytest
from httpx import MockTransport

from vsummary.util.holodex import (
    IGNORED_TOPICS,
    ChannelNotFoundError,
    HolodexClient,
    is_ignored,
    is_transcript_ready,
)

CHANNEL_ID = "UCQ0UDLQCjY0rmuxCDE38FGg"


class FakeTransport:
    """Callable handler for httpx.MockTransport returning canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def __call__(self, request):
        self.requests.append(request)
        response = self._responses.pop(0)
        return httpx.Response(
            response.status_code, json=response._json, request=request
        )


class FakeResponse:
    def __init__(self, status_code, json_data):
        self.status_code = status_code
        self._json = json_data


def make_client(transport):
    return HolodexClient(transport=MockTransport(transport))


def channel_payload(overrides=None):
    payload = {
        "id": CHANNEL_ID,
        "name": "Test Channel",
        "type": "vtuber",
        "english_name": "Test Channel EN",
        "org": "Hololive",
        "suborg": "",
        "photo": "https://example.com/photo.png",
    }
    if overrides:
        payload.update(overrides)
    return payload


def video_payload(overrides=None):
    payload = {
        "id": "abcdefghijk",
        "title": "New Stream",
        "type": "stream",
        "topic_id": "stream",
        "published_at": "2026-08-09T04:54:35.000Z",
        "available_at": "2026-08-09T15:30:48.000Z",
        "duration": 1880,
        "status": "past",
        "channel": {
            "id": CHANNEL_ID,
            "name": "Test Channel",
            "org": "Hololive",
            "suborg": "",
            "type": "vtuber",
            "photo": "https://example.com/photo.png",
            "english_name": "Test Channel EN",
        },
    }
    if overrides:
        payload.update(overrides)
    return payload


class TestGetChannel:
    async def test_returns_channel_info(self):
        client = make_client(FakeTransport([FakeResponse(200, channel_payload())]))
        channel = await client.get_channel(CHANNEL_ID)
        assert channel.id == CHANNEL_ID
        assert channel.name == "Test Channel"

    async def test_raises_not_found_on_404(self):
        client = make_client(FakeTransport([FakeResponse(404, {"error": "not found"})]))
        with pytest.raises(ChannelNotFoundError):
            await client.get_channel("nonexistent")

    async def test_uses_browser_headers(self):
        transport = FakeTransport([FakeResponse(200, channel_payload())])
        client = make_client(transport)
        await client.get_channel(CHANNEL_ID)
        request = transport.requests[0]
        assert request.headers["referer"] == f"https://holodex.net/channel/{CHANNEL_ID}"
        assert "Mozilla/5.0" in request.headers["user-agent"]


class TestGetChannelVideos:
    async def test_parses_video_list(self):
        client = make_client(FakeTransport([FakeResponse(200, [video_payload()])]))
        videos = await client.get_channel_videos(CHANNEL_ID)
        assert len(videos) == 1
        video = videos[0]
        assert video.id == "abcdefghijk"
        assert video.title == "New Stream"
        assert video.topic_id == "stream"
        assert video.available_at == datetime(2026, 8, 9, 15, 30, 48, tzinfo=UTC)
        assert video.channel_name == "Test Channel"

    async def test_passes_limit_param(self):
        client = make_client(FakeTransport([FakeResponse(200, [])]))
        await client.get_channel_videos(CHANNEL_ID, limit=10)
        request = client._last_request
        assert request.url.params["limit"] == "10"


class TestIsIgnored:
    @pytest.mark.parametrize("topic_id", sorted(IGNORED_TOPICS))
    def test_ignored_topics(self, topic_id):
        assert is_ignored(topic_id)

    def test_normal_topic_not_ignored(self):
        assert not is_ignored("stream")

    def test_none_not_ignored(self):
        assert not is_ignored(None)


class TestIsTranscriptReady:
    def test_ready_after_two_hours(self):
        now = datetime.now(UTC)
        available_at = now - timedelta(hours=2, minutes=1)
        assert is_transcript_ready(available_at, now)

    def test_not_ready_within_two_hours(self):
        now = datetime.now(UTC)
        available_at = now - timedelta(hours=1, minutes=59)
        assert not is_transcript_ready(available_at, now)
