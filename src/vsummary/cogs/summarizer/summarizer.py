import discord
from discord import app_commands
from discord.ext import commands
from notebooklm import SourceAddError

from vsummary.util.summarizer import (
    get_topic_details,
    get_topic_details_all,
    get_topic_list,
)
from vsummary.util.video import UnsupportedVideoSource, parse_video_source

ALL_TOPICS_LABEL = "All topics"


class TopicsView(discord.ui.View):
    """Message components attached to the /topics result for picking details."""

    def __init__(self, bot, ref, topics, *, timeout: float | None = 180.0):
        super().__init__(timeout=timeout)
        self.bot = bot
        self.ref = ref

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
            await interaction.followup.send(
                f"Invalid topic index: {int(self.topic_select.values[0])}"
            )
        except SourceAddError:
            await interaction.followup.send(
                "Provided link or id is invalid or no transcript available."
            )
        finally:
            await interaction.edit_original_response(view=self)

    async def _send_selected_topic_detail(self, interaction):
        index = int(self.topic_select.values[0]) - 1
        detail = await get_topic_details(self.bot.notebook_client, self.ref, index)
        await Summarizer.send_chunked_message(
            interaction,
            f"**{detail['topic_name']}**\n{detail['detail']}",
        )

    async def _send_all_topic_details(self, interaction):
        details = await get_topic_details_all(self.bot.notebook_client, self.ref)
        for detail in details:
            message = f"**{detail['topic_name']}**\n{detail['detail']}"
            await Summarizer.send_chunked_message(interaction, message)


class Summarizer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    async def send_chunked_message(
        interaction: discord.Interaction,
        text: str,
        view: discord.ui.View | None = None,
    ):
        """
        Helper function to handle sending messages over Discord's 2000 char limit
        """
        first = True
        while len(text) > 2000:
            # Find a suitable split point to avoid breaking words/lines
            split_index = text.rfind("\n", 0, 2000)
            if split_index == -1:
                split_index = text.rfind(".", 0, 200000)
                if split_index == -1:
                    split_index = (
                        2000  # Fallback to hard limit if no suitable split point
                    )
                    # is found

            if first and view is not None:
                await interaction.followup.send(text[:split_index], view=view)
            else:
                await interaction.followup.send(text[:split_index])
            first = False
            text = text[
                split_index:
            ].lstrip()  # Remove leading newlines for the next chunk

        if text:
            if first and view is not None:
                await interaction.followup.send(text, view=view)
            else:
                await interaction.followup.send(text)

    @app_commands.command(name="topics", description="Get topic list from a video.")
    @app_commands.describe(video="The video URL or ID (for YouTube only) to summarize.")
    async def topics(
        self,
        interaction: discord.Interaction,
        video: str,
    ):
        """Get the topic list from a video.

        ``video`` may be a full link or a bare video ID.
        """
        try:
            await interaction.response.defer(thinking=True)

            ref = parse_video_source(video)
            topics = await get_topic_list(self.bot.notebook_client, ref)
            topics_list = [f"{i + 1}: {topic.name}" for i, topic in enumerate(topics)]

            topics_str = "\n".join(topics_list)

            if len(topics) > 25:
                topics_str += (
                    "\n\nOnly the first 25 topics are selectable here. "
                    "Use /detail for topics with an index larger than 25."
                )

            await self.send_chunked_message(
                interaction,
                f"Here are the topics mentioned in the video:\n{topics_str}",
                view=TopicsView(
                    bot=self.bot,
                    ref=ref,
                    topics=topics,
                ),
            )
        except UnsupportedVideoSource as exc:
            await interaction.followup.send(str(exc))
        except SourceAddError:
            await interaction.followup.send(
                "Provided link or id is invalid or no transcript available."
            )

    @app_commands.command(
        name="detail", description="Get details about a specific topic in the video."
    )
    @app_commands.describe(
        video="The video URL or ID (for YouTube only) to summarize.",
        topic_index="The index of the topic to get details for (1-based).",
    )
    async def detail(
        self,
        interaction: discord.Interaction,
        video: str,
        topic_index: int | None = None,
    ):
        """Get details about a topic in a video.

        ``video`` may be a full link or a bare video ID.
        """
        await interaction.response.defer(thinking=True)

        try:
            ref = parse_video_source(video)
            if topic_index is None:
                details = await get_topic_details_all(self.bot.notebook_client, ref)
                for i, detail in enumerate(details):
                    message = f"**{i + 1}. {detail['topic_name']}**\n{detail['detail']}"
                    await self.send_chunked_message(interaction, message)
            else:
                detail = await get_topic_details(
                    self.bot.notebook_client, ref, topic_index - 1
                )
                message = f"**{detail['topic_name']}**\n{detail['detail']}"
                await self.send_chunked_message(interaction, message)

        except UnsupportedVideoSource as exc:
            await interaction.followup.send(str(exc))
        except IndexError:
            await interaction.followup.send(f"Invalid topic index: {topic_index}")
        except SourceAddError:
            await interaction.followup.send(
                "Provided link or id is invalid or no transcript available."
            )


async def setup(bot):
    await bot.add_cog(Summarizer(bot))
