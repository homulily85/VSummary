import discord
from discord import app_commands
from discord.ext import commands

from vsummary.model.channel import (
    JobStatus,
    ManualSummaryJob,
    ManualSummaryOperation,
    SummaryJob,
)
from vsummary.util.discord import send_limited, split_message
from vsummary.util.video import (
    UnsupportedVideoSource,
    VideoRef,
    build_video_url,
    parse_video_source,
)

_ACTIVE_JOB_STATUSES = [
    JobStatus.QUEUED.value,
    JobStatus.GENERATING.value,
    JobStatus.READY_TO_DELIVER.value,
    JobStatus.DELIVERING.value,
]


class Summarizer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="topics", description="Get topic list from a video.")
    @app_commands.describe(
        video="A YouTube URL or ID, or a full public Twitch VOD URL to summarize."
    )
    async def topics(
        self,
        interaction: discord.Interaction,
        video: str,
    ):
        """Queue a topic-list request for a supported video source."""
        await interaction.response.defer(thinking=True)

        try:
            ref = parse_video_source(video)
            await self.bot.get_cog("ManualSummary").enqueue(
                ref=ref,
                operation=ManualSummaryOperation.TOPICS,
                channel_id=interaction.channel_id,
                requester_id=interaction.user.id,
            )
            source_label = "Twitch VOD" if ref.source == "twitch" else "YouTube video"
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"{source_label} topics are queued and will be posted here.",
            )
        except UnsupportedVideoSource as exc:
            await send_limited(self.bot, interaction.followup.send, str(exc))

    @app_commands.command(
        name="detail", description="Get details about a specific topic in the video."
    )
    @app_commands.describe(
        video="A YouTube URL or ID, or a full public Twitch VOD URL to summarize.",
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
            ref = parse_video_source(video)
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
            source_label = "Twitch VOD" if ref.source == "twitch" else "YouTube video"
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"{source_label} summary is queued and will be posted here.",
            )
        except UnsupportedVideoSource as exc:
            await send_limited(self.bot, interaction.followup.send, str(exc))

    @app_commands.command(name="queue", description="List videos awaiting a summary.")
    async def queue(self, interaction: discord.Interaction):
        """List active auto-summary and manual-summary jobs in processing order."""
        await interaction.response.defer(thinking=True)
        query = {"status": {"$in": _ACTIVE_JOB_STATUSES}}
        auto_jobs = await SummaryJob.find(query).to_list()
        manual_jobs = await ManualSummaryJob.find(query).to_list()
        entries = [
            (
                job.next_attempt_at,
                "Auto",
                job.title,
                build_video_url(VideoRef(source="youtube", video_id=job.video_id)),
            )
            for job in auto_jobs
        ]
        entries.extend(
            (
                job.next_attempt_at,
                "Manual",
                getattr(job, "title", None) or job.video_id,
                build_video_url(VideoRef(source=job.source, video_id=job.video_id)),
            )
            for job in manual_jobs
        )
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
    await bot.add_cog(Summarizer(bot))
