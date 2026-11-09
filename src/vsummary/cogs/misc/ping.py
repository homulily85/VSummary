"""Discord gateway latency reporting."""

import discord
from discord import app_commands
from discord.ext import commands


class Ping(commands.Cog):
    """Expose the gateway latency through the ``/ping`` command."""

    def __init__(self, bot):
        """Initialize the health-check cog.

        Args:
            bot: Discord bot whose gateway latency the command reports.
        """
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction):
        """Reply to ``/ping`` with the gateway latency rounded to milliseconds.

        Args:
            interaction: Discord interaction used to send the reply.

        Raises:
            discord.HTTPException: If Discord cannot send the reply.
            discord.InteractionResponded: If the interaction was already answered.
        """
        latency = round(self.bot.latency * 1000)  # Convert to milliseconds

        await interaction.response.send_message(f"Pong! Latency: {latency}ms")


async def setup(bot):
    """Register the ping cog with the extension loader.

    Args:
        bot: Discord bot receiving the cog.

    Raises:
        discord.ClientException: If the cog is already registered.
    """
    await bot.add_cog(Ping(bot))
