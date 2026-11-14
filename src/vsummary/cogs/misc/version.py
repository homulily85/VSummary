"""Discord command for reporting the running bot version."""

from importlib import metadata

import discord
from discord import app_commands
from discord.ext import commands


class Version(commands.Cog):
    """Expose the installed VSummary version through the ``/version`` command."""

    @app_commands.command(name="version", description="Show the bot's current version.")
    async def version(self, interaction: discord.Interaction):
        """Reply with the version of the installed VSummary package."""
        installed_version = metadata.version("vsummary")
        await interaction.response.send_message(f"VSummary v{installed_version}")


async def setup(bot):
    """Register the version cog with the extension loader."""
    await bot.add_cog(Version())
