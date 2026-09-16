from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vsummary.cogs.autosummary.autosummary import Autosummary
from vsummary.model.channel import JobStatus, SummaryJob
from vsummary.model.video import Topic
from vsummary.util.summarizer import InvalidSummaryResponse


class FakeJob:
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


@pytest.mark.asyncio
async def test_delivery_failure_keeps_persisted_summary_and_does_not_regenerate(
    monkeypatch,
):
    target = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("discord down")))
    cog = Autosummary.__new__(Autosummary)
    cog.bot = SimpleNamespace(get_channel=lambda _: target, notebook_client=object())
    cog.settings = SimpleNamespace(auto_summary_channel_id=123, delivery_retry_limit=3)
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

    claimed = await cog._claim_due_jobs()

    assert len(claimed) == 1
    assert claimed[0].video_id == "video"
    assert collection.find_one_and_update.await_count == 5


@pytest.mark.asyncio
async def test_claim_due_jobs_reclaims_an_expired_delivery_lease(monkeypatch):
    now = datetime.now(UTC)
    record = {
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
    cog.bot = SimpleNamespace(get_channel=lambda _: Target())
    cog.settings = SimpleNamespace(auto_summary_channel_id=123, delivery_retry_limit=3)
    job = FakeJob()
    job.status = JobStatus.DELIVERING
    job.summary_details = [Topic(name="Topic", detail="a" * 2_100)]

    await cog._deliver_for_job(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.delivery_chunk_index == 1
    first_chunk = sent[0]

    class WorkingTarget:
        async def send(self, chunk):
            sent.append(chunk)

    cog.bot = SimpleNamespace(get_channel=lambda _: WorkingTarget())
    await cog._deliver_for_job(job)

    assert sent.count(first_chunk) == 1
    assert job.status is JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_source_failure_uses_configured_retry_limit_in_notification():
    messages = []

    class Target:
        async def send(self, message):
            messages.append(message)

    cog = Autosummary.__new__(Autosummary)
    cog.bot = SimpleNamespace(get_channel=lambda _: Target())
    cog.settings = SimpleNamespace(auto_summary_channel_id=123, source_retry_limit=2)
    job = FakeJob()
    job.retry_count = 1

    await cog._handle_source_error(job, RuntimeError("no transcript"))

    assert job.status is JobStatus.FAILED
    assert "after 2 attempts" in messages[0]


@pytest.mark.asyncio
async def test_invalid_notebooklm_topic_response_is_retried():
    cog = Autosummary.__new__(Autosummary)
    cog.bot = SimpleNamespace(notebook_client=object())
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.summary_service = SimpleNamespace(
        summarize=AsyncMock(
            side_effect=InvalidSummaryResponse(
                "NotebookLM did not return JSON topic data"
            )
        )
    )
    job = FakeJob()

    assert not await cog._generate_for_job(job)

    assert job.retry_count == 1
    assert job.status is JobStatus.QUEUED
    assert job.last_error == "NotebookLM did not return JSON topic data"
    assert job.next_attempt_at > datetime.now(UTC)


@pytest.mark.asyncio
async def test_owned_job_writes_include_the_current_claimed_by(monkeypatch):
    job = SummaryJob.model_construct(
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

    assert await cog._save_item(job)
    assert collection.update_one.await_args.args[0] == {
        "_id": "job-id",
        "claimed_by": "worker",
    }
