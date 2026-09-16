from unittest.mock import AsyncMock

import pytest

from vsummary.util.discord import DiscordMessageRateLimiter


@pytest.mark.asyncio
async def test_message_rate_limiter_spaces_out_consecutive_sends():
    now = [10.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    sender = AsyncMock()
    limiter = DiscordMessageRateLimiter(
        1.0,
        clock=lambda: now[0],
        sleep=sleep,
    )

    await limiter.send(sender, "first")
    now[0] += 0.25
    await limiter.send(sender, "second")

    assert sleeps == [0.75]
    assert sender.await_args_list[0].args == ("first",)
    assert sender.await_args_list[1].args == ("second",)
