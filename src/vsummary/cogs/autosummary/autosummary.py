from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from vsummary.logging import log_event
from vsummary.model.channel import (
    FollowedChannel,
    JobStatus,
    PendingVideoStatus,
    SummaryJob,
)
from vsummary.model.video import Topic
from vsummary.settings import (
    Settings,
    SettingsError,
    configured_auto_summary_channel_id,
)
from vsummary.util.discord import split_message
from vsummary.util.holodex import (
    MAX_RETRIES,
    ChannelNotFoundError,
    HolodexClient,
    HolodexVideo,
    PermanentHolodexError,
    TransientHolodexError,
    exponential_backoff_hours,
    is_ignored,
)
from vsummary.util.summarizer import (
    NotebookLMSummaryService,
    PermanentSummaryError,
    TransientSummaryError,
    get_topic_details_all,
)
from vsummary.util.video import VideoRef
from vsummary.util.video_work import get_video_work_coordinator

logger = logging.getLogger(__name__)

HOLODEX_PAGE_SIZE = 50
JOB_LEASE_DURATION = timedelta(minutes=15)

# These names remain module-level for existing integrations and test doubles.
Channel = FollowedChannel
PendingVideo = SummaryJob


class Autosummary(commands.Cog):
    def __init__(
        self,
        bot,
        *,
        holodex=None,
        summary_service=None,
        settings: Settings | None = None,
        start_polling: bool = True,
    ):
        self.bot = bot
        self.settings = settings or getattr(bot, "settings", None)
        self.holodex = holodex or getattr(bot, "holodex", None)
        self._owns_holodex = self.holodex is None
        if self.holodex is None:
            timeout = getattr(self.settings, "http_timeout_seconds", 15.0)
            self.holodex = HolodexClient(timeout=timeout)
        self.notebook = getattr(bot, "notebook_client", None)
        self.summary_service = summary_service or (
            NotebookLMSummaryService(self.notebook)
            if self.notebook is not None
            else None
        )
        self.worker_id = uuid.uuid4().hex
        poll_interval = getattr(self.settings, "poll_interval_minutes", 30)
        self.poll_loop.change_interval(minutes=poll_interval)
        if start_polling:
            self.poll_loop.start()

    def cog_unload(self):
        self.poll_loop.cancel()
        if self._owns_holodex:
            task = getattr(self.bot, "loop", None)
            if task is not None and task.is_running():
                task.create_task(self.holodex.aclose())

    async def close(self):
        self.poll_loop.cancel()
        if self._owns_holodex:
            await self.holodex.aclose()
            self.holodex = None

    async def cog_load(self):
        self.notebook = self.bot.notebook_client
        if self.summary_service is None:
            self.summary_service = NotebookLMSummaryService(self.notebook)

    @tasks.loop(minutes=30)
    async def poll_loop(self):
        await self.check_new_videos()
        await self.process_due_videos()

    @poll_loop.before_loop
    async def before_poll_loop(self):
        await self.bot.wait_until_ready()

    async def check_new_videos(self):
        channels = await Channel.find().to_list()
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
        """Use pagination for the production client, retaining fake compatibility."""
        client_type = type(self.holodex)
        if hasattr(client_type, "fetch_channel_videos_since"):
            return await self.holodex.fetch_channel_videos_since(
                channel.channel_id,
                channel.added_at,
                page_size=HOLODEX_PAGE_SIZE,
            )
        if hasattr(client_type, "get_channel_videos_since"):
            return await self.holodex.get_channel_videos_since(
                channel.channel_id,
                channel.added_at,
                page_size=HOLODEX_PAGE_SIZE,
            )
        return await self.holodex.get_channel_videos(channel.channel_id)

    async def _enqueue_new_video(self, video: HolodexVideo, channel):
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
        if is_ignored(video.topic_id):
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

        try:
            existing = await PendingVideo.find_one(
                PendingVideo.channel_id == channel.channel_id,
                PendingVideo.video_id == video.video_id,
            )
        except TypeError:
            existing = await PendingVideo.find_one(
                PendingVideo.video_id == video.video_id
            )
        if existing:
            return

        next_attempt = max(
            datetime.now(UTC),
            video.available_at + timedelta(seconds=video.duration, hours=2),
        )
        pending = PendingVideo(
            video_id=video.video_id,
            channel_id=channel.channel_id,
            channel_name=channel.name,
            title=video.title,
            available_at=video.available_at,
            next_attempt_at=next_attempt,
        )
        try:
            await pending.save()
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
            job_id=str(getattr(pending, "id", "")),
        )

    async def process_due_videos(self):
        due = await self._claim_due_jobs()
        for item in due:
            try:
                await self._process_pending_video(item)
            except Exception:
                logger.exception(
                    "Failed to process summary job %s",
                    item.video_id,
                    extra={"context": self._job_context(item)},
                )

    async def _claim_due_jobs(self) -> list:
        now = datetime.now(UTC)
        if PendingVideo is not SummaryJob:
            return await PendingVideo.find(
                PendingVideo.status == PendingVideoStatus.QUEUED,
                PendingVideo.next_attempt_at <= now,
            ).to_list()

        claimed = []
        lease_expires_at = now + self._lease_duration()
        claims = (
            (
                {
                    "status": JobStatus.QUEUED.value,
                    "next_attempt_at": {"$lte": now},
                },
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
                    "$or": [
                        {"lease_expires_at": {"$lte": now}},
                        {"lease_expires_at": None},
                        {"lease_expires_at": {"$exists": False}},
                    ],
                },
                JobStatus.GENERATING,
            ),
            (
                {
                    "status": JobStatus.DELIVERING.value,
                    "$or": [
                        {"lease_expires_at": {"$lte": now}},
                        {"lease_expires_at": None},
                        {"lease_expires_at": {"$exists": False}},
                    ],
                },
                JobStatus.DELIVERING,
            ),
        )
        for claim_index, (query, claimed_status) in enumerate(claims):
            while True:
                update = {
                    "$set": {
                        "status": claimed_status.value,
                        "claimed_by": self.worker_id,
                        "claimed_at": now,
                        "lease_expires_at": lease_expires_at,
                    }
                }
                record = (
                    await PendingVideo.get_pymongo_collection().find_one_and_update(
                        query,
                        update,
                        return_document=ReturnDocument.AFTER,
                    )
                )
                if record is None:
                    break
                item = PendingVideo.model_validate(record)
                claimed.append(item)
                log_event(
                    logger,
                    logging.INFO,
                    "Claimed summary job %s for %s.",
                    item.video_id,
                    claimed_status.value,
                    **self._job_context(
                        item,
                        claim_type="reclaim" if claim_index >= 2 else "due",
                        claimed_status=claimed_status.value,
                    ),
                )
        return claimed

    async def _process_pending_video(self, item):
        stop_renewal = asyncio.Event()
        renewal_task = None
        if self._uses_leases(item):
            renewal_task = asyncio.create_task(
                self._renew_lease_until_finished(item, stop_renewal)
            )
        try:
            status = getattr(item, "status", JobStatus.QUEUED)
            status_value = getattr(status, "value", status)
            if status_value in {JobStatus.QUEUED.value, JobStatus.GENERATING.value}:
                if not await self._generate_for_job(item):
                    return
                status = getattr(item, "status", JobStatus.READY_TO_DELIVER)
                status_value = getattr(status, "value", status)
                if (
                    getattr(item, "summary_details", None)
                    and status_value == PendingVideoStatus.QUEUED.value
                ):
                    status_value = JobStatus.READY_TO_DELIVER.value

            if status_value in {
                JobStatus.READY_TO_DELIVER.value,
                JobStatus.DELIVERING.value,
            }:
                await self._deliver_for_job(item)
        finally:
            if renewal_task is not None:
                stop_renewal.set()
                with contextlib.suppress(asyncio.CancelledError):
                    await renewal_task

    async def _generate_for_job(self, item):
        if not getattr(item, "summary_details", None):
            ref = VideoRef(source="youtube", video_id=item.video_id)
            try:
                async with get_video_work_coordinator(self.bot).for_video(ref):
                    summary_service = getattr(self, "summary_service", None)
                    if summary_service is not None:
                        details = await summary_service.summarize(ref)
                    else:
                        details = await get_topic_details_all(
                            getattr(self, "notebook", None), ref
                        )
                item.summary_details = [self._as_topic(detail) for detail in details]
            except (PermanentSummaryError, PermanentHolodexError) as exc:
                logger.warning(
                    "Permanent summary source failure for %s",
                    item.video_id,
                    exc_info=True,
                    extra={"context": self._job_context(item)},
                )
                await self._handle_source_error(item, exc, permanent=True)
                return False
            except (TransientSummaryError, TransientHolodexError) as exc:
                logger.warning(
                    "Transient summary source failure for %s",
                    item.video_id,
                    exc_info=True,
                    extra={"context": self._job_context(item)},
                )
                await self._handle_source_error(item, exc)
                return False
            except Exception as exc:
                logger.exception(
                    "Unexpected summary error for %s",
                    item.video_id,
                    extra={"context": self._job_context(item)},
                )
                await self._handle_source_error(item, exc)
                return False

        item.generated_at = datetime.now(UTC)
        self._set_status(item, JobStatus.READY_TO_DELIVER)
        item.last_error = None
        saved = await self._save_item(item)
        if saved:
            log_event(
                logger,
                logging.INFO,
                "Generated summary for %s.",
                item.video_id,
                **self._job_context(item),
            )
        return saved

    async def _deliver_for_job(self, item):
        self._set_status(item, JobStatus.DELIVERING)
        chunks = list(getattr(item, "delivery_chunks", []))
        if not chunks:
            chunks = split_message(self._summary_message(item, item.summary_details))
            if not chunks:
                await self._handle_delivery_error(
                    item, RuntimeError("summary has no deliverable content")
                )
                return False
            item.delivery_chunks = chunks
            item.delivery_chunk_index = 0
        if not await self._save_item(item):
            return False
        try:
            channel = await self._get_auto_summary_channel()
            if channel is None:
                raise RuntimeError("auto-summary Discord channel is unavailable")
            start = getattr(item, "delivery_chunk_index", 0)
            for chunk in chunks[start:]:
                await channel.send(chunk)
                item.delivery_chunk_index += 1
                if not await self._save_item(item):
                    return False
        except Exception as exc:  # noqa: BLE001 - Discord can fail transiently
            await self._handle_delivery_error(item, exc)
            return False
        item.delivered_at = datetime.now(UTC)
        self._set_status(item, JobStatus.COMPLETED)
        item.last_error = None
        self._release_lease(item)
        saved = await self._save_item(item)
        if saved:
            log_event(
                logger,
                logging.INFO,
                "Delivered summary for %s.",
                item.video_id,
                **self._job_context(item, delivery_chunks=len(chunks)),
            )
        return saved

    async def _handle_source_error(
        self,
        item,
        exc: Exception | None = None,
        permanent: bool = False,
    ):
        exc = exc or RuntimeError("summary generation failed")
        item.retry_count = getattr(item, "retry_count", 0) + 1
        max_retries = getattr(
            getattr(self, "settings", None), "source_retry_limit", MAX_RETRIES
        )
        if permanent or item.retry_count >= max_retries:
            self._set_status(item, JobStatus.FAILED)
            item.last_error = str(exc)
            self._release_lease(item)
            saved = await self._save_item(item)
            if saved and not permanent:
                await self._post_failure(item)
            log_event(
                logger,
                logging.ERROR if permanent else logging.WARNING,
                "Summary generation failed permanently for %s.",
                item.video_id,
                **self._job_context(
                    item,
                    attempt=item.retry_count,
                    retry_limit=max_retries,
                    permanent=permanent,
                ),
            )
            return saved
        backoff = exponential_backoff_hours(item.retry_count)
        item.next_attempt_at = datetime.now(UTC) + timedelta(hours=backoff)
        self._set_status(item, JobStatus.QUEUED)
        item.last_error = str(exc)
        self._release_lease(item)
        saved = await self._save_item(item)
        log_event(
            logger,
            logging.WARNING,
            "Summary generation failed for %s; retrying in %s hour(s)",
            item.video_id,
            backoff,
            **self._job_context(
                item,
                attempt=item.retry_count,
                retry_limit=max_retries,
                retry_in_hours=backoff,
            ),
        )
        return saved

    async def _handle_delivery_error(self, item, exc: Exception):
        item.delivery_retry_count = getattr(item, "delivery_retry_count", 0) + 1
        max_retries = getattr(
            getattr(self, "settings", None), "delivery_retry_limit", MAX_RETRIES
        )
        item.last_error = str(exc)
        if item.delivery_retry_count >= max_retries:
            self._set_status(item, JobStatus.FAILED)
        else:
            backoff = exponential_backoff_hours(item.delivery_retry_count)
            item.next_attempt_at = datetime.now(UTC) + timedelta(hours=backoff)
            self._set_status(item, JobStatus.READY_TO_DELIVER)
        self._release_lease(item)
        saved = await self._save_item(item)
        log_event(
            logger,
            logging.ERROR
            if getattr(item, "status", None) == JobStatus.FAILED
            else logging.WARNING,
            "Discord delivery failed for summary job %s: %s",
            item.video_id,
            exc,
            **self._job_context(
                item,
                attempt=item.delivery_retry_count,
                retry_limit=max_retries,
            ),
        )
        return saved

    async def _post_summary(self, item, details):
        channel = await self._get_auto_summary_channel()
        if channel is None:
            raise RuntimeError("auto-summary Discord channel is unavailable")
        await self._send_to_channel(channel, self._summary_message(item, details))

    def _summary_message(self, item, details) -> str:
        message = f"**Video:** {item.title}\n**Channel:** {item.channel_name}"
        for detail in details:
            topic = self._as_topic(detail)
            message += f"\n\n**{topic.name}**\n{topic.detail or ''}"
        return message

    async def _post_failure(self, item):
        channel = await self._get_auto_summary_channel()
        if channel is None:
            return
        message = (
            f"**Video:** {item.title}\n**Channel:** {item.channel_name}\n\n"
            f"Failed to generate a summary after {getattr(getattr(self, 'settings', None), 'source_retry_limit', MAX_RETRIES)} attempts; "
            "no transcript could be fetched for this video."
        )
        await self._send_to_channel(channel, message)

    async def _get_auto_summary_channel(self):
        try:
            channel_id = configured_auto_summary_channel_id(
                getattr(self, "settings", None)
            )
        except SettingsError:
            logger.error("AUTO_SUMMARY_CHANNEL_ID is not a valid integer.")
            return None
        if channel_id is None:
            logger.error(
                "AUTO_SUMMARY_CHANNEL_ID is not set; skipping auto-summary post."
            )
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            log_event(
                logger,
                logging.ERROR,
                "Auto-summary channel %s not found; skipping post.",
                channel_id,
                discord_channel_id=channel_id,
            )
        return channel

    async def _send_to_channel(self, channel, text: str):
        for chunk in split_message(text):
            await channel.send(chunk)

    def _lease_duration(self) -> timedelta:
        seconds = getattr(getattr(self, "settings", None), "job_lease_seconds", None)
        if seconds is None:
            return JOB_LEASE_DURATION
        return timedelta(seconds=max(1, seconds))

    @staticmethod
    def _uses_leases(item) -> bool:
        return PendingVideo is SummaryJob and isinstance(item, SummaryJob)

    @staticmethod
    def _job_context(item, **additional) -> dict[str, object]:
        """Return identifiers needed to correlate durable workflow log records."""
        context: dict[str, object] = {}
        for field in ("id", "video_id", "channel_id"):
            value = getattr(item, field, None)
            if value is not None:
                context["job_id" if field == "id" else field] = str(value)
        status = getattr(item, "status", None)
        if status is not None:
            context["job_status"] = getattr(status, "value", status)
        context.update(additional)
        return context

    def _release_lease(self, item) -> None:
        if self._uses_leases(item):
            item.claimed_by = None
            item.claimed_at = None
            item.lease_expires_at = None

    async def _save_item(self, item) -> bool:
        """Persist only if this worker still owns a claimed SummaryJob."""
        if not self._uses_leases(item):
            await item.save()
            return True

        values = item.model_dump(mode="python", by_alias=True, exclude={"id"})
        values.pop("_id", None)
        result = await PendingVideo.get_pymongo_collection().update_one(
            {"_id": item.id, "claimed_by": self.worker_id}, {"$set": values}
        )
        if getattr(result, "matched_count", 0) != 1:
            log_event(
                logger,
                logging.WARNING,
                "Lost lease for summary job %s",
                item.video_id,
                **self._job_context(item, worker_id=self.worker_id),
            )
            return False
        return True

    async def _renew_lease_until_finished(self, item, stop: asyncio.Event) -> None:
        interval = max(1.0, self._lease_duration().total_seconds() / 3)
        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
                return
            except TimeoutError:
                pass
            if not await self._renew_lease(item):
                return

    async def _renew_lease(self, item) -> bool:
        expires_at = datetime.now(UTC) + self._lease_duration()
        result = await PendingVideo.get_pymongo_collection().update_one(
            {"_id": item.id, "claimed_by": self.worker_id},
            {"$set": {"lease_expires_at": expires_at}},
        )
        if getattr(result, "matched_count", 0) != 1:
            log_event(
                logger,
                logging.WARNING,
                "Could not renew lease for summary job %s",
                item.video_id,
                **self._job_context(item, worker_id=self.worker_id),
            )
            return False
        item.lease_expires_at = expires_at
        return True

    @staticmethod
    def _as_topic(detail) -> Topic:
        if isinstance(detail, Topic):
            return detail
        return Topic(name=detail["topic_name"], detail=detail.get("detail"))

    @staticmethod
    def _set_status(item, status: JobStatus):
        current = getattr(item, "status", None)
        if isinstance(current, PendingVideoStatus):
            old_status = {
                JobStatus.QUEUED: PendingVideoStatus.QUEUED,
                JobStatus.COMPLETED: PendingVideoStatus.DONE,
                JobStatus.FAILED: PendingVideoStatus.FAILED,
            }
            item.status = old_status.get(status, PendingVideoStatus.QUEUED)
        else:
            item.status = status

    @app_commands.command(
        name="addchannel",
        description="Add a Vtuber channel to follow for automatic summaries.",
    )
    @app_commands.describe(
        channel_id="The Holodex/YouTube channel ID (e.g. UCQ0UDLQCjY0rmuxCDE38FGg)."
    )
    async def addchannel(self, interaction: discord.Interaction, channel_id: str):
        await interaction.response.defer(thinking=True)
        try:
            channel = await self.holodex.get_channel(channel_id)
        except ChannelNotFoundError:
            await interaction.followup.send(
                f"Channel '{channel_id}' was not found on Holodex."
            )
            return
        except (TransientHolodexError, PermanentHolodexError):
            channel = None

        if channel is None:
            await interaction.followup.send(
                f"An error occurred while fetching channel '{channel_id}' from Holodex."
            )
            return

        existing = await Channel.find_one(Channel.channel_id == channel_id)
        if existing:
            await interaction.followup.send(
                f"Channel **{channel.name}** (`{channel_id}`) is already being followed."
            )
            return
        try:
            await Channel(
                channel_id=channel_id,
                name=channel.name,
                added_at=datetime.now(UTC),
            ).save()
        except DuplicateKeyError:
            await interaction.followup.send(
                f"Channel **{channel.name}** (`{channel_id}`) is already being followed."
            )
            return
        await interaction.followup.send(
            f"Added channel **{channel.name}** (`{channel_id}`) for automatic summaries."
        )

    @app_commands.command(
        name="removechannel",
        description="Stop following a channel for automatic summaries.",
    )
    @app_commands.describe(
        channel_id="The Holodex/YouTube channel ID to stop following."
    )
    async def removechannel(self, interaction: discord.Interaction, channel_id: str):
        await interaction.response.defer(thinking=True)
        result = await Channel.find_one(Channel.channel_id == channel_id)
        if result is None:
            await interaction.followup.send(
                f"Channel '{channel_id}' is not being followed."
            )
            return
        await result.delete()
        delete_many = getattr(PendingVideo, "delete_many", None)
        if delete_many is not None:
            await delete_many(PendingVideo.channel_id == channel_id)
        else:
            await PendingVideo.delete_one(PendingVideo.channel_id == channel_id)
        await interaction.followup.send(
            f"Removed channel '{channel_id}' and its pending videos."
        )

    @app_commands.command(
        name="listchannels",
        description="List channels followed for automatic summaries.",
    )
    async def listchannels(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        channels = await Channel.find().to_list()
        if not channels:
            await interaction.followup.send(
                "No channels are currently followed for automatic summaries."
            )
            return
        lines = [f"**{channel.name}** (`{channel.channel_id}`)" for channel in channels]
        for chunk in split_message("Followed channels:\n" + "\n".join(lines)):
            await interaction.followup.send(chunk)


async def setup(bot):
    await bot.add_cog(Autosummary(bot))
