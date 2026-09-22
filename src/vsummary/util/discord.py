"""Discord-sized message formatting and process-wide send throttling."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

DISCORD_MESSAGE_LIMIT = 2000


class DiscordMessageRateLimiter:
    """Serialize outbound messages and leave a safe gap between sends."""

    def __init__(
        self,
        minimum_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        """Create a limiter with injectable time functions for deterministic tests."""
        if minimum_interval_seconds <= 0:
            raise ValueError("minimum_interval_seconds must be positive")
        self.minimum_interval_seconds = minimum_interval_seconds
        self._clock = clock
        self._sleep = sleep
        self._lock = asyncio.Lock()
        self._next_send_at = 0.0

    async def send(self, sender, content: str, **kwargs: Any) -> Any:
        """Send one message after the global interval reserved for the last send."""
        async with self._lock:
            delay = self._next_send_at - self._clock()
            if delay > 0:
                await self._sleep(delay)
            self._next_send_at = self._clock() + self.minimum_interval_seconds
            return await sender(content, **kwargs)


async def send_limited(bot, sender, content: str, **kwargs: Any) -> Any:
    """Send through the bot's shared limiter, if the caller provides one."""
    limiter = getattr(bot, "message_rate_limiter", None)
    if limiter is None:
        return await sender(content, **kwargs)
    return await limiter.send(sender, content, **kwargs)


def split_message(text: str, limit: int = DISCORD_MESSAGE_LIMIT) -> list[str]:
    """Split text into non-empty Discord-sized chunks without losing content."""
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


async def send_chunked(sender, text: str, **kwargs) -> None:
    """Send every non-empty chunk of ``text`` through an async sender."""
    chunks = split_message(text)
    for index, chunk in enumerate(chunks):
        await sender(chunk, **kwargs if index == 0 else {})
