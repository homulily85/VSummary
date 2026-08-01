import discord
from discord.ext import commands
from notebooklm import NotebookLMClient


class VSummaryBot(commands.Bot):
    def __init__(self, notebook_client: NotebookLMClient):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='/', intents=intents)
        self.notebook_client = notebook_client

    async def setup_hook(self):
        cogs = [
            "cogs.misc.ping",
            "cogs.summarizer.summarizer"
        ]

        for cog in cogs:
            await self.load_extension(cog)

        await self.tree.sync()

