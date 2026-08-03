import asyncio
import logging
import os

from beanie import init_beanie
from dotenv import load_dotenv
from notebooklm import NotebookLMClient
from pymongo import AsyncMongoClient
from rich.logging import RichHandler

from vsummary.bot import Bot
from vsummary.model.video import Video

logging.basicConfig(level=logging.INFO, handlers=[RichHandler()])


async def async_main():
    load_dotenv()
    TOKEN = os.getenv("DISCORD_TOKEN")
    MONGODB_URI = os.getenv("MONGODB_URI")

    if not TOKEN:
        logging.error("Please set DISCORD_TOKEN")
        exit(1)

    if not MONGODB_URI:
        logging.error("Please set MONGODB_URI")
        exit(1)

    logging.info("Connecting to MongoDB...")
    client = AsyncMongoClient(MONGODB_URI)
    await init_beanie(database=client.VSummary, document_models=[Video])
    logging.info(f"Connected to MongoDB!")

    async with NotebookLMClient.from_storage() as client:
        bot = Bot(notebook_client=client)
        logging.info("Bot is connecting...")
        await bot.start(TOKEN)


def main():
    """Synchronous entry point for the console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
print("NEW VERSION")
