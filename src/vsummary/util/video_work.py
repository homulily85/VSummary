"""In-memory coordination for cache work belonging to one video."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from vsummary.util.video import VideoRef


@dataclass
class _VideoWorkState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


class VideoWorkCoordinator:
    """Serialize NotebookLM/cache work for each normalized video reference.

    The coordinator is intentionally process-local.  A state is retained while
    work is active or waiting, then removed once its final user exits.
    """

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], _VideoWorkState] = {}

    @property
    def tracked_video_count(self) -> int:
        """Number of videos that currently have active or waiting work."""
        return len(self._states)

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


def get_video_work_coordinator(bot) -> VideoWorkCoordinator:
    """Return the coordinator shared by a bot, including lightweight test bots."""
    coordinator = getattr(bot, "video_work", None)
    if coordinator is None:
        coordinator = VideoWorkCoordinator()
        bot.video_work = coordinator
    return coordinator
