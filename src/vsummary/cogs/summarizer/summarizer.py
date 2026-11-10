"""Slash commands for manual requests, queue inspection, and cache management."""

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
    VIDEO_SOURCE_LABELS,
    UnsupportedVideoSource,
    VideoRef,
    build_video_url,
    parse_non_space_video_source,
)
from vsummary.util.video_cache import delete_cached_summary
from vsummary.util.x_space import (
    XPostWithoutSpaceError,
    XSpaceInput,
    XSpaceInputKind,
    XSpaceResolutionError,
    parse_x_space_input,
    resolve_parsed_x_space,
)

logger = logging.getLogger(__name__)

_ACTIVE_JOB_STATUSES = [
    JobStatus.QUEUED.value,
    JobStatus.GENERATING.value,
    JobStatus.READY_TO_DELIVER.value,
    JobStatus.DELIVERING.value,
]


async def _resolve_x_status(x_input: XSpaceInput) -> VideoRef:
    """Resolve a status post to an archived Space or generic media.

    Args:
        x_input: Recognized X status URL, including its original URL.

    Returns:
        Reference for the archived Space, or a generic media reference if the post
            contains no Space.

    Raises:
        XSpaceResolutionError: If the post has a Space without a playable archive or the
            lookup fails.
        ValueError: If the original URL cannot be normalized for generic media.
    """
    try:
        return await resolve_parsed_x_space(x_input)
    except XPostWithoutSpaceError:
        return parse_non_space_video_source(x_input.original_url)


async def _resolve_manual_source(value: str) -> VideoRef:
    """Resolve user input to a reference suitable for creating a manual job.

    Verify an archived replay for Space links. A status post without a Space can proceed
    as generic media.

    Args:
        value: Media URL or bare YouTube video ID.

    Returns:
        Normalized source reference for the worker.

    Raises:
        ValueError: If local parsing rejects the media input.
        XSpaceResolutionError: If a recognized X link cannot be resolved to a usable
            replay.
    """
    x_input = parse_x_space_input(value)
    if x_input is None:
        return parse_non_space_video_source(value)
    if x_input.kind is XSpaceInputKind.STATUS:
        return await _resolve_x_status(x_input)
    return await resolve_parsed_x_space(x_input)


async def _resolve_cached_source(value: str) -> VideoRef:
    """Resolve a reference for locating existing summary caches.

    Direct Space URLs supply their cache ID without a remote replay check. X posts still
    require resolution.

    Args:
        value: Media URL or bare YouTube video ID.

    Returns:
        Source reference used to select cache records.

    Raises:
        ValueError: If local parsing rejects the media input.
        XSpaceResolutionError: If a status post cannot be resolved and a direct link is
            needed.
    """
    x_input = parse_x_space_input(value)
    if x_input is None:
        return parse_non_space_video_source(value)
    if x_input.kind is XSpaceInputKind.SPACE:
        return VideoRef(source="x_space", video_id=x_input.input_id)
    try:
        return await _resolve_x_status(x_input)
    except XSpaceResolutionError as exc:
        raise XSpaceResolutionError(
            "Could not resolve this X post. Use the direct X Space URL "
            "to delete its cached summary."
        ) from exc


class Summarizer(commands.Cog):
    """Handle manual summary requests, queue inspection, and cache deletion."""

    def __init__(self, bot):
        """Initialize the slash-command cog.

        Args:
            bot: Discord bot providing the job worker, cache lock, and send limiter.
        """
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
        """Queue a topic-list request and acknowledge it in the request channel.

        Report handled source errors in the interaction follow-up.

        Args:
            interaction: Discord request carrying the channel and requester IDs.
            video: YouTube ID or public URL for a completed recording.

        Raises:
            discord.HTTPException: If deferral or the follow-up fails.
            discord.InteractionResponded: If the interaction was already answered.
            pymongo.errors.PyMongoError: If the job cannot be saved.
        """
        await interaction.response.defer(thinking=True)

        try:
            ref = await _resolve_manual_source(video)
            await self.bot.get_cog("ManualSummary").enqueue(
                ref=ref,
                operation=ManualSummaryOperation.TOPICS,
                channel_id=interaction.channel_id,
                requester_id=interaction.user.id,
            )
            source_label = VIDEO_SOURCE_LABELS[ref.source]
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
        video="A YouTube ID or a public URL for a finished video or an audio item.",
        topic_index="The index of the topic to get details for (1-based).",
    )
    async def detail(
        self,
        interaction: discord.Interaction,
        video: str,
        topic_index: int | None = None,
    ):
        """Queue a single-topic explanation or all topic details.

        Convert a supplied one-based index before enqueuing. Report handled source
        errors in the interaction follow-up.

        Args:
            interaction: Discord request carrying the channel and requester IDs.
            video: YouTube ID or public URL for a completed recording.
            topic_index: Optional one-based topic index. When omitted, request every
                topic.

        Raises:
            discord.HTTPException: If deferral or the follow-up fails.
            discord.InteractionResponded: If the interaction was already answered.
            pydantic.ValidationError: If the converted topic index fails job validation.
            pymongo.errors.PyMongoError: If the job cannot be saved.
        """
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
            source_label = VIDEO_SOURCE_LABELS[ref.source]
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
        """Clear cached summaries for one video and report the result privately.

        Hold the per-video lock during deletion. The follow-up reports the number of
        documents deleted, an invalid input, or a database failure.

        Args:
            interaction: Discord request used for the ephemeral response.
            video: YouTube ID or URL identifying the media to clear.

        Raises:
            discord.HTTPException: If deferral or the follow-up fails.
            discord.InteractionResponded: If the interaction was already answered.
        """
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            ref = await _resolve_cached_source(video)
        except ValueError as exc:
            await send_limited(
                self.bot, interaction.followup.send, str(exc), ephemeral=True
            )
            return

        try:
            async with self.bot.video_work.for_video(ref):
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
        """Report active automatic and manual jobs in next-attempt order.

        Use the stored title or video ID, source URL, and UTC next-attempt time for each
        entry. Split long responses into Discord-sized chunks.

        Args:
            interaction: Discord request used to report the queue.

        Raises:
            discord.HTTPException: If deferral or a follow-up fails.
            discord.InteractionResponded: If the interaction was already answered.
            pymongo.errors.PyMongoError: If MongoDB cannot read the queue.
            UnsupportedVideoSource: If a stored job cannot be turned into a media URL.
        """
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
    """Register the manual request and cache-management commands.

    Args:
        bot: Discord bot receiving the cog.

    Raises:
        discord.ClientException: If the cog is already registered.
    """
    await bot.add_cog(Summarizer(bot))
