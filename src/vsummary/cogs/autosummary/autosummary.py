"""Holodex subscriptions, topic policies, and automatic summary delivery."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import discord
from discord import app_commands
from discord.ext import commands, tasks
from pymongo.errors import DuplicateKeyError

from vsummary.logging import log_event
from vsummary.model.channel import (
    FollowedChannel,
    JobStatus,
    SummaryJob,
)
from vsummary.model.video import Topic
from vsummary.services.summary_jobs import SummaryJobService
from vsummary.services.summary_retry import delivery_failure, source_failure
from vsummary.settings import (
    RuntimeConfig,
    SettingsError,
    configured_auto_summary_channel_id,
    runtime_config,
)
from vsummary.util.auto_summary_topics import (
    ensure_default_auto_summary_topic_policies,
    get_ignored_auto_summary_topics,
    policy_ignores_video,
    set_auto_summary_topic_policy,
)
from vsummary.util.discord import send_limited, split_message
from vsummary.util.holodex import (
    ChannelNotFoundError,
    HolodexVideo,
    PermanentHolodexError,
    TransientHolodexError,
)
from vsummary.util.summarizer import (
    PermanentSummaryError,
    TransientSummaryError,
    retry_invalid_summary_response,
)
from vsummary.util.video import VideoRef
from vsummary.util.video_metadata import VideoMetadata

if TYPE_CHECKING:
    from vsummary.bot import Bot

logger = logging.getLogger(__name__)

HOLODEX_PAGE_SIZE = 50


class Autosummary(commands.Cog):
    """Follow channels, schedule their videos, and deliver persisted summaries.

    Automatic jobs are claimed with a renewable lease and use separate
    generation and delivery phases so a Discord outage never repeats expensive
    NotebookLM work.
    """

    def __init__(self, bot: Bot, *, start_polling: bool = True):
        """Configure the automatic worker and optionally start polling.

        Args:
            bot: Discord bot providing Holodex, NotebookLM, and shared delivery
                services.
            start_polling: Whether to start the polling loop immediately.

        Raises:
            RuntimeError: If polling is started without a running event loop.
        """
        self.bot = bot
        self.settings = bot.settings
        self.holodex = bot.holodex
        self.notebook = bot.notebook_client
        self.summary_service = bot.summary_service
        self.worker_id = uuid.uuid4().hex
        self.poll_loop.change_interval(minutes=self.config.poll_interval_minutes)
        if start_polling:
            self.poll_loop.start()

    @property
    def jobs(self):
        """Job service bound to the automatic worker's identity and runtime settings."""
        return SummaryJobService("auto", self.worker_id, self.config, logger)

    @property
    def config(self) -> RuntimeConfig:
        """Effective operational settings for automatic polling and delivery."""
        return runtime_config(self.settings)

    def cog_unload(self):
        """Cancel polling when Discord unloads the automatic summary cog."""
        self.poll_loop.cancel()

    async def close(self):
        """Request cancellation of polling during application shutdown."""
        self.poll_loop.cancel()

    async def cog_load(self):
        """Seed the built-in topic policies when Discord loads the cog.

        Raises:
            pymongo.errors.PyMongoError: If MongoDB cannot load or insert the policies.
        """
        await ensure_default_auto_summary_topic_policies()

    @tasks.loop()
    async def poll_loop(self):
        """Scan followed channels and advance due automatic jobs for one poll."""
        await self.check_new_videos()
        await self.process_due_videos()

    @poll_loop.before_loop
    async def before_poll_loop(self):
        """Wait for Discord readiness before the first automatic poll."""
        await self.bot.wait_until_ready()

    async def check_new_videos(self):
        """Poll followed channels and queue eligible streams.

        Log individual channel failures and continue with the remaining subscriptions.

        Raises:
            pymongo.errors.PyMongoError: If the followed channel list cannot be loaded.
        """
        channels = await FollowedChannel.find().to_list()
        for channel in channels:
            try:
                videos = await self._get_new_channel_videos(channel)
                for video in videos:
                    if video.available_at <= channel.added_at:
                        log_event(
                            logger,
                            logging.INFO,
                            "Skipping video %s released before channel %s was added.",
                            video.video_id,
                            channel.channel_id,
                            video_id=video.video_id,
                            channel_id=channel.channel_id,
                        )
                        continue
                    await self._enqueue_new_video(video, channel)
            except Exception:
                logger.exception(
                    "Failed to poll channel %s",
                    channel.channel_id,
                    extra={"context": {"channel_id": channel.channel_id}},
                )

    async def _get_new_channel_videos(self, channel) -> list[HolodexVideo]:
        """Fetch past streams published after the subscription began.

        Args:
            channel: Followed channel providing the Holodex ID and publication cutoff.

        Returns:
            Past streams strictly newer than the cutoff in Holodex order.

        Raises:
            TransientHolodexError: If a request exhausts its retries.
            PermanentHolodexError: If Holodex rejects a request or returns invalid data.
        """
        return await self.holodex.fetch_channel_videos_since(
            channel.channel_id,
            channel.added_at,
            page_size=HOLODEX_PAGE_SIZE,
        )

    async def _enqueue_new_video(self, video: HolodexVideo, channel):
        """Queue a stream if its publication time and topic meet the subscription policy.

        Schedule eligible streams no earlier than their estimated end time plus the
        transcript delay. Treat existing jobs and concurrent duplicate inserts as
        already queued.

        Args:
            video: Holodex stream with publication time, duration, and optional topic.
            channel: Subscription whose metadata is copied into the job.

        Raises:
            ValueError: If the topic ID is blank or its publication timestamp is naive.
            pymongo.errors.PyMongoError: If a policy read or job read or write fails.
        """
        if video.available_at <= channel.added_at:
            log_event(
                logger,
                logging.INFO,
                "Skipping video %s released before channel %s was added.",
                video.video_id,
                channel.channel_id,
                video_id=video.video_id,
                channel_id=channel.channel_id,
            )
            return
        if video.topic_id is not None and await policy_ignores_video(
            video.topic_id,
            video.available_at,
        ):
            log_event(
                logger,
                logging.INFO,
                "Skipping ignored video %s (topic %s)",
                video.video_id,
                video.topic_id,
                video_id=video.video_id,
                channel_id=channel.channel_id,
                topic_id=video.topic_id,
            )
            return

        existing = await SummaryJob.find_one(
            {
                "job_type": "auto",
                "channel_id": channel.channel_id,
                "video_id": video.video_id,
            }
        )
        if existing:
            return

        next_attempt = max(
            datetime.now(UTC),
            video.available_at
            + timedelta(seconds=video.duration + self.config.transcript_delay_seconds),
        )
        job = SummaryJob(
            job_type="auto",
            video_id=video.video_id,
            channel_id=channel.channel_id,
            channel_name=channel.name,
            title=video.title,
            available_at=video.available_at,
            next_attempt_at=next_attempt,
        )
        try:
            await job.save()
        except DuplicateKeyError:
            log_event(
                logger,
                logging.INFO,
                "Video %s was already queued by another poller",
                video.video_id,
                video_id=video.video_id,
                channel_id=channel.channel_id,
            )
            return
        log_event(
            logger,
            logging.INFO,
            "Queued video %s for auto-summary.",
            video.video_id,
            video_id=video.video_id,
            channel_id=channel.channel_id,
            job_id=str(job.id),
        )

    async def process_due_videos(self):
        """Advance due automatic jobs when the delivery channel is available.

        Log failures from individual jobs and continue processing the rest.

        Raises:
            pymongo.errors.PyMongoError: If claiming due jobs fails.
            pydantic.ValidationError: If a claimed job fails model validation.
        """
        if await self._get_auto_summary_channel() is None:
            return
        due = await self._claim_due_jobs()
        for job in due:
            try:
                await self._process_pending_video(job)
            except Exception:
                logger.exception(
                    "Failed to process summary job %s",
                    job.video_id,
                    extra={"context": self._job_context(job)},
                )

    async def _claim_due_jobs(self):
        """Claim due automatic jobs and reclaim active work with missing or expired leases.

        Returns:
            Claimed jobs to generate or deliver.

        Raises:
            pymongo.errors.PyMongoError: If the claim operation fails.
            pydantic.ValidationError: If a claimed job fails model validation.
        """
        return await self.jobs.claim_due_jobs()

    async def _process_pending_video(self, job):
        """Advance one automatic job under a renewable lease.

        Attempt delivery after successful generation or when the job already has output
        ready.

        Args:
            job: Claimed automatic job.

        Raises:
            LeaseLostError: If the lease heartbeat interrupts processing.
            pymongo.errors.PyMongoError: If job state cannot be persisted.
        """
        async with self.jobs.maintain_lease(job):
            if job.status in {
                JobStatus.QUEUED,
                JobStatus.GENERATING,
            } and not await self._generate_for_job(job):
                return
            if job.status in {JobStatus.READY_TO_DELIVER, JobStatus.DELIVERING}:
                await self._deliver_for_job(job)

    async def _generate_for_job(self, job: SummaryJob):
        """Prepare and save the topic details required for automatic delivery.

        Reuse saved details. When generating, refresh the Holodex topic, cancel jobs
        excluded by policy, and coordinate through the per-video lock. Route generation
        failures through retry or notice handling.

        Args:
            job: Automatic job in the generation phase.

        Returns:
            ``True`` if the ready-to-deliver state was saved, otherwise ``False``.

        Raises:
            pymongo.errors.PyMongoError: If a job checkpoint cannot be saved.
        """
        if not job.summary_details:
            ref = VideoRef(source="youtube", video_id=job.video_id)
            use_youtube_audio = job.youtube_audio_fallback
            if use_youtube_audio and not self.config.youtube_yt_dlp_fallback_enabled:
                job.last_error = "YouTube audio fallback is disabled."
                self.jobs.prepare_failure(job, self._failure_message(job))
                self._release_lease(job)
                await self._save_item(job)
                return False
            generation_options = (
                {"use_youtube_audio": True} if use_youtube_audio else {}
            )
            generation_started = False

            async def generate_summary():
                """Generate details for the enclosing job using its selected source mode.

                Returns:
                    Ordered topic details from the shared summary service.

                Raises:
                    SummaryServiceError: If the service classifies a generation failure.
                    notebooklm.NotebookLMError: If another NotebookLM error propagates.
                    pymongo.errors.PyMongoError: If a cache operation fails.
                """
                if self.config.wiki_enabled and job.title:
                    generation_options["metadata_hint"] = VideoMetadata(
                        job.title,
                        job.channel_name,
                        channel_id=job.channel_id,
                    )
                return await self.summary_service.summarize(ref, **generation_options)

            try:
                topic_id = await self._refresh_video_topic(job)
                if topic_id is not None and await policy_ignores_video(
                    topic_id,
                    job.available_at,
                ):
                    self._set_status(job, JobStatus.CANCELLED)
                    job.last_error = None
                    self._release_lease(job)
                    saved = await self._save_item(job)
                    if saved:
                        log_event(
                            logger,
                            logging.INFO,
                            "Cancelled auto-summary for ignored video %s (topic %s).",
                            job.video_id,
                            topic_id,
                            **self._job_context(job, topic_id=topic_id),
                        )
                    return False
                async with self.bot.video_work.for_video(ref):
                    generation_started = True
                    details = (
                        await generate_summary()
                        if use_youtube_audio
                        else await retry_invalid_summary_response(
                            generate_summary,
                            retries=self.config.invalid_summary_retries,
                        )
                    )
                job.summary_details = [self._as_topic(detail) for detail in details]
            except (PermanentSummaryError, PermanentHolodexError) as exc:
                logger.warning(
                    "Permanent summary source failure for %s",
                    job.video_id,
                    exc_info=True,
                    extra={"context": self._job_context(job)},
                )
                await self._handle_source_error(job, exc, permanent=True)
                return False
            except (TransientSummaryError, TransientHolodexError) as exc:
                logger.warning(
                    "Transient summary source failure for %s",
                    job.video_id,
                    exc_info=True,
                    extra={"context": self._job_context(job)},
                )
                await self._handle_source_error(
                    job, exc, allow_youtube_fallback=generation_started
                )
                return False
            except Exception as exc:
                logger.exception(
                    "Unexpected summary error for %s",
                    job.video_id,
                    extra={"context": self._job_context(job)},
                )
                await self._handle_source_error(
                    job, exc, allow_youtube_fallback=generation_started
                )
                return False

        job.generated_at = datetime.now(UTC)
        self._set_status(job, JobStatus.READY_TO_DELIVER)
        job.last_error = None
        saved = await self._save_item(job)
        if saved:
            log_event(
                logger,
                logging.INFO,
                "Generated summary for %s.",
                job.video_id,
                **self._job_context(job),
            )
        return saved

    async def _refresh_video_topic(self, job: SummaryJob) -> str | None:
        """Try twice to refresh the video's Holodex topic.

        Log lookup failures and let generation proceed with an unknown topic if neither
        attempt supplies one.

        Args:
            job: Automatic job whose YouTube video is looked up.

        Returns:
            Holodex topic ID, or ``None`` if unavailable.
        """
        for attempt in (1, 2):
            try:
                topic_id = await self.holodex.get_video_topic(job.video_id)
                if topic_id is not None:
                    return topic_id
                log_event(
                    logger,
                    logging.INFO,
                    "Holodex topic is unavailable for video %s (attempt %s/2).",
                    job.video_id,
                    attempt,
                    **self._job_context(job, attempt=attempt),
                )
            except Exception as exc:  # noqa: BLE001 - lookup failure must not block generation
                log_event(
                    logger,
                    logging.WARNING,
                    "Could not refresh Holodex topic for video %s (attempt %s/2): %s",
                    job.video_id,
                    attempt,
                    exc,
                    **self._job_context(
                        job, attempt=attempt, error_type=type(exc).__name__
                    ),
                )
        return None

    async def _deliver_for_job(self, job):
        """Render missing chunks and deliver the remaining automatic summary payload.

        Args:
            job: Automatic job with stored topic details or previously rendered chunks.

        Returns:
            Whether delivery and final persistence both succeeded.

        Raises:
            KeyError: If a detail mapping lacks its topic name.
            pymongo.errors.PyMongoError: If a job checkpoint cannot be saved.
        """
        if not job.delivery_chunks:
            job.delivery_chunks = split_message(
                self._summary_message(job, job.summary_details)
            )
            job.delivery_chunk_index = 0

        async def send(channel, chunk):
            """Send one automatic-job chunk through the shared rate limiter.

            Args:
                channel: Resolved Discord destination.
                chunk: Saved summary or failure-notice text.

            Raises:
                discord.HTTPException: If Discord cannot send the chunk.
            """
            await send_limited(self.bot, channel.send, chunk)

        return await self.jobs.deliver(
            job, self._get_auto_summary_channel, send, self._save_item
        )

    async def _handle_source_error(
        self, job, exc=None, permanent=False, *, allow_youtube_fallback=False
    ):
        """Apply generation retry policy, release the lease, and save the outcome.

        Args:
            job: Automatic job whose generation failed.
            exc: Error to record, or ``None`` to use a generic generation error.
            permanent: Whether the failure immediately requires a notice.
            allow_youtube_fallback: Whether exhausted native retries may switch to
                audio.

        Returns:
            Whether the resulting job state was saved while still owned.

        Raises:
            pymongo.errors.PyMongoError: If the save fails.
        """
        exc = exc or RuntimeError("summary generation failed")
        terminal = source_failure(
            job,
            exc,
            self.config,
            permanent=permanent,
            allow_youtube_fallback=allow_youtube_fallback,
        )
        if terminal:
            self.jobs.prepare_failure(job, self._failure_message(job))
        self._release_lease(job)
        return await self._save_item(job)

    async def _handle_delivery_error(self, job, exc):
        """Record a delivery failure and save the job after releasing its lease.

        Args:
            job: Automatic job with saved delivery progress.
            exc: Exception from the delivery attempt.

        Returns:
            Whether the resulting job state was saved while still owned.

        Raises:
            pymongo.errors.PyMongoError: If the save fails.
        """
        delivery_failure(job, exc, self.config)
        self._release_lease(job)
        return await self._save_item(job)

    def _summary_message(self, job: SummaryJob, details) -> str:
        """Render the video heading and ordered topics as Discord Markdown.

        Args:
            job: Automatic job with a title and channel name.
            details: Iterable of topic objects or detail mappings.

        Returns:
            Unsplit message containing the metadata and topic explanations.

        Raises:
            KeyError: If a detail mapping lacks its topic name.
            pydantic.ValidationError: If a detail mapping fails topic validation.
        """
        message = f"**Video:** {job.title}\n**Channel:** {job.channel_name}"
        for detail in details:
            topic = self._as_topic(detail)
            message += f"\n\n**{topic.name}**\n{topic.detail or ''}"
        return message

    def _failure_message(self, job: SummaryJob):
        """Format a notice describing a generation failure and the attempted source modes.

        Args:
            job: Job carrying the video metadata, latest error, and retry history.

        Returns:
            Discord message with the video heading and the failure description.
        """
        if job.youtube_audio_fallback:
            if not self.config.youtube_yt_dlp_fallback_enabled:
                failure = (
                    f"Failed to generate a summary after {job.retry_count} normal attempts; "
                    "the yt-dlp audio fallback was disabled before it could run."
                )
            else:
                failure = (
                    f"Failed to generate a summary after {job.retry_count - 1} normal attempts "
                    "and one yt-dlp audio fallback attempt."
                )
        else:
            failure = (
                f"Failed to generate a summary after {job.retry_count} attempts; "
                f"{(job.last_error or 'summary generation failed')[:1500]}"
            )
        message = (
            f"**Video:** {job.title}\n**Channel:** {job.channel_name}\n\n{failure}"
        )
        return message

    async def _get_auto_summary_channel(self):
        """Resolve the configured automatic summary destination from Discord's cache.

        Returns:
            Cached channel, or ``None`` when the destination is unavailable or
                unconfigured.
        """
        try:
            channel_id = configured_auto_summary_channel_id(self.settings)
        except SettingsError:
            logger.error("discord.auto_summary_channel_id is not a valid integer.")
            return None
        if channel_id is None:
            logger.error(
                "discord.auto_summary_channel_id is not set; pausing auto-summary jobs."
            )
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log_event(
                logger,
                logging.ERROR,
                "Auto-summary channel %s not found; pausing auto-summary jobs.",
                channel_id,
                discord_channel_id=channel_id,
            )
        return channel

    async def _send_to_channel(self, channel, text: str):
        """Split and send a Discord message through the shared rate limiter.

        Args:
            channel: Discord destination with an asynchronous send method.
            text: Unsplit message body.

        Raises:
            discord.HTTPException: If Discord cannot send a chunk.
        """
        for chunk in split_message(text):
            await send_limited(self.bot, channel.send, chunk)

    def _lease_duration(self) -> timedelta:
        """Read the configured worker claim lifetime.

        Returns:
            Lease interval as a timedelta.
        """
        return timedelta(seconds=self.config.job_lease_seconds)

    @staticmethod
    def _job_context(job: SummaryJob, **additional) -> dict[str, object]:
        """Build structured context for job-related logs.

        Args:
            job: Automatic job whose available identifiers and status are included.
            **additional: Extra fields that can override generated keys.

        Returns:
            Mapping suitable for structured logging.
        """
        context: dict[str, object] = {}
        for field, value in (
            ("job_id", job.id),
            ("video_id", job.video_id),
            ("channel_id", job.channel_id),
        ):
            if value is not None:
                context[field] = str(value)
        status = job.status
        if status is not None:
            context["job_status"] = status.value
        context.update(additional)
        return context

    @staticmethod
    def _release_lease(job):
        """Clear the in-memory claim on an automatic job.

        Args:
            job: Job whose lease fields should be cleared.
        """
        SummaryJobService.release(job)

    async def _save_item(self, job):
        """Persist the job through the automatic worker's ownership filter.

        Args:
            job: Automatic job state to save.

        Returns:
            Whether the ownership filter matched and the job was saved.

        Raises:
            pymongo.errors.PyMongoError: If the save fails.
        """
        return await self.jobs.save_owned(job)

    @staticmethod
    def _as_topic(detail) -> Topic:
        """Convert a persisted detail mapping to a topic if needed.

        Args:
            detail: Topic object or mapping with a topic name and optional explanation.

        Returns:
            Original topic or a new topic built from the mapping.

        Raises:
            KeyError: If a mapping lacks its topic name.
            pydantic.ValidationError: If mapping values fail topic validation.
        """
        if isinstance(detail, Topic):
            return detail
        return Topic(name=detail["topic_name"], detail=detail.get("detail"))

    @staticmethod
    def _set_status(job: SummaryJob, status: JobStatus):
        """Set the durable job's lifecycle state in memory.

        Args:
            job: Job to update.
            status: New generation or delivery state.
        """
        job.status = status

    @app_commands.command(
        name="addignoredtopic",
        description="Ignore a Holodex topic for future automatic summaries.",
    )
    @app_commands.describe(topic_id="The Holodex topic ID to ignore.")
    async def addignoredtopic(self, interaction: discord.Interaction, topic_id: str):
        """Append an ignore decision for subsequently published videos.

        Reply privately with the activation time, existing state, or validation error.

        Args:
            interaction: Discord request used for the ephemeral response.
            topic_id: Holodex topic ID to ignore.

        Raises:
            discord.HTTPException: If Discord cannot send the response.
            discord.InteractionResponded: If the interaction was already answered.
            pymongo.errors.PyMongoError: If the policy cannot be read or saved.
        """
        try:
            changed, policy = await set_auto_summary_topic_policy(
                topic_id, ignored=True
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not changed:
            await interaction.response.send_message(
                f"Topic `{policy.topic_id if policy else topic_id.strip()}` is already "
                "ignored for new videos.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Topic `{policy.topic_id}` will be ignored for videos published after "
            f"<t:{int(policy.effective_at.timestamp())}:F>.",
            ephemeral=True,
        )

    @app_commands.command(
        name="removeignoredtopic",
        description="Allow a Holodex topic in future automatic summaries.",
    )
    @app_commands.describe(topic_id="The Holodex topic ID to allow.")
    async def removeignoredtopic(self, interaction: discord.Interaction, topic_id: str):
        """Append an allow decision for subsequently published videos.

        Reply privately with the activation time, existing state, or validation error.

        Args:
            interaction: Discord request used for the ephemeral response.
            topic_id: Holodex topic ID to allow.

        Raises:
            discord.HTTPException: If Discord cannot send the response.
            discord.InteractionResponded: If the interaction was already answered.
            pymongo.errors.PyMongoError: If the policy cannot be read or saved.
        """
        try:
            changed, policy = await set_auto_summary_topic_policy(
                topic_id, ignored=False
            )
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        if not changed:
            await interaction.response.send_message(
                f"Topic `{policy.topic_id if policy else topic_id.strip()}` is already "
                "allowed for new videos.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"Topic `{policy.topic_id}` will be allowed for videos published after "
            f"<t:{int(policy.effective_at.timestamp())}:F>.",
            ephemeral=True,
        )

    @app_commands.command(
        name="listignoredtopics",
        description="List Holodex topics ignored by automatic summaries.",
    )
    async def listignoredtopics(self, interaction: discord.Interaction):
        """Report topics whose latest saved policy is to ignore them.

        Args:
            interaction: Discord request used for the ephemeral response.

        Raises:
            discord.HTTPException: If Discord cannot send the response.
            discord.InteractionResponded: If the interaction was already answered.
            pymongo.errors.PyMongoError: If policy history cannot be queried.
        """
        ignored = await get_ignored_auto_summary_topics()
        if not ignored:
            await interaction.response.send_message(
                "No topics are ignored for automatic summaries.", ephemeral=True
            )
            return
        await interaction.response.send_message(
            "Ignored auto-summary topics:\n"
            + "\n".join(f"- `{topic}`" for topic in ignored),
            ephemeral=True,
        )

    @app_commands.command(
        name="addchannel",
        description="Add a Vtuber channel to follow for automatic summaries.",
    )
    @app_commands.describe(
        channel_id="The Holodex/YouTube channel ID (e.g. UCQ0UDLQCjY0rmuxCDE38FGg)."
    )
    async def addchannel(self, interaction: discord.Interaction, channel_id: str):
        """Validate a Holodex channel and save its subscription for future streams.

        Report lookup failures and already-followed channels in the follow-up.

        Args:
            interaction: Discord request used to acknowledge the subscription.
            channel_id: Holodex or YouTube channel ID.

        Raises:
            discord.HTTPException: If deferral or the follow-up fails.
            discord.InteractionResponded: If the interaction was already answered.
            pymongo.errors.PyMongoError: If the subscription cannot be read or saved,
                except for handled duplicate inserts.
        """
        await interaction.response.defer(thinking=True)
        try:
            channel = await self.holodex.fetch_channel(channel_id)
        except ChannelNotFoundError:
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"Channel '{channel_id}' was not found on Holodex.",
            )
            return
        except (TransientHolodexError, PermanentHolodexError) as exc:
            log_event(
                logger,
                logging.ERROR,
                "Error fetching channel %s: %s",
                channel_id,
                exc,
                channel_id=channel_id,
                error_type=type(exc).__name__,
            )
            channel = None

        if channel is None:
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"An error occurred while fetching channel '{channel_id}' from Holodex.",
            )
            return

        existing = await FollowedChannel.find_one(
            FollowedChannel.channel_id == channel_id
        )
        if existing:
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"Channel **{channel.name}** (`{channel_id}`) is already being followed.",
            )
            return
        try:
            await FollowedChannel(
                channel_id=channel_id,
                name=channel.name,
                added_at=datetime.now(UTC),
            ).save()
        except DuplicateKeyError:
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"Channel **{channel.name}** (`{channel_id}`) is already being followed.",
            )
            return
        await send_limited(
            self.bot,
            interaction.followup.send,
            f"Added channel **{channel.name}** (`{channel_id}`) for automatic summaries.",
        )

    @app_commands.command(
        name="removechannel",
        description="Stop following a channel for automatic summaries.",
    )
    @app_commands.describe(
        channel_id="The Holodex/YouTube channel ID to stop following."
    )
    async def removechannel(self, interaction: discord.Interaction, channel_id: str):
        """Unfollow a channel and delete its queued or ready-to-deliver automatic jobs.

        Args:
            interaction: Discord request used to confirm the removal or report a missing
                subscription.
            channel_id: Holodex or YouTube channel ID to unfollow.

        Raises:
            discord.HTTPException: If deferral or the follow-up fails.
            discord.InteractionResponded: If the interaction was already answered.
            pymongo.errors.PyMongoError: If MongoDB cannot read or delete the
                subscription or jobs.
        """
        await interaction.response.defer(thinking=True)
        result = await FollowedChannel.find_one(
            FollowedChannel.channel_id == channel_id
        )
        if result is None:
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"Channel '{channel_id}' is not being followed.",
            )
            return
        await result.delete()
        await SummaryJob.find(
            {
                "job_type": "auto",
                "channel_id": channel_id,
                "status": {
                    "$in": [JobStatus.QUEUED.value, JobStatus.READY_TO_DELIVER.value]
                },
            }
        ).delete()
        await send_limited(
            self.bot,
            interaction.followup.send,
            f"Removed channel '{channel_id}' and its pending videos.",
        )

    @app_commands.command(
        name="listchannels",
        description="List channels followed for automatic summaries.",
    )
    async def listchannels(self, interaction: discord.Interaction):
        """Report followed channels in Discord-sized response chunks.

        Args:
            interaction: Discord request used to send the list.

        Raises:
            discord.HTTPException: If deferral or a follow-up fails.
            discord.InteractionResponded: If the interaction was already answered.
            pymongo.errors.PyMongoError: If the subscription list cannot be loaded.
        """
        await interaction.response.defer(thinking=True)
        channels = await FollowedChannel.find().to_list()
        if not channels:
            await send_limited(
                self.bot,
                interaction.followup.send,
                "No channels are currently followed for automatic summaries.",
            )
            return
        lines = [f"**{channel.name}** (`{channel.channel_id}`)" for channel in channels]
        for chunk in split_message("Followed channels:\n" + "\n".join(lines)):
            await send_limited(self.bot, interaction.followup.send, chunk)


async def setup(bot):
    """Register and start the automatic summary cog.

    Args:
        bot: Discord bot receiving the cog.

    Raises:
        discord.ClientException: If the cog is already registered.
        pymongo.errors.PyMongoError: If MongoDB cannot load or seed topic policies.
    """
    await bot.add_cog(Autosummary(bot))
