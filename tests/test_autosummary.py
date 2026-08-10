from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from notebooklm import SourceAddError

from vsummary.cogs.autosummary import autosummary as autosummary_module
from vsummary.cogs.autosummary.autosummary import Autosummary
from vsummary.model.channel import PendingVideoStatus
from vsummary.util.holodex import (
    MAX_RETRIES,
    ChannelNotFoundError,
    HolodexVideo,
    exponential_backoff_hours,
)

CHANNEL_ID = "UCQ0UDLQCjY0rmuxCDE38FGg"
VIDEO_ID = "abcdefghijk"


def expr(attr, op, value):
    return {"attr": attr, "op": op, "value": value}


class FieldProxy:
    """Mimics beanie's ``Model.field == value`` expression API."""

    def __init__(self, obj, name):
        self.obj = obj
        self.name = name

    def __eq__(self, other):
        return expr(self.name, "eq", other)

    def __le__(self, other):
        return expr(self.name, "le", other)


class FakeChannel:
    def __init__(self, channel_id=CHANNEL_ID, name="Test Channel"):
        self.channel_id = channel_id
        self.name = name
        self.save = AsyncCall()
        self.deleted = False

    async def delete(self):
        self.deleted = True


class FakePending:
    def __init__(
        self,
        *,
        video_id=VIDEO_ID,
        channel_id=CHANNEL_ID,
        channel_name="Test Channel",
        title="New Stream",
        topic_id="stream",
        available_at=None,
        retry_count=0,
        next_attempt_at=None,
        status=PendingVideoStatus.QUEUED,
    ):
        self.video_id = video_id
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.title = title
        self.topic_id = topic_id
        self.available_at = available_at or (datetime.now(UTC) - timedelta(hours=3))
        self.retry_count = retry_count
        self.next_attempt_at = next_attempt_at or (
            datetime.now(UTC) - timedelta(minutes=1)
        )
        self.status = status
        self.save = AsyncCall()


class AsyncCall:
    def __init__(self):
        self.await_count = 0

    async def __call__(self, *args, **kwargs):
        self.await_count += 1


class FakeHolodex:
    def __init__(self, videos=None, channel_valid=True):
        self.videos = videos or []
        self.channel_valid = channel_valid
        self.get_channel_calls = 0

    async def get_channel(self, channel_id):
        self.get_channel_calls += 1
        if not self.channel_valid:
            raise ChannelNotFoundError("not found")
        return type("Channel", (), {"id": channel_id, "name": "Test Channel"})()

    async def get_channel_videos(self, channel_id):
        return list(self.videos)


class SimpleResult:
    def __init__(self, items):
        self.items = items

    async def to_list(self):
        return list(self.items)


class BaseModelStub:
    """Base for fake model classes exposing beanie-style query expressions."""

    def __init__(self, fields):
        for name in fields:
            setattr(self, name, FieldProxy(self, name))

    def _matches(self, item, condition):
        attr, op, value = condition["attr"], condition["op"], condition["value"]
        actual = getattr(item, attr)
        if op == "eq":
            return actual == value
        if op == "le":
            return actual <= value
        raise AssertionError(f"unhandled op {op}")


class FakeChannelModel(BaseModelStub):
    def __init__(self, channels=None):
        super().__init__(["channel_id", "name", "added_at"])
        self.channels = channels or []
        self.created = []
        self.deleted = []
        self.find_one_calls = []

    def find(self, *args, **kwargs):
        return SimpleResult(self.channels)

    async def find_one(self, *conditions, **kwargs):
        self.find_one_calls.append((conditions, kwargs))
        for channel in self.channels:
            if all(self._matches(channel, c) for c in conditions):
                return channel
        return None

    def __call__(self, **kwargs):
        channel = FakeChannel(channel_id=kwargs["channel_id"], name=kwargs["name"])
        self.created.append(channel)
        return channel

    async def delete(self, obj=None):
        if obj is not None:
            self.deleted.append(obj)


class FakePendingModel(BaseModelStub):
    def __init__(self, items=None):
        super().__init__(["video_id", "channel_id", "status", "next_attempt_at"])
        self.items = items or []
        self.created = []
        self.deleted_queries = []

    def find(self, *conditions, **kwargs):
        matching = []
        for item in self.items + self.created:
            if all(self._matches(item, c) for c in conditions):
                matching.append(item)
        return SimpleResult(matching)

    async def find_one(self, *conditions, **kwargs):
        for item in self.items + self.created:
            if all(self._matches(item, c) for c in conditions):
                return item
        return None

    def __call__(self, **kwargs):
        item = FakePending(**kwargs)
        self.created.append(item)
        return item

    async def delete_many(self, *conditions, **kwargs):
        self.deleted_queries.append((conditions, kwargs))


def make_video(
    *,
    id=VIDEO_ID,
    title="New Stream",
    topic_id="stream",
    available_at=None,
    channel_name="Test Channel",
):
    return HolodexVideo(
        id=id,
        title=title,
        topic_id=topic_id,
        available_at=available_at or (datetime.now(UTC) - timedelta(hours=3)),
        channel_name=channel_name,
    )


def make_cog(mocker, holodex=None, channel_model=None, pending_model=None):
    bot = mocker.MagicMock()
    target_channel = mocker.MagicMock()
    target_channel.send = AsyncMock()
    bot.get_channel = mocker.MagicMock(return_value=target_channel)
    bot.notebook_client = mocker.MagicMock()
    cog = Autosummary(bot)
    cog.holodex = holodex or FakeHolodex()
    cog.channel_model = channel_model or FakeChannelModel()
    cog.pending_model = pending_model or FakePendingModel()
    return cog


def make_interaction(mocker):
    interaction = mocker.MagicMock()
    interaction.response.defer = mocker.AsyncMock()
    interaction.followup.send = mocker.AsyncMock()
    return interaction


class TestAddChannel:
    async def test_success_replies_and_saves(self, mocker):
        holodex = FakeHolodex()
        channel_model = FakeChannelModel()
        cog = make_cog(mocker, holodex=holodex, channel_model=channel_model)
        interaction = make_interaction(mocker)

        await cog.addchannel.callback(cog, interaction, channel_id=CHANNEL_ID)

        interaction.response.defer.assert_awaited_once()
        sent = interaction.followup.send.await_args.args[0]
        assert "Test Channel" in sent
        assert holodex.get_channel_calls == 1
        assert len(channel_model.created) == 1

    async def test_404_reports_error_and_does_not_save(self, mocker):
        holodex = FakeHolodex(channel_valid=False)
        channel_model = FakeChannelModel()
        cog = make_cog(mocker, holodex=holodex, channel_model=channel_model)
        interaction = make_interaction(mocker)

        await cog.addchannel.callback(cog, interaction, channel_id="bad")

        sent = interaction.followup.send.await_args.args[0]
        assert "not found" in sent.lower()
        assert channel_model.created == []

    async def test_existing_channel_reports_already_followed(self, mocker):
        channel_model = FakeChannelModel(
            channels=[FakeChannel(channel_id=CHANNEL_ID, name="Already")]
        )
        cog = make_cog(mocker, channel_model=channel_model)
        interaction = make_interaction(mocker)

        await cog.addchannel.callback(cog, interaction, channel_id=CHANNEL_ID)

        sent = interaction.followup.send.await_args.args[0]
        assert "already" in sent.lower()
        assert channel_model.created == []


class TestRemoveChannel:
    async def test_removes_channel_and_videos(self, mocker):
        target = FakeChannel(channel_id=CHANNEL_ID, name="Test Channel")
        channel_model = FakeChannelModel(channels=[target])
        pending_model = FakePendingModel()
        cog = make_cog(mocker, channel_model=channel_model, pending_model=pending_model)
        interaction = make_interaction(mocker)

        await cog.removechannel.callback(cog, interaction, channel_id=CHANNEL_ID)

        sent = interaction.followup.send.await_args.args[0]
        assert "removed" in sent.lower()
        assert len(pending_model.deleted_queries) == 1
        assert pending_model.deleted_queries[0][0][0]["attr"] == "channel_id"
        assert target.deleted

    async def test_not_followed_reports(self, mocker):
        cog = make_cog(mocker, channel_model=FakeChannelModel())
        interaction = make_interaction(mocker)

        await cog.removechannel.callback(cog, interaction, channel_id="unknown")

        sent = interaction.followup.send.await_args.args[0]
        assert "not" in sent.lower()


class TestListChannels:
    async def test_empty_state(self, mocker):
        cog = make_cog(mocker, channel_model=FakeChannelModel())
        interaction = make_interaction(mocker)

        await cog.listchannels.callback(cog, interaction)

        sent = interaction.followup.send.await_args.args[0]
        assert "no channels" in sent.lower()

    async def test_lists_channels(self, mocker):
        channel_model = FakeChannelModel(
            channels=[
                FakeChannel(channel_id=CHANNEL_ID, name="Test Channel"),
                FakeChannel(channel_id="ch2", name="Beta"),
            ]
        )
        cog = make_cog(mocker, channel_model=channel_model)
        interaction = make_interaction(mocker)

        await cog.listchannels.callback(cog, interaction)

        sent = interaction.followup.send.await_args.args[0]
        assert "Test Channel" in sent
        assert "Beta" in sent


class TestPollLoop:
    async def test_skips_ignored_topic(self, mocker):
        channel_model = FakeChannelModel(channels=[FakeChannel()])
        pending_model = FakePendingModel()
        cog = make_cog(
            mocker,
            holodex=FakeHolodex(
                videos=[make_video(topic_id="membersonly", id="mems1")]
            ),
            channel_model=channel_model,
            pending_model=pending_model,
        )
        await cog.check_new_videos()
        assert pending_model.created == []

    async def test_enqueues_unseen_video(self, mocker):
        channel_model = FakeChannelModel(channels=[FakeChannel()])
        pending_model = FakePendingModel()
        cog = make_cog(
            mocker,
            holodex=FakeHolodex(videos=[make_video()]),
            channel_model=channel_model,
            pending_model=pending_model,
        )
        await cog.check_new_videos()
        assert len(pending_model.created) == 1
        item = pending_model.created[0]
        assert item.video_id == VIDEO_ID
        assert item.status == PendingVideoStatus.QUEUED
        assert item.next_attempt_at is not None

    async def test_does_not_duplicate_existing_video(self, mocker):
        existing = FakePending(
            video_id=VIDEO_ID,
            next_attempt_at=datetime.now(UTC) + timedelta(hours=1),
        )
        channel_model = FakeChannelModel(channels=[FakeChannel()])
        pending_model = FakePendingModel(items=[existing])
        cog = make_cog(
            mocker,
            holodex=FakeHolodex(videos=[make_video()]),
            channel_model=channel_model,
            pending_model=pending_model,
        )
        await cog.check_new_videos()
        assert pending_model.created == []


class TestProcessQueue:
    async def test_summarizes_due_video_and_posts_header_first(
        self, mocker, monkeypatch
    ):
        monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "123456")

        item = FakePending(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
        get_details = mocker.patch.object(
            autosummary_module,
            "get_topic_details_all",
            AsyncMock(
                return_value=[
                    {"topic_name": "alpha", "detail": "one"},
                    {"topic_name": "beta", "detail": "two"},
                ]
            ),
        )
        cog = make_cog(mocker, pending_model=FakePendingModel(items=[item]))

        await cog.process_due_videos(now=datetime.now(UTC))

        assert get_details.await_count == 1
        assert item.status == PendingVideoStatus.DONE
        sends = cog.bot.get_channel.return_value.send.await_args_list
        sent = " ".join(c.args[0] for c in sends)
        assert "**Video:** New Stream" in sent
        assert "**Channel:** Test Channel" in sent
        assert "alpha" in sent

    async def test_not_processed_before_next_attempt(self, mocker, monkeypatch):
        monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "123456")

        item = FakePending(next_attempt_at=datetime.now(UTC) + timedelta(minutes=5))
        get_details = mocker.patch.object(
            autosummary_module,
            "get_topic_details_all",
            AsyncMock(return_value=[]),
        )
        cog = make_cog(mocker, pending_model=FakePendingModel(items=[item]))

        await cog.process_due_videos(now=datetime.now(UTC))

        assert get_details.await_count == 0
        assert item.status == PendingVideoStatus.QUEUED

    async def test_retries_with_exponential_backoff(self, mocker, monkeypatch):
        monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "123456")

        item = FakePending(next_attempt_at=datetime.now(UTC) - timedelta(minutes=1))
        mocker.patch.object(
            autosummary_module,
            "get_topic_details_all",
            AsyncMock(side_effect=SourceAddError("http://example.com")),
        )
        cog = make_cog(mocker, pending_model=FakePendingModel(items=[item]))
        now = datetime.now(UTC)

        await cog.process_due_videos(now=now)

        assert item.status == PendingVideoStatus.QUEUED
        assert item.retry_count == 1
        expected_delay = timedelta(hours=exponential_backoff_hours(1))
        assert abs((item.next_attempt_at - now) - expected_delay) < timedelta(minutes=1)

    async def test_retry_backoff_grows(self, mocker, monkeypatch):
        monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "123456")

        item = FakePending(
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
            retry_count=2,
        )
        mocker.patch.object(
            autosummary_module,
            "get_topic_details_all",
            AsyncMock(side_effect=SourceAddError("http://example.com")),
        )
        cog = make_cog(mocker, pending_model=FakePendingModel(items=[item]))
        now = datetime.now(UTC)

        await cog.process_due_videos(now=now)

        assert item.retry_count == 3
        expected_delay = timedelta(hours=exponential_backoff_hours(3))
        assert abs((item.next_attempt_at - now) - expected_delay) < timedelta(minutes=1)

    async def test_gives_up_after_max_retries_and_notifies(self, mocker, monkeypatch):
        monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "123456")

        item = FakePending(
            next_attempt_at=datetime.now(UTC) - timedelta(minutes=1),
            retry_count=MAX_RETRIES - 1,
        )
        mocker.patch.object(
            autosummary_module,
            "get_topic_details_all",
            AsyncMock(side_effect=SourceAddError("http://example.com")),
        )
        cog = make_cog(mocker, pending_model=FakePendingModel(items=[item]))

        await cog.process_due_videos(now=datetime.now(UTC))

        assert item.status == PendingVideoStatus.FAILED
        sent = " ".join(
            c.args[0] for c in cog.bot.get_channel.return_value.send.await_args_list
        )
        assert "New Stream" in sent
        assert "Test Channel" in sent
        assert "failed to generate a summary" in sent.lower()


class TestExponentialBackoff:
    def test_first_five_delays(self):
        assert exponential_backoff_hours(1) == 1
        assert exponential_backoff_hours(2) == 2
        assert exponential_backoff_hours(3) == 4
        assert exponential_backoff_hours(4) == 8
        assert exponential_backoff_hours(5) == 16
