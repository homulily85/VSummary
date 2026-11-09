"""In-memory coordination for cache work belonging to one video."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from vsummary.util.video import VideoRef


@dataclass
class _VideoWorkState:
    """Lock and reference count retained while one normalized video is in use."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class VideoWorkCoordinator:
    """Serialize NotebookLM/cache work for each normalized video reference.

    The coordinator is intentionally process-local.  A state is retained while
    work is active or waiting, then removed once its final user exits.
    """

    def __init__(self) -> None:
        """Initialize an empty registry of per-video locks."""
        self._states: dict[tuple[str, str], _VideoWorkState] = {}

    @asynccontextmanager
    async def for_video(self, ref: VideoRef):
        """Acquire exclusive work access for ``ref`` until the context exits."""
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
