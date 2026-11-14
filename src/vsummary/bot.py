"""Composition and lifecycle of the Discord client and its shared services."""

import discord
from discord.ext import commands
from notebooklm import NotebookLMClient

from vsummary.logging import DiscordLogHandler
from vsummary.settings import Settings, runtime_config
from vsummary.util.discord import DiscordMessageRateLimiter
from vsummary.util.holodex import HolodexClient
from vsummary.util.summarizer import NotebookLMSummaryService
from vsummary.util.video_work import VideoWorkCoordinator


class Bot(commands.Bot):
    """Host the Discord extensions and provide their shared application services.

    Attributes:
        notebook_client: Authenticated NotebookLM client supplied by the application.
        settings: Injected settings or ``None`` when using runtime defaults.
        holodex: Shared Holodex client managed by the application.
        summary_service: NotebookLM summary generator and cache interface.
        discord_log_handler: Optional sink started when Discord is ready.
        video_work: Process-local coordinator for per-video summary and cache work.
        message_rate_limiter: Shared limiter used by Discord send helpers.
    """

    def __init__(
        self,
        notebook_client: NotebookLMClient,
        settings: Settings | None = None,
        *,
        holodex: HolodexClient,
        discord_log_handler: DiscordLogHandler | None = None,
    ):
        """Initialize Discord and compose the services used by the cogs.

        Args:
            notebook_client: Authenticated NotebookLM client managed by the caller.
            settings: Application settings. When omitted, use the runtime defaults.
            holodex: Shared Holodex client managed by the caller.
            discord_log_handler: Optional handler for forwarding log events to Discord.
        """
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="/", intents=intents)
        self.notebook_client = notebook_client
        self.settings = settings
        self.holodex = holodex
        self.summary_service = NotebookLMSummaryService(
            notebook_client, config=runtime_config(settings)
        )
        self.discord_log_handler = discord_log_handler
        self.video_work = VideoWorkCoordinator()
        self.message_rate_limiter = DiscordMessageRateLimiter(
            runtime_config(settings).discord_send_interval_seconds
        )

    async def close(self):
        """Stop Discord logging, close cogs, and disconnect.

        Shutdown awaits each close operation in order, so errors from a close operation
        propagate to the caller.
        """
        if self.discord_log_handler is not None:
            await self.discord_log_handler.stop()
        for cog in tuple(self.cogs.values()):
            close = getattr(cog, "close", None)
            if close is not None:
                await close()
        await super().close()

    async def on_ready(self):
        """Start the optional Discord log sink after the gateway is ready."""
        if self.discord_log_handler is not None:
            await self.discord_log_handler.start(self)

    async def setup_hook(self):
        """Load the four application extensions and sync their slash commands.

        Raises:
            discord.ext.commands.ExtensionError: If an extension cannot be loaded.
            discord.HTTPException: If Discord rejects or cannot complete the command
                sync.
        """
        cogs = [
            "vsummary.cogs.misc.version",
            "vsummary.cogs.manualsummary.manualsummary",
            "vsummary.cogs.summarizer.summarizer",
            "vsummary.cogs.autosummary.autosummary",
        ]

        for cog in cogs:
            await self.load_extension(cog)

        await self.tree.sync()
