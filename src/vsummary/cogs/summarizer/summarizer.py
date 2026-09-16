import discord
from discord import app_commands
from discord.ext import commands

from vsummary.model.channel import ManualSummaryOperation
from vsummary.util.discord import send_limited
from vsummary.util.video import UnsupportedVideoSource, parse_video_source


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


async def setup(bot):
    await bot.add_cog(Summarizer(bot))
