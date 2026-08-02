import asyncio
import logging
import os
import sys

from beanie import init_beanie
from notebooklm import NotebookLMClient
from pymongo import AsyncMongoClient
from src.model.video import Video

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'src')))

from dotenv import load_dotenv
from rich.logging import RichHandler
from src.VSummaryBot import VSummaryBot

logging.basicConfig(level=logging.INFO, handlers=[RichHandler()])

async def main():
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
    client =  AsyncMongoClient(MONGODB_URI)
    await init_beanie(database=client.VSummary, document_models=[Video])
    logging.info(f"Connected to MongoDB!")

    async with NotebookLMClient.from_storage() as client:
        bot = VSummaryBot(notebook_client=client)
        logging.info("Bot is connecting...")
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
