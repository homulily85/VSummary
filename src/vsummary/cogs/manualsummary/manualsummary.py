"""Durable generation and Discord delivery for manual summary requests."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from discord.ext import commands, tasks
from pydantic import TypeAdapter

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
_WIKI_METADATA = TypeAdapter(VideoMetadata)


class ManualSummary(commands.Cog):
    """Run manual video requests independently of expiring Discord interactions."""

    def __init__(
        self, bot: Bot, *, settings: Settings | None = None, start_worker: bool = True
    ):
        """Configure the worker and optionally start its polling loop.

        Args:
            bot: Discord bot providing NotebookLM, video locks, and delivery services.
            settings: Settings override. When omitted, use the bot's settings.
            start_worker: Whether to start polling immediately.

        Raises:
            RuntimeError: If polling is started without a running event loop.
        """
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
        """Job service for the manual worker's identity and current runtime configuration."""
        return SummaryJobService("manual", self.worker_id, self.config, logger)

    @property
    def config(self) -> RuntimeConfig:
        """Effective operational settings for the worker, including runtime defaults."""
        return runtime_config(self.settings)

    def _lease_duration(self) -> timedelta:
        """Read the configured claim lifetime.

        Returns:
            Lease interval as a timedelta.
        """
        return timedelta(seconds=self.config.job_lease_seconds)

    def cog_unload(self):
        """Cancel the manual worker loop when Discord unloads the cog."""
        self.worker_loop.cancel()

    async def close(self):
        """Request cancellation of the manual worker during application shutdown."""
        self.worker_loop.cancel()

    @tasks.loop()
    async def worker_loop(self):
        """Run one poll of the durable manual-job queue."""
        await self.process_due_jobs()

    @worker_loop.before_loop
    async def before_worker_loop(self):
        """Wait for Discord readiness before the first manual-job poll."""
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
        """Inspect media metadata and save a manual job that is due immediately.

        Missing metadata for dedicated sources is logged and the job can proceed.
        Generic URLs are inspected before any job is saved.

        Args:
            ref: Normalized source reference, including the URL for generic media.
            operation: Topics, one topic detail, or all topic details.
            channel_id: Discord channel where the worker will deliver the result.
            requester_id: ID of the Discord user requesting the summary.
            topic_index: Zero-based index for a single-topic job, or ``None`` when
                omitted.

        Returns:
            Persisted manual job with any available display metadata.

        Raises:
            GenericMediaError: If generic media cannot be validated or inspected.
            UnsupportedVideoSource: If the reference cannot be turned into a supported
                URL.
            pydantic.ValidationError: If the proposed job does not pass model
                validation.
            pymongo.errors.PyMongoError: If the job cannot be saved.
        """
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
            **(
                {"wiki_metadata": _WIKI_METADATA.dump_python(metadata, mode="json")}
                if ref.source == "youtube" and self.config.wiki_enabled and metadata
                else {}
            ),
            discord_channel_id=channel_id,
            requester_id=requester_id,
            next_attempt_at=datetime.now(UTC),
        )
        await job.save()
        return job

    async def process_due_jobs(self):
        """Claim due manual jobs and advance each independently.

        Log per-job failures and continue with the remaining claimed jobs.

        Raises:
            pymongo.errors.PyMongoError: If claiming due jobs fails.
            pydantic.ValidationError: If a claimed job fails model validation.
        """
        for job in await self._claim_due_jobs():
            try:
                await self._process(job)
            except Exception:
                logger.exception(
                    "Manual summary job crashed",
                    extra={"context": {"job_id": str(job.id)}},
                )

    async def _claim_due_jobs(self):
        """Claim due manual jobs and reclaim work with an expired or absent lease.

        Returns:
            Claimed jobs to generate or deliver.

        Raises:
            pymongo.errors.PyMongoError: If the claim operation fails.
            pydantic.ValidationError: If a claimed job fails model validation.
        """
        return await self.jobs.claim_due_jobs()

    async def _process(self, job):
        """Advance one manual job under a renewable lease.

        Generate output for a job in the generation phase. Deliver the saved payload if
        the job is ready and still owned by this worker.

        Args:
            job: Claimed manual job.

        Raises:
            LeaseLostError: If the lease heartbeat interrupts processing.
            pymongo.errors.PyMongoError: If job state cannot be persisted.
        """
        async with self.jobs.maintain_lease(job):
            if job.status == JobStatus.GENERATING:
                await self._generate(job)
            if (
                job.status in {JobStatus.READY_TO_DELIVER, JobStatus.DELIVERING}
                and job.claimed_by == self.worker_id
            ):
                await self._deliver(job)

    async def _generate(self, job: SummaryJob):
        """Build and save the manual job's Discord delivery payload.

        Generate under the per-video lock. Convert an invalid topic index into a
        delivery message and route source failures through retry or terminal-notice
        handling.

        Args:
            job: Claimed job to populate with delivery chunks and generation state.

        Raises:
            UnsupportedVideoSource: If a metadata lookup cannot build a URL.
            pymongo.errors.PyMongoError: If job state cannot be persisted.
        """
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

        if (
            self.config.wiki_enabled
            and ref.source == "youtube"
            and metadata is not None
        ):
            generation_options["metadata_hint"] = metadata

        async def generate_delivery_chunks():
            """Generate and format the output requested by the enclosing job.

            Returns:
                Discord-sized chunks containing the requested topics or details.

            Raises:
                IndexError: If a requested topic index is outside the generated list.
                SummaryServiceError: If generation fails with a classified summary
                    error.
                notebooklm.NotebookLMError: If a NotebookLM request fails.
                pymongo.errors.PyMongoError: If a cache read or write fails.
            """
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
        """Deliver saved chunks to the originating Discord channel.

        Prefix the first topics chunk with an introduction. Use the durable service to
        record progress on success or failure.

        Args:
            job: Manual job with its saved delivery payload and destination.

        Returns:
            Whether delivery and final persistence both succeeded.

        Raises:
            pymongo.errors.PyMongoError: If a job checkpoint cannot be saved.
        """

        async def resolve_channel():
            """Resolve the destination for the enclosing job.

            Returns:
                Cached or fetched Discord channel.

            Raises:
                discord.HTTPException: If the channel is absent from the cache and the
                    fetch fails.
            """
            return self.bot.get_channel(
                job.discord_channel_id
            ) or await self.bot.fetch_channel(job.discord_channel_id)

        async def send(channel, chunk):
            """Send one manual-job chunk through the shared rate limiter.

            Args:
                channel: Resolved Discord destination.
                chunk: Saved content, with the topics introduction added for the first
                    chunk when applicable.

            Raises:
                discord.HTTPException: If Discord cannot send the chunk.
            """
            if (
                job.operation == ManualSummaryOperation.TOPICS
                and job.delivery_chunk_index == 0
                and not job.failure_notice
            ):
                chunk = "Here are the topics mentioned in the video:\n" + chunk
            await send_limited(self.bot, channel.send, chunk)

        return await self.jobs.deliver(job, resolve_channel, send, self._save)

    async def _fail(self, job, exc, *, permanent=False, allow_youtube_fallback=False):
        """Record a source failure, schedule the next action, and release the lease.

        Args:
            job: Manual job whose generation failed.
            exc: Exception to log and record.
            permanent: Whether to prepare a terminal notice immediately.
            allow_youtube_fallback: Whether an exhausted native attempt may switch to
                audio.

        Returns:
            Whether the resulting job state was saved while still owned.

        Raises:
            pymongo.errors.PyMongoError: If the save fails.
        """
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
        """Record a delivery failure and save the updated state after releasing the lease.

        Args:
            job: Manual job with saved delivery progress.
            exc: Exception from the delivery attempt.

        Returns:
            Whether the resulting job state was saved while still owned.

        Raises:
            pymongo.errors.PyMongoError: If the save fails.
        """
        delivery_failure(job, exc, self.config)
        self._release(job)
        return await self._save(job)

    def _release(self, job):
        """Clear the in-memory claim on a manual job.

        Args:
            job: Job whose lease fields should be cleared.
        """
        SummaryJobService.release(job)

    @staticmethod
    def _format_detail(detail, metadata) -> str:
        """Render one topic as Discord Markdown with an optional video heading.

        Args:
            detail: Mapping with ``topic_name`` and ``detail`` entries.
            metadata: Video metadata to place above the topic, or ``None`` for topic
                text alone.

        Returns:
            Formatted detail, including title and channel when supplied.

        Raises:
            KeyError: If either detail field is missing.
        """
        message = f"**{detail['topic_name']}**\n{detail['detail']}"
        if metadata is None:
            return message
        heading = f"**Video:** {metadata.title}"
        if metadata.channel_name:
            heading += f"\n**Channel:** {metadata.channel_name}"
        return f"{heading}\n\n{message}"

    @staticmethod
    def _stored_metadata(job: SummaryJob) -> VideoMetadata | None:
        """Recover the metadata snapshot or display fields saved with a job.

        Args:
            job: Manual job with optional video metadata.

        Returns:
            The wiki snapshot when available, or the saved title and channel.
            Return None when neither a snapshot nor a title was saved.

        Raises:
            pydantic.ValidationError: If the saved wiki snapshot is invalid.
        """
        snapshot = getattr(job, "wiki_metadata", None)
        if snapshot:
            return _WIKI_METADATA.validate_python(snapshot)
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
        """Look up display metadata for a dedicated media source.

        Args:
            ref: Media reference to inspect with yt-dlp.

        Returns:
            Video metadata, or ``None`` if yt-dlp cannot provide the required fields.

        Raises:
            UnsupportedVideoSource: If the reference cannot be turned into a URL.
        """
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
        """Persist a manual job through the ownership filter.

        Args:
            job: Job state to save.

        Returns:
            Whether the worker still matched the claim and saved the job.

        Raises:
            pymongo.errors.PyMongoError: If the save fails.
        """
        return await self.jobs.save_owned(job)


async def setup(bot):
    """Register and start the durable manual-summary cog.

    Args:
        bot: Discord bot receiving the worker cog.

    Raises:
        discord.ClientException: If the cog is already registered.
    """
    await bot.add_cog(ManualSummary(bot))
