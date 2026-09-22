"""Application startup, resource ownership, and console entry points."""

from __future__ import annotations

import asyncio
import logging

from beanie import init_beanie
from dotenv import load_dotenv
from notebooklm import NotebookLMClient
from pymongo import AsyncMongoClient

from vsummary.bot import Bot
from vsummary.logging import LoggingConfigurationError, configure_logging
from vsummary.model.channel import FollowedChannel, ManualSummaryJob, SummaryJob
from vsummary.model.video import VideoSummary
from vsummary.settings import Settings, SettingsError, load_settings

logger = logging.getLogger(__name__)


async def async_main(settings: Settings | None = None):
    """Start the bot after configuring logging and database-backed models.

    The function owns the MongoDB client and NotebookLM context, so both are
    closed when Discord exits or startup fails.
    """
    load_dotenv()
    if settings is None:
        try:
            settings = load_settings(load_dotenv_file=False)
        except SettingsError as exc:
            logger.error(str(exc))
            raise SystemExit(1) from exc
    try:
        discord_log_handler = configure_logging(settings)
    except LoggingConfigurationError as exc:
        logger.critical("Application startup aborted: %s", exc)
        raise SystemExit(1) from exc

    logger.info("Connecting to MongoDB...")
    mongo_client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True)
    try:
        database = mongo_client[settings.mongodb_database]
        await init_beanie(
            database=database,
            document_models=[
                FollowedChannel,
                SummaryJob,
                ManualSummaryJob,
                VideoSummary,
            ],
        )
        logger.info("Connected to MongoDB!")

        async with NotebookLMClient.from_storage() as notebook_client:
            bot = Bot(
                notebook_client=notebook_client,
                settings=settings,
                discord_log_handler=discord_log_handler,
            )
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
