"""Durable worker that generates and delivers manual Discord summary requests."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from discord.ext import commands, tasks

from vsummary.model.channel import JobStatus, ManualSummaryOperation, SummaryJob
from vsummary.services.summary_jobs import SummaryJobService
from vsummary.services.summary_retry import delivery_failure, source_failure
from vsummary.settings import RuntimeConfig, Settings, runtime_config
from vsummary.util.discord import send_limited, split_message
from vsummary.util.generic_media import (
    GenericMediaError,
    GenericMediaUnavailableError,
    inspect_generic_media,
)
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
from vsummary.util.x_space import XSpaceAudioError, XSpaceAudioUnavailableError

if TYPE_CHECKING:
    from vsummary.bot import Bot

logger = logging.getLogger(__name__)


class ManualSummary(commands.Cog):
    """Run manual video requests independently of expiring Discord interactions."""

    def __init__(
        self, bot: Bot, *, settings: Settings | None = None, start_worker: bool = True
    ):
        """Bind shared services and optionally begin the configured worker loop."""
        self.bot = bot
        self.settings = settings if settings is not None else bot.settings
        self.worker_id = uuid.uuid4().hex
        self.worker_loop.change_interval(
            seconds=self.config.manual_poll_interval_seconds
        )
        if start_worker:
            self.worker_loop.start()

    @property
    def jobs(self):
        """Bind shared job mechanics to this worker's current settings."""
        return SummaryJobService("manual", self.worker_id, self.config, logger)

    @property
    def config(self) -> RuntimeConfig:
        """Return injected operational settings or the centralized defaults."""
        return runtime_config(self.settings)

    def _lease_duration(self) -> timedelta:
        return timedelta(seconds=self.config.job_lease_seconds)

    def cog_unload(self):
        """Stop future worker-loop iterations when Discord unloads this cog."""
        self.worker_loop.cancel()

    async def close(self):
        """Stop the worker during the application's explicit shutdown sequence."""
        self.worker_loop.cancel()

    @tasks.loop()
    async def worker_loop(self):
        """Claim and process all manual jobs that are due in this polling interval."""
        await self.process_due_jobs()

    @worker_loop.before_loop
    async def before_worker_loop(self):
        """Wait until Discord can safely resolve channels before processing jobs."""
        await self.bot.wait_until_ready()

    async def enqueue(
        self,
        *,
        ref: VideoRef,
        operation: ManualSummaryOperation,
        channel_id: int,
        requester_id: int,
        topic_index: int | None = None,
    ) -> SummaryJob:
        """Persist a new manual job with optional source metadata for delivery."""
        metadata = (
            await inspect_generic_media(ref)
            if ref.source == "yt_dlp"
            else await self._load_metadata(ref)
        )
        job = SummaryJob(
            job_type="manual",
            source=ref.source,
            video_id=ref.video_id,
            source_url=ref.url,
            title=metadata.title if metadata else None,
            channel_name=metadata.channel_name if metadata else None,
            operation=operation,
            topic_index=topic_index,
            discord_channel_id=channel_id,
            requester_id=requester_id,
            next_attempt_at=datetime.now(UTC),
        )
        await job.save()
        return job

    async def process_due_jobs(self):
        """Process each atomically claimed job without stopping on a peer failure."""
        for job in await self._claim_due_jobs():
            try:
                await self._process(job)
            except Exception:
                logger.exception(
                    "Manual summary job crashed",
                    extra={"context": {"job_id": str(job.id)}},
                )

    async def _claim_due_jobs(self):
        return await self.jobs.claim_due_jobs()

    async def _process(self, job):
        async with self.jobs.maintain_lease(job):
            if job.status == JobStatus.GENERATING:
                await self._generate(job)
            if (
                job.status in {JobStatus.READY_TO_DELIVER, JobStatus.DELIVERING}
                and job.claimed_by == self.worker_id
            ):
                await self._deliver(job)

    async def _generate(self, job: SummaryJob):
        """Build and persist delivery chunks, retrying source failures durably."""
        use_youtube_audio = job.source == "youtube" and job.youtube_audio_fallback
        if use_youtube_audio and not self.config.youtube_yt_dlp_fallback_enabled:
            job.last_error = "YouTube audio fallback is disabled."
            self.jobs.prepare_failure(job, job.last_error)
            self._release(job)
            await self._save(job)
            return
        generation_options = {"config": self.config}
        if use_youtube_audio:
            generation_options["use_youtube_audio"] = True
        ref = VideoRef(
            source=job.source,
            video_id=job.video_id,
            url=job.source_url,
        )
        metadata = self._stored_metadata(job)
        if job.operation != ManualSummaryOperation.TOPICS:
            metadata = metadata or await self._load_metadata(ref)
            if metadata is not None:
                job.title = metadata.title
                job.channel_name = metadata.channel_name

        async def generate_delivery_chunks():
            """Execute the requested NotebookLM operation without mutating job state."""
            if job.operation == ManualSummaryOperation.TOPICS:
                topics = await get_topic_list(
                    self.bot.notebook_client, ref, **generation_options
                )
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
                    **generation_options,
                )
                return split_message(self._format_detail(detail, metadata))

            details = await get_topic_details_all(
                self.bot.notebook_client, ref, **generation_options
            )
            return [
                chunk
                for index, detail in enumerate(details)
                for chunk in split_message(
                    self._format_detail(detail, metadata if index == 0 else None)
                )
            ]

        try:
            async with self.bot.video_work.for_video(ref):
                job.delivery_chunks = (
                    await generate_delivery_chunks()
                    if use_youtube_audio
                    else await retry_invalid_summary_response(
                        generate_delivery_chunks,
                        retries=self.config.invalid_summary_retries,
                    )
                )
        except IndexError:
            job.delivery_chunks = [f"Invalid topic index: {(job.topic_index or 0) + 1}"]
        except (
            TwitchAudioUnavailableError,
            XSpaceAudioUnavailableError,
            GenericMediaUnavailableError,
        ) as exc:
            await self._fail(job, exc, permanent=True)
            return
        except (
            TwitchAudioError,
            XSpaceAudioError,
            GenericMediaError,
            TransientSummaryError,
        ) as exc:
            await self._fail(job, exc, allow_youtube_fallback=True)
            return
        except PermanentSummaryError as exc:
            await self._fail(job, exc, permanent=True)
            return
        except Exception as exc:  # noqa: BLE001 - source backends vary
            await self._fail(job, exc, allow_youtube_fallback=True)
            return
        job.status = JobStatus.READY_TO_DELIVER
        job.generated_at = datetime.now(UTC)
        job.delivery_chunk_index = 0
        job.last_error = None
        if not await self._save(job):
            job.claimed_by = None

    async def _deliver(self, job):
        async def resolve_channel():
            return self.bot.get_channel(
                job.discord_channel_id
            ) or await self.bot.fetch_channel(job.discord_channel_id)

        async def send(channel, chunk):
            if (
                job.operation == ManualSummaryOperation.TOPICS
                and job.delivery_chunk_index == 0
                and not job.failure_notice
            ):
                chunk = "Here are the topics mentioned in the video:\n" + chunk
            await send_limited(self.bot, channel.send, chunk)

        return await self.jobs.deliver(job, resolve_channel, send, self._save)

    async def _fail(self, job, exc, *, permanent=False, allow_youtube_fallback=False):
        logger.error(
            "Manual summary failed: %s",
            exc,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={"context": {"source": job.source, "video_id": job.video_id}},
        )
        terminal = source_failure(
            job,
            exc,
            self.config,
            permanent=permanent,
            allow_youtube_fallback=allow_youtube_fallback,
        )
        if terminal:
            self.jobs.prepare_failure(
                job, f"Could not summarize the requested media: {str(exc)[:1500]}"
            )
        self._release(job)
        return await self._save(job)

    async def _delivery_failure(self, job, exc):
        delivery_failure(job, exc, self.config)
        self._release(job)
        return await self._save(job)

    def _release(self, job):
        SummaryJobService.release(job)

    @staticmethod
    def _format_detail(detail, metadata) -> str:
        """Render one generated detail, adding source metadata only for the first one."""
        message = f"**{detail['topic_name']}**\n{detail['detail']}"
        if metadata is None:
            return message
        heading = f"**Video:** {metadata.title}"
        if metadata.channel_name:
            heading += f"\n**Channel:** {metadata.channel_name}"
        return f"{heading}\n\n{message}"

    @staticmethod
    def _stored_metadata(job: SummaryJob) -> VideoMetadata | None:
        """Return complete metadata previously persisted on a job, if any."""
        title = job.title
        channel_name = job.channel_name
        if isinstance(title, str) and title:
            return VideoMetadata(
                title=title,
                channel_name=channel_name
                if isinstance(channel_name, str) and channel_name
                else None,
            )
        return None

    async def _load_metadata(self, ref: VideoRef) -> VideoMetadata | None:
        """Load display metadata without allowing lookup failures to block a request."""
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
        return await self.jobs.save_owned(job)


async def setup(bot):
    """Register the durable manual-summary worker cog."""
    await bot.add_cog(ManualSummary(bot))
