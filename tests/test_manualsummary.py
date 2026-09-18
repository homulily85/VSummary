import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import vsummary.cogs.manualsummary.manualsummary as manualsummary_module
from vsummary.cogs.manualsummary.manualsummary import ManualSummary
from vsummary.model.channel import JobStatus, ManualSummaryJob, ManualSummaryOperation
from vsummary.util.twitch import TwitchAudioUnavailableError


@pytest.mark.asyncio
async def test_topic_delivery_posts_plain_text_without_a_topic_picker(monkeypatch):
    sent = []

    class Channel:
        async def send(self, content, **kwargs):
            sent.append((content, kwargs))

    channel = Channel()
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(get_channel=lambda _: channel)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = ManualSummaryJob.model_construct(
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=ManualSummaryOperation.TOPICS,
        channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
        status=JobStatus.DELIVERING,
        delivery_chunks=["1: Intro", "2: Outro"],
    )
    get_topic_list = AsyncMock(side_effect=AssertionError("must not build a picker"))
    monkeypatch.setattr(manualsummary_module, "get_topic_list", get_topic_list)

    await cog._deliver(job)

    assert sent == [
        ("Here are the topics mentioned in the video:\n1: Intro", {}),
        ("2: Outro", {}),
    ]
    assert job.status is JobStatus.COMPLETED
    get_topic_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_twitch_audio_failure_is_logged_with_video_context(monkeypatch, caplog):
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(notebook_client=object())
    cog.settings = SimpleNamespace(
        source_retry_limit=5, twitch_max_duration_seconds=21_600
    )
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = ManualSummaryJob.model_construct(
        source="twitch",
        video_id="123456789",
        operation=ManualSummaryOperation.TOPICS,
        channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
    )
    error = TwitchAudioUnavailableError(
        "Twitch audio conversion failed: Postprocessing: Conversion failed!"
    )
    monkeypatch.setattr(
        manualsummary_module,
        "get_topic_list",
        AsyncMock(side_effect=error),
    )

    with caplog.at_level(logging.ERROR, logger=manualsummary_module.__name__):
        await cog._generate(job)

    record = next(
        item
        for item in caplog.records
        if item.getMessage() == f"Manual summary failed: {error}"
    )
    assert record.context == {"source": "twitch", "video_id": "123456789"}
    assert job.status is JobStatus.FAILED
    assert job.last_error == str(error)


@pytest.mark.asyncio
async def test_detail_includes_video_and_channel_metadata(monkeypatch):
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(notebook_client=object())
    cog.settings = SimpleNamespace(twitch_max_duration_seconds=21_600)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = ManualSummaryJob.model_construct(
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=ManualSummaryOperation.DETAIL_ONE,
        topic_index=0,
        channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        manualsummary_module,
        "get_topic_details",
        AsyncMock(return_value={"topic_name": "Introduction", "detail": "Details"}),
    )
    monkeypatch.setattr(
        manualsummary_module,
        "get_video_metadata",
        AsyncMock(
            return_value=SimpleNamespace(
                title="Test Stream", channel_name="Test Channel"
            )
        ),
        raising=False,
    )

    await cog._generate(job)

    assert job.delivery_chunks == [
        (
            "**Video:** Test Stream\n**Channel:** Test Channel\n\n"
            "**Introduction**\nDetails"
        )
    ]


@pytest.mark.asyncio
async def test_all_details_include_video_and_channel_metadata_once(monkeypatch):
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(notebook_client=object())
    cog.settings = SimpleNamespace(twitch_max_duration_seconds=21_600)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = ManualSummaryJob.model_construct(
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=ManualSummaryOperation.DETAIL_ALL,
        channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
    )
    monkeypatch.setattr(
        manualsummary_module,
        "get_topic_details_all",
        AsyncMock(
            return_value=[
                {"topic_name": "Introduction", "detail": "First details"},
                {"topic_name": "Conclusion", "detail": "Last details"},
            ]
        ),
    )
    monkeypatch.setattr(
        manualsummary_module,
        "get_video_metadata",
        AsyncMock(
            return_value=SimpleNamespace(
                title="Test Stream", channel_name="Test Channel"
            )
        ),
        raising=False,
    )

    await cog._generate(job)

    assert job.delivery_chunks == [
        (
            "**Video:** Test Stream\n**Channel:** Test Channel\n\n"
            "**Introduction**\nFirst details"
        ),
        "**Conclusion**\nLast details",
    ]
