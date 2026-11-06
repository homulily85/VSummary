"""Discord slash commands for durable summary jobs and cached output."""

import logging

import discord
from discord import app_commands
from discord.ext import commands
from pymongo.errors import PyMongoError

from vsummary.model.channel import (
    JobStatus,
    ManualSummaryOperation,
    SummaryJob,
)
from vsummary.util.discord import send_limited, split_message
from vsummary.util.generic_media import GenericMediaError
from vsummary.util.video import (
    UnsupportedVideoSource,
    VideoRef,
    build_video_url,
    parse_video_source,
)
from vsummary.util.video_cache import delete_cached_summary
from vsummary.util.video_work import get_video_work_coordinator
from vsummary.util.x_space import (
    XSpaceInputKind,
    XSpaceResolutionError,
    is_x_space_input,
    parse_x_space_input,
    resolve_x_space,
)

logger = logging.getLogger(__name__)

_ACTIVE_JOB_STATUSES = [
    JobStatus.QUEUED.value,
    JobStatus.GENERATING.value,
    JobStatus.READY_TO_DELIVER.value,
    JobStatus.DELIVERING.value,
]
_SOURCE_LABELS = {
    "youtube": "YouTube video",
    "twitch": "Twitch VOD",
    "x_space": "X Space",
    "yt_dlp": "Media",
}


async def _resolve_manual_source(value: str) -> VideoRef:
    """Resolve X status URLs before a manual-summary job is created."""
    if is_x_space_input(value):
        try:
            return await resolve_x_space(value)
        except XSpaceResolutionError as exc:
            if "does not contain an X Space" not in str(exc):
                raise
    return parse_video_source(value)


async def _resolve_cached_source(value: str) -> VideoRef:
    """Resolve cache identity without checking direct recording availability."""
    x_input = parse_x_space_input(value)
    if x_input and x_input.kind is XSpaceInputKind.STATUS:
        try:
            return await _resolve_manual_source(value)
        except XSpaceResolutionError as exc:
            raise XSpaceResolutionError(
                "Could not resolve this X post. Use the direct X Space URL "
                "to delete its cached summary."
            ) from exc
    return parse_video_source(value)


class Summarizer(commands.Cog):
    """Handle manual summary requests, queue inspection, and cache deletion."""

    def __init__(self, bot):
        """Retain the Discord client used to enqueue jobs and send follow-ups."""
        self.bot = bot

    @app_commands.command(name="topics", description="Get topic list from a video.")
    @app_commands.describe(
        video=("A YouTube ID or a public URL for one finished video or audio item.")
    )
    async def topics(
        self,
        interaction: discord.Interaction,
        video: str,
    ):
        """Queue a topic-list request for a supported video source."""
        await interaction.response.defer(thinking=True)

        try:
            ref = await _resolve_manual_source(video)
            await self.bot.get_cog("ManualSummary").enqueue(
                ref=ref,
                operation=ManualSummaryOperation.TOPICS,
                channel_id=interaction.channel_id,
                requester_id=interaction.user.id,
            )
            source_label = _SOURCE_LABELS[ref.source]
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"{source_label} topics are queued and will be posted here.",
            )
        except (
            UnsupportedVideoSource,
            XSpaceResolutionError,
            GenericMediaError,
        ) as exc:
            await send_limited(self.bot, interaction.followup.send, str(exc))

    @app_commands.command(
        name="detail", description="Get details about a specific topic in the video."
    )
    @app_commands.describe(
        video=("A YouTube ID or a public URL for one finished video or audio item."),
        topic_index="The index of the topic to get details for (1-based).",
    )
    async def detail(
        self,
        interaction: discord.Interaction,
        video: str,
        topic_index: int | None = None,
    ):
        """Queue a detail request for a supported video source."""
        await interaction.response.defer(thinking=True)

        try:
            ref = await _resolve_manual_source(video)
            operation = (
                ManualSummaryOperation.DETAIL_ALL
                if topic_index is None
                else ManualSummaryOperation.DETAIL_ONE
            )
            await self.bot.get_cog("ManualSummary").enqueue(
                ref=ref,
                operation=operation,
                topic_index=None if topic_index is None else topic_index - 1,
                channel_id=interaction.channel_id,
                requester_id=interaction.user.id,
            )
            source_label = _SOURCE_LABELS[ref.source]
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"{source_label} summary is queued and will be posted here.",
            )
        except (
            UnsupportedVideoSource,
            XSpaceResolutionError,
            GenericMediaError,
        ) as exc:
            await send_limited(self.bot, interaction.followup.send, str(exc))

    @app_commands.command(
        name="deletecache",
        description="Delete a video's cached topics and details in all languages.",
    )
    @app_commands.describe(
        video="A YouTube ID or the URL of the cached video or audio item."
    )
    async def deletecache(self, interaction: discord.Interaction, video: str):
        """Clear one video's cache after local generation releases its lock."""
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            ref = await _resolve_cached_source(video)
        except ValueError as exc:
            await send_limited(
                self.bot, interaction.followup.send, str(exc), ephemeral=True
            )
            return

        try:
            async with get_video_work_coordinator(self.bot).for_video(ref):
                deleted_count = await delete_cached_summary(ref)
        except PyMongoError:
            logger.exception(
                "Could not delete cached summary for %s:%s.", ref.source, ref.video_id
            )
            message = "Could not delete the cached summary. Please try again."
        else:
            if deleted_count:
                noun = "record" if deleted_count == 1 else "records"
                message = (
                    f"Deleted {deleted_count} cached summary {noun} for this video "
                    "across all languages."
                )
            else:
                message = "No cached summary found for this video."
        await send_limited(self.bot, interaction.followup.send, message, ephemeral=True)

    @app_commands.command(name="queue", description="List videos awaiting a summary.")
    async def queue(self, interaction: discord.Interaction):
        """List active auto-summary and manual-summary jobs in processing order."""
        await interaction.response.defer(thinking=True)
        query = {"status": {"$in": _ACTIVE_JOB_STATUSES}}
        jobs = await SummaryJob.find(query).to_list()
        entries = [
            (
                job.next_attempt_at,
                "Auto" if job.job_type == "auto" else "Manual",
                job.title or job.video_id,
                build_video_url(
                    VideoRef(
                        source=job.source, video_id=job.video_id, url=job.source_url
                    )
                ),
            )
            for job in jobs
        ]
        if not entries:
            await send_limited(
                self.bot, interaction.followup.send, "The queue is empty."
            )
            return
        entries.sort(key=lambda entry: entry[0])
        message = "Queued videos:\n" + "\n".join(
            f"{index}. **{kind}:** {title} — <{url}> — "
            f"{timestamp.isoformat(timespec='seconds').replace('+00:00', 'Z')}"
            for index, (timestamp, kind, title, url) in enumerate(entries, start=1)
        )
        for chunk in split_message(message):
            await send_limited(self.bot, interaction.followup.send, chunk)


async def setup(bot):
    """Register the slash-command cog with Discord's extension loader."""
    await bot.add_cog(Summarizer(bot))
