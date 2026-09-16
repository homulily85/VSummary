import discord
import httpx
from discord import app_commands
from discord.ext import commands
from notebooklm import NotebookLMError, SourceAddError

from vsummary.model.channel import ManualSummaryOperation
from vsummary.util.discord import send_limited, split_message
from vsummary.util.summarizer import (
    InvalidSummaryResponse,
    get_topic_details,
    get_topic_details_all,
    get_topic_list,
)
from vsummary.util.video import UnsupportedVideoSource, parse_video_source
from vsummary.util.video_work import get_video_work_coordinator

ALL_TOPICS_LABEL = "All topics"


class TopicsView(discord.ui.View):
    """Message components attached to the /topics result for picking details."""

    def __init__(self, bot, ref, topics, *, timeout: float | None = 180.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.ref = ref

        if not topics:
            raise ValueError("at least one topic is required for a topic selector")
        for topic in topics:
            if not topic.name.strip():
                raise ValueError("Topic labels cannot be blank")
            if len(topic.name) > 100:
                raise ValueError("Topic labels cannot exceed 100 characters")

        options = [
            discord.SelectOption(
                label=topic.name[:100],
                value=str(i + 1),
            )
            for i, topic in enumerate(topics[:25])
        ]
        self.topic_select = discord.ui.Select(
            placeholder="Choose a topic for details...",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.topic_select.callback = self.on_topic_selected
        self.add_item(self.topic_select)

        self.all_button = discord.ui.Button(
            label=ALL_TOPICS_LABEL,
            style=discord.ButtonStyle.secondary,
        )
        self.all_button.callback = self.on_all_topics
        self.add_item(self.all_button)

    def disable_all_components(self):
        for child in self.children:
            child.disabled = True

    async def on_topic_selected(self, interaction: discord.Interaction):
        await self._handle_detail_request(
            interaction,
            send_details=self._send_selected_topic_detail,
        )

    async def on_all_topics(self, interaction: discord.Interaction):
        await self._handle_detail_request(
            interaction,
            send_details=self._send_all_topic_details,
        )

    async def _handle_detail_request(self, interaction, send_details):
        await interaction.response.defer()
        self.disable_all_components()
        try:
            await send_details(interaction)
        except IndexError:
            await send_limited(
                self.bot,
                interaction.followup.send,
                f"Invalid topic index: {int(self.topic_select.values[0])}",
            )
        except SourceAddError:
            await send_limited(
                self.bot,
                interaction.followup.send,
                "Provided link or id is invalid or no transcript available.",
            )
        except InvalidSummaryResponse:
            await send_limited(
                self.bot,
                interaction.followup.send,
                "NotebookLM returned an invalid topic list. Please try again later.",
            )
        except (NotebookLMError, httpx.HTTPError, OSError):
            await send_limited(
                self.bot,
                interaction.followup.send,
                "NotebookLM is temporarily unavailable. Please try again later.",
            )
        finally:
            await interaction.edit_original_response(view=self)

    async def _send_selected_topic_detail(self, interaction):
        index = int(self.topic_select.values[0]) - 1
        if self.ref.source == "twitch":
            await self.bot.get_cog("ManualSummary").enqueue(
                ref=self.ref,
                operation=ManualSummaryOperation.DETAIL_ONE,
                topic_index=index,
                channel_id=interaction.channel_id,
                requester_id=interaction.user.id,
            )
            await send_limited(
                self.bot,
                interaction.followup.send,
                "Twitch detail is queued and will be posted here.",
            )
            return
        async with get_video_work_coordinator(self.bot).for_video(self.ref):
            detail = await get_topic_details(self.bot.notebook_client, self.ref, index)
        await Summarizer.send_chunked_message(
            self.bot,
            interaction,
            f"**{detail['topic_name']}**\n{detail['detail']}",
        )

    async def _send_all_topic_details(self, interaction):
        if self.ref.source == "twitch":
            await self.bot.get_cog("ManualSummary").enqueue(
                ref=self.ref,
                operation=ManualSummaryOperation.DETAIL_ALL,
                channel_id=interaction.channel_id,
                requester_id=interaction.user.id,
            )
            await send_limited(
                self.bot,
                interaction.followup.send,
                "Twitch details are queued and will be posted here.",
            )
            return
        async with get_video_work_coordinator(self.bot).for_video(self.ref):
            details = await get_topic_details_all(self.bot.notebook_client, self.ref)
        for detail in details:
            message = f"**{detail['topic_name']}**\n{detail['detail']}"
            await Summarizer.send_chunked_message(self.bot, interaction, message)


class Summarizer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    async def send_chunked_message(
        bot,
        interaction: discord.Interaction,
        text: str,
        view: discord.ui.View | None = None,
    ):
        for index, chunk in enumerate(split_message(text)):
            if index == 0 and view is not None:
                await send_limited(bot, interaction.followup.send, chunk, view=view)
            else:
                await send_limited(bot, interaction.followup.send, chunk)

    @app_commands.command(name="topics", description="Get topic list from a video.")
    @app_commands.describe(
        video="A YouTube URL or ID, or a full public Twitch VOD URL to summarize."
    )
    async def topics(
        self,
        interaction: discord.Interaction,
        video: str,
    ):
        """Get the topic list from a video.

        ``video`` may be a YouTube URL/ID or a full Twitch VOD URL.
        """
        try:
            await interaction.response.defer(thinking=True)

            ref = parse_video_source(video)
            if ref.source == "twitch":
                await self.bot.get_cog("ManualSummary").enqueue(
                    ref=ref,
                    operation=ManualSummaryOperation.TOPICS,
                    channel_id=interaction.channel_id,
                    requester_id=interaction.user.id,
                )
                await send_limited(
                    self.bot,
                    interaction.followup.send,
                    "Twitch VOD topics are queued and will be posted here.",
                )
                return
            async with get_video_work_coordinator(self.bot).for_video(ref):
                topics = await get_topic_list(self.bot.notebook_client, ref)
            topics_list = [f"{i + 1}: {topic.name}" for i, topic in enumerate(topics)]

            topics_str = "\n".join(topics_list)

            if len(topics) > 25:
                topics_str += (
                    "\n\nOnly the first 25 topics are selectable here. "
                    "Use /detail for topics with an index larger than 25."
                )

            await self.send_chunked_message(
                self.bot,
                interaction,
                f"Here are the topics mentioned in the video:\n{topics_str}",
                view=TopicsView(
                    bot=self.bot,
                    ref=ref,
                    topics=topics,
                ),
            )
        except UnsupportedVideoSource as exc:
            await send_limited(self.bot, interaction.followup.send, str(exc))
        except SourceAddError:
            await send_limited(
                self.bot,
                interaction.followup.send,
                "Provided link or id is invalid or no transcript available.",
            )
        except InvalidSummaryResponse:
            await send_limited(
                self.bot,
                interaction.followup.send,
                "NotebookLM returned an invalid topic list. Please try again later.",
            )
        except (NotebookLMError, httpx.HTTPError, OSError):
            await send_limited(
                self.bot,
                interaction.followup.send,
                "NotebookLM is temporarily unavailable. Please try again later.",
            )

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
        """Get details about a topic in a video.

        ``video`` may be a YouTube URL/ID or a full Twitch VOD URL.
        """
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
