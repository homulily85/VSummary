"""Minimal Discord health-check command."""

import discord
from discord import app_commands
from discord.ext import commands


class Ping(commands.Cog):
    """Expose the gateway latency through the ``/ping`` command."""

    def __init__(self, bot):
        """Keep the Discord client used to read latency and reply."""
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot's latency.")
    async def ping(self, interaction: discord.Interaction):
        """Reply with the current Discord gateway latency in milliseconds."""
        latency = round(self.bot.latency * 1000)  # Convert to milliseconds

        await interaction.response.send_message(f"Pong! Latency: {latency}ms")


async def setup(bot):
    """Register this cog when Discord loads the extension."""
    await bot.add_cog(Ping(bot))
