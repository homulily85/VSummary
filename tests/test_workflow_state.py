from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from vsummary.cogs.autosummary.autosummary import Autosummary
from vsummary.model.channel import JobStatus
from vsummary.model.video import Topic


class FakeJob:
    status = JobStatus.QUEUED

    def __init__(self):
        self.video_id = "video"
        self.channel_id = "channel"
        self.channel_name = "Channel"
        self.title = "Title"
        self.available_at = datetime.now(UTC)
        self.next_attempt_at = datetime.now(UTC)
        self.retry_count = 0
        self.delivery_retry_count = 0
        self.summary_details = []
        self.last_error = None
        self.saved = 0

    async def save(self):
        self.saved += 1


@pytest.mark.asyncio
async def test_delivery_failure_keeps_persisted_summary_and_does_not_regenerate(
    monkeypatch,
):
    target = SimpleNamespace(send=AsyncMock(side_effect=RuntimeError("discord down")))
    cog = Autosummary.__new__(Autosummary)
    cog.bot = SimpleNamespace(get_channel=lambda _: target, notebook_client=object())
    cog.settings = SimpleNamespace(auto_summary_channel_id=123, delivery_retry_limit=3)
    cog.summary_service = SimpleNamespace(
        summarize=AsyncMock(return_value=[Topic(name="Intro", detail="Details")])
    )
    job = FakeJob()

    await cog._process_pending_video(job)

    assert job.summary_details == [Topic(name="Intro", detail="Details")]
    assert job.status is JobStatus.READY_TO_DELIVER
    cog.summary_service.summarize.assert_awaited_once()
    assert job.last_error == "discord down"

    monkeypatch.setattr(target, "send", AsyncMock())
    await cog._process_pending_video(job)

    cog.summary_service.summarize.assert_awaited_once()
    assert job.status is JobStatus.COMPLETED
