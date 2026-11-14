"""Quota scheduling, fail-open checks, and generation boundaries."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from notebooklm import ChatError, NotebookLMError, RateLimitError
from notebooklm.types import (
    UsageSummary,
    UsageSummaryStatus,
    UsageWindow,
    UsageWindowKind,
)

from vsummary.services import notebook_quota as quota
from vsummary.util import summarizer
from vsummary.util.video import VideoRef


def usage(five=0, weekly=0, *, past=False):
    now = datetime.now(UTC)
    return UsageSummary(
        UsageSummaryStatus.READY,
        windows=(
            UsageWindow(
                UsageWindowKind.FIVE_HOUR,
                five,
                100 - five,
                now + timedelta(hours=-1 if past else 5),
            ),
            UsageWindow(
                UsageWindowKind.WEEKLY,
                weekly,
                100 - weekly,
                now + timedelta(days=-1 if past else 7),
            ),
        ),
    )


def client_for(snapshot):
    return SimpleNamespace(
        settings=SimpleNamespace(get_usage=AsyncMock(return_value=snapshot))
    )


@pytest.mark.parametrize(
    ("five", "weekly", "index"), [(100, 0, 0), (0, 100, 1), (100, 100, 1), (101, 0, 0)]
)
async def test_exhausted_windows_use_latest_blocking_reset(five, weekly, index):
    snapshot = usage(five, weekly)
    with pytest.raises(quota.NotebookQuotaExceeded) as caught:
        await quota.check_quota(client_for(snapshot))
    assert caught.value.retry_at == snapshot.windows[index].resets_at


@pytest.mark.parametrize(
    "snapshot",
    [
        usage(),
        UsageSummary(UsageSummaryStatus.DISABLED),
        UsageSummary(UsageSummaryStatus.SKIPPED),
    ],
)
async def test_available_or_unavailable_metering_allows_work(snapshot):
    await quota.check_quota(client_for(snapshot))


@pytest.mark.parametrize("error", [NotebookLMError("offline"), TimeoutError()])
async def test_lookup_errors_allow_work(error):
    client = client_for(None)
    client.settings.get_usage.side_effect = error
    await quota.check_quota(client)


async def test_lookup_timeout_allows_work(monkeypatch):
    monkeypatch.setattr(quota, "QUOTA_TIMEOUT_SECONDS", 0.001)

    async def slow():
        await asyncio.sleep(10)

    client = client_for(None)
    client.settings.get_usage.side_effect = slow
    await quota.check_quota(client)


async def test_stale_reset_waits_five_minutes():
    before = datetime.now(UTC)
    with pytest.raises(quota.NotebookQuotaExceeded) as caught:
        await quota.check_quota(client_for(usage(100, past=True)))
    assert 300 <= (caught.value.retry_at - before).total_seconds() < 301


@pytest.mark.parametrize(
    "delay, expected", [(45, 45), (None, 300), (0, 300), (-1, 300), (float("nan"), 300)]
)
async def test_rate_limit_without_reset_uses_retry_after_or_recheck(delay, expected):
    client = client_for(usage())
    before = datetime.now(UTC)
    with pytest.raises(quota.NotebookQuotaExceeded) as caught:
        async with quota.quota_guard(client):
            raise RateLimitError("limited", retry_after=delay)
    assert expected <= (caught.value.retry_at - before).total_seconds() < expected + 1
    assert client.settings.get_usage.await_count == 2


async def test_failure_refresh_detects_newly_exhausted_quota():
    snapshot = usage(100)
    client = client_for(None)
    client.settings.get_usage.side_effect = [usage(), snapshot]
    with pytest.raises(quota.NotebookQuotaExceeded) as caught:
        async with quota.quota_guard(client):
            raise ChatError("request failed")
    assert caught.value.retry_at == snapshot.windows[0].resets_at


async def test_other_sdk_error_preserved_when_quota_available():
    error = ChatError("not a quota error")
    with pytest.raises(ChatError) as caught:
        async with quota.quota_guard(client_for(usage())):
            raise error
    assert caught.value is error


async def test_quota_prevents_notebook_and_audio_work():
    client = client_for(usage(100))
    client.notebooks = SimpleNamespace(create=AsyncMock())
    with pytest.raises(quota.NotebookQuotaExceeded):
        await summarizer._create_notebook_with_source(
            client, VideoRef(source="youtube", video_id="video"), use_youtube_audio=True
        )
    client.notebooks.create.assert_not_awaited()


async def test_quota_prevents_chat_request():
    client = client_for(usage(100))
    client.chat = SimpleNamespace(ask=AsyncMock())
    with pytest.raises(quota.NotebookQuotaExceeded):
        await summarizer._ask_notebook(
            client,
            "notebook",
            "question",
            phase="topic_detail",
            ref=VideoRef(source="youtube", video_id="video"),
            topic_count=1,
        )
    client.chat.ask.assert_not_awaited()
