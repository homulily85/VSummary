import discord
from discord import app_commands
from discord.ext import commands
from rich import json
from src.util.summarizer import get_topic_list


class Summarizer(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="topics", description="Get topics from a video.")
    async def topics(self, interaction: discord.Interaction, video_link: str):
        await interaction.response.defer(thinking=True)

        topics = json.loads(await get_topic_list(self.bot.notebook_client, video_link))
        topics_list = [f"{topic['index']}: {topic['name']}" for topic in topics]
        topics_str = "\n".join(topics_list)

        await interaction.followup.send(
            f"Here are the topics mentioned in the video:\n{topics_str}")


async def setup(bot):
    await bot.add_cog(Summarizer(bot))
