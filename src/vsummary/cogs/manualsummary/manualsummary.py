from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta

from discord.ext import commands, tasks
from pymongo import ReturnDocument

from vsummary.model.channel import JobStatus, ManualSummaryJob, ManualSummaryOperation
from vsummary.settings import Settings
from vsummary.util.discord import send_limited, split_message
from vsummary.util.holodex import MAX_RETRIES, exponential_backoff_hours
from vsummary.util.summarizer import (
    PermanentSummaryError,
    TransientSummaryError,
    get_topic_details,
    get_topic_details_all,
    get_topic_list,
    retry_invalid_summary_response,
)
from vsummary.util.twitch import TwitchAudioError, TwitchAudioUnavailableError
from vsummary.util.video import VideoRef
from vsummary.util.video_metadata import (
    VideoMetadata,
    VideoMetadataError,
    get_video_metadata,
)
from vsummary.util.video_work import get_video_work_coordinator
from vsummary.util.x_space import XSpaceAudioError, XSpaceAudioUnavailableError

logger = logging.getLogger(__name__)
LEASE_DURATION = timedelta(minutes=15)


class ManualSummary(commands.Cog):
    """Run manual video requests independently of expiring Discord interactions."""

    def __init__(
        self, bot, *, settings: Settings | None = None, start_worker: bool = True
    ):
        self.bot = bot
        self.settings = settings or getattr(bot, "settings", None)
        self.worker_id = uuid.uuid4().hex
        if start_worker:
            self.worker_loop.start()

    def cog_unload(self):
        self.worker_loop.cancel()

    async def close(self):
        self.worker_loop.cancel()

    @tasks.loop(seconds=10)
    async def worker_loop(self):
        await self.process_due_jobs()

    @worker_loop.before_loop
    async def before_worker_loop(self):
        await self.bot.wait_until_ready()

    async def enqueue(
        self,
        *,
        ref: VideoRef,
        operation: ManualSummaryOperation,
        channel_id: int,
        requester_id: int,
        topic_index: int | None = None,
    ):
        metadata = await self._load_metadata(ref)
        job = ManualSummaryJob(
            source=ref.source,
            video_id=ref.video_id,
            title=metadata.title if metadata else None,
            channel_name=metadata.channel_name if metadata else None,
            operation=operation,
            topic_index=topic_index,
            channel_id=channel_id,
            requester_id=requester_id,
            next_attempt_at=datetime.now(UTC),
        )
        await job.save()
        return job

    async def process_due_jobs(self):
        for job in await self._claim_due_jobs():
            try:
                await self._process(job)
            except Exception:
                logger.exception(
                    "Manual summary job crashed",
                    extra={"context": {"job_id": str(job.id)}},
                )

    async def _claim_due_jobs(self):
        now = datetime.now(UTC)
        claimed = []
        collection = ManualSummaryJob.get_pymongo_collection()
        for query, status in (
            (
                {"status": JobStatus.QUEUED.value, "next_attempt_at": {"$lte": now}},
                JobStatus.GENERATING,
            ),
            (
                {
                    "status": JobStatus.READY_TO_DELIVER.value,
                    "next_attempt_at": {"$lte": now},
                },
                JobStatus.DELIVERING,
            ),
            (
                {
                    "status": JobStatus.GENERATING.value,
                    "lease_expires_at": {"$lte": now},
                },
                JobStatus.GENERATING,
            ),
            (
                {
                    "status": JobStatus.DELIVERING.value,
                    "lease_expires_at": {"$lte": now},
                },
                JobStatus.DELIVERING,
            ),
        ):
            while record := await collection.find_one_and_update(
                query,
                {
                    "$set": {
                        "status": status.value,
                        "claimed_by": self.worker_id,
                        "claimed_at": now,
                        "lease_expires_at": now + LEASE_DURATION,
                    }
                },
                return_document=ReturnDocument.AFTER,
            ):
                claimed.append(ManualSummaryJob.model_validate(record))
        return claimed

    async def _process(self, job):
        if job.status == JobStatus.GENERATING:
            await self._generate(job)
        if job.status == JobStatus.DELIVERING:
            await self._deliver(job)

    async def _generate(self, job):
        ref = VideoRef(source=job.source, video_id=job.video_id)
        metadata = self._stored_metadata(job)
        if job.operation != ManualSummaryOperation.TOPICS:
            metadata = metadata or await self._load_metadata(ref)
            if metadata is not None:
                job.title = metadata.title
                job.channel_name = metadata.channel_name

        async def generate_delivery_chunks():
            if job.operation == ManualSummaryOperation.TOPICS:
                topics = await get_topic_list(self.bot.notebook_client, ref)
                return split_message(
                    "\n".join(
                        f"{i + 1}: {topic.name}" for i, topic in enumerate(topics)
                    )
                )
            if job.operation == ManualSummaryOperation.DETAIL_ONE:
                detail = await get_topic_details(
                    self.bot.notebook_client,
                    ref,
                    job.topic_index or 0,
                )
                return split_message(self._format_detail(detail, metadata))

            details = await get_topic_details_all(self.bot.notebook_client, ref)
            return [
                chunk
                for index, detail in enumerate(details)
                for chunk in split_message(
                    self._format_detail(detail, metadata if index == 0 else None)
                )
            ]

        try:
            async with get_video_work_coordinator(self.bot).for_video(ref):
                job.delivery_chunks = await retry_invalid_summary_response(
                    generate_delivery_chunks
                )
        except IndexError:
            job.delivery_chunks = [f"Invalid topic index: {(job.topic_index or 0) + 1}"]
        except (TwitchAudioUnavailableError, XSpaceAudioUnavailableError) as exc:
            await self._fail(job, exc, permanent=True)
            return
        except (TwitchAudioError, XSpaceAudioError, TransientSummaryError) as exc:
            await self._fail(job, exc)
            return
        except PermanentSummaryError as exc:
            await self._fail(job, exc, permanent=True)
            return
        except Exception as exc:  # noqa: BLE001 - source backends vary
            await self._fail(job, exc)
            return
        job.status = JobStatus.DELIVERING
        job.delivery_chunk_index = 0
        job.last_error = None
        await self._save(job)

    async def _deliver(self, job):
        try:
            channel = self.bot.get_channel(
                job.channel_id
            ) or await self.bot.fetch_channel(job.channel_id)
            if (
                job.operation == ManualSummaryOperation.TOPICS
                and job.delivery_chunk_index == 0
            ):
                await send_limited(
                    self.bot,
                    channel.send,
                    "Here are the topics mentioned in the video:\n"
                    + job.delivery_chunks[0],
                )
                job.delivery_chunk_index = 1
            for chunk in job.delivery_chunks[job.delivery_chunk_index :]:
                await send_limited(self.bot, channel.send, chunk)
                job.delivery_chunk_index += 1
            job.status = JobStatus.COMPLETED
            self._release(job)
            await self._save(job)
        except Exception as exc:  # noqa: BLE001 - Discord channel delivery varies
            await self._delivery_failure(job, exc)

    async def _fail(self, job, exc, *, permanent=False):
        logger.error(
            "Manual summary failed: %s",
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "context": {
                    "source": job.source,
                    "video_id": job.video_id,
                }
            },
        )
        job.retry_count += 1
        job.last_error = str(exc)
        if permanent or job.retry_count >= getattr(
            self.settings, "source_retry_limit", MAX_RETRIES
        ):
            job.status = JobStatus.FAILED
        else:
            job.status = JobStatus.QUEUED
            job.next_attempt_at = datetime.now(UTC) + timedelta(
                hours=exponential_backoff_hours(job.retry_count)
            )
        self._release(job)
        await self._save(job)

    async def _delivery_failure(self, job, exc):
        job.delivery_retry_count += 1
        job.last_error = str(exc)
        if job.delivery_retry_count >= getattr(
            self.settings, "delivery_retry_limit", MAX_RETRIES
        ):
            job.status = JobStatus.FAILED
        else:
            job.status = JobStatus.READY_TO_DELIVER
            job.next_attempt_at = datetime.now(UTC) + timedelta(
                hours=exponential_backoff_hours(job.delivery_retry_count)
            )
        self._release(job)
        await self._save(job)

    def _release(self, job):
        job.claimed_by = job.claimed_at = job.lease_expires_at = None

    @staticmethod
    def _format_detail(detail, metadata) -> str:
        message = f"**{detail['topic_name']}**\n{detail['detail']}"
        if metadata is None:
            return message
        return (
            f"**Video:** {metadata.title}\n**Channel:** {metadata.channel_name}\n\n"
            f"{message}"
        )

    @staticmethod
    def _stored_metadata(job) -> VideoMetadata | None:
        title = getattr(job, "title", None)
        channel_name = getattr(job, "channel_name", None)
        if (
            isinstance(title, str)
            and title
            and isinstance(channel_name, str)
            and channel_name
        ):
            return VideoMetadata(title=title, channel_name=channel_name)
        return None

    async def _load_metadata(self, ref: VideoRef) -> VideoMetadata | None:
        try:
            return await get_video_metadata(ref)
        except VideoMetadataError as exc:
            logger.warning(
                "Could not load manual summary metadata: %s",
                exc,
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={"context": {"source": ref.source, "video_id": ref.video_id}},
            )
            return None

    async def _save(self, job):
        values = job.model_dump(mode="python", by_alias=True, exclude={"id"})
        values.pop("_id", None)
        result = await ManualSummaryJob.get_pymongo_collection().update_one(
            {"_id": job.id, "claimed_by": self.worker_id}, {"$set": values}
        )
        return result.matched_count == 1


async def setup(bot):
    await bot.add_cog(ManualSummary(bot))
