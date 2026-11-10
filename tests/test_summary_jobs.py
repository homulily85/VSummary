"""Shared worker invariants independent of Discord Cog wiring."""

import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
async def test_failure_notice_retries_then_finishes_failed(service):
    job = SimpleNamespace()
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
