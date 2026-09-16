import discord
from discord.ext import commands
from notebooklm import NotebookLMClient

from vsummary.logging import DiscordLogHandler
from vsummary.settings import Settings
from vsummary.util.video_work import VideoWorkCoordinator


class Bot(commands.Bot):
    def __init__(
        self,
        notebook_client: NotebookLMClient,
        settings: Settings | None = None,
        holodex=None,
        discord_log_handler: DiscordLogHandler | None = None,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="/", intents=intents)
        self.notebook_client = notebook_client
        self.settings = settings
        self.holodex = holodex
        self.discord_log_handler = discord_log_handler
        self.video_work = VideoWorkCoordinator()

    async def close(self):
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
        cogs = [
            "vsummary.cogs.misc.ping",
            "vsummary.cogs.summarizer.summarizer",
            "vsummary.cogs.autosummary.autosummary",
        ]

        for cog in cogs:
            await self.load_extension(cog)

        await self.tree.sync()
