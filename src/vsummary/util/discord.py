"""Message splitting and shared rate limiting for Discord sends."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from vsummary.bot import Bot

DISCORD_MESSAGE_LIMIT = 2000


class DiscordMessageRateLimiter:
    """Coordinate sends through one lock and reserve a minimum interval between them."""

    def __init__(
        self,
        minimum_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        """Create a limiter with injectable timing functions.

        Args:
            minimum_interval_seconds: Positive gap between reserved send times.
            clock: Monotonic clock returning seconds.
            sleep: Asynchronous function used to wait for a send slot.

        Raises:
            ValueError: If the minimum interval is zero or negative.
        """
        if minimum_interval_seconds <= 0:
            raise ValueError("minimum_interval_seconds must be positive")
        self.minimum_interval_seconds = minimum_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_send_at = 0.0

    async def send(self, sender, content: str, **kwargs: Any) -> Any:
        """Reserve a send slot and await the sender while holding the limiter lock.

        Args:
            sender: Asynchronous callback that sends one Discord message.
            content: Message body passed as the callback's first argument.
            **kwargs: Additional keyword arguments forwarded to the sender.

        Returns:
            Result returned by the sender. Exceptions from the sender propagate.
        """
        async with self._lock:
            delay = self._next_send_at - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next_send_at = self._clock() + self.minimum_interval_seconds
            return await sender(content, **kwargs)


async def send_limited(bot: "Bot", sender, content: str, **kwargs: Any) -> Any:
    """Send a message using the bot's limiter when one is installed.

    Args:
        bot: Bot exposing the shared Discord message limiter.
        sender: Asynchronous message callback.
        content: Body of the message.
        **kwargs: Additional keyword arguments forwarded to the sender.

    Returns:
        Result returned by the sender. Exceptions from the sender propagate.
    """
    limiter = bot.message_rate_limiter
    if limiter is None:
        return await sender(content, **kwargs)
    return await limiter.send(sender, content, **kwargs)


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split a message into ordered chunks that fit the Discord character limit.

    Prefer a newline or sentence boundary before cutting at the limit. Trim whitespace
    at chunk boundaries.

    Args:
        text: Message to split.
        limit: Maximum characters per chunk, defaulting to Discord's message limit.

    Returns:
        Non-empty chunks in send order. Return an empty list for empty input or for
        whitespace-only input with a positive limit.

    Raises:
        ValueError: If the limit is non-positive and the input string is non-empty.
    """
    if not text:
        return []
    if limit <= 0:
        raise ValueError("limit must be positive")

    chunks: list[str] = []
    remaining = text.lstrip()
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, limit + 1)
        if split_at <= 0:
            punctuation = max(
                remaining.rfind(". ", 0, limit + 1),
                remaining.rfind("! ", 0, limit + 1),
                remaining.rfind("? ", 0, limit + 1),
            )
            split_at = punctuation + 1 if punctuation > 0 else limit

        chunk = remaining[:split_at].rstrip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].lstrip()

    return chunks
