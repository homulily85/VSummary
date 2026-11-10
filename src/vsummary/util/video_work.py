"""Process-local serialization of summary and cache operations by video."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from vsummary.util.video import VideoRef


@dataclass
class _VideoWorkState:
    """Track one video while callers hold or wait for its lock.

    Attributes:
        lock: Mutual exclusion for work on this video.
        users: Number of callers holding or waiting for the lock.
    """

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class VideoWorkCoordinator:
    """Serialize work by source and video ID within one process.

    Keep each lock while work is active or waiting. Discard it when the final caller
    exits.
    """

    def __init__(self) -> None:
        """Start with an empty registry of video locks."""
        self._states: dict[tuple[str, str], _VideoWorkState] = {}

    @asynccontextmanager
    async def for_video(self, ref: VideoRef):
        """Hold exclusive work access for a video within an asynchronous context.

        Args:
            ref: Video reference keyed by its source and video ID.

        Yields:
            ``None`` after the caller acquires the lock.

        Raises:
            asyncio.CancelledError: If the caller is cancelled while waiting for access.
        """
        key = (ref.source, ref.video_id)
        state = self._states.get(key)
        if state is None:
            state = _VideoWorkState()
            self._states[key] = state

        state.users += 1
        acquired = False
        try:
            await state.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                state.lock.release()
            state.users -= 1
            if state.users == 0 and self._states.get(key) is state:
                del self._states[key]
