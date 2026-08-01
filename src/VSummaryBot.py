import discord
from discord.ext import commands


class VSummaryBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='/', intents=intents)

    async def setup_hook(self):
        cogs = [
            "cogs.misc.ping",
        ]

        for cog in cogs:
            await self.load_extension(cog)

        await self.tree.sync()

