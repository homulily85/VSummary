"""Discord client composition and cog lifecycle management."""

import discord
from discord.ext import commands
from notebooklm import NotebookLMClient

from vsummary.logging import DiscordLogHandler
from vsummary.settings import Settings
from vsummary.util.discord import DiscordMessageRateLimiter
from vsummary.util.video_work import VideoWorkCoordinator


class Bot(commands.Bot):
    """Application Discord client with shared external-service dependencies.

    Attributes:
        notebook_client: Process-local authenticated NotebookLM client.
        settings: Validated runtime configuration.
        holodex: Optional shared Holodex client owned by the application.
        video_work: Per-video coordinator used by summary workers.
        message_rate_limiter: Shared limiter for every Discord send.
    """

    def __init__(
        self,
        notebook_client: NotebookLMClient,
        settings: Settings | None = None,
        holodex=None,
        discord_log_handler: DiscordLogHandler | None = None,
    ):
        """Create a bot and attach services that cogs consume by dependency lookup."""
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="/", intents=intents)
        self.notebook_client = notebook_client
        self.settings = settings
        self.holodex = holodex
        self.discord_log_handler = discord_log_handler
        self.video_work = VideoWorkCoordinator()
        self.message_rate_limiter = DiscordMessageRateLimiter(
            getattr(settings, "discord_send_interval_seconds", 1.0)
        )

    async def close(self):
        """Stop log delivery, close cogs and owned clients, then close Discord."""
        if self.discord_log_handler is not None:
            await self.discord_log_handler.stop()
        for cog in tuple(self.cogs.values()):
            close = getattr(cog, "close", None)
            if close is not None:
                await close()
        if self.holodex is not None:
            await self.holodex.aclose()
            self.holodex = None
        await super().close()

    async def on_ready(self):
        """Start optional Discord logging only after the client is ready."""
        if self.discord_log_handler is not None:
            await self.discord_log_handler.start(self)

    async def setup_hook(self):
        """Load all cogs and synchronize their slash commands with Discord."""
        cogs = [
            "vsummary.cogs.misc.ping",
            "vsummary.cogs.manualsummary.manualsummary",
            "vsummary.cogs.summarizer.summarizer",
            "vsummary.cogs.autosummary.autosummary",
        ]

        for cog in cogs:
            await self.load_extension(cog)

        await self.tree.sync()
