"""Console entry points and ownership of the bot's external connections."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack

from beanie import init_beanie
from dotenv import load_dotenv
from notebooklm import NotebookLMClient
from pymongo import AsyncMongoClient

from vsummary.bot import Bot
from vsummary.logging import LoggingConfigurationError, configure_logging
from vsummary.model.channel import (
    AutoSummaryTopicPolicy,
    FollowedChannel,
    SummaryJob,
)
from vsummary.model.video import VideoSummary
from vsummary.settings import Settings, SettingsError, load_settings
from vsummary.util.holodex import HolodexClient

logger = logging.getLogger(__name__)


async def async_main(settings: Settings | None = None):
    """Configure and run the Discord bot until it exits.

    Load dotenv, configure logging, and open the Holodex, MongoDB, and NotebookLM
    clients. Close the bot and its external connections at shutdown.

    Args:
        settings: Validated settings to use. When omitted, load them from the
            environment and the default TOML file.

    Raises:
        SystemExit: If settings, logging configuration, or Holodex setup fails.
        pymongo.errors.PyMongoError: If MongoDB initialization fails.
        discord.LoginFailure: If Discord rejects the configured token.
        notebooklm.AuthError: If the stored NotebookLM session cannot authenticate.
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
    try:
        holodex = HolodexClient(api_key=settings.holodex_api_key)
    except ValueError as exc:
        logger.error("Application startup aborted: %s", exc)
        raise SystemExit(1) from exc

    async with AsyncExitStack() as services:
        services.push_async_callback(holodex.aclose)
        logger.info("Connecting to MongoDB...")
        mongo_client = AsyncMongoClient(settings.mongodb_uri, tz_aware=True)
        services.push_async_callback(mongo_client.close)
        database = mongo_client[settings.mongodb_database]
        await init_beanie(
            database=database,
            document_models=[
                FollowedChannel,
                AutoSummaryTopicPolicy,
                SummaryJob,
                VideoSummary,
            ],
        )
        logger.info("Connected to MongoDB!")

        notebook_client = await services.enter_async_context(
            NotebookLMClient.from_storage(keepalive=600)
        )
        bot = Bot(
            notebook_client=notebook_client,
            settings=settings,
            holodex=holodex,
            discord_log_handler=discord_log_handler,
        )
        services.push_async_callback(bot.close)
        logger.info("Bot is connecting...")
        await bot.start(settings.discord_token)


def main():
    """Run the bot on a new event loop as the console entry point.

    Raises:
        SystemExit: If startup rejects the settings or logging configuration.
        RuntimeError: If called from a running event loop in the same thread.
    """
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
