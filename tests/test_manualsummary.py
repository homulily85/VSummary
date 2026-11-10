import asyncio
import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

import vsummary.cogs.manualsummary.manualsummary as manualsummary_module
from vsummary.cogs.manualsummary.manualsummary import ManualSummary
from vsummary.cogs.summarizer.summarizer import Summarizer
from vsummary.model.channel import JobStatus, ManualSummaryOperation, SummaryJob
from vsummary.model.video import Topic
from vsummary.services import summary_jobs
from vsummary.settings import RuntimeConfig
from vsummary.util.generic_media import GenericMediaUnavailableError
from vsummary.util.summarizer import InvalidSummaryResponse, PermanentSummaryError
from vsummary.util.twitch import TwitchAudioUnavailableError
from vsummary.util.video import parse_non_space_video_source
from vsummary.util.video_work import VideoWorkCoordinator


@pytest.mark.parametrize("command", [Summarizer.topics, Summarizer.detail])
def test_manual_slash_command_has_required_video_url_or_id(command):
    option = command.parameters[0]

    assert option.display_name == "video_url_or_id"
    assert option.required


def test_manual_worker_inherits_bot_settings():
    settings = RuntimeConfig(output_language="Vietnamese")
    cog = ManualSummary(SimpleNamespace(settings=settings), start_worker=False)

    assert cog.config.output_language == "Vietnamese"
    assert cog.worker_loop.seconds == 10


@pytest.mark.asyncio
async def test_manual_worker_logs_its_identity_when_ready(caplog):
    bot = SimpleNamespace(settings=None, wait_until_ready=AsyncMock())
    cog = ManualSummary(bot, start_worker=False)

    with caplog.at_level(logging.INFO, logger=manualsummary_module.__name__):
        await cog.before_worker_loop()

    bot.wait_until_ready.assert_awaited_once()
    assert "Manual summary worker ready" in caplog.text
    assert caplog.records[-1].context["worker_id"] == cog.worker_id


def make_manual_fallback_job(
    *,
    enabled=True,
    source="youtube",
    retry_count=1,
    operation=ManualSummaryOperation.TOPICS,
):
    cog = ManualSummary.__new__(ManualSummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace(
        source_retry_limit=2, youtube_yt_dlp_fallback_enabled=enabled
    )
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source=source,
        video_id="dQw4w9WgXcQ",
        title="Title",
        channel_name="Channel",
        operation=operation,
        topic_index=0,
        discord_channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
        retry_count=retry_count,
    )
    return cog, job


@pytest.mark.asyncio
async def test_manual_invalid_response_retry_budget_and_config_propagation(monkeypatch):
    cog, job = make_manual_fallback_job(retry_count=0)
    cog.settings = RuntimeConfig(
        invalid_summary_retries=2, output_language="Vietnamese"
    )
    error = InvalidSummaryResponse("bad JSON")
    summary = AsyncMock(side_effect=[error, error, [Topic(name="Intro")]])
    monkeypatch.setattr(manualsummary_module, "get_topic_list", summary)
    await cog._generate(job)
    assert job.status is JobStatus.READY_TO_DELIVER
    assert summary.await_count == 3
    assert summary.await_args.kwargs["config"].output_language == "Vietnamese"


@pytest.mark.asyncio
async def test_manual_job_backoff_uses_operational_config():
    cog, job = make_manual_fallback_job(retry_count=0, enabled=False)
    cog.settings = RuntimeConfig(
        source_retry_limit=5, retry_initial_delay_seconds=7, retry_multiplier=3
    )
    for delay in (7, 21):
        before = datetime.now(UTC)
        await cog._fail(job, RuntimeError("offline"))
        assert delay <= (job.next_attempt_at - before).total_seconds() < delay + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(ManualSummaryOperation))
async def test_manual_summary_queues_audio_fallback_after_native_retries(
    monkeypatch, operation
):
    cog, job = make_manual_fallback_job(operation=operation)
    name = {
        ManualSummaryOperation.TOPICS: "get_topic_list",
        ManualSummaryOperation.DETAIL_ONE: "get_topic_details",
        ManualSummaryOperation.DETAIL_ALL: "get_topic_details_all",
    }[operation]
    summary = AsyncMock(side_effect=RuntimeError("no transcript"))
    monkeypatch.setattr(manualsummary_module, name, summary)
    before = datetime.now(UTC)

    await cog._generate(job)

    assert job.status is JobStatus.QUEUED
    assert job.youtube_audio_fallback is True
    assert job.retry_count == 2
    assert before <= job.next_attempt_at <= datetime.now(UTC)
    assert SummaryJob.model_construct(**job.model_dump()).youtube_audio_fallback is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "source", "retry_count", "error"),
    [
        (False, "youtube", 1, RuntimeError("no transcript")),
        (True, "youtube", 0, RuntimeError("no transcript")),
        (True, "twitch", 1, RuntimeError("unavailable")),
        (True, "youtube", 1, PermanentSummaryError("invalid")),
        (True, "youtube", 1, IndexError("invalid topic index")),
    ],
)
async def test_manual_summary_excludes_ineligible_audio_fallbacks(
    monkeypatch, enabled, source, retry_count, error
):
    cog, job = make_manual_fallback_job(
        enabled=enabled, source=source, retry_count=retry_count
    )
    monkeypatch.setattr(
        manualsummary_module, "get_topic_list", AsyncMock(side_effect=error)
    )

    await cog._generate(job)

    assert job.youtube_audio_fallback is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "name", "result"),
    [
        (ManualSummaryOperation.TOPICS, "get_topic_list", [Topic(name="Intro")]),
        (
            ManualSummaryOperation.DETAIL_ONE,
            "get_topic_details",
            {"topic_name": "Intro", "detail": "Details"},
        ),
        (
            ManualSummaryOperation.DETAIL_ALL,
            "get_topic_details_all",
            [{"topic_name": "Intro", "detail": "Details"}],
        ),
    ],
)
async def test_manual_audio_fallback_resumes_each_operation(
    monkeypatch, operation, name, result
):
    cog, job = make_manual_fallback_job(operation=operation, retry_count=2)
    job.youtube_audio_fallback = True
    job = SummaryJob.model_construct(**job.model_dump())
    summary = AsyncMock(return_value=result)
    monkeypatch.setattr(manualsummary_module, name, summary)

    await cog._generate(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert summary.await_args.kwargs == {
        "use_youtube_audio": True,
        "config": cog.config,
    }
    assert summary.await_args.args[1].source == "youtube"
    assert job.title == "Title"
    assert job.channel_name == "Channel"
    assert job.delivery_chunks
    cog._save.assert_awaited_once_with(job)

    saved_chunks = list(job.delivery_chunks)
    target = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("discord down")))
    cog.bot.get_channel = lambda _: target
    await cog._deliver(job)
    assert job.status is JobStatus.READY_TO_DELIVER
    target.send = AsyncMock()
    job.status = JobStatus.DELIVERING  # The next lease claim selects delivery.
    job.claimed_by = "worker"
    await cog._process(job)
    assert job.status is JobStatus.COMPLETED
    assert job.delivery_chunks == saved_chunks
    summary.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", list(ManualSummaryOperation))
async def test_manual_audio_fallback_does_not_retry_invalid_responses(
    monkeypatch, operation
):
    cog, job = make_manual_fallback_job(operation=operation, retry_count=2)
    job.youtube_audio_fallback = True
    name = {
        ManualSummaryOperation.TOPICS: "get_topic_list",
        ManualSummaryOperation.DETAIL_ONE: "get_topic_details",
        ManualSummaryOperation.DETAIL_ALL: "get_topic_details_all",
    }[operation]
    summary = AsyncMock(side_effect=InvalidSummaryResponse("bad JSON"))
    monkeypatch.setattr(manualsummary_module, name, summary)

    await cog._generate(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.failure_notice
    assert job.retry_count == 3
    summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_pending_audio_fallback_stops_when_setting_is_disabled(
    monkeypatch,
):
    cog, job = make_manual_fallback_job(enabled=False, retry_count=2)
    job.youtube_audio_fallback = True
    summary = AsyncMock()
    monkeypatch.setattr(manualsummary_module, "get_topic_list", summary)

    await cog._generate(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.failure_notice
    assert job.retry_count == 2
    summary.assert_not_awaited()


@pytest.mark.asyncio
async def test_enabling_audio_fallback_does_not_revive_failed_manual_jobs():
    cog, job = make_manual_fallback_job(retry_count=2)
    job.status = JobStatus.FAILED
    cog._generate = AsyncMock()

    await cog._process(job)

    assert job.status is JobStatus.FAILED
    cog._generate.assert_not_awaited()
    cog._save.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_at", [0, 1])
async def test_legacy_topic_delivery_posts_plain_text(monkeypatch, start_at):
    sent = []

    class Channel:
        async def send(self, content, **kwargs):
            sent.append((content, kwargs))

    channel = Channel()
    cog = ManualSummary.__new__(ManualSummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        get_channel=lambda _: channel,
    )
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=ManualSummaryOperation.TOPICS,
        discord_channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
        status=JobStatus.DELIVERING,
        delivery_chunks=["1: Intro", "2: Outro"],
        delivery_chunk_index=start_at,
    )
    get_topic_list = AsyncMock(side_effect=AssertionError("must not build a picker"))
    monkeypatch.setattr(manualsummary_module, "get_topic_list", get_topic_list)

    await cog._deliver(job)

    assert (
        sent
        == [
            ("Here are the topics mentioned in the video:\n1: Intro", {}),
            ("2: Outro", {}),
        ][start_at:]
    )
    assert job.status is JobStatus.COMPLETED
    get_topic_list.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_topic_delivery_preserves_a_full_length_chunk():
    cog, job = make_manual_fallback_job()
    chunk = "1: " + "x" * 1997
    job.delivery_chunks = [chunk]
    channel = SimpleNamespace(send=AsyncMock())
    cog.bot.get_channel = lambda _: channel

    assert await cog._deliver(job)

    channel.send.assert_awaited_once_with(chunk)


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 24])
async def test_topic_pages_link_to_source_and_keep_topic_indices(monkeypatch, count):
    cog, job = make_manual_fallback_job()
    job.source = "yt_dlp"
    job.source_url = "https://vimeo.com/123"
    summary = AsyncMock(
        return_value=[Topic(name=f"Mục {i}") for i in range(1, count + 1)]
    )
    monkeypatch.setattr(manualsummary_module, "get_topic_list", summary)
    channel = SimpleNamespace(send=AsyncMock())
    cog.bot.get_channel = lambda _: channel

    await cog._generate(job)
    assert job.topic_count == count
    job = SummaryJob.model_construct(**job.model_dump())
    assert await cog._deliver(job)

    pages = [call.kwargs["embed"] for call in channel.send.await_args_list]
    topic_rows = []
    for index, page in enumerate(pages, start=1):
        assert page.title == "Topics · Title"
        assert page.url == job.source_url
        assert page.author.name == "Channel"
        assert page.footer.text.startswith(f"Page {index}/{len(pages)} · {count} topic")
        assert (
            "/detail video_url_or_id:<URL or ID> topic_index:<number>"
            in page.description
        )
        rows = [row for row in page.description.splitlines() if row.startswith("**")]
        assert len(rows) <= 10
        topic_rows.extend(rows)
    assert len(pages) == (count + 9) // 10
    assert topic_rows == [f"**{i}.** Mục {i}" for i in range(1, count + 1)]
    summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_topic_embed_uses_source_label_when_metadata_is_missing(monkeypatch):
    cog, job = make_manual_fallback_job(source="twitch")
    job.video_id = "12345"
    job.title = job.channel_name = None
    monkeypatch.setattr(
        manualsummary_module,
        "get_topic_list",
        AsyncMock(return_value=[Topic(name="Intro")]),
    )
    channel = SimpleNamespace(send=AsyncMock())
    cog.bot.get_channel = lambda _: channel

    await cog._generate(job)
    assert await cog._deliver(job)

    embed = channel.send.await_args.kwargs["embed"]
    assert embed.title == "Topics · Twitch VOD"
    assert embed.url == "https://www.twitch.tv/videos/12345"
    assert embed.author.name is None


@pytest.mark.asyncio
@pytest.mark.parametrize("long_url", [False, True])
async def test_topic_text_fallback_keeps_pages_safe(monkeypatch, long_url):
    cog, job = make_manual_fallback_job(source="yt_dlp")
    job.source_url = "https://example.org/" + ("x" * 2000 if long_url else "video")
    job.title = "😀" * 300
    job.channel_name = "**News**" * 70
    name = "**Mục** @everyone\n" + "😀" * 80
    monkeypatch.setattr(
        manualsummary_module,
        "get_topic_list",
        AsyncMock(return_value=[Topic(name=name) for _ in range(10)]),
    )
    member = object()
    channel = SimpleNamespace(
        send=AsyncMock(),
        guild=SimpleNamespace(me=member),
        permissions_for=lambda _: discord.Permissions(send_messages=True),
    )
    cog.bot.get_channel = lambda _: channel

    await cog._generate(job)
    assert await cog._deliver(job)

    calls = channel.send.await_args_list
    assert len(calls) > 1
    for index, call in enumerate(calls, start=1):
        body = call.args[0]
        assert len(body.encode("utf-16-le")) // 2 <= 2000
        assert f"Page {index}/{len(calls)} · 10 topics" in body
        assert "/detail video_url_or_id:<URL or ID> topic_index:<number>" in body
        assert r"\*\*Mục\*\*" + " @\u200beveryone " in body
        assert (job.source_url in body) is not long_url
        assert "embed" not in call.kwargs
        assert call.kwargs["allowed_mentions"].to_dict() == {"parse": []}
    assert sum(call.args[0].count(r"\*\*Mục\*\*") for call in calls) == 10


@pytest.mark.asyncio
async def test_topic_delivery_falls_back_if_discord_rejects_embed(monkeypatch):
    cog, job = make_manual_fallback_job()
    monkeypatch.setattr(
        manualsummary_module,
        "get_topic_list",
        AsyncMock(return_value=[Topic(name="Intro")]),
    )
    missing_permission = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013}
    )
    channel = SimpleNamespace(send=AsyncMock(side_effect=[missing_permission, None]))
    cog.bot.get_channel = lambda _: channel

    await cog._generate(job)
    assert await cog._deliver(job)

    embed_attempt, text_attempt = channel.send.await_args_list
    assert embed_attempt.kwargs["embed"].title == "Topics · Title"
    assert "**1.** Intro" in text_attempt.args[0]
    assert job.delivery_chunk_index == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("lose_lease", [False, True])
async def test_topic_delivery_checkpoints_each_page_and_resumes(
    monkeypatch, lose_lease
):
    cog, job = make_manual_fallback_job()
    summary = AsyncMock(return_value=[Topic(name=f"Topic {i}") for i in range(24)])
    monkeypatch.setattr(manualsummary_module, "get_topic_list", summary)
    await cog._generate(job)
    progress = []
    attempts = []

    async def save(job):
        progress.append(job.delivery_chunk_index)
        return not (lose_lease and job.delivery_chunk_index == 1)

    async def send(_, *, embed, **kwargs):
        page = embed.footer.text.split(" · ", 1)[0]
        attempts.append(page)
        assert progress[-1] == int(page.split("/")[0].split()[-1]) - 1
        if attempts == ["Page 1/3", "Page 2/3"]:
            raise RuntimeError("Discord unavailable")

    cog._save = save
    cog.bot.get_channel = lambda _: SimpleNamespace(send=send)
    assert not await cog._deliver(job)
    assert job.delivery_chunk_index == 1
    if lose_lease:
        assert attempts == ["Page 1/3"]
        assert progress == [0, 1]
    else:
        assert job.status is JobStatus.READY_TO_DELIVER
        job = SummaryJob.model_construct(**job.model_dump())
        job.claimed_by = cog.worker_id
        assert await cog._deliver(job)
        assert attempts == ["Page 1/3", "Page 2/3", "Page 2/3", "Page 3/3"]
        assert job.status is JobStatus.COMPLETED
    summary.assert_awaited_once()


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
    cog.settings = None
    cog.worker_id = "worker"
    monkeypatch.setattr(manualsummary_module, "SummaryJob", FakeManualSummaryJob)
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

    monkeypatch.setattr(manualsummary_module, "SummaryJob", FakeJob)
    inspect = AsyncMock(return_value=SimpleNamespace(title="Talk", channel_name=None))
    monkeypatch.setattr(manualsummary_module, "inspect_generic_media", inspect)
    ref = parse_non_space_video_source("https://vimeo.com/123")
    cog = ManualSummary.__new__(ManualSummary)
    cog.worker_id = "worker"
    cog.settings = None

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
    cog.worker_id = "worker"
    cog.settings = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace()
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    ref = parse_non_space_video_source("https://vimeo.com/123")
    job = SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source=ref.source,
        video_id=ref.video_id,
        source_url=ref.url,
        operation=ManualSummaryOperation.TOPICS,
        discord_channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
    )
    summary = AsyncMock(return_value=[Topic(name="Introduction")])
    monkeypatch.setattr(manualsummary_module, "get_topic_list", summary)

    await cog._generate(job)

    assert summary.await_args.args[1] == ref
    assert job.delivery_chunks == ["**1.** Introduction"]


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
    cog.worker_id = "worker"
    cog.settings = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        get_channel=lambda _: channel,
    )
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source="yt_dlp",
        video_id="hash",
        source_url="https://vimeo.com/123",
        operation=ManualSummaryOperation.TOPICS,
        discord_channel_id=123,
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
            assert query == {"_id": "job", "job_type": "manual", "claimed_by": "worker"}
            assert values["$set"]["lease_expires_at"] > datetime.now(UTC)
            renewed.set()
            return SimpleNamespace(matched_count=1)

    monkeypatch.setattr(
        manualsummary_module.SummaryJob,
        "get_pymongo_collection",
        lambda: Collection(),
    )
    cog = ManualSummary.__new__(ManualSummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.worker_id = "worker"

    monkeypatch.setattr(summary_jobs, "_JOB_LEASE_DURATION", timedelta(seconds=0.03))

    async with cog.jobs.maintain_lease(SimpleNamespace(id="job")):
        await asyncio.wait_for(renewed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_twitch_audio_failure_is_logged_with_video_context(monkeypatch, caplog):
    cog = ManualSummary.__new__(ManualSummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source="twitch",
        video_id="123456789",
        operation=ManualSummaryOperation.TOPICS,
        discord_channel_id=123,
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
    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.failure_notice
    assert job.last_error == str(error)


@pytest.mark.asyncio
async def test_detail_includes_video_and_channel_metadata(monkeypatch):
    cog = ManualSummary.__new__(ManualSummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace()
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=ManualSummaryOperation.DETAIL_ONE,
        topic_index=0,
        discord_channel_id=123,
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
    cog.worker_id = "worker"
    cog.settings = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace()
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=ManualSummaryOperation.DETAIL_ALL,
        discord_channel_id=123,
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
    cog.worker_id = "worker"
    cog.settings = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=operation,
        topic_index=0,
        discord_channel_id=123,
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
    assert job.status is JobStatus.READY_TO_DELIVER


@pytest.mark.asyncio
async def test_manual_summary_queues_after_two_invalid_responses(monkeypatch):
    error = InvalidSummaryResponse("NotebookLM did not return JSON topic data")
    cog = ManualSummary.__new__(ManualSummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=ManualSummaryOperation.TOPICS,
        discord_channel_id=123,
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


@pytest.mark.asyncio
async def test_manual_delivery_checkpoints_and_stops_on_lost_lease():
    cog, job = make_manual_fallback_job()
    target = SimpleNamespace(send=AsyncMock())
    cog.bot.get_channel = lambda _: target
    job.operation = ManualSummaryOperation.DETAIL_ALL
    job.delivery_chunks = ["first", "second", "third"]
    job.status = JobStatus.DELIVERING
    cog._save = AsyncMock(side_effect=[True, False])
    await cog._deliver(job)
    assert [call.args[0] for call in target.send.await_args_list] == [
        "first",
        "second",
        "third",
    ]
    assert job.delivery_chunk_index == 3
    assert cog._save.await_count == 2


@pytest.mark.asyncio
async def test_manual_lost_lease_after_generation_does_not_deliver(monkeypatch):
    cog, job = make_manual_fallback_job()
    job.status = JobStatus.GENERATING
    job.claimed_by = "worker"
    cog._save = AsyncMock(return_value=False)
    cog._deliver = AsyncMock()
    monkeypatch.setattr(
        manualsummary_module,
        "get_topic_list",
        AsyncMock(return_value=[Topic(name="Intro")]),
    )
    await cog._process(job)
    cog._deliver.assert_not_awaited()
