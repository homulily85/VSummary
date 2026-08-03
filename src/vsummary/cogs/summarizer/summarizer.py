import traceback
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from vsummary.util.summarizer import get_topic_list, get_topic_details, get_topic_details_all


class Summarizer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="topics", description="Get topics from a video.")
    async def topics(self, interaction: discord.Interaction, video_link: str):
        await interaction.response.defer(thinking=True)

        topics = await get_topic_list(self.bot.notebook_client, video_link)
        topics_list = [f"{i + 1}: {topic.name}" for i, topic in enumerate(topics)]

        topics_str = "\n".join(topics_list)

        await interaction.followup.send(
            f"Here are the topics mentioned in the video:\n{topics_str}")

    @app_commands.command(name="detail",
                          description="Get details about a specific topic in the video.")
    async def detail(self, interaction: discord.Interaction, video_link: str,
                     topic_index: Optional[int] = None):
        await interaction.response.defer(thinking=True)

        async def send_chunked_message(text: str):
            """
            Helper function to handle sending messages over Discord's 2000 char limit
            """
            while len(text) > 2000:
                # Find a suitable split point to avoid breaking words/lines
                split_index = text.rfind('\n', 0, 2000)
                if split_index == -1:
                    split_index = text.rfind('.', 0, 200000)
                    if split_index == -1:
                        split_index = 2000  # Fallback to hard limit if no suitable split point
                        # is found

                await interaction.followup.send(text[:split_index])
                text = text[split_index:].lstrip()  # Remove leading newlines for the next chunk

            if text:
                await interaction.followup.send(text)

        try:
            if topic_index is None:
                details = await get_topic_details_all(self.bot.notebook_client, video_link)
                for i, detail in enumerate(details):
                    message = f"**{i + 1}. {detail['topic_name']}**\n{detail['detail']}"
                    await send_chunked_message(message)
            else:
                detail = await get_topic_details(self.bot.notebook_client, video_link,
                                                 topic_index - 1)
                message = f"**{detail['topic_name']}**\n{detail['detail']}"
                await send_chunked_message(message)

        except IndexError:
            await interaction.followup.send(f"Invalid topic index: {topic_index}")
        except Exception:
            traceback.print_exc()
            await interaction.followup.send("Unexpected error occurred. Please try again later.")


async def setup(bot):
    await bot.add_cog(Summarizer(bot))
