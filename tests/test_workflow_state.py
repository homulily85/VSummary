from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import vsummary.cogs.autosummary.autosummary as autosummary_module
from vsummary.cogs.autosummary.autosummary import Autosummary
from vsummary.model.channel import JobStatus, SummaryJob
from vsummary.model.video import Topic
from vsummary.settings import RuntimeConfig
from vsummary.util.holodex import TransientHolodexError
from vsummary.util.summarizer import (
    InvalidSummaryResponse,
    PermanentSummaryError,
    TransientSummaryError,
)
from vsummary.util.video_work import VideoWorkCoordinator


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", [None, "invalid", 123])
async def test_auto_jobs_wait_until_destination_can_be_resolved(channel_id):
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = SimpleNamespace(auto_summary_channel_id=channel_id)
    cog.bot = SimpleNamespace(get_channel=Mock(return_value=None))
    job = object()
    cog._claim_due_jobs = AsyncMock(return_value=[job])
    cog._process_pending_video = AsyncMock()

    await cog.process_due_videos()

    cog._claim_due_jobs.assert_not_awaited()
    cog._process_pending_video.assert_not_awaited()

    cog.settings.auto_summary_channel_id = 123
    cog.bot.get_channel.return_value = object()

    await cog.process_due_videos()

    cog._claim_due_jobs.assert_awaited_once()
    cog._process_pending_video.assert_awaited_once_with(job)


class FakeJob:
    source = "youtube"
    failure_notice = False
    id = None
    youtube_audio_fallback = False
    delivery_chunks = ()
    delivery_chunk_index = 0
    status = JobStatus.QUEUED

    def __init__(self):
        self.video_id = "video"
        self.channel_id = "channel"
        self.channel_name = "Channel"
        self.title = "Title"
        self.available_at = datetime.now(UTC)
        self.next_attempt_at = datetime.now(UTC)
        self.retry_count = 0
        self.delivery_retry_count = 0
        self.summary_details = []
        self.last_error = None
        self.saved = 0

    async def save(self):
        self.saved += 1


@pytest.fixture(autouse=True)
def fake_job_persistence(monkeypatch):
    original = Autosummary._save_item

    async def save(cog, job):
        if isinstance(job, FakeJob):
            await job.save()
            return True
        return await original(cog, job)

    monkeypatch.setattr(Autosummary, "_save_item", save)


def make_youtube_fallback_job(*, enabled=True, retry_count=1):
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace(
        source_retry_limit=2,
        delivery_retry_limit=3,
        auto_summary_channel_id=123,
        youtube_yt_dlp_fallback_enabled=enabled,
    )
    cog.holodex = SimpleNamespace(get_video_topic=AsyncMock(return_value=None))
    cog.summary_service = SimpleNamespace(
        summarize=AsyncMock(side_effect=TransientSummaryError("no transcript"))
    )
    cog._save_item = AsyncMock(return_value=True)
    job = SummaryJob.model_construct(
        job_type="auto",
        video_id="dQw4w9WgXcQ",
        channel_id="channel",
        channel_name="Channel",
        title="Title",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
        retry_count=retry_count,
    )
    return cog, job


@pytest.mark.asyncio
async def test_auto_job_backoff_and_lease_use_operational_config():
    cog, job = make_youtube_fallback_job(enabled=False, retry_count=0)
    cog.settings = RuntimeConfig(
        source_retry_limit=5,
        retry_initial_delay_seconds=7,
        retry_multiplier=3,
        job_lease_seconds=123,
    )
    assert cog._lease_duration().total_seconds() == 123
    for delay in (7, 21):
        before = datetime.now(UTC)
        await cog._handle_source_error(job, RuntimeError("offline"))
        assert delay <= (job.next_attempt_at - before).total_seconds() < delay + 1


@pytest.mark.asyncio
async def test_auto_invalid_response_retry_budget_comes_from_config():
    cog, job = make_youtube_fallback_job(retry_count=0)
    cog.settings = RuntimeConfig(invalid_summary_retries=2)
    error = InvalidSummaryResponse("bad JSON")
    cog.summary_service.summarize.side_effect = [
        error,
        error,
        [Topic(name="Intro", detail="Details")],
    ]
    assert await cog._generate_for_job(job)
    assert cog.summary_service.summarize.await_count == 3
    assert job.retry_count == 0


@pytest.mark.asyncio
async def test_auto_summary_queues_one_persisted_audio_fallback_after_native_retries():
    cog, job = make_youtube_fallback_job()
    before = datetime.now(UTC)

    assert not await cog._generate_for_job(job)

    assert job.status is JobStatus.QUEUED
    assert job.youtube_audio_fallback is True
    assert job.retry_count == 2
    assert before <= job.next_attempt_at <= datetime.now(UTC)
    assert not job.failure_notice
    cog._save_item.assert_awaited_once_with(job)
    assert SummaryJob.model_construct(**job.model_dump()).youtube_audio_fallback is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("enabled", "retry_count", "permanent", "expected_status"),
    [
        (False, 1, False, JobStatus.READY_TO_DELIVER),
        (True, 0, False, JobStatus.QUEUED),
        (True, 1, True, JobStatus.READY_TO_DELIVER),
    ],
)
async def test_auto_summary_only_schedules_audio_fallback_for_eligible_failures(
    enabled,
    retry_count,
    permanent,
    expected_status,
):
    cog, job = make_youtube_fallback_job(enabled=enabled, retry_count=retry_count)
    if permanent:
        cog.summary_service.summarize.side_effect = PermanentSummaryError("invalid")

    assert not await cog._generate_for_job(job)

    assert job.status is expected_status
    assert job.youtube_audio_fallback is False


@pytest.mark.asyncio
async def test_holodex_failure_does_not_trigger_youtube_audio_fallback():
    cog, job = make_youtube_fallback_job()
    cog._refresh_video_topic = AsyncMock(side_effect=TransientHolodexError("offline"))

    assert not await cog._generate_for_job(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.failure_notice
    assert job.youtube_audio_fallback is False
    cog.summary_service.summarize.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_audio_fallback_resumes_and_delivery_retries_reuse_its_output():
    cog, job = make_youtube_fallback_job(retry_count=2)
    job.youtube_audio_fallback = True
    job = SummaryJob.model_construct(**job.model_dump())
    cog.summary_service.summarize = AsyncMock(
        return_value=[Topic(name="Intro", detail="Details")]
    )
    target = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("discord down")))
    cog.bot.get_channel = lambda _: target

    await cog._process_pending_video(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.summary_details == [Topic(name="Intro", detail="Details")]
    assert cog.summary_service.summarize.await_args.kwargs == {
        "use_youtube_audio": True
    }
    assert cog.summary_service.summarize.await_args.args[0].source == "youtube"
    target.send = AsyncMock()
    await cog._process_pending_video(job)
    assert job.status is JobStatus.COMPLETED
    cog.summary_service.summarize.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        InvalidSummaryResponse("bad JSON"),
        TransientSummaryError("upload failed"),
        PermanentSummaryError("too large"),
    ],
)
async def test_auto_audio_fallback_failure_is_terminal_without_generation_retries(
    error,
):
    cog, job = make_youtube_fallback_job(retry_count=2)
    job.youtube_audio_fallback = True
    cog.summary_service.summarize.side_effect = error

    assert not await cog._generate_for_job(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.failure_notice
    assert job.retry_count == 3
    cog.summary_service.summarize.assert_awaited_once()
    assert job.failure_notice
    assert job.delivery_chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("last_error", ["summary source unavailable", None])
async def test_auto_pending_audio_fallback_stops_when_setting_is_disabled(last_error):
    cog, job = make_youtube_fallback_job(enabled=False, retry_count=2)
    job.youtube_audio_fallback = True
    job.last_error = last_error

    assert not await cog._generate_for_job(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.failure_notice
    assert job.retry_count == 2
    assert job.last_error == "YouTube audio fallback is disabled."
    cog.summary_service.summarize.assert_not_awaited()
    assert job.failure_notice
    assert job.delivery_chunks


@pytest.mark.asyncio
async def test_auto_terminal_failure_notice_mentions_the_audio_fallback():
    cog, job = make_youtube_fallback_job(retry_count=3)
    job.youtube_audio_fallback = True
    target = SimpleNamespace(send=AsyncMock())
    cog.bot.get_channel = lambda _: target

    message = cog._failure_message(job)
    assert "2 normal attempts" in message
    assert "one yt-dlp audio fallback attempt" in message
    assert "no transcript could be fetched" not in message


@pytest.mark.asyncio
async def test_enabling_audio_fallback_does_not_revive_failed_auto_jobs():
    cog, job = make_youtube_fallback_job(retry_count=2)
    job.status = JobStatus.FAILED

    await cog._process_pending_video(job)

    assert job.status is JobStatus.FAILED
    cog.summary_service.summarize.assert_not_awaited()
    cog._save_item.assert_not_awaited()


@pytest.mark.asyncio
async def test_delivery_failure_keeps_persisted_summary_and_does_not_regenerate(
    monkeypatch,
):
    target = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("discord down")))
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        get_channel=lambda _: target,
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace(auto_summary_channel_id=123, delivery_retry_limit=3)
    cog.holodex = SimpleNamespace(get_video_topic=AsyncMock(return_value=None))
    cog.summary_service = SimpleNamespace(
        summarize=AsyncMock(return_value=[Topic(name="Intro", detail="Details")])
    )
    job = FakeJob()

    await cog._process_pending_video(job)

    assert job.summary_details == [Topic(name="Intro", detail="Details")]
    assert job.status is JobStatus.READY_TO_DELIVER
    cog.summary_service.summarize.assert_awaited_once()
    assert job.last_error == "discord down"

    monkeypatch.setattr(target, "send", AsyncMock())
    await cog._process_pending_video(job)

    cog.summary_service.summarize.assert_awaited_once()
    assert job.status is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_claim_due_jobs_uses_current_beanie_collection_api(monkeypatch):
    record = {
        "job_type": "auto",
        "video_id": "video",
        "channel_id": "channel",
        "channel_name": "Channel",
        "title": "Title",
        "available_at": datetime.now(UTC),
        "next_attempt_at": datetime.now(UTC),
        "status": JobStatus.GENERATING.value,
        "claimed_by": "worker",
        "claimed_at": datetime.now(UTC),
    }
    collection = SimpleNamespace(
        find_one_and_update=AsyncMock(side_effect=[record, None, None, None, None])
    )
    monkeypatch.setattr(
        SummaryJob,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )

    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.worker_id = "worker"

    claimed = await cog._claim_due_jobs()

    assert len(claimed) == 1
    assert claimed[0].video_id == "video"
    assert collection.find_one_and_update.await_count == 5


@pytest.mark.asyncio
async def test_claim_due_jobs_reclaims_an_expired_delivery_lease(monkeypatch):
    now = datetime.now(UTC)
    record = {
        "job_type": "auto",
        "video_id": "video",
        "channel_id": "channel",
        "channel_name": "Channel",
        "title": "Title",
        "available_at": now,
        "next_attempt_at": now,
        "status": JobStatus.DELIVERING.value,
        "claimed_by": "old-worker",
        "claimed_at": now - timedelta(hours=1),
        "lease_expires_at": now - timedelta(minutes=1),
    }
    collection = SimpleNamespace(
        find_one_and_update=AsyncMock(side_effect=[None, None, None, record, None])
    )
    monkeypatch.setattr(
        SummaryJob,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )

    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.worker_id = "new-worker"

    claimed = await cog._claim_due_jobs()

    assert [job.status for job in claimed] == [JobStatus.DELIVERING]
    reclaim_query = collection.find_one_and_update.await_args_list[3].args[0]
    assert reclaim_query["status"] == JobStatus.DELIVERING.value
    assert reclaim_query["$or"][0]["lease_expires_at"]["$lte"] <= datetime.now(UTC)


@pytest.mark.asyncio
async def test_delivery_retry_resumes_after_the_last_confirmed_chunk():
    sent = []

    class Target:
        async def send(self, chunk):
            sent.append(chunk)
            if len(sent) == 2:
                raise RuntimeError("discord down")

    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        get_channel=lambda _: Target(),
    )
    cog.settings = SimpleNamespace(auto_summary_channel_id=123, delivery_retry_limit=3)
    job = FakeJob()
    job.status = JobStatus.DELIVERING
    job.summary_details = [Topic(name="Topic", detail="a" * 2_100)]

    await cog._deliver_for_job(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.delivery_chunk_index == 1
    assert job.saved == 2  # Initial state and error progress.
    first_chunk = sent[0]

    class WorkingTarget:
        async def send(self, chunk):
            sent.append(chunk)

    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        get_channel=lambda _: WorkingTarget(),
    )
    await cog._deliver_for_job(job)

    assert sent.count(first_chunk) == 1
    assert job.status is JobStatus.COMPLETED
    assert job.delivery_chunk_index == len(job.delivery_chunks)
    assert job.saved == 4  # Retry initial state and completion add two saves.


@pytest.mark.asyncio
async def test_source_failure_uses_configured_retry_limit_in_notification():
    messages = []

    class Target:
        async def send(self, message):
            messages.append(message)

    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        get_channel=lambda _: Target(),
    )
    cog.settings = SimpleNamespace(auto_summary_channel_id=123, source_retry_limit=2)
    job = FakeJob()
    job.retry_count = 1

    await cog._handle_source_error(job, RuntimeError("no transcript"))

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.failure_notice
    assert "after 2 attempts" in job.delivery_chunks[0]
    assert not messages


@pytest.mark.asyncio
async def test_invalid_notebooklm_topic_response_retries_immediately_then_succeeds():
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.holodex = SimpleNamespace(get_video_topic=AsyncMock(return_value=None))
    cog.summary_service = SimpleNamespace(
        summarize=AsyncMock(
            side_effect=[
                InvalidSummaryResponse("NotebookLM did not return JSON topic data"),
                [Topic(name="Intro", detail="Details")],
            ]
        )
    )
    job = FakeJob()

    assert await cog._generate_for_job(job)

    assert cog.summary_service.summarize.await_count == 2
    assert job.retry_count == 0
    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.last_error is None


@pytest.mark.asyncio
async def test_invalid_notebooklm_topic_response_queues_after_immediate_retry():
    error = InvalidSummaryResponse("NotebookLM did not return JSON topic data")
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.holodex = SimpleNamespace(get_video_topic=AsyncMock(return_value=None))
    cog.summary_service = SimpleNamespace(
        summarize=AsyncMock(side_effect=[error, error])
    )
    job = FakeJob()

    assert not await cog._generate_for_job(job)

    assert cog.summary_service.summarize.await_count == 2
    assert job.retry_count == 1
    assert job.status is JobStatus.QUEUED
    assert job.last_error == "NotebookLM did not return JSON topic data"
    assert job.next_attempt_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_generation_cancels_when_refreshed_topic_is_ignored(monkeypatch):
    published_at = datetime(2026, 9, 20, tzinfo=UTC)

    monkeypatch.setattr(
        autosummary_module, "policy_ignores_video", AsyncMock(return_value=True)
    )
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.holodex = SimpleNamespace(get_video_topic=AsyncMock(return_value="SHORTS"))
    cog.summary_service = SimpleNamespace(summarize=AsyncMock())
    job = FakeJob()
    job.available_at = published_at

    assert not await cog._generate_for_job(job)

    cog.holodex.get_video_topic.assert_awaited_once_with(job.video_id)
    cog.summary_service.summarize.assert_not_awaited()
    assert job.status is JobStatus.CANCELLED
    assert job.retry_count == 0
    assert job.saved == 1


@pytest.mark.asyncio
async def test_cancellation_persists_and_releases_claimed_job(monkeypatch):
    published_at = datetime(2026, 9, 20, tzinfo=UTC)

    monkeypatch.setattr(
        autosummary_module, "policy_ignores_video", AsyncMock(return_value=True)
    )
    collection = SimpleNamespace(
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1))
    )
    monkeypatch.setattr(
        SummaryJob,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    job = SummaryJob.model_construct(
        job_type="auto",
        id="job-id",
        video_id="video",
        channel_id="channel",
        channel_name="Channel",
        title="Title",
        available_at=published_at,
        next_attempt_at=datetime.now(UTC),
        status=JobStatus.GENERATING,
        claimed_by="worker",
        claimed_at=datetime.now(UTC),
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=15),
    )
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.worker_id = "worker"
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.holodex = SimpleNamespace(get_video_topic=AsyncMock(return_value="shorts"))
    cog.summary_service = SimpleNamespace(summarize=AsyncMock())

    assert not await cog._generate_for_job(job)

    query, update = collection.update_one.await_args.args
    assert query == {"_id": "job-id", "job_type": "auto", "claimed_by": "worker"}
    assert update["$set"]["status"] == JobStatus.CANCELLED
    assert update["$set"]["claimed_by"] is None
    assert update["$set"]["lease_expires_at"] is None
    cog.summary_service.summarize.assert_not_awaited()


@pytest.mark.asyncio
async def test_generation_uses_policy_effective_at_video_publication(monkeypatch):
    published_at = datetime(2026, 9, 20, tzinfo=UTC)

    monkeypatch.setattr(
        autosummary_module, "policy_ignores_video", AsyncMock(return_value=False)
    )
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.holodex = SimpleNamespace(get_video_topic=AsyncMock(return_value="shorts"))
    cog.summary_service = SimpleNamespace(
        summarize=AsyncMock(return_value=[Topic(name="Intro", detail="Details")])
    )
    job = FakeJob()
    job.available_at = published_at

    assert await cog._generate_for_job(job)
    cog.summary_service.summarize.assert_awaited_once()
    assert job.status is JobStatus.READY_TO_DELIVER


@pytest.mark.asyncio
async def test_generation_continues_after_two_failed_topic_refreshes():
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.holodex = SimpleNamespace(
        get_video_topic=AsyncMock(
            side_effect=[
                TransientHolodexError("unavailable"),
                TransientHolodexError("still unavailable"),
            ]
        )
    )
    cog.summary_service = SimpleNamespace(
        summarize=AsyncMock(return_value=[Topic(name="Intro", detail="Details")])
    )
    job = FakeJob()

    assert await cog._generate_for_job(job)

    assert cog.holodex.get_video_topic.await_count == 2
    cog.summary_service.summarize.assert_awaited_once()
    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.retry_count == 0


@pytest.mark.asyncio
async def test_second_topic_refresh_can_still_cancel_ignored_video(monkeypatch):
    published_at = datetime(2026, 9, 20, tzinfo=UTC)

    monkeypatch.setattr(
        autosummary_module, "policy_ignores_video", AsyncMock(return_value=True)
    )
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.holodex = SimpleNamespace(
        get_video_topic=AsyncMock(
            side_effect=[TransientHolodexError("unavailable"), "shorts"]
        )
    )
    cog.summary_service = SimpleNamespace(summarize=AsyncMock())
    job = FakeJob()
    job.available_at = published_at

    assert not await cog._generate_for_job(job)

    assert cog.holodex.get_video_topic.await_count == 2
    cog.summary_service.summarize.assert_not_awaited()
    assert job.status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_missing_topic_is_retried_before_generation(monkeypatch):
    published_at = datetime(2026, 9, 20, tzinfo=UTC)

    monkeypatch.setattr(
        autosummary_module, "policy_ignores_video", AsyncMock(return_value=True)
    )
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.holodex = SimpleNamespace(
        get_video_topic=AsyncMock(side_effect=[None, "shorts"])
    )
    cog.summary_service = SimpleNamespace(summarize=AsyncMock())
    job = FakeJob()
    job.available_at = published_at

    assert not await cog._generate_for_job(job)

    assert cog.holodex.get_video_topic.await_count == 2
    cog.summary_service.summarize.assert_not_awaited()
    assert job.status is JobStatus.CANCELLED


@pytest.mark.asyncio
async def test_owned_job_writes_include_the_current_claimed_by(monkeypatch):
    job = SummaryJob.model_construct(
        job_type="auto",
        id="job-id",
        video_id="video",
        channel_id="channel",
        channel_name="Channel",
        title="Title",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
        status=JobStatus.GENERATING,
        claimed_by="worker",
    )
    collection = SimpleNamespace(
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=1))
    )
    monkeypatch.setattr(
        SummaryJob,
        "get_pymongo_collection",
        classmethod(lambda cls: collection),
    )
    cog = Autosummary.__new__(Autosummary)
    cog.worker_id = "worker"
    cog.settings = None
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.worker_id = "worker"

    assert await cog._save_item(job)
    assert collection.update_one.await_args.args[0] == {
        "_id": "job-id",
        "job_type": "auto",
        "claimed_by": "worker",
    }
