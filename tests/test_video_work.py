import asyncio

import pytest

from vsummary.util.video import VideoRef
from vsummary.util.video_work import VideoWorkCoordinator


@pytest.mark.asyncio
async def test_same_video_work_is_serialized():
    coordinator = VideoWorkCoordinator()
    ref = VideoRef(source="youtube", video_id="video")
    first_entered = asyncio.Event()
    release_first = asyncio.Event()
    second_entered = asyncio.Event()

    async def first():
        async with coordinator.for_video(ref):
            first_entered.set()
            await release_first.wait()

    async def second():
        await first_entered.wait()
        async with coordinator.for_video(ref):
            second_entered.set()

    first_task = asyncio.create_task(first())
    await first_entered.wait()
    second_task = asyncio.create_task(second())
    await asyncio.sleep(0)

    assert not second_entered.is_set()

    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert second_entered.is_set()


@pytest.mark.asyncio
async def test_different_videos_can_run_concurrently():
    coordinator = VideoWorkCoordinator()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release = asyncio.Event()

    async def work(ref, entered):
        async with coordinator.for_video(ref):
            entered.set()
            await release.wait()

    first = asyncio.create_task(
        work(VideoRef(source="youtube", video_id="first"), first_entered)
    )
    second = asyncio.create_task(
        work(VideoRef(source="youtube", video_id="second"), second_entered)
    )

    await asyncio.wait_for(
        asyncio.gather(first_entered.wait(), second_entered.wait()), timeout=0.1
    )
    release.set()
    await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_completed_and_failed_work_is_removed_from_coordinator():
    coordinator = VideoWorkCoordinator()
    ref = VideoRef(source="youtube", video_id="video")

    async with coordinator.for_video(ref):
        assert coordinator.tracked_video_count == 1

    assert coordinator.tracked_video_count == 0

    with pytest.raises(RuntimeError, match="failed"):
        async with coordinator.for_video(ref):
            raise RuntimeError("failed")

    assert coordinator.tracked_video_count == 0
