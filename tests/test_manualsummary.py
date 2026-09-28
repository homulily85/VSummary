import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import vsummary.cogs.manualsummary.manualsummary as manualsummary_module
from vsummary.cogs.manualsummary.manualsummary import ManualSummary
from vsummary.model.channel import JobStatus, ManualSummaryJob, ManualSummaryOperation
from vsummary.model.video import Topic
from vsummary.util.generic_media import GenericMediaUnavailableError
from vsummary.util.summarizer import InvalidSummaryResponse
from vsummary.util.twitch import TwitchAudioUnavailableError
from vsummary.util.video import parse_video_source


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
async def test_enqueue_stores_video_metadata(monkeypatch):
    captured = {}

    class FakeManualSummaryJob:
        def __init__(self, **values):
            captured.update(values)

        async def save(self):
            captured["saved"] = True

    cog = ManualSummary.__new__(ManualSummary)
    cog.worker_id = "worker"
    monkeypatch.setattr(manualsummary_module, "ManualSummaryJob", FakeManualSummaryJob)
    monkeypatch.setattr(
        manualsummary_module,
        "get_video_metadata",
        AsyncMock(
            return_value=SimpleNamespace(
                title="Test Stream", channel_name="Test Channel"
            )
        ),
    )

    await cog.enqueue(
        ref=manualsummary_module.VideoRef(source="youtube", video_id="dQw4w9WgXcQ"),
        operation=ManualSummaryOperation.DETAIL_ONE,
        channel_id=123,
        requester_id=456,
    )

    assert captured["title"] == "Test Stream"
    assert captured["channel_name"] == "Test Channel"
    assert captured["saved"] is True


@pytest.mark.asyncio
async def test_enqueue_checks_generic_media_and_persists_url(monkeypatch):
    saved = {}

    class FakeJob:
        def __init__(self, **values):
            saved.update(values)

        async def save(self):
            saved["saved"] = True

    monkeypatch.setattr(manualsummary_module, "ManualSummaryJob", FakeJob)
    inspect = AsyncMock(return_value=SimpleNamespace(title="Talk", channel_name=None))
    monkeypatch.setattr(manualsummary_module, "inspect_generic_media", inspect)
    ref = parse_video_source("https://vimeo.com/123")
    cog = ManualSummary.__new__(ManualSummary)

    await cog.enqueue(
        ref=ref,
        operation=ManualSummaryOperation.TOPICS,
        channel_id=123,
        requester_id=456,
    )

    inspect.assert_awaited_once_with(ref)
    assert saved["source_url"] == "https://vimeo.com/123"
    assert saved["title"] == "Talk"
    assert saved["saved"] is True


@pytest.mark.asyncio
async def test_generic_job_restores_url_for_generation(monkeypatch):
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(notebook_client=object())
    cog.settings = SimpleNamespace()
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    ref = parse_video_source("https://vimeo.com/123")
    job = ManualSummaryJob.model_construct(
        source=ref.source,
        video_id=ref.video_id,
        source_url=ref.url,
        operation=ManualSummaryOperation.TOPICS,
        channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
    )
    summary = AsyncMock(return_value=[Topic(name="Introduction")])
    monkeypatch.setattr(manualsummary_module, "get_topic_list", summary)

    await cog._generate(job)

    assert summary.await_args.args[1] == ref
    assert job.delivery_chunks == ["1: Introduction"]


def test_generic_detail_displays_title_without_missing_channel():
    message = ManualSummary._format_detail(
        {"topic_name": "Introduction", "detail": "Details"},
        SimpleNamespace(title="Talk", channel_name=None),
    )

    assert message == "**Video:** Talk\n\n**Introduction**\nDetails"


@pytest.mark.asyncio
async def test_generic_terminal_failure_is_delivered_as_failure_notice(monkeypatch):
    sent = []

    class Channel:
        async def send(self, content, **kwargs):
            sent.append(content)

    channel = Channel()
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(get_channel=lambda _: channel)
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = ManualSummaryJob.model_construct(
        source="yt_dlp",
        video_id="hash",
        source_url="https://vimeo.com/123",
        operation=ManualSummaryOperation.TOPICS,
        channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
        retry_count=0,
        delivery_chunk_index=0,
    )

    await cog._fail(
        job,
        GenericMediaUnavailableError("Playlist URLs are unsupported."),
        permanent=True,
    )
    assert job.status is JobStatus.READY_TO_DELIVER

    await cog._deliver(job)

    assert sent == [
        "Could not summarize the requested media: Playlist URLs are unsupported."
    ]
    assert job.status is JobStatus.FAILED


@pytest.mark.asyncio
async def test_manual_generation_renews_owned_lease(monkeypatch):
    renewed = asyncio.Event()

    class Collection:
        async def update_one(self, query, values):
            assert query == {"_id": "job", "claimed_by": "worker"}
            assert values["$set"]["lease_expires_at"] > datetime.now(UTC)
            renewed.set()

    monkeypatch.setattr(
        manualsummary_module, "LEASE_DURATION", timedelta(milliseconds=30)
    )
    monkeypatch.setattr(
        manualsummary_module.ManualSummaryJob,
        "get_pymongo_collection",
        lambda: Collection(),
    )
    cog = ManualSummary.__new__(ManualSummary)
    cog.worker_id = "worker"

    async with cog._renew_generation_lease(SimpleNamespace(id="job")):
        await asyncio.wait_for(renewed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_twitch_audio_failure_is_logged_with_video_context(monkeypatch, caplog):
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(notebook_client=object())
    cog.settings = SimpleNamespace(source_retry_limit=5)
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
    cog.settings = SimpleNamespace()
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
    cog.settings = SimpleNamespace()
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "summary_function", "summary_result"),
    [
        (
            ManualSummaryOperation.TOPICS,
            "get_topic_list",
            [Topic(name="Introduction")],
        ),
        (
            ManualSummaryOperation.DETAIL_ONE,
            "get_topic_details",
            {"topic_name": "Introduction", "detail": "Details"},
        ),
        (
            ManualSummaryOperation.DETAIL_ALL,
            "get_topic_details_all",
            [{"topic_name": "Introduction", "detail": "Details"}],
        ),
    ],
)
async def test_manual_summary_retries_invalid_response_immediately(
    monkeypatch, operation, summary_function, summary_result
):
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(notebook_client=object())
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = ManualSummaryJob.model_construct(
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=operation,
        topic_index=0,
        channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
    )
    summary = AsyncMock(
        side_effect=[
            InvalidSummaryResponse("NotebookLM did not return JSON topic data"),
            summary_result,
        ]
    )
    monkeypatch.setattr(manualsummary_module, summary_function, summary)
    monkeypatch.setattr(
        manualsummary_module,
        "get_video_metadata",
        AsyncMock(return_value=None),
    )

    await cog._generate(job)

    assert summary.await_count == 2
    assert job.retry_count == 0
    assert job.status is JobStatus.DELIVERING


@pytest.mark.asyncio
async def test_manual_summary_queues_after_two_invalid_responses(monkeypatch):
    error = InvalidSummaryResponse("NotebookLM did not return JSON topic data")
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(notebook_client=object())
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = ManualSummaryJob.model_construct(
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=ManualSummaryOperation.TOPICS,
        channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
    )
    summary = AsyncMock(side_effect=[error, error])
    monkeypatch.setattr(manualsummary_module, "get_topic_list", summary)

    await cog._generate(job)

    assert summary.await_count == 2
    assert job.retry_count == 1
    assert job.status is JobStatus.QUEUED
    assert job.last_error == "NotebookLM did not return JSON topic data"
    assert job.next_attempt_at > datetime.now(UTC)
