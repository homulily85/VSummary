import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), 'src')))

from dotenv import load_dotenv
from rich.logging import RichHandler
from src.VSummaryBot import VSummaryBot

logging.basicConfig(level=logging.INFO, handlers=[RichHandler()])

async def main():
    load_dotenv()
    TOKEN = os.getenv("DISCORD_TOKEN")

    if not TOKEN:
        logging.error("Please set DISCORD_TOKEN")
        exit(1)

    bot = VSummaryBot()

    logging.info("Bot is connecting...")
    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
