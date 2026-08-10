from __future__ import annotations

import asyncio
import logging

from beanie import init_beanie
from dotenv import load_dotenv
from notebooklm import NotebookLMClient
from pymongo import AsyncMongoClient
from rich.logging import RichHandler

from vsummary.bot import Bot
from vsummary.migrations import migrate_database
from vsummary.model.channel import FollowedChannel, SummaryJob
from vsummary.model.video import VideoSummary
from vsummary.settings import Settings, SettingsError, load_settings

logger = logging.getLogger(__name__)


def configure_logging() -> None:
    """Configure logging once, during application startup."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, handlers=[RichHandler()])


async def async_main(settings: Settings | None = None):
    configure_logging()
    load_dotenv()
    if settings is None:
        try:
            settings = load_settings(load_dotenv_file=False)
        except SettingsError as exc:
            logger.error(str(exc))
            raise SystemExit(1) from exc

    logger.info("Connecting to MongoDB...")
    mongo_client = AsyncMongoClient(settings.mongodb_uri)
    try:
        database = mongo_client[settings.mongodb_database]
        await migrate_database(database)
        await init_beanie(
            database=database,
            document_models=[FollowedChannel, SummaryJob, VideoSummary],
        )
        logger.info("Connected to MongoDB!")

        async with NotebookLMClient.from_storage() as notebook_client:
            bot = Bot(notebook_client=notebook_client, settings=settings)
            logger.info("Bot is connecting...")
            try:
                await bot.start(settings.discord_token)
            finally:
                await bot.close()
    finally:
        await mongo_client.close()


def main():
    """Synchronous entry point for the console script."""
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
