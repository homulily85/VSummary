"""Discord command for the active NotebookLM account's quota."""

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands
from notebooklm import NotebookLMError
from notebooklm.types import UsageWindowKind

from vsummary.bot import Bot

logger = logging.getLogger(__name__)
QUOTA_UNAVAILABLE = "NotebookLM quota is temporarily unavailable. Please try again."
QUOTA_WINDOW_LABELS = {
    UsageWindowKind.FIVE_HOUR: "5h",
    UsageWindowKind.WEEKLY: "7d",
}


class Quota(commands.Cog):
    """Report live quota for the NotebookLM account used by the bot."""

    def __init__(self, bot: Bot):
        self.bot = bot

    @app_commands.command(
        name="quota", description="Show the bot's current NotebookLM quota."
    )
    async def quota(self, interaction: discord.Interaction):
        """Read quota and send a public response within Discord's reply window."""
        try:
            # Leave time for the initial Discord response without deferring.
            async with asyncio.timeout(60):
                usage = await self.bot.notebook_client.settings.get_usage()
        except (NotebookLMError, TimeoutError) as exc:
            logger.warning("Could not read NotebookLM quota (%s).", type(exc).__name__)
            await interaction.response.send_message(QUOTA_UNAVAILABLE)
            return

        if not usage.enabled:
            message = (
                "The current NotebookLM account does not have quota metering enabled."
            )
        elif not usage.available:
            message = QUOTA_UNAVAILABLE
        else:
            lines = ["**NotebookLM quota**"]
            for window in sorted(usage.windows, key=lambda window: window.kind):
                label = QUOTA_WINDOW_LABELS[window.kind]
                reset = discord.utils.format_dt(window.resets_at, style="F")
                lines.append(
                    f"**{label}** · {window.used_percent:.2f}% used · "
                    f"{window.remaining_percent:.2f}% remaining · Resets {reset}"
                )
            message = "\n".join(lines)

        await interaction.response.send_message(message)


async def setup(bot: Bot):
    """Register the quota cog with the extension loader."""
    await bot.add_cog(Quota(bot))
