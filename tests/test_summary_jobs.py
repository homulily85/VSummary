"""Shared worker invariants independent of Discord Cog wiring."""

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from vsummary.model.channel import JobStatus, SummaryJob
from vsummary.services import summary_jobs
from vsummary.services.summary_jobs import LeaseLostError, SummaryJobService
from vsummary.settings import RuntimeConfig


@pytest.fixture(params=["auto", "manual"])
def service(request):
    return SummaryJobService(
        request.param,
        "worker",
        RuntimeConfig(),
        logging.getLogger(__name__),
    )


@pytest.fixture
def short_lease(monkeypatch):
    monkeypatch.setattr(summary_jobs, "_JOB_LEASE_DURATION", timedelta(seconds=0.03))


@pytest.mark.asyncio
async def test_claim_queries_recover_missing_leases_and_isolate_job_types(
    service, monkeypatch
):
    collection = SimpleNamespace(find_one_and_update=AsyncMock(return_value=None))
    monkeypatch.setattr(SummaryJob, "get_pymongo_collection", lambda: collection)
    assert await service.claim_due_jobs() == []
    calls = collection.find_one_and_update.call_args_list
    assert len(calls) == 4
    for call in calls:
        assert call.args[0]["job_type"] == service.job_type
        claim = call.args[1]["$set"]
        assert claim["claimed_by"] == "worker"
        assert claim["lease_expires_at"] - claim["claimed_at"] == timedelta(seconds=900)
    for call in calls[2:]:
        assert {"lease_expires_at": None} in call.args[0]["$or"]
        assert {"lease_expires_at": {"$exists": False}} in call.args[0]["$or"]


@pytest.mark.asyncio
@pytest.mark.parametrize("renewal", [False, RuntimeError("database offline")])
async def test_lost_lease_interrupts_processing(service, renewal, short_lease):
    service.renew = AsyncMock(
        side_effect=renewal if isinstance(renewal, Exception) else None,
        return_value=False,
    )
    finished = False
    with pytest.raises(LeaseLostError):
        async with service.maintain_lease(SimpleNamespace(id="job")):
            await asyncio.sleep(1)
            finished = True
    assert not finished
    count = service.renew.await_count
    await asyncio.sleep(0.02)
    assert service.renew.await_count == count


@pytest.mark.asyncio
async def test_external_cancellation_is_preserved(service):
    service.renew = AsyncMock(return_value=True)
    entered = asyncio.Event()

    async def work():
        async with service.maintain_lease(SimpleNamespace(id="job")):
            entered.set()
            await asyncio.sleep(1)

    task = asyncio.create_task(work())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()


@pytest.mark.asyncio
async def test_delivery_renews_and_checkpoints_only_on_error_or_completion(
    service, short_lease
):
    job = SimpleNamespace(
        id="job",
        delivery_chunks=["one", "two", "three"],
        delivery_chunk_index=0,
        failure_notice=False,
        delivery_retry_count=0,
    )
    saved = []

    async def save(job):
        saved.append(job.delivery_chunk_index)
        return True

    attempts = []

    async def send(channel, chunk):
        attempts.append(chunk)
        await asyncio.sleep(0.025)
        if chunk == "two" and attempts == ["one", "two"]:
            raise RuntimeError("Discord offline")

    service.renew = AsyncMock(return_value=True)
    async with service.maintain_lease(job):
        assert not await service.deliver(
            job, AsyncMock(return_value=object()), send, save
        )
    assert service.renew.await_count >= 2
    assert saved == [0, 1]
    assert job.status == JobStatus.READY_TO_DELIVER
    async with service.maintain_lease(job):
        assert await service.deliver(job, AsyncMock(return_value=object()), send, save)
    assert saved == [0, 1, 1, 3]
    assert attempts == ["one", "two", "two", "three"]
    assert job.status == JobStatus.COMPLETED


@pytest.mark.asyncio
async def test_delivery_logs_discord_rejection_with_job_context(service, caplog):
    job = SimpleNamespace(
        id="job",
        delivery_chunks=["saved page"],
        delivery_chunk_index=0,
        delivery_retry_count=0,
    )
    error = discord.HTTPException(
        SimpleNamespace(status=400, reason="Bad Request"),
        {
            "code": 50035,
            "message": "Invalid Form Body",
            "errors": {
                "content": {
                    "_errors": [{"message": "Must be 2000 or fewer in length."}]
                }
            },
        },
    )
    with caplog.at_level(logging.WARNING, logger=__name__):
        assert not await service.deliver(
            job,
            AsyncMock(return_value=object()),
            AsyncMock(side_effect=error),
            AsyncMock(return_value=True),
        )

    assert "50035" in caplog.text
    record = next(item for item in caplog.records if "50035" in item.getMessage())
    assert "In content: Must be 2000 or fewer in length." in record.getMessage()
    assert record.context["job_id"] == "job"
    assert record.context["worker_id"] == service.worker_id
    assert record.context["delivery_chunk_index"] == 0
    assert job.status is JobStatus.READY_TO_DELIVER


@pytest.mark.asyncio
async def test_failure_notice_retries_then_finishes_failed(service):
    job = SimpleNamespace(id="job")
    service.prepare_failure(job, "Source failed")
    save = AsyncMock(return_value=True)
    send = AsyncMock(side_effect=RuntimeError("Discord offline"))
    assert not await service.deliver(job, AsyncMock(return_value=object()), send, save)
    assert job.failure_notice
    assert job.status == JobStatus.READY_TO_DELIVER
    assert job.delivery_chunks == ["Source failed"]
    assert await service.deliver(
        job, AsyncMock(return_value=object()), AsyncMock(), save
    )
    assert job.status == JobStatus.FAILED


@pytest.mark.asyncio
async def test_failed_owned_save_prevents_delivery(service):
    job = SimpleNamespace(delivery_chunks=["one"], delivery_chunk_index=0)
    send = AsyncMock()
    assert not await service.deliver(
        job, AsyncMock(), send, AsyncMock(return_value=False)
    )
    send.assert_not_awaited()


@pytest.mark.parametrize("matched", [0, 1])
async def test_quota_deferral_saves_only_while_job_is_owned(
    service, monkeypatch, matched
):
    from datetime import UTC, datetime

    from vsummary.services.notebook_quota import NotebookQuotaExceeded
    from vsummary.services.summary_retry import source_failure

    reset = datetime.now(UTC) + timedelta(hours=5)
    job = SummaryJob.model_construct(
        job_type=service.job_type, video_id="video", claimed_by="worker", retry_count=99
    )
    collection = SimpleNamespace(
        update_one=AsyncMock(return_value=SimpleNamespace(matched_count=matched))
    )
    monkeypatch.setattr(SummaryJob, "get_pymongo_collection", lambda: collection)
    assert not source_failure(
        job, NotebookQuotaExceeded(reset), service.config, allow_youtube_fallback=True
    )
    service.release(job)
    assert await service.save_owned(job) is bool(matched)
    query, update = collection.update_one.await_args.args
    assert query["claimed_by"] == "worker"
    assert query["job_type"] == service.job_type
    assert update["$set"]["next_attempt_at"] == reset
    assert update["$set"]["retry_count"] == 99
    assert update["$set"]["status"] == JobStatus.QUEUED
    assert update["$set"]["claimed_by"] is None
