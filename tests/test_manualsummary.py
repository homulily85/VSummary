from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import vsummary.cogs.manualsummary.manualsummary as manualsummary_module
from vsummary.cogs.manualsummary.manualsummary import ManualSummary
from vsummary.model.channel import JobStatus, ManualSummaryJob, ManualSummaryOperation


@pytest.mark.asyncio
async def test_topic_delivery_posts_plain_text_without_a_topic_picker(monkeypatch):
    sent = []

    class Channel:
        async def send(self, content, **kwargs):
            sent.append((content, kwargs))

    channel = Channel()
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = SimpleNamespace(get_channel=lambda _: channel)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    job = ManualSummaryJob.model_construct(
        source="youtube",
        video_id="dQw4w9WgXcQ",
        operation=ManualSummaryOperation.TOPICS,
        channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
        status=JobStatus.DELIVERING,
        delivery_chunks=["1: Intro", "2: Outro"],
    )
    get_topic_list = AsyncMock(side_effect=AssertionError("must not build a picker"))
    monkeypatch.setattr(manualsummary_module, "get_topic_list", get_topic_list)

    await cog._deliver(job)

    assert sent == [
        ("Here are the topics mentioned in the video:\n1: Intro", {}),
        ("2: Outro", {}),
    ]
    assert job.status is JobStatus.COMPLETED
    get_topic_list.assert_not_awaited()
