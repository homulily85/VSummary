import discord
from discord.ext import commands
from notebooklm import NotebookLMClient

from vsummary.settings import Settings


class Bot(commands.Bot):
    def __init__(
        self,
        notebook_client: NotebookLMClient,
        settings: Settings | None = None,
        holodex=None,
    ):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="/", intents=intents)
        self.notebook_client = notebook_client
        self.settings = settings
        self.holodex = holodex

    async def close(self):
        for cog in tuple(self.cogs.values()):
            close = getattr(cog, "close", None)
            if close is not None:
                await close()
        if self.holodex is not None:
            await self.holodex.aclose()
            self.holodex = None
        await super().close()

    async def setup_hook(self):
        cogs = [
            "vsummary.cogs.misc.ping",
            "vsummary.cogs.summarizer.summarizer",
            "vsummary.cogs.autosummary.autosummary",
        ]

        for cog in cogs:
            await self.load_extension(cog)

        await self.tree.sync()
