from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from notebooklm import SourceAddError

import vsummary.cogs.autosummary.autosummary as autosummary_module
import vsummary.cogs.summarizer.summarizer as summarizer_module
import vsummary.main as main_module
import vsummary.util.summarizer as summarizer_util
from vsummary.bot import Bot
from vsummary.cogs.autosummary.autosummary import Autosummary
from vsummary.cogs.misc.ping import Ping
from vsummary.cogs.summarizer.summarizer import Summarizer, TopicsView
from vsummary.model.channel import PendingVideoStatus
from vsummary.model.video import Topic
from vsummary.settings import Settings
from vsummary.util.holodex import (
    ChannelNotFoundError,
    HolodexChannel,
    HolodexVideo,
)
from vsummary.util.video import VideoRef

VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_REF = VideoRef(source="youtube", id=VIDEO_ID)


class QueryField:
    def __eq__(self, other):
        return (self, other)

    def __le__(self, other):
        return (self, "<=", other)


class FakeResponse:
    def __init__(self):
        self.deferred = []
        self.sent = None

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)

    async def send_message(self, content):
        self.sent = content


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append((content, kwargs))


class FakeInteraction:
    def __init__(self):
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.edited_views = []

    async def edit_original_response(self, **kwargs):
        self.edited_views.append(kwargs)


class FakeChannelTarget:
    def __init__(self):
        self.messages = []

    async def send(self, content):
        self.messages.append(content)


class FakeNotebookClient:
    def __init__(self, answers=None, source_error=False):
        self.answers = list(answers or [])
        self.source_error = source_error
        self.created = []
        self.deleted = []
        self.added_urls = []
        self.prompts = []
        self._next_id = 1
        self.notebooks = SimpleNamespace(
            create=self.create_notebook,
            delete=self.delete_notebook,
        )
        self.sources = SimpleNamespace(add_url=self.add_source)
        self.chat = SimpleNamespace(ask=self.ask)

    async def create_notebook(self, title):
        notebook = SimpleNamespace(id=f"notebook-{self._next_id}", title=title)
        self._next_id += 1
        self.created.append(notebook)
        return notebook

    async def delete_notebook(self, notebook_id):
        self.deleted.append(notebook_id)

    async def add_source(self, notebook_id, url):
        if self.source_error:
            raise SourceAddError("source unavailable")
        self.added_urls.append((notebook_id, url))

    async def ask(self, notebook_id, prompt):
        self.prompts.append((notebook_id, prompt))
        answer = self.answers.pop(0)
        return SimpleNamespace(answer=answer)


class FakeVideoRecord:
    source = QueryField()
    video_id = QueryField()
    records: ClassVar[list] = []

    def __init__(self, source, video_id, topics=None):
        self.source = source
        self.video_id = video_id
        self.topics = topics

    @classmethod
    async def find_one(cls, *args):
        for record in cls.records:
            if record.source == VIDEO_REF.source and record.video_id == VIDEO_REF.id:
                return record
        return None

    async def save(self):
        if self not in self.records:
            self.records.append(self)


class FakePendingRecord:
    video_id = QueryField()
    channel_id = QueryField()
    status = QueryField()
    next_attempt_at = QueryField()
    records: ClassVar[list] = []
    deleted_queries: ClassVar[list] = []

    def __init__(
        self,
        video_id,
        channel_id,
        channel_name,
        title,
        available_at,
        next_attempt_at,
        retry_count=0,
        status=PendingVideoStatus.QUEUED,
    ):
        self.video_id = video_id
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.title = title
        self.available_at = available_at
        self.next_attempt_at = next_attempt_at
        self.retry_count = retry_count
        self.status = status
        self.save_count = 0

    @classmethod
    async def find_one(cls, query):
        if isinstance(query, tuple) and query[0] is cls.video_id:
            return next(
                (record for record in cls.records if record.video_id == query[1]),
                None,
            )
        return cls.records[0] if cls.records else None

    @classmethod
    def find(cls, *args):
        return SimpleNamespace(to_list=lambda: cls._to_list())

    @classmethod
    async def _to_list(cls):
        return list(cls.records)

    @classmethod
    async def delete_one(cls, query):
        cls.deleted_queries.append(query)

    async def save(self):
        self.save_count += 1
        if self not in self.records:
            self.records.append(self)


class FakeFollowedChannel:
    channel_id = QueryField()
    records: ClassVar[list] = []
    find_one_result: ClassVar[object | None] = None

    def __init__(self, channel_id, name, added_at=None):
        self.channel_id = channel_id
        self.name = name
        self.added_at = added_at or datetime.min.replace(tzinfo=UTC)
        self.deleted = False

    @classmethod
    async def find_one(cls, *args):
        return cls.find_one_result

    @classmethod
    def find(cls, *args):
        return SimpleNamespace(to_list=lambda: cls._to_list())

    @classmethod
    async def _to_list(cls):
        return list(cls.records)

    async def save(self):
        self.records.append(self)

    async def delete(self):
        self.deleted = True


def make_autosummary(bot=None):
    cog = Autosummary.__new__(Autosummary)
    cog.bot = bot or SimpleNamespace(notebook_client=object())
    cog.holodex = AsyncMock()
    cog.notebook = cog.bot.notebook_client
    return cog


@pytest.fixture(autouse=True)
def reset_fake_storage(monkeypatch):
    FakeVideoRecord.records = []
    FakePendingRecord.records = []
    FakePendingRecord.deleted_queries = []
    FakeFollowedChannel.records = []
    FakeFollowedChannel.find_one_result = None
    monkeypatch.setattr(summarizer_util, "Video", FakeVideoRecord)
    monkeypatch.setattr(autosummary_module, "Channel", FakeFollowedChannel)
    monkeypatch.setattr(autosummary_module, "PendingVideo", FakePendingRecord)


@pytest.mark.asyncio
async def test_summarization_pipeline_generates_caches_and_reuses_all_topic_details():
    client = FakeNotebookClient(
        answers=[
            '[{"name": "Introduction"}, {"name": "Conclusion"}]',
            "The speaker introduces the subject.",
            "The speaker closes the discussion.",
        ]
    )

    details = await summarizer_util.get_topic_details_all(client, VIDEO_REF)

    assert details == [
        {"topic_name": "Introduction", "detail": "The speaker introduces the subject."},
        {"topic_name": "Conclusion", "detail": "The speaker closes the discussion."},
    ]
    assert len(FakeVideoRecord.records) == 1
    assert [topic.detail for topic in FakeVideoRecord.records[0].topics] == [
        "The speaker introduces the subject.",
        "The speaker closes the discussion.",
    ]
    assert client.deleted == ["notebook-1"]

    second_client = FakeNotebookClient()
    cached_details = await summarizer_util.get_topic_details_all(
        second_client, VIDEO_REF
    )

    assert cached_details == details
    assert second_client.created == []
    assert second_client.prompts == []


@pytest.mark.asyncio
async def test_summarization_pipeline_deletes_notebook_when_source_cannot_be_added():
    client = FakeNotebookClient(source_error=True)

    with pytest.raises(SourceAddError):
        await summarizer_util.get_topic_list(client, VIDEO_REF)

    assert client.created[0].id == "notebook-1"
    assert client.deleted == ["notebook-1"]


@pytest.mark.asyncio
async def test_summarization_cleanup_failure_does_not_mask_source_error(monkeypatch):
    client = FakeNotebookClient(source_error=True)
    client.notebooks.delete = AsyncMock(side_effect=RuntimeError("delete failed"))

    with pytest.raises(SourceAddError):
        await summarizer_util._create_notebook_with_source(client, VIDEO_REF)
    assert FakeVideoRecord.records == []


@pytest.mark.asyncio
async def test_topics_command_returns_selectable_topic_view(monkeypatch):
    bot = SimpleNamespace(notebook_client=object())
    cog = Summarizer(bot)
    interaction = FakeInteraction()
    monkeypatch.setattr(
        summarizer_module,
        "get_topic_list",
        AsyncMock(return_value=[Topic(name="Intro"), Topic(name="Outro")]),
    )

    await Summarizer.topics.callback(cog, interaction, VIDEO_ID)

    assert interaction.response.deferred == [{"thinking": True}]
    assert len(interaction.followup.messages) == 1
    content, kwargs = interaction.followup.messages[0]
    assert content == "Here are the topics mentioned in the video:\n1: Intro\n2: Outro"
    assert isinstance(kwargs["view"], TopicsView)
    assert [option.label for option in kwargs["view"].topic_select.options] == [
        "Intro",
        "Outro",
    ]


@pytest.mark.asyncio
async def test_topics_command_reports_invalid_video_input(monkeypatch):
    cog = Summarizer(SimpleNamespace(notebook_client=object()))
    interaction = FakeInteraction()
    get_topics = AsyncMock()
    monkeypatch.setattr(summarizer_module, "get_topic_list", get_topics)

    await Summarizer.topics.callback(cog, interaction, "https://example.com/video")

    assert interaction.response.deferred == [{"thinking": True}]
    assert interaction.followup.messages == [
        ("'https://example.com/video' is not a supported video source.", {})
    ]
    get_topics.assert_not_awaited()


@pytest.mark.asyncio
async def test_topics_view_selected_topic_flow_disables_components(monkeypatch):
    bot = SimpleNamespace(notebook_client=object())
    view = TopicsView(bot, VIDEO_REF, [Topic(name="Intro")])
    view.topic_select._values = ["1"]
    interaction = FakeInteraction()
    monkeypatch.setattr(
        summarizer_module,
        "get_topic_details",
        AsyncMock(return_value={"topic_name": "Intro", "detail": "Details"}),
    )

    await view.on_topic_selected(interaction)

    assert interaction.response.deferred == [{}]
    assert interaction.followup.messages == [("**Intro**\nDetails", {})]
    assert all(child.disabled for child in view.children)
    assert interaction.edited_views == [{"view": view}]


@pytest.mark.asyncio
async def test_topics_view_reports_invalid_selected_topic(monkeypatch):
    bot = SimpleNamespace(notebook_client=object())
    view = TopicsView(bot, VIDEO_REF, [Topic(name="Intro")])
    view.topic_select._values = ["2"]
    interaction = FakeInteraction()
    monkeypatch.setattr(
        summarizer_module,
        "get_topic_details",
        AsyncMock(side_effect=IndexError),
    )

    await view.on_topic_selected(interaction)

    assert interaction.followup.messages == [("Invalid topic index: 2", {})]
    assert interaction.edited_views == [{"view": view}]


@pytest.mark.asyncio
async def test_topics_view_all_topics_flow_sends_each_detail(monkeypatch):
    bot = SimpleNamespace(notebook_client=object())
    view = TopicsView(bot, VIDEO_REF, [Topic(name="Intro"), Topic(name="Outro")])
    interaction = FakeInteraction()
    monkeypatch.setattr(
        summarizer_module,
        "get_topic_details_all",
        AsyncMock(
            return_value=[
                {"topic_name": "Intro", "detail": "A"},
                {"topic_name": "Outro", "detail": "B"},
            ]
        ),
    )

    await view.on_all_topics(interaction)

    assert interaction.followup.messages == [("**Intro**\nA", {}), ("**Outro**\nB", {})]
    assert all(child.disabled for child in view.children)
    assert interaction.edited_views == [{"view": view}]


@pytest.mark.asyncio
async def test_detail_command_supports_all_topics_and_single_topic(monkeypatch):
    cog = Summarizer(SimpleNamespace(notebook_client=object()))
    all_interaction = FakeInteraction()
    all_details = AsyncMock(
        return_value=[
            {"topic_name": "Intro", "detail": "A"},
            {"topic_name": "Outro", "detail": "B"},
        ]
    )
    monkeypatch.setattr(summarizer_module, "get_topic_details_all", all_details)

    await Summarizer.detail.callback(cog, all_interaction, VIDEO_ID, None)

    assert all_interaction.followup.messages == [
        ("**1. Intro**\nA", {}),
        ("**2. Outro**\nB", {}),
    ]
    all_details.assert_awaited_once()

    one_interaction = FakeInteraction()
    one_detail = AsyncMock(return_value={"topic_name": "Outro", "detail": "B"})
    monkeypatch.setattr(summarizer_module, "get_topic_details", one_detail)

    await Summarizer.detail.callback(cog, one_interaction, VIDEO_ID, 2)

    assert one_interaction.followup.messages == [("**Outro**\nB", {})]
    one_detail.assert_awaited_once_with(cog.bot.notebook_client, VIDEO_REF, 1)


@pytest.mark.asyncio
async def test_detail_command_reports_invalid_index_and_source_errors(monkeypatch):
    cog = Summarizer(SimpleNamespace(notebook_client=object()))
    invalid_interaction = FakeInteraction()
    monkeypatch.setattr(
        summarizer_module,
        "get_topic_details",
        AsyncMock(side_effect=IndexError),
    )

    await Summarizer.detail.callback(cog, invalid_interaction, VIDEO_ID, 4)

    assert invalid_interaction.followup.messages == [("Invalid topic index: 4", {})]

    source_interaction = FakeInteraction()
    monkeypatch.setattr(
        summarizer_module,
        "get_topic_details",
        AsyncMock(side_effect=SourceAddError("no transcript")),
    )

    await Summarizer.detail.callback(cog, source_interaction, VIDEO_ID, 1)

    assert source_interaction.followup.messages == [
        ("Provided link or id is invalid or no transcript available.", {})
    ]


@pytest.mark.asyncio
async def test_bot_setup_loads_all_cogs_and_syncs_commands(monkeypatch):
    bot = Bot(notebook_client=object())
    loaded = []
    load_extension = AsyncMock(side_effect=lambda cog: loaded.append(cog))
    sync = AsyncMock()
    monkeypatch.setattr(bot, "load_extension", load_extension)
    monkeypatch.setattr(bot.tree, "sync", sync)

    await bot.setup_hook()

    assert loaded == [
        "vsummary.cogs.misc.ping",
        "vsummary.cogs.summarizer.summarizer",
        "vsummary.cogs.autosummary.autosummary",
    ]
    sync.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_ping_command_reports_latency_in_milliseconds():
    cog = Ping(SimpleNamespace(latency=0.1234))
    interaction = FakeInteraction()

    await Ping.ping.callback(cog, interaction)

    assert interaction.response.sent == "Pong! Latency: 123ms"


@pytest.mark.asyncio
async def test_autosummary_add_channel_handles_success_duplicate_and_api_errors(
    monkeypatch,
):
    channel = HolodexChannel(id="UC123", name="Test Channel")
    cog = make_autosummary()
    interaction = FakeInteraction()
    cog.holodex.get_channel = AsyncMock(return_value=channel)
    FakeFollowedChannel.find_one_result = None

    await Autosummary.addchannel.callback(cog, interaction, "UC123")

    assert interaction.followup.messages == [
        ("Added channel **Test Channel** (`UC123`) for automatic summaries.", {})
    ]
    assert [
        (record.channel_id, record.name) for record in FakeFollowedChannel.records
    ] == [("UC123", "Test Channel")]

    duplicate_interaction = FakeInteraction()
    FakeFollowedChannel.find_one_result = FakeFollowedChannel("UC123", "Test Channel")

    await Autosummary.addchannel.callback(cog, duplicate_interaction, "UC123")

    assert duplicate_interaction.followup.messages == [
        ("Channel **Test Channel** (`UC123`) is already being followed.", {})
    ]

    not_found_interaction = FakeInteraction()
    cog.holodex.get_channel = AsyncMock(side_effect=ChannelNotFoundError())

    await Autosummary.addchannel.callback(cog, not_found_interaction, "missing")

    assert not_found_interaction.followup.messages == [
        ("Channel 'missing' was not found on Holodex.", {})
    ]

    error_interaction = FakeInteraction()
    cog.holodex.get_channel = AsyncMock(return_value=None)

    await Autosummary.addchannel.callback(cog, error_interaction, "broken")

    assert error_interaction.followup.messages == [
        ("An error occurred while fetching channel 'broken' from Holodex.", {})
    ]


@pytest.mark.asyncio
async def test_autosummary_remove_channel_deletes_pending_videos_and_handles_missing():
    cog = make_autosummary()
    followed = FakeFollowedChannel("UC123", "Test Channel")
    FakeFollowedChannel.find_one_result = followed
    interaction = FakeInteraction()

    await Autosummary.removechannel.callback(cog, interaction, "UC123")

    assert followed.deleted is True
    assert len(FakePendingRecord.deleted_queries) == 1
    assert interaction.followup.messages == [
        ("Removed channel 'UC123' and its pending videos.", {})
    ]

    missing_interaction = FakeInteraction()
    FakeFollowedChannel.find_one_result = None

    await Autosummary.removechannel.callback(cog, missing_interaction, "missing")

    assert missing_interaction.followup.messages == [
        ("Channel 'missing' is not being followed.", {})
    ]


@pytest.mark.asyncio
async def test_autosummary_list_channels_handles_empty_and_populated_storage():
    cog = make_autosummary()
    empty_interaction = FakeInteraction()

    await Autosummary.listchannels.callback(cog, empty_interaction)

    assert empty_interaction.followup.messages == [
        ("No channels are currently followed for automatic summaries.", {})
    ]

    FakeFollowedChannel.records = [
        FakeFollowedChannel("UC1", "First"),
        FakeFollowedChannel("UC2", "Second"),
    ]
    populated_interaction = FakeInteraction()

    await Autosummary.listchannels.callback(cog, populated_interaction)

    assert populated_interaction.followup.messages == [
        ("Followed channels:\n**First** (`UC1`)\n**Second** (`UC2`)", {})
    ]


@pytest.mark.asyncio
async def test_autosummary_poll_and_process_loops_delegate_each_due_item():
    cog = make_autosummary()
    FakeFollowedChannel.records = [FakeFollowedChannel("UC1", "First")]
    videos = [HolodexVideo("video", "Title", None, datetime.now(UTC), "First")]
    cog.holodex.get_channel_videos = AsyncMock(return_value=videos)
    cog._enqueue_new_video = AsyncMock()

    await cog.check_new_videos()

    cog.holodex.get_channel_videos.assert_awaited_once_with("UC1")
    cog._enqueue_new_video.assert_awaited_once_with(
        videos[0], FakeFollowedChannel.records[0]
    )

    due_item = FakePendingRecord(
        video_id="due",
        channel_id="UC1",
        channel_name="First",
        title="Due",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    FakePendingRecord.records = [due_item]
    cog._process_pending_video = AsyncMock()

    await cog.process_due_videos()

    cog._process_pending_video.assert_awaited_once_with(due_item)


@pytest.mark.asyncio
async def test_autosummary_poll_only_enqueues_videos_released_after_channel_was_added():
    cog = make_autosummary()
    added_at = datetime.now(UTC) - timedelta(hours=1)
    channel = FakeFollowedChannel("UC1", "First", added_at=added_at)
    FakeFollowedChannel.records = [channel]
    videos = [
        HolodexVideo(
            "before",
            "Before",
            None,
            added_at - timedelta(seconds=1),
            "First",
        ),
        HolodexVideo("after", "After", None, added_at + timedelta(seconds=1), "First"),
    ]
    cog.holodex.get_channel_videos = AsyncMock(return_value=videos)
    cog._enqueue_new_video = AsyncMock()

    await cog.check_new_videos()

    cog._enqueue_new_video.assert_awaited_once_with(videos[1], channel)


@pytest.mark.asyncio
async def test_autosummary_enqueue_skips_ignored_and_duplicate_videos(monkeypatch):
    cog = make_autosummary()
    channel = FakeFollowedChannel("UC1", "Test")
    ignored = HolodexVideo(
        id="ignored",
        title="Ignored",
        topic_id="shorts",
        available_at=datetime.now(UTC) - timedelta(hours=3),
        channel_name="Test",
    )

    await cog._enqueue_new_video(ignored, channel)

    assert FakePendingRecord.records == []

    existing = FakePendingRecord(
        video_id=VIDEO_ID,
        channel_id="UC1",
        channel_name="Test",
        title="Existing",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    FakePendingRecord.records = [existing]
    duplicate = HolodexVideo(
        id=VIDEO_ID,
        title="Duplicate",
        topic_id=None,
        available_at=datetime.now(UTC) - timedelta(hours=3),
        channel_name="Test",
    )

    await cog._enqueue_new_video(duplicate, channel)

    assert FakePendingRecord.records == [existing]


@pytest.mark.asyncio
async def test_autosummary_enqueue_sets_ready_and_delayed_attempt_times(monkeypatch):
    cog = make_autosummary()
    channel = FakeFollowedChannel("UC1", "Test")
    ready_at = datetime.now(UTC) - timedelta(hours=3)
    ready_video = HolodexVideo("ready", "Ready", None, ready_at, "Test")

    await cog._enqueue_new_video(ready_video, channel)

    ready_pending = FakePendingRecord.records[-1]
    assert ready_pending.video_id == "ready"
    assert ready_pending.next_attempt_at <= datetime.now(UTC)

    delayed_at = datetime.now(UTC) - timedelta(minutes=30)
    delayed_video = HolodexVideo(
        "delayed", "Delayed", None, delayed_at, "Test", duration=1800
    )

    await cog._enqueue_new_video(delayed_video, channel)

    delayed_pending = FakePendingRecord.records[-1]
    assert abs(
        delayed_pending.next_attempt_at
        - (delayed_at + timedelta(seconds=1800, hours=2))
    ) < timedelta(seconds=1)


@pytest.mark.asyncio
async def test_autosummary_processes_success_and_posts_summary(monkeypatch):
    target = FakeChannelTarget()
    bot = SimpleNamespace(
        notebook_client=object(),
        get_channel=lambda channel_id: target if channel_id == 123 else None,
    )
    cog = make_autosummary(bot)
    item = FakePendingRecord(
        video_id=VIDEO_ID,
        channel_id="UC1",
        channel_name="Test",
        title="A stream",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "123")
    monkeypatch.setattr(
        autosummary_module,
        "get_topic_details_all",
        AsyncMock(
            return_value=[
                {"topic_name": "Intro", "detail": "Details"},
                {"topic_name": "Outro", "detail": "More details"},
            ]
        ),
    )

    await cog._process_pending_video(item)

    assert item.status is PendingVideoStatus.DONE
    assert target.messages == [
        "**Video:** A stream\n**Channel:** Test\n\n**Intro**\nDetails\n\n**Outro**\nMore details"
    ]


@pytest.mark.asyncio
async def test_autosummary_retries_then_marks_video_failed_and_posts_failure(
    monkeypatch,
):
    target = FakeChannelTarget()
    bot = SimpleNamespace(notebook_client=object(), get_channel=lambda _: target)
    cog = make_autosummary(bot)
    monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "123")

    retry_item = FakePendingRecord(
        video_id="retry",
        channel_id="UC1",
        channel_name="Test",
        title="Retry me",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
        retry_count=0,
    )
    await cog._handle_source_error(retry_item)

    assert retry_item.status is PendingVideoStatus.QUEUED
    assert retry_item.retry_count == 1
    assert retry_item.next_attempt_at > datetime.now(UTC)
    assert target.messages == []

    failed_item = FakePendingRecord(
        video_id="failed",
        channel_id="UC1",
        channel_name="Test",
        title="Failed video",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
        retry_count=4,
    )
    await cog._handle_source_error(failed_item)

    assert failed_item.status is PendingVideoStatus.FAILED
    assert len(target.messages) == 1
    assert "Failed to generate a summary after 5 attempts" in target.messages[0]


@pytest.mark.asyncio
async def test_autosummary_channel_lookup_skips_missing_or_invalid_configuration(
    monkeypatch,
):
    cog = make_autosummary(
        SimpleNamespace(notebook_client=object(), get_channel=lambda _: None)
    )

    monkeypatch.delenv("AUTO_SUMMARY_CHANNEL_ID", raising=False)
    assert await cog._get_auto_summary_channel() is None

    monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "not-an-integer")
    assert await cog._get_auto_summary_channel() is None

    monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "123")
    assert await cog._get_auto_summary_channel() is None


@pytest.mark.asyncio
async def test_autosummary_sends_long_summaries_in_discord_sized_chunks():
    target = FakeChannelTarget()
    cog = make_autosummary()
    text = "a" * 2000 + "\n" + "b" * 100

    await cog._send_to_channel(target, text)

    assert [len(message) for message in target.messages] == [2000, 100]
    assert "".join(target.messages) == text.replace("\n", "")


@pytest.mark.asyncio
async def test_startup_requires_discord_token_and_mongodb_uri(monkeypatch):
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)

    with pytest.raises(SystemExit) as missing_token:
        await main_module.async_main()

    assert missing_token.value.code == 1

    monkeypatch.setenv("DISCORD_TOKEN", "token")

    with pytest.raises(SystemExit) as missing_uri:
        await main_module.async_main()

    assert missing_uri.value.code == 1


@pytest.mark.asyncio
async def test_startup_configures_timezone_aware_mongodb_datetimes(monkeypatch):
    class FakeMongoClient:
        def __init__(self, uri, **kwargs):
            self.uri = uri
            self.kwargs = kwargs

        def __getitem__(self, name):
            return name

        async def close(self):
            pass

    mongo_client = None

    def make_client(uri, **kwargs):
        nonlocal mongo_client
        mongo_client = FakeMongoClient(uri, **kwargs)
        return mongo_client

    monkeypatch.setattr(main_module, "AsyncMongoClient", make_client)
    monkeypatch.setattr(
        main_module,
        "init_beanie",
        AsyncMock(side_effect=RuntimeError("stop after client setup")),
    )

    with pytest.raises(RuntimeError, match="stop after client setup"):
        await main_module.async_main(
            Settings(discord_token="token", mongodb_uri="mongodb://localhost")
        )

    assert mongo_client is not None
    assert mongo_client.uri == "mongodb://localhost"
    assert mongo_client.kwargs == {"tz_aware": True}
