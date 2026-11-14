"""Quota checks and deferral signals shared by NotebookLM generation paths."""

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from notebooklm import NotebookLMError, RateLimitError
from notebooklm.types import UsageWindowKind

logger = logging.getLogger(__name__)
QUOTA_TIMEOUT_SECONDS = 60
QUOTA_RECHECK_SECONDS = 300


class NotebookQuotaExceeded(Exception):
    """Defer generation without consuming the job's retry budget."""

    def __init__(self, retry_at: datetime):
        self.retry_at = retry_at
        super().__init__(f"NotebookLM quota exhausted; retry at {retry_at.isoformat()}")


async def check_quota(client) -> None:
    """Raise a deferral for exhausted windows; allow work if metering is unavailable."""
    try:
        async with asyncio.timeout(QUOTA_TIMEOUT_SECONDS):
            usage = await client.settings.get_usage()
    except (NotebookLMError, TimeoutError) as exc:
        logger.warning("Could not read NotebookLM quota (%s).", type(exc).__name__)
        return
    if not usage.enabled:
        return
    if not usage.available:
        logger.warning("NotebookLM quota is unavailable; allowing generation.")
        return
    exhausted = [
        window
        for window in usage.windows
        if window.kind in (UsageWindowKind.FIVE_HOUR, UsageWindowKind.WEEKLY)
        and (window.remaining_percent <= 0 or window.used_percent >= 100)
    ]
    if exhausted:
        now = datetime.now(UTC)
        retry_at = max(window.resets_at for window in exhausted)
        if retry_at <= now:
            retry_at = now + timedelta(seconds=QUOTA_RECHECK_SECONDS)
        raise NotebookQuotaExceeded(retry_at)


@asynccontextmanager
async def quota_guard(client):
    """Check before work and classify SDK failures using a fresh quota snapshot."""
    await check_quota(client)
    try:
        yield
    except NotebookLMError as exc:
        await check_quota(client)
        if isinstance(exc, RateLimitError):
            delay = exc.retry_after
            if (
                isinstance(delay, bool)
                or not isinstance(delay, (int, float))
                or not math.isfinite(delay)
                or delay <= 0
            ):
                delay = QUOTA_RECHECK_SECONDS
            raise NotebookQuotaExceeded(
                datetime.now(UTC) + timedelta(seconds=delay)
            ) from exc
        raise
