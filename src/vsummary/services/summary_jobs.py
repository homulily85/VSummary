"""Worker ownership and resumable delivery for MongoDB-backed summary jobs."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from logging import Logger
from typing import Literal

from pymongo import ReturnDocument

from vsummary.model.channel import JobStatus, SummaryJob
from vsummary.services.summary_retry import delivery_failure
from vsummary.settings import RuntimeConfig
from vsummary.util.discord import split_message


class LeaseLostError(Exception):
    """Signal that a running job's lease could no longer be renewed."""


class SummaryJobService:
    """Coordinate claims, persistence, and Discord delivery for one worker category."""

    def __init__(
        self,
        job_type: Literal["auto", "manual"],
        worker_id: str,
        config: RuntimeConfig,
        logger: Logger,
    ) -> None:
        """Bind the persistence service to a worker and its operational settings.

        Args:
            job_type: Durable job category owned by this worker, either ``auto`` or
                ``manual``.
            worker_id: Worker identifier stored in job leases.
            config: Lease and retry settings.
            logger: Logger used for ownership and renewal failures.
        """
        self.job_type = job_type
        self.worker_id = worker_id
        self.config = config
        self.logger = logger

    def lease_duration(self) -> timedelta:
        """Convert the configured lease interval to a duration.

        Returns:
            Time a new or renewed claim remains valid.
        """
        return timedelta(seconds=self.config.job_lease_seconds)

    def owned_query(self, job: SummaryJob) -> dict[str, object]:
        """Build a write filter for a job owned by this worker.

        Args:
            job: Persisted job whose identifier is used in the filter.

        Returns:
            MongoDB filter matching the job ID, category, and claiming worker.
        """
        return {"_id": job.id, "job_type": self.job_type, "claimed_by": self.worker_id}

    async def claim_due_jobs(self) -> list[SummaryJob]:
        """Claim due jobs and reclaim active jobs with missing or expired leases.

        Each claim updates one MongoDB record atomically.

        Returns:
            Validated records for jobs claimed by this worker. Queued jobs enter
                generation, and ready jobs enter delivery.

        Raises:
            pymongo.errors.PyMongoError: If a MongoDB claim operation fails.
            pydantic.ValidationError: If a claimed document fails model validation.
        """
        now = datetime.now(UTC)
        claimed: list[SummaryJob] = []
        for old, new in (
            (JobStatus.QUEUED, JobStatus.GENERATING),
            (JobStatus.READY_TO_DELIVER, JobStatus.DELIVERING),
            (JobStatus.GENERATING, JobStatus.GENERATING),
            (JobStatus.DELIVERING, JobStatus.DELIVERING),
        ):
            query: dict[str, object] = {"job_type": self.job_type, "status": old.value}
            if old in (JobStatus.QUEUED, JobStatus.READY_TO_DELIVER):
                query["next_attempt_at"] = {"$lte": now}
            else:
                query["$or"] = [
                    {"lease_expires_at": {"$lte": now}},
                    {"lease_expires_at": None},
                    {"lease_expires_at": {"$exists": False}},
                ]
            while True:
                record = await SummaryJob.get_pymongo_collection().find_one_and_update(
                    query,
                    {
                        "$set": {
                            "status": new.value,
                            "claimed_by": self.worker_id,
                            "claimed_at": now,
                            "lease_expires_at": now + self.lease_duration(),
                        }
                    },
                    return_document=ReturnDocument.AFTER,
                )
                if record is None:
                    break
                claimed.append(SummaryJob.model_validate(record))
        return claimed

    async def save_owned(self, job: SummaryJob) -> bool:
        """Persist a job's fields while its record still matches this worker.

        Args:
            job: In-memory job state to save.

        Returns:
            ``True`` if the ownership filter matched, otherwise ``False``.

        Raises:
            pymongo.errors.PyMongoError: If MongoDB cannot complete the update.
        """
        values = job.model_dump(mode="python", by_alias=True, exclude={"id"})
        values.pop("_id", None)
        result = await SummaryJob.get_pymongo_collection().update_one(
            self.owned_query(job), {"$set": values}
        )
        if result.matched_count != 1:
            self.logger.warning("Lost lease for summary job %s", job.id)
            return False
        return True

    @staticmethod
    def release(job: SummaryJob) -> None:
        """Clear the claim fields on a job in memory.

        Args:
            job: Job whose claim will be cleared on the next successful save.
        """
        job.claimed_by = job.claimed_at = job.lease_expires_at = None

    async def renew(self, job: SummaryJob) -> bool:
        """Extend a matching job lease from the current UTC time.

        Args:
            job: Claimed job whose local expiration is updated on success.

        Returns:
            ``True`` if the lease was renewed, or ``False`` if ownership no longer
                matched.

        Raises:
            pymongo.errors.PyMongoError: If MongoDB cannot complete the update.
        """
        expires = datetime.now(UTC) + self.lease_duration()
        result = await SummaryJob.get_pymongo_collection().update_one(
            self.owned_query(job), {"$set": {"lease_expires_at": expires}}
        )
        if result.matched_count != 1:
            return False
        job.lease_expires_at = expires
        return True

    @asynccontextmanager
    async def maintain_lease(self, job: SummaryJob) -> AsyncIterator[None]:
        """Renew a job's lease while the surrounding asynchronous work runs.

        Args:
            job: Claimed job to renew every third of the configured lease duration.

        Yields:
            ``None`` while the lease heartbeat is running.

        Raises:
            LeaseLostError: If a renewal fails and the heartbeat interrupts the owner.
            asyncio.CancelledError: If the owner is independently cancelled.
        """
        owner = asyncio.current_task()
        assert owner is not None
        lost = False

        async def heartbeat() -> None:
            """Renew periodically and cancel the owner if the lease cannot be extended."""
            nonlocal lost
            while True:
                await asyncio.sleep(self.lease_duration().total_seconds() / 3)
                try:
                    if await self.renew(job):
                        continue
                except Exception:
                    self.logger.exception("Could not renew summary lease")
                lost = True
                owner.cancel()
                return

        task = asyncio.create_task(heartbeat())
        try:
            yield
        except asyncio.CancelledError:
            if not lost:
                raise
            if owner.uncancel():
                raise
            raise LeaseLostError(str(job.id)) from None
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    @staticmethod
    def prepare_failure(job: SummaryJob, message: str) -> None:
        """Replace the in-memory delivery payload with an immediately due failure notice.

        Args:
            job: Job whose delivery position and retry count are reset.
            message: Notice to split into Discord-sized chunks.
        """
        job.failure_notice = True
        job.delivery_chunks = split_message(message)
        job.delivery_chunk_index = 0
        job.delivery_retry_count = 0
        job.status = JobStatus.READY_TO_DELIVER
        job.next_attempt_at = datetime.now(UTC)

    async def deliver[ChannelT](
        self,
        job: SummaryJob,
        resolve_channel: Callable[[], Awaitable[ChannelT | None]],
        send: Callable[[ChannelT, str], Awaitable[None]],
        save: Callable[[SummaryJob], Awaitable[bool]] | None = None,
    ) -> bool:
        """Send remaining chunks and persist the outcome at completion or failure.

        Mark the job as delivering before sending. A failed channel lookup or send
        updates retry state and saves the confirmed progress.

        Args:
            job: Claimed job with a saved delivery payload and chunk position.
            resolve_channel: Asynchronous lookup for the destination, returning ``None``
                if unavailable.
            send: Asynchronous callback that sends one chunk to the resolved channel.
            save: Optional persistence callback. Defaults to the ownership-checked save.

        Returns:
            ``True`` if delivery finished and the final save succeeded. Return ``False``
                for a delivery failure or an unsuccessful ownership-checked save.

        Raises:
            pymongo.errors.PyMongoError: If the default persistence operation fails.
                Exceptions from a supplied save callback also propagate.
        """
        save = save or self.save_owned
        job.status = JobStatus.DELIVERING
        if not await save(job):
            return False
        try:
            if not job.delivery_chunks:
                raise RuntimeError("summary has no deliverable content")
            channel = await resolve_channel()
            if channel is None:
                raise RuntimeError("summary Discord channel is unavailable")
            for chunk in job.delivery_chunks[job.delivery_chunk_index :]:
                await send(channel, chunk)
                job.delivery_chunk_index += 1
        except Exception as exc:  # noqa: BLE001 - Discord failures are retried
            delivery_failure(job, exc, self.config)
            self.release(job)
            await save(job)
            return False
        job.delivered_at = datetime.now(UTC)
        job.status = JobStatus.FAILED if job.failure_notice else JobStatus.COMPLETED
        job.last_error = None
        self.release(job)
        return await save(job)
