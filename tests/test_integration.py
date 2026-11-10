import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from notebooklm import ChatError, SourceAddError
from pymongo.errors import OperationFailure

import vsummary.cogs.autosummary.autosummary as autosummary_module
import vsummary.cogs.manualsummary.manualsummary as manualsummary_module
import vsummary.cogs.summarizer.summarizer as summarizer_module
import vsummary.main as main_module
import vsummary.util.summarizer as summarizer_util
from vsummary.bot import Bot
from vsummary.cogs.autosummary.autosummary import Autosummary
from vsummary.cogs.manualsummary.manualsummary import ManualSummary
from vsummary.cogs.misc.ping import Ping
from vsummary.cogs.summarizer.summarizer import Summarizer
from vsummary.model.channel import (
    JobStatus,
    ManualSummaryOperation,
    SummaryJob,
)
from vsummary.model.video import Topic, VideoSummary
from vsummary.settings import RuntimeConfig, Settings
from vsummary.util.holodex import (
    ChannelNotFoundError,
    HolodexChannel,
    HolodexVideo,
    PermanentHolodexError,
    TransientHolodexError,
)
from vsummary.util.summarizer import (
    InvalidSummaryResponse,
    NotebookLMSummaryService,
    PermanentSummaryError,
    TransientSummaryError,
)
from vsummary.util.video import VideoRef, parse_non_space_video_source
from vsummary.util.video_work import VideoWorkCoordinator

VIDEO_ID = "dQw4w9WgXcQ"
VIDEO_REF = VideoRef(source="youtube", video_id=VIDEO_ID)


class QueryField:
    def __init__(self, name=None):
        self.name = name

    def __eq__(self, other):
        return (self, other)

    def __le__(self, other):
        return (self, "<=", other)


class FakeResponse:
    def __init__(self):
        self.deferred = []
        self.sent = None
        self.sent_kwargs = None

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)

    async def send_message(self, content, **kwargs):
        self.sent = content
        self.sent_kwargs = kwargs


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
        self.channel_id = 123
        self.user = SimpleNamespace(id=456)

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
        if isinstance(answer, Exception):
            raise answer
        return SimpleNamespace(answer=answer)


class FakeVideoRecord:
    source = QueryField("source")
    video_id = QueryField("video_id")
    output_language = QueryField("output_language")
    records: ClassVar[list] = []

    def __init__(self, source, video_id, topics=None, *, output_language=None):
        self.source = source
        self.video_id = video_id
        self.topics = topics
        self.output_language = output_language

    @classmethod
    async def find_one(cls, *args):
        for record in cls.records:
            if all(getattr(record, field.name) == value for field, value in args):
                return record
        return None

    async def save(self):
        if self not in self.records:
            self.records.append(self)


class FakeSummaryJob:
    source = "youtube"
    failure_notice = False
    id = None
    youtube_audio_fallback = False
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
        job_type="auto",
        status=JobStatus.QUEUED,
    ):
        self.video_id = video_id
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.title = title
        self.available_at = available_at
        self.next_attempt_at = next_attempt_at
        self.retry_count = retry_count
        self.status = status
        self.job_type = job_type
        self.summary_details = []
        self.delivery_chunks = []
        self.delivery_chunk_index = 0
        self.delivery_retry_count = 0
        self.claimed_by = "worker"
        self.save_count = 0

    @classmethod
    async def find_one(cls, query):
        return next(
            (
                record
                for record in cls.records
                if all(getattr(record, key) == value for key, value in query.items())
            ),
            None,
        )

    @classmethod
    def find(cls, *args):
        async def delete():
            cls.deleted_queries.append(args[0])

        return SimpleNamespace(to_list=lambda: cls._to_list(), delete=delete)

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
    cog.settings = None
    cog.summary_service = SimpleNamespace(summarize=AsyncMock())
    cog.notebook = None
    cog.bot = bot or SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
    )
    cog.holodex = AsyncMock()
    cog.holodex.get_video_topic = AsyncMock(return_value=None)
    cog.notebook = cog.bot.notebook_client
    cog.worker_id = "worker"

    async def save(job):
        await job.save()
        return True

    cog._save_item = AsyncMock(side_effect=save)
    return cog


def make_manual_summary(bot):
    cog = ManualSummary.__new__(ManualSummary)
    cog.bot = bot
    cog.settings = SimpleNamespace(source_retry_limit=5)
    cog.worker_id = "worker"
    cog._save = AsyncMock(return_value=True)
    return cog


def make_manual_detail_job(topic_index=0):
    return SummaryJob.model_construct(
        job_type="manual",
        claimed_by="worker",
        source="youtube",
        video_id=VIDEO_ID,
        operation=ManualSummaryOperation.DETAIL_ONE,
        topic_index=topic_index,
        discord_channel_id=123,
        requester_id=456,
        next_attempt_at=datetime.now(UTC),
    )


def make_worker_job(client, config, operation):
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        notebook_client=client,
        video_work=VideoWorkCoordinator(),
    )
    if operation == "automatic":
        cog = make_autosummary(bot)
        cog.summary_service = NotebookLMSummaryService(client, config=config)
        cog._save_item = AsyncMock(return_value=True)
        job = FakeSummaryJob(
            video_id=VIDEO_ID,
            channel_id="UC1",
            channel_name="Channel",
            title="A stream",
            available_at=datetime.now(UTC),
            next_attempt_at=datetime.now(UTC),
            status=JobStatus.QUEUED,
        )
    else:
        cog = make_manual_summary(bot)
        job = make_manual_detail_job()
        job.operation = {
            "topics": ManualSummaryOperation.TOPICS,
            "detail": ManualSummaryOperation.DETAIL_ONE,
            "all": ManualSummaryOperation.DETAIL_ALL,
        }[operation]
        job.title, job.channel_name = "A stream", "Channel"
    cog.settings = config
    return cog, job


@pytest.fixture(autouse=True)
def reset_fake_storage(monkeypatch):
    FakeVideoRecord.records = []
    FakeSummaryJob.records = []
    FakeSummaryJob.deleted_queries = []
    FakeFollowedChannel.records = []
    FakeFollowedChannel.find_one_result = None
    monkeypatch.setattr(summarizer_util, "VideoSummary", FakeVideoRecord)
    monkeypatch.setattr(autosummary_module, "FollowedChannel", FakeFollowedChannel)
    monkeypatch.setattr(autosummary_module, "SummaryJob", FakeSummaryJob)


@pytest.mark.asyncio
async def test_topic_list_requests_english_names_and_caches_them_in_order():
    client = FakeNotebookClient(
        answers=['[{"name": "Preparing for the concert"}, {"name": "Future plans"}]']
        * 2
    )

    topics = await summarizer_util.get_topic_list(client, VIDEO_REF)

    assert len(client.prompts) == 2
    for notebook_id, text in client.prompts:
        prompt = " ".join(text.split())
        assert notebook_id == "notebook-1"
        assert "Write every topic name in English" in prompt
        assert (
            "regardless of the language spoken in the video or used in its transcript"
            in prompt
        )
        assert (
            "Translate descriptive topic names from other languages into English"
            in prompt
        )
        assert "100 characters" in prompt
        assert '[{"name": "topic name"}]' in prompt
        assert "JSON only" in prompt
    extraction = " ".join(client.prompts[0][1].split())
    for instruction in (
        "every substantive discussion",
        "chronological order",
        "brief discussions",
        "secondary topics",
        "audience questions",
        "announcements",
        "anecdotes",
        "later returns",
        "new information",
        "filler",
        "incidental mentions",
        "Do not target a fixed topic count",
    ):
        assert instruction in extraction
    review = " ".join(client.prompts[1][1].split())
    for instruction in (
        "beginning to end",
        "omitted discussions",
        "overly broad entries",
        "merged recurring discussions",
        "complete corrected list",
        "Retain supported discussions",
        "chronological order",
    ):
        assert instruction in review
    assert [topic.name for topic in topics] == [
        "Preparing for the concert",
        "Future plans",
    ]
    assert len(FakeVideoRecord.records) == 1
    assert FakeVideoRecord.records[0].topics == topics
    assert client.deleted == ["notebook-1"]

    cached_client = FakeNotebookClient()
    assert await summarizer_util.get_topic_list(cached_client, VIDEO_REF) == topics
    assert cached_client.created == []
    assert cached_client.prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("draft", "reviewed"),
    [
        (["Concert", "Travel"], ["Concert", "Audience questions", "Travel"]),
        (["Concert", "Travel"], ["Concert", "Travel", "Concert"]),
    ],
    ids=["omission", "recurring"],
)
async def test_topic_review_saves_only_the_corrected_list(monkeypatch, draft, reviewed):
    draft_json = json.dumps([{"name": name} for name in draft])
    client = FakeNotebookClient(
        answers=[draft_json, json.dumps([{"name": name} for name in reviewed])]
    )
    ask = client.chat.ask

    async def ask_before_save(notebook_id, prompt):
        assert FakeVideoRecord.records == []
        return await ask(notebook_id, prompt)

    monkeypatch.setattr(client.chat, "ask", ask_before_save)

    topics = await summarizer_util.get_topic_list(client, VIDEO_REF)

    assert [topic.name for topic in topics] == reviewed
    assert len(client.prompts) == 2
    assert draft_json not in client.prompts[1][1]
    assert (
        "complete candidate topic list in your previous answer" in client.prompts[1][1]
    )
    assert len(FakeVideoRecord.records) == 1
    assert FakeVideoRecord.records[0].topics == topics
    assert client.deleted == ["notebook-1"]
    cached_client = FakeNotebookClient()
    assert await summarizer_util.get_topic_list(cached_client, VIDEO_REF) == topics
    assert cached_client.prompts == cached_client.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["topics", "detail", "all", "service"])
async def test_large_topic_inventories_keep_every_topic_with_bounded_chat_requests(
    monkeypatch, operation
):
    names = [f"Segment {i:04}: " + "🎵" * 86 for i in range(100)]
    reviewed_names = names + ["Audience questions"]
    draft = json.dumps([{"name": name} for name in names], ensure_ascii=False)
    reviewed = json.dumps(
        [{"name": name} for name in reviewed_names], ensure_ascii=False
    )
    answers = [draft, reviewed]
    if operation == "detail":
        answers.append("Selected details")
    elif operation != "topics":
        answers += [f"Details {index}" for index in range(len(reviewed_names))]
    client = FakeNotebookClient(answers=answers)
    ask = client.chat.ask

    async def size_limited_ask(notebook_id, prompt):
        if len(prompt.encode("utf-8")) > 4000:
            raise ChatError(
                "Chat request was rejected by the server (status 3). Question too large."
            )
        return await ask(notebook_id, prompt)

    monkeypatch.setattr(client.chat, "ask", size_limited_ask)
    config = RuntimeConfig(output_language="Japanese")
    if operation == "topics":
        result = await summarizer_util.get_topic_list(client, VIDEO_REF, config=config)
        assert [topic.name for topic in result] == reviewed_names
    elif operation == "detail":
        result = await summarizer_util.get_topic_details(
            client, VIDEO_REF, 50, config=config
        )
        assert result == {"topic_name": names[50], "detail": "Selected details"}
    elif operation == "all":
        result = await summarizer_util.get_topic_details_all(
            client, VIDEO_REF, config=config
        )
        assert [item["topic_name"] for item in result] == reviewed_names
    else:
        result = await NotebookLMSummaryService(client, config=config).summarize(
            VIDEO_REF
        )
        assert [topic.name for topic in result] == reviewed_names
    assert [topic.name for topic in FakeVideoRecord.records[0].topics] == reviewed_names
    assert client.deleted == ["notebook-1"]
    assert all(notebook_id == "notebook-1" for notebook_id, _ in client.prompts)
    assert all(len(prompt.encode("utf-8")) <= 4000 for _, prompt in client.prompts)
    assert "previous answer" in client.prompts[1][1]
    cached_client = FakeNotebookClient()
    assert [
        topic.name
        for topic in await summarizer_util.get_topic_list(
            cached_client, VIDEO_REF, config=config
        )
    ] == reviewed_names
    assert cached_client.created == cached_client.prompts == []


@pytest.mark.asyncio
async def test_oversized_language_never_dispatches_a_chat_request():
    client = FakeNotebookClient()
    with pytest.raises(PermanentSummaryError, match="4000"):
        await summarizer_util.get_topic_list(
            client, VIDEO_REF, config=RuntimeConfig(output_language="語" * 4000)
        )
    assert client.prompts == []
    assert FakeVideoRecord.records == []
    assert client.deleted == [notebook.id for notebook in client.created]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "stage"),
    [
        ("automatic", "topic_detail"),
        ("topics", "topic_extraction"),
        ("detail", "topic_detail"),
        ("all", "topic_review"),
    ],
)
async def test_workers_do_not_retry_or_download_audio_for_status_3(operation, stage):
    phase_index = {"topic_extraction": 0, "topic_review": 1, "topic_detail": 2}[stage]
    client = FakeNotebookClient(
        answers=['[{"name": "Concert"}]'] * phase_index
        + [
            ChatError(
                "Chat request was rejected by the server (status 3). Question too large."
            )
        ]
    )
    config = RuntimeConfig(source_retry_limit=1, youtube_yt_dlp_fallback_enabled=True)
    cog, job = make_worker_job(client, config, operation)
    if operation == "automatic":
        assert not await cog._generate_for_job(job)
    else:
        await cog._generate(job)
    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.retry_count == 1
    assert not job.youtube_audio_fallback
    assert len(client.prompts) == phase_index + 1
    assert client.deleted == ["notebook-1"]
    if stage == "topic_detail":
        assert FakeVideoRecord.records[0].topics == [Topic(name="Concert")]
    else:
        assert FakeVideoRecord.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("existing_record", "answer", "error_type"),
    [
        pytest.param(False, "not JSON", InvalidSummaryResponse, id="json-no-cache"),
        pytest.param(True, "not JSON", InvalidSummaryResponse, id="json-empty-cache"),
        pytest.param(
            False,
            httpx.ReadTimeout("review timed out"),
            TransientSummaryError,
            id="timeout",
        ),
        pytest.param(False, RuntimeError("review failed"), RuntimeError, id="request"),
    ],
)
async def test_topic_review_failure_leaves_no_draft_cache(
    existing_record, answer, error_type
):
    if existing_record:
        video = FakeVideoRecord(
            source="youtube", video_id=VIDEO_ID, topics=[], output_language="english"
        )
        await video.save()
    client = FakeNotebookClient(answers=['[{"name": "Concert"}]', answer])

    with pytest.raises(error_type):
        await NotebookLMSummaryService(client).summarize(VIDEO_REF)

    assert len(client.prompts) == 2
    assert client.deleted == ["notebook-1"]
    if existing_record:
        assert FakeVideoRecord.records == [video]
        assert video.topics == []
    else:
        assert FakeVideoRecord.records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "outcome"),
    [
        ("automatic", "recovered"),
        ("automatic", "exhausted"),
        ("automatic", "audio_failure"),
        ("topics", "recovered"),
        ("detail", "exhausted"),
        ("all", "audio_failure"),
    ],
)
async def test_workers_preserve_retry_policy_for_topic_review(
    monkeypatch, tmp_path, operation, outcome
):
    draft = '[{"name": "Concert"}]'
    reviewed = '[{"name": "Audience questions"}]'
    answers = [draft, "not JSON"]
    if outcome == "recovered":
        answers += [draft, reviewed]
        if operation != "topics":
            answers.append("The speaker answers questions.")
    elif outcome == "exhausted":
        answers += [draft, "not JSON"]
    client = FakeNotebookClient(answers=answers)
    client.sources.add_file = AsyncMock()
    audio = tmp_path / "audio.m4a"
    audio.write_bytes(b"audio")
    downloads = []
    cleaned = []

    @asynccontextmanager
    async def fake_download(ref):
        downloads.append(ref)
        try:
            yield audio
        finally:
            cleaned.append(ref)

    monkeypatch.setattr(summarizer_util, "download_generic_audio", fake_download)
    config = RuntimeConfig(
        youtube_yt_dlp_fallback_enabled=True, output_language="Japanese"
    )
    cog, job = make_worker_job(client, config, operation)
    if outcome == "audio_failure":
        job.youtube_audio_fallback = True
        job.retry_count = config.source_retry_limit

    if operation == "automatic":
        await cog._generate_for_job(job)
    else:
        await cog._generate(job)

    if outcome == "recovered":
        assert job.status is JobStatus.READY_TO_DELIVER
        assert job.retry_count == 0
        assert [topic.name for topic in FakeVideoRecord.records[0].topics] == [
            "Audience questions"
        ]
        assert FakeVideoRecord.records[0].output_language == "japanese"
        if operation == "automatic":
            assert job.summary_details == [
                Topic(
                    name="Audience questions", detail="The speaker answers questions."
                )
            ]
        else:
            assert "Audience questions" in "\n".join(job.delivery_chunks)
        assert len(client.prompts) == (4 if operation == "topics" else 5)
    else:
        assert FakeVideoRecord.records == []
        if outcome == "exhausted":
            assert job.status is JobStatus.QUEUED
            assert job.retry_count == 1
            assert job.next_attempt_at > datetime.now(UTC)
            assert len(client.prompts) == 4
        else:
            assert job.status is JobStatus.READY_TO_DELIVER
            assert job.retry_count == config.source_retry_limit + 1
            assert len(client.prompts) == 2
    notebook_ids = [notebook.id for notebook in client.created]
    assert client.deleted == notebook_ids
    assert len(notebook_ids) == (1 if outcome == "audio_failure" else 2)
    for _, prompt in client.prompts:
        assert "in Japanese" in prompt
        assert "in English" not in prompt
    if outcome == "audio_failure":
        assert downloads == cleaned == [VIDEO_REF]
        client.sources.add_file.assert_awaited_once()
    else:
        assert downloads == cleaned == []
        client.sources.add_file.assert_not_awaited()


@pytest.mark.asyncio
async def test_topic_list_reuses_cached_names_in_the_selected_language():
    topics = [Topic(name="今後の予定", detail="The speaker discusses future plans.")]
    video = FakeVideoRecord(
        source=VIDEO_REF.source,
        video_id=VIDEO_REF.video_id,
        topics=topics,
        output_language="japanese",
    )
    await video.save()
    client = FakeNotebookClient()

    cached_topics = await summarizer_util.get_topic_list(
        client, VIDEO_REF, config=RuntimeConfig(output_language="Japanese")
    )

    assert cached_topics == topics
    assert cached_topics[0].name == "今後の予定"
    assert cached_topics[0].detail == "The speaker discusses future plans."
    assert client.created == []
    assert client.prompts == []


@pytest.mark.asyncio
async def test_topic_cache_separates_languages_and_preserves_untagged_legacy_data():
    legacy = FakeVideoRecord(
        source="youtube",
        video_id=VIDEO_ID,
        topics=[Topic(name="Legacy topic", detail="Legacy details")],
    )
    await legacy.save()
    records = {}
    for language, name, detail in [
        ("English", "Concert", "The speaker discusses the concert."),
        ("Japanese", "コンサート", "話者はコンサートについて話します。"),
    ]:
        topics_json = json.dumps([{"name": name}])
        client = FakeNotebookClient(answers=[topics_json, topics_json, detail])
        result = await NotebookLMSummaryService(
            client, config=RuntimeConfig(output_language=language)
        ).summarize(VIDEO_REF)
        assert result == [Topic(name=name, detail=detail)]
        assert len(client.prompts) == 3
        for _, prompt in client.prompts:
            assert f"in {language}" in prompt
        records[language] = result
    assert [record.output_language for record in FakeVideoRecord.records] == [
        None,
        "english",
        "japanese",
    ]
    for language, key in [(" eNgLiSh ", "English"), ("JAPANESE", "Japanese")]:
        cached_client = FakeNotebookClient()
        config = RuntimeConfig(output_language=language)
        assert (
            await NotebookLMSummaryService(cached_client, config=config).summarize(
                VIDEO_REF
            )
            == records[key]
        )
        assert [
            topic.name
            for topic in await summarizer_util.get_topic_list(
                cached_client, VIDEO_REF, config=config
            )
        ] == [records[key][0].name]
        assert await summarizer_util.get_topic_details(
            cached_client, VIDEO_REF, 0, config=config
        ) == {"topic_name": records[key][0].name, "detail": records[key][0].detail}
        assert cached_client.prompts == cached_client.created == []
    assert legacy.output_language is None
    assert legacy.topics == [Topic(name="Legacy topic", detail="Legacy details")]


@pytest.mark.asyncio
@pytest.mark.parametrize("all_details", [False, True])
async def test_missing_details_use_only_the_selected_language_cache(
    all_details, monkeypatch
):
    cached_detail = "保存された詳細 [8, 9]"
    cited_detail = (
        "新しい詳細 [1]。  補足[2,3][4, 5] と [メモ]。\n\n"
        "[6] [1](https://example.com) はリンクです。[7]  \n最後の詳細。[8]"
    )
    clean_detail = (
        "新しい詳細。  補足 と [メモ]。\n\n"
        "[1](https://example.com) はリンクです。  \n最後の詳細。"
    )
    english = FakeVideoRecord(
        source="youtube",
        video_id=VIDEO_ID,
        output_language="english",
        topics=[
            Topic(name="Concert", detail="English details"),
            Topic(name="Questions"),
        ],
    )
    japanese = FakeVideoRecord(
        source="youtube",
        video_id=VIDEO_ID,
        output_language="japanese",
        topics=[Topic(name="コンサート", detail=cached_detail), Topic(name="質問")],
    )
    await english.save()
    await japanese.save()
    save = AsyncMock(wraps=japanese.save)
    monkeypatch.setattr(japanese, "save", save)
    client = FakeNotebookClient(answers=[cited_detail])
    config = RuntimeConfig(output_language="Japanese")
    if all_details:
        result = await summarizer_util.get_topic_details_all(
            client, VIDEO_REF, config=config
        )
        assert result == [
            {"topic_name": "コンサート", "detail": cached_detail},
            {"topic_name": "質問", "detail": clean_detail},
        ]
    else:
        assert await summarizer_util.get_topic_details(
            client, VIDEO_REF, 1, config=config
        ) == {"topic_name": "質問", "detail": clean_detail}
        assert await summarizer_util.get_topic_details(
            client, VIDEO_REF, 0, config=config
        ) == {"topic_name": "コンサート", "detail": cached_detail}
    assert english.topics == [
        Topic(name="Concert", detail="English details"),
        Topic(name="Questions"),
    ]
    assert japanese.topics[0].detail == cached_detail
    assert japanese.topics[1].detail == clean_detail
    save.assert_awaited_once()
    assert len(client.prompts) == 1
    assert "explanation in Japanese" in client.prompts[0][1]


@pytest.mark.asyncio
@pytest.mark.parametrize("all_details", [False, True])
async def test_citation_only_detail_is_rejected_without_caching(
    all_details, monkeypatch
):
    video = FakeVideoRecord(
        source="youtube",
        video_id=VIDEO_ID,
        output_language="english",
        topics=[Topic(name="Concert")],
    )
    await video.save()
    save = AsyncMock(wraps=video.save)
    monkeypatch.setattr(video, "save", save)
    client = FakeNotebookClient(answers=[" [1][2, 3]\n\n [4] "])

    with pytest.raises(InvalidSummaryResponse):
        if all_details:
            await summarizer_util.get_topic_details_all(client, VIDEO_REF)
        else:
            await summarizer_util.get_topic_details(client, VIDEO_REF, 0)

    assert video.topics[0].detail is None
    save.assert_not_awaited()
    assert client.deleted == ["notebook-1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("automatic", [False, True])
async def test_current_language_applies_to_generation_but_not_saved_delivery(automatic):
    english = FakeVideoRecord(
        source="youtube",
        video_id=VIDEO_ID,
        output_language="english",
        topics=[Topic(name="Concert", detail="English details")],
    )
    await english.save()
    topics = json.dumps([{"name": "コンサート"}])
    client = FakeNotebookClient(answers=[topics, topics, "日本語の詳細"])
    target = SimpleNamespace(
        send=AsyncMock(side_effect=RuntimeError("Discord offline"))
    )

    def make_worker(language):
        bot = SimpleNamespace(
            fetch_channel=None,
            message_rate_limiter=None,
            settings=None,
            notebook_client=client,
            video_work=VideoWorkCoordinator(),
            get_channel=lambda _: target,
        )
        config = RuntimeConfig(output_language=language, auto_summary_channel_id=123)
        if automatic:
            cog = make_autosummary(bot)
            cog.summary_service = NotebookLMSummaryService(client, config=config)
            cog._save_item = AsyncMock(return_value=True)
        else:
            cog = make_manual_summary(bot)
        cog.settings = config
        return cog

    if automatic:
        job = FakeSummaryJob(
            video_id=VIDEO_ID,
            channel_id="UC1",
            channel_name="Channel",
            title="Video",
            available_at=datetime.now(UTC),
            next_attempt_at=datetime.now(UTC),
            status=JobStatus.QUEUED,
        )
    else:
        job = make_manual_detail_job()
        job.title = "Video"
        job.channel_name = "Channel"

    # The job already exists when a restart changes the configured language.
    cog = make_worker("Japanese")
    if automatic:
        await cog._generate_for_job(job)
        await cog._deliver_for_job(job)
    else:
        await cog._generate(job)
        await cog._deliver(job)
    assert job.status is JobStatus.READY_TO_DELIVER
    assert "日本語の詳細" in "\n".join(job.delivery_chunks)
    saved_chunks = list(job.delivery_chunks)
    assert len(client.prompts) == 3

    # A later restart changes the setting again before a delivery retry.
    target.send = AsyncMock()
    restarted_worker = make_worker("English")
    if automatic:
        await restarted_worker._process_pending_video(job)
    else:
        job.status = JobStatus.DELIVERING
        job.claimed_by = "worker"
        await restarted_worker._process(job)
    assert job.status is JobStatus.COMPLETED
    assert job.delivery_chunks == saved_chunks
    assert len(client.prompts) == 3
    assert "日本語の詳細" in "".join(
        call.args[0] for call in target.send.await_args_list
    )
    assert english.topics == [Topic(name="Concert", detail="English details")]


@pytest.mark.asyncio
async def test_summarization_pipeline_generates_caches_and_reuses_all_topic_details():
    client = FakeNotebookClient(
        answers=[
            '[{"name": "Introduction"}, {"name": "Conclusion"}]',
            '[{"name": "Introduction"}, {"name": "Audience questions"}, {"name": "Conclusion"}]',
            "The speaker introduces the subject.",
            "The speaker answers questions.",
            "The speaker closes the discussion.",
        ]
    )

    details = await summarizer_util.get_topic_details_all(client, VIDEO_REF)

    assert details == [
        {"topic_name": "Introduction", "detail": "The speaker introduces the subject."},
        {
            "topic_name": "Audience questions",
            "detail": "The speaker answers questions.",
        },
        {"topic_name": "Conclusion", "detail": "The speaker closes the discussion."},
    ]
    assert len(FakeVideoRecord.records) == 1
    assert [topic.detail for topic in FakeVideoRecord.records[0].topics] == [
        "The speaker introduces the subject.",
        "The speaker answers questions.",
        "The speaker closes the discussion.",
    ]
    assert len(client.prompts) == 5
    assert all(notebook_id == "notebook-1" for notebook_id, _ in client.prompts)
    assert "'Audience questions'" in client.prompts[3][1]
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
async def test_twitch_source_uploads_temporary_audio_file(monkeypatch, tmp_path):
    client = FakeNotebookClient()
    uploaded = []
    client.sources.add_file = AsyncMock(
        side_effect=lambda *args, **kwargs: uploaded.append((args, kwargs))
    )
    audio = tmp_path / "vod.m4a"
    audio.write_bytes(b"audio")

    @asynccontextmanager
    async def fake_download(ref):
        assert ref == VideoRef(source="twitch", video_id="123456789")
        yield audio

    monkeypatch.setattr(summarizer_util, "download_twitch_audio", fake_download)

    notebook = await summarizer_util._create_notebook_with_source(
        client, VideoRef(source="twitch", video_id="123456789")
    )

    assert uploaded == [
        (
            (notebook.id, audio),
            {
                "mime_type": "audio/mp4",
                "wait": True,
                "wait_timeout": 600,
                "title": "Twitch VOD 123456789",
            },
        )
    ]


@pytest.mark.asyncio
async def test_x_space_source_uploads_temporary_audio_file(monkeypatch, tmp_path):
    client = FakeNotebookClient()
    uploaded = []
    client.sources.add_file = AsyncMock(
        side_effect=lambda *args, **kwargs: uploaded.append((args, kwargs))
    )
    audio = tmp_path / "space.m4a"
    audio.write_bytes(b"audio")

    @asynccontextmanager
    async def fake_download(ref):
        assert ref == VideoRef(source="x_space", video_id="1OwxWwQOPlNxQ")
        yield audio

    monkeypatch.setattr(summarizer_util, "download_x_space_audio", fake_download)

    notebook = await summarizer_util._create_notebook_with_source(
        client, VideoRef(source="x_space", video_id="1OwxWwQOPlNxQ")
    )

    assert uploaded == [
        (
            (notebook.id, audio),
            {
                "mime_type": "audio/mp4",
                "wait": True,
                "wait_timeout": 600,
                "title": "X Space 1OwxWwQOPlNxQ",
            },
        )
    ]


@pytest.mark.asyncio
async def test_generic_source_uploads_temporary_audio_file(monkeypatch, tmp_path):
    client = FakeNotebookClient()
    client.sources.add_file = AsyncMock()
    audio = tmp_path / "media.m4a"
    audio.write_bytes(b"audio")
    ref = parse_non_space_video_source("https://vimeo.com/123")

    @asynccontextmanager
    async def fake_download(received):
        assert received == ref
        yield audio

    monkeypatch.setattr(summarizer_util, "download_generic_audio", fake_download)

    notebook = await summarizer_util._create_notebook_with_source(client, ref)

    client.sources.add_file.assert_awaited_once_with(
        notebook.id,
        audio,
        mime_type="audio/mp4",
        wait=True,
        wait_timeout=600,
        title="Media",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["topics", "service"])
async def test_youtube_audio_fallback_uploads_audio_and_keeps_the_youtube_cache(
    monkeypatch,
    tmp_path,
    operation,
):
    config = RuntimeConfig(output_language="Japanese")
    answers = ['[{"name": "Intro"}]'] * 2
    if operation != "topics":
        answers.append("Details")
    client = FakeNotebookClient(answers=answers, source_error=True)
    client.sources.add_file = AsyncMock()
    audio = tmp_path / "youtube.m4a"
    audio.write_bytes(b"audio")
    downloads = []
    cleaned = []

    @asynccontextmanager
    async def fake_download(ref):
        assert ref == VIDEO_REF
        downloads.append(ref)
        try:
            yield audio
        finally:
            cleaned.append(ref)

    monkeypatch.setattr(summarizer_util, "download_generic_audio", fake_download)
    if operation == "topics":
        result = await summarizer_util.get_topic_list(
            client, VIDEO_REF, use_youtube_audio=True, config=config
        )
        assert result == [Topic(name="Intro")]
    else:
        result = await NotebookLMSummaryService(client, config=config).summarize(
            VIDEO_REF, use_youtube_audio=True
        )
        assert result == [Topic(name="Intro", detail="Details")]

    assert len(downloads) == len(cleaned) == 1
    client.sources.add_file.assert_awaited_once_with(
        "notebook-1",
        audio,
        mime_type="audio/mp4",
        wait=True,
        wait_timeout=600,
        title=f"YouTube video {VIDEO_ID}",
    )
    assert client.added_urls == []
    assert client.deleted == ["notebook-1"]
    assert len(client.prompts) == (2 if operation == "topics" else 3)
    assert all("in Japanese" in prompt for _, prompt in client.prompts)
    assert len(FakeVideoRecord.records) == 1
    assert FakeVideoRecord.records[0].source == "youtube"
    assert FakeVideoRecord.records[0].video_id == VIDEO_ID
    assert await summarizer_util.get_topic_list(client, VIDEO_REF, config=config) == [
        Topic(name="Intro", detail=None if operation == "topics" else "Details")
    ]
    assert len(downloads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("all_details", [False, True])
async def test_youtube_audio_fallback_preserves_cached_topics_and_completed_details(
    monkeypatch,
    tmp_path,
    all_details,
):
    video = FakeVideoRecord(
        source="youtube",
        video_id=VIDEO_ID,
        topics=[
            Topic(name="Intro", detail="Cached details"),
            Topic(name="Outro"),
        ],
        output_language="english",
    )
    await video.save()
    client = FakeNotebookClient(answers=["New details"], source_error=True)
    client.sources.add_file = AsyncMock()
    audio = tmp_path / "youtube.m4a"
    audio.write_bytes(b"audio")

    @asynccontextmanager
    async def fake_download(ref):
        assert ref == VIDEO_REF
        yield audio

    monkeypatch.setattr(summarizer_util, "download_generic_audio", fake_download)
    if all_details:
        assert await summarizer_util.get_topic_details_all(
            client, VIDEO_REF, use_youtube_audio=True
        ) == [
            {"topic_name": "Intro", "detail": "Cached details"},
            {"topic_name": "Outro", "detail": "New details"},
        ]
    else:
        assert await summarizer_util.get_topic_details(
            client, VIDEO_REF, 1, use_youtube_audio=True
        ) == {"topic_name": "Outro", "detail": "New details"}
    assert video.topics == [
        Topic(name="Intro", detail="Cached details"),
        Topic(name="Outro", detail="New details"),
    ]
    assert len(client.prompts) == 1
    client.sources.add_file.assert_awaited_once()
    assert client.deleted == ["notebook-1"]
    cached_client = FakeNotebookClient()
    assert await summarizer_util.get_topic_details_all(
        cached_client, VIDEO_REF, use_youtube_audio=True
    )
    assert cached_client.created == []


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["download", "upload", "chat"])
async def test_youtube_audio_fallback_cleans_up_failed_generation(
    monkeypatch, tmp_path, failure_stage
):
    client = FakeNotebookClient(answers=["not JSON"], source_error=True)
    client.sources.add_file = AsyncMock(
        side_effect=RuntimeError("upload failed") if failure_stage == "upload" else None
    )
    audio = tmp_path / "youtube.m4a"
    audio.write_bytes(b"audio")
    cleaned = []

    @asynccontextmanager
    async def fake_download(ref):
        try:
            if failure_stage == "download":
                raise RuntimeError("download failed")
            yield audio
        finally:
            cleaned.append(ref)

    monkeypatch.setattr(summarizer_util, "download_generic_audio", fake_download)
    with pytest.raises((RuntimeError, InvalidSummaryResponse)):
        await summarizer_util.get_topic_list(client, VIDEO_REF, use_youtube_audio=True)
    assert cleaned == [VIDEO_REF]
    assert client.deleted == ["notebook-1"]
    assert FakeVideoRecord.records == []


@pytest.mark.asyncio
async def test_summarization_cleanup_failure_does_not_mask_source_error(monkeypatch):
    client = FakeNotebookClient(source_error=True)
    client.notebooks.delete = AsyncMock(side_effect=RuntimeError("delete failed"))

    with pytest.raises(SourceAddError):
        await summarizer_util._create_notebook_with_source(client, VIDEO_REF)
    assert FakeVideoRecord.records == []


@pytest.fixture
def fake_cache_collection(monkeypatch):
    async def delete_many(query):
        before = len(FakeVideoRecord.records)
        FakeVideoRecord.records[:] = [
            record
            for record in FakeVideoRecord.records
            if not all(getattr(record, key) == value for key, value in query.items())
        ]
        return SimpleNamespace(deleted_count=before - len(FakeVideoRecord.records))

    collection = SimpleNamespace(delete_many=AsyncMock(side_effect=delete_many))
    monkeypatch.setattr(VideoSummary, "get_pymongo_collection", lambda: collection)
    return collection


def test_deletecache_command_has_one_required_argument_and_existing_access_model():
    command = Summarizer.deletecache
    assert command.name == "deletecache"
    assert [
        (parameter.name, parameter.required) for parameter in command.parameters
    ] == [("video", True)]
    assert (
        command.default_permissions == Autosummary.addignoredtopic.default_permissions
    )
    assert command.checks == Autosummary.addignoredtopic.checks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("video", "expected"),
    [
        (f"https://youtu.be/{VIDEO_ID}?t=42", VideoRef("youtube", VIDEO_ID)),
        ("https://m.twitch.tv/videos/123456789", VideoRef("twitch", "123456789")),
        (
            "https://mobile.twitter.com/i/spaces/1OwxWwQOPlNxQ?foo=bar",
            VideoRef("x_space", "1OwxWwQOPlNxQ"),
        ),
        (
            "https://VIMEO.com:443/123456789#fragment",
            parse_non_space_video_source("https://vimeo.com/123456789"),
        ),
    ],
)
async def test_deletecache_normalizes_direct_references_without_availability_checks(
    monkeypatch, video, expected
):
    delete = AsyncMock(return_value=3)
    resolver = AsyncMock(
        side_effect=AssertionError("direct references must not need remote resolution")
    )
    monkeypatch.setattr(
        summarizer_module, "delete_cached_summary", delete, raising=False
    )
    monkeypatch.setattr(summarizer_module, "resolve_parsed_x_space", resolver)
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=FakeNotebookClient(),
        get_cog=AsyncMock(),
    )
    interaction = FakeInteraction()

    await Summarizer.deletecache.callback(Summarizer(bot), interaction, video)

    delete.assert_awaited_once_with(expected)
    resolver.assert_not_awaited()
    bot.get_cog.assert_not_called()
    assert bot.notebook_client.created == bot.notebook_client.prompts == []
    assert interaction.response.deferred == [{"thinking": True, "ephemeral": True}]
    assert interaction.followup.messages == [
        (
            "Deleted 3 cached summary records for this video across all languages.",
            {"ephemeral": True},
        )
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [0, 1])
async def test_deletecache_reports_count_or_missing_cache_through_rate_limiter(
    monkeypatch, count
):
    monkeypatch.setattr(
        summarizer_module,
        "delete_cached_summary",
        AsyncMock(return_value=count),
        raising=False,
    )

    async def send(sender, content, **kwargs):
        return await sender(content, **kwargs)

    limiter = SimpleNamespace(send=AsyncMock(side_effect=send))
    bot = SimpleNamespace(
        fetch_channel=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        message_rate_limiter=limiter,
    )
    interaction = FakeInteraction()
    await Summarizer.deletecache.callback(Summarizer(bot), interaction, VIDEO_ID)

    message = (
        "No cached summary found for this video."
        if count == 0
        else "Deleted 1 cached summary record for this video across all languages."
    )
    limiter.send.assert_awaited_once_with(
        interaction.followup.send, message, ephemeral=True
    )
    assert interaction.followup.messages == [(message, {"ephemeral": True})]


@pytest.mark.asyncio
@pytest.mark.parametrize("contains_space", [False, True])
async def test_deletecache_resolves_x_posts_using_existing_media_fallback(
    monkeypatch, contains_space
):
    value = "https://x.com/example/status/1234567890123456789"
    space_ref = VideoRef(source="x_space", video_id="1OwxWwQOPlNxQ")
    resolver = AsyncMock(return_value=space_ref)
    if not contains_space:
        from vsummary.util.x_space import XPostWithoutSpaceError

        resolver.side_effect = XPostWithoutSpaceError(
            "This X post does not contain an X Space."
        )
    delete = AsyncMock(return_value=1)
    monkeypatch.setattr(summarizer_module, "resolve_parsed_x_space", resolver)
    monkeypatch.setattr(
        summarizer_module, "delete_cached_summary", delete, raising=False
    )

    parser = Mock(wraps=summarizer_module.parse_x_space_input)
    monkeypatch.setattr(summarizer_module, "parse_x_space_input", parser)
    await Summarizer.deletecache.callback(
        Summarizer(
            SimpleNamespace(
                video_work=VideoWorkCoordinator(), message_rate_limiter=None
            )
        ),
        FakeInteraction(),
        value,
    )
    parser.assert_called_once()

    resolver.assert_awaited_once_with(summarizer_module.parse_x_space_input(value))
    delete.assert_awaited_once_with(
        space_ref if contains_space else parse_non_space_video_source(value)
    )


@pytest.mark.asyncio
async def test_deletecache_requests_direct_space_url_when_post_cannot_be_resolved(
    monkeypatch,
):
    from vsummary.util.x_space import XSpaceResolutionError

    resolver = AsyncMock(
        side_effect=XSpaceResolutionError("This X Space is unavailable.")
    )
    delete = AsyncMock()
    monkeypatch.setattr(summarizer_module, "resolve_parsed_x_space", resolver)
    monkeypatch.setattr(
        summarizer_module, "delete_cached_summary", delete, raising=False
    )
    interaction = FakeInteraction()

    await Summarizer.deletecache.callback(
        Summarizer(
            SimpleNamespace(
                video_work=VideoWorkCoordinator(), message_rate_limiter=None
            )
        ),
        interaction,
        "https://x.com/example/status/1234567890123456789",
    )

    delete.assert_not_awaited()
    assert "direct X Space URL" in interaction.followup.messages[0][0]
    assert interaction.followup.messages[0][1] == {"ephemeral": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("video", ["http://127.0.0.1/video", "https://[invalid"])
async def test_deletecache_rejects_invalid_references_without_deleting(
    monkeypatch, video
):
    delete = AsyncMock()
    monkeypatch.setattr(
        summarizer_module, "delete_cached_summary", delete, raising=False
    )
    interaction = FakeInteraction()

    await Summarizer.deletecache.callback(
        Summarizer(
            SimpleNamespace(
                video_work=VideoWorkCoordinator(), message_rate_limiter=None
            )
        ),
        interaction,
        video,
    )

    delete.assert_not_awaited()
    assert len(interaction.followup.messages) == 1
    assert interaction.followup.messages[0][1] == {"ephemeral": True}


@pytest.mark.asyncio
async def test_deletecache_reports_database_failure_and_releases_lock(
    fake_cache_collection, caplog
):
    fake_cache_collection.delete_many.side_effect = OperationFailure(
        "database unavailable"
    )
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
    )
    interaction = FakeInteraction()

    await Summarizer.deletecache.callback(Summarizer(bot), interaction, VIDEO_ID)

    assert interaction.followup.messages == [
        ("Could not delete the cached summary. Please try again.", {"ephemeral": True})
    ]
    assert "Could not delete cached summary" in caplog.text
    async with asyncio.timeout(0.1):
        async with bot.video_work.for_video(VIDEO_REF):
            pass


@pytest.mark.asyncio
async def test_deletecache_waits_for_generation_and_preserves_jobs_and_delivery(
    monkeypatch, fake_cache_collection
):
    client = FakeNotebookClient(
        answers=['[{"name": "Concert"}]'] * 2 + ["Concert details"]
    )
    started = asyncio.Event()
    release = asyncio.Event()
    ask = client.chat.ask

    async def blocked_ask(notebook_id, prompt):
        started.set()
        await release.wait()
        return await ask(notebook_id, prompt)

    monkeypatch.setattr(client.chat, "ask", blocked_ask)
    target = FakeChannelTarget()
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        notebook_client=client,
        video_work=VideoWorkCoordinator(),
        get_channel=lambda _: target,
    )
    automatic = make_autosummary(bot)
    automatic.settings = RuntimeConfig(auto_summary_channel_id=123)
    automatic.summary_service = NotebookLMSummaryService(client)
    automatic._save_item = AsyncMock(return_value=True)
    job = FakeSummaryJob(
        video_id=VIDEO_ID,
        channel_id="UC1",
        channel_name="Channel",
        title="Video",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
        status=JobStatus.QUEUED,
    )
    queued_job = make_manual_detail_job()
    queued_before = queued_job.model_dump()
    interaction = FakeInteraction()
    generation = asyncio.create_task(automatic._generate_for_job(job))
    await started.wait()
    deletion = asyncio.create_task(
        Summarizer.deletecache.callback(Summarizer(bot), interaction, VIDEO_ID)
    )
    await asyncio.sleep(0)

    assert interaction.response.deferred == [{"thinking": True, "ephemeral": True}]
    fake_cache_collection.delete_many.assert_not_awaited()
    release.set()
    assert await generation
    await deletion

    fake_cache_collection.delete_many.assert_awaited_once_with(
        {"source": "youtube", "video_id": VIDEO_ID}
    )
    assert FakeVideoRecord.records == []
    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.summary_details == [Topic(name="Concert", detail="Concert details")]
    assert queued_job.model_dump() == queued_before
    assert target.messages == []
    assert len(client.prompts) == 3

    assert await automatic._deliver_for_job(job)
    assert job.status is JobStatus.COMPLETED
    assert "Concert details" in "".join(target.messages)
    assert len(client.prompts) == 3
    fresh_client = FakeNotebookClient(answers=['[{"name": "Fresh concert topic"}]'] * 2)
    topics = await summarizer_util.get_topic_list(fresh_client, VIDEO_REF)
    assert topics == [Topic(name="Fresh concert topic")]
    assert len(fresh_client.prompts) == 2


@pytest.mark.asyncio
async def test_topics_command_queues_youtube_topics_without_a_picker():
    manual_summary = SimpleNamespace(enqueue=AsyncMock())
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
        get_cog=lambda name: manual_summary if name == "ManualSummary" else None,
    )
    cog = Summarizer(bot)
    interaction = FakeInteraction()

    await Summarizer.topics.callback(cog, interaction, VIDEO_ID)

    assert interaction.response.deferred == [{"thinking": True}]
    assert interaction.followup.messages == [
        ("YouTube video topics are queued and will be posted here.", {}),
    ]
    manual_summary.enqueue.assert_awaited_once_with(
        ref=VIDEO_REF,
        operation=ManualSummaryOperation.TOPICS,
        channel_id=123,
        requester_id=456,
    )
    assert not hasattr(summarizer_module, "get_topic_list")


@pytest.mark.asyncio
async def test_topics_command_reports_invalid_video_input():
    cog = Summarizer(
        SimpleNamespace(
            fetch_channel=None,
            message_rate_limiter=None,
            settings=None,
            video_work=VideoWorkCoordinator(),
            notebook_client=object(),
        )
    )
    interaction = FakeInteraction()

    await Summarizer.topics.callback(cog, interaction, "not-a-url")

    assert interaction.response.deferred == [{"thinking": True}]
    assert interaction.followup.messages == [
        ("'not-a-url' is not a supported video source.", {})
    ]


@pytest.mark.asyncio
async def test_topics_command_queues_generic_media_url():
    manual_summary = SimpleNamespace(enqueue=AsyncMock())
    cog = Summarizer(
        SimpleNamespace(message_rate_limiter=None, get_cog=lambda _: manual_summary)
    )
    interaction = FakeInteraction()

    await Summarizer.topics.callback(cog, interaction, "https://vimeo.com/123")

    assert interaction.followup.messages == [
        ("Media topics are queued and will be posted here.", {})
    ]
    assert manual_summary.enqueue.await_args.kwargs[
        "ref"
    ] == parse_non_space_video_source("https://vimeo.com/123")


@pytest.mark.asyncio
async def test_detail_reports_generic_media_preflight_failure():
    from vsummary.util.generic_media import GenericMediaError

    manual_summary = SimpleNamespace(
        enqueue=AsyncMock(
            side_effect=GenericMediaError("Could not resolve the media host.")
        )
    )
    cog = Summarizer(
        SimpleNamespace(message_rate_limiter=None, get_cog=lambda _: manual_summary)
    )
    interaction = FakeInteraction()

    await Summarizer.detail.callback(cog, interaction, "https://vimeo.com/123", 1)

    assert interaction.followup.messages == [("Could not resolve the media host.", {})]


@pytest.mark.asyncio
async def test_x_post_without_space_falls_back_to_generic_media(monkeypatch):
    from vsummary.util.x_space import XPostWithoutSpaceError

    manual_summary = SimpleNamespace(enqueue=AsyncMock())
    cog = Summarizer(
        SimpleNamespace(message_rate_limiter=None, get_cog=lambda _: manual_summary)
    )
    interaction = FakeInteraction()
    monkeypatch.setattr(
        summarizer_module,
        "resolve_parsed_x_space",
        AsyncMock(side_effect=XPostWithoutSpaceError("ordinary post")),
    )

    parser = Mock(wraps=summarizer_module.parse_x_space_input)
    monkeypatch.setattr(summarizer_module, "parse_x_space_input", parser)
    value = "https://twitter.com/speaker/status/123456789?ref=share"
    await Summarizer.topics.callback(cog, interaction, value)
    parser.assert_called_once()

    assert manual_summary.enqueue.await_args.kwargs[
        "ref"
    ] == parse_non_space_video_source(value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "https://x.com/i/spaces/1OwxWwQOPlNxQ",
        "https://x.com/example/status/1234567890123456789",
    ],
)
async def test_topics_command_resolves_an_archived_x_space_before_enqueueing(
    monkeypatch,
    value,
):
    manual_summary = SimpleNamespace(enqueue=AsyncMock())
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
        get_cog=lambda name: manual_summary if name == "ManualSummary" else None,
    )
    cog = Summarizer(bot)
    interaction = FakeInteraction()
    x_space_ref = VideoRef(source="x_space", video_id="1OwxWwQOPlNxQ")
    resolver = AsyncMock(return_value=x_space_ref)
    monkeypatch.setattr(summarizer_module, "resolve_parsed_x_space", resolver)

    await Summarizer.topics.callback(cog, interaction, value)

    resolver.assert_awaited_once_with(summarizer_module.parse_x_space_input(value))
    assert interaction.followup.messages == [
        ("X Space topics are queued and will be posted here.", {}),
    ]
    manual_summary.enqueue.assert_awaited_once_with(
        ref=x_space_ref,
        operation=ManualSummaryOperation.TOPICS,
        channel_id=123,
        requester_id=456,
    )


@pytest.mark.asyncio
async def test_detail_command_reports_unarchived_x_space_without_queueing(monkeypatch):
    from vsummary.util.x_space import XSpaceResolutionError

    manual_summary = SimpleNamespace(enqueue=AsyncMock())
    cog = Summarizer(
        SimpleNamespace(
            fetch_channel=None,
            message_rate_limiter=None,
            settings=None,
            video_work=VideoWorkCoordinator(),
            notebook_client=object(),
            get_cog=lambda name: manual_summary if name == "ManualSummary" else None,
        )
    )
    interaction = FakeInteraction()
    monkeypatch.setattr(
        summarizer_module,
        "resolve_parsed_x_space",
        AsyncMock(
            side_effect=XSpaceResolutionError(
                "This X Space has ended but has not been archived yet."
            )
        ),
    )

    await Summarizer.detail.callback(
        cog, interaction, "https://x.com/i/spaces/1OwxWwQOPlNxQ", None
    )

    assert interaction.followup.messages == [
        ("This X Space has ended but has not been archived yet.", {}),
    ]
    manual_summary.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_queue_lists_active_auto_and_manual_jobs_in_processing_order(monkeypatch):
    class QueueModel:
        jobs: ClassVar[list] = []
        query: ClassVar[object | None] = None

        @classmethod
        def find(cls, query):
            cls.query = query
            return SimpleNamespace(to_list=AsyncMock(return_value=cls.jobs))

    auto_jobs = type("AutoQueue", (QueueModel,), {})
    auto_jobs.jobs = [
        SimpleNamespace(
            video_id="dQw4w9WgXcQ",
            title="Auto video",
            status=JobStatus.QUEUED,
            next_attempt_at=datetime(2026, 9, 19, 12, tzinfo=UTC),
        )
    ]
    manual_jobs = type("ManualQueue", (QueueModel,), {})
    manual_jobs.jobs = [
        SimpleNamespace(
            source="twitch",
            video_id="123456789",
            title="Manual video",
            status=JobStatus.GENERATING,
            next_attempt_at=datetime.fromisoformat("2026-09-19T18:00:20+07:00"),
        ),
        SimpleNamespace(
            source="x_space",
            video_id="1DxleVnmlOmKL",
            title="Archived Space",
            status=JobStatus.READY_TO_DELIVER,
            next_attempt_at=datetime(2026, 9, 19, 11, 30, tzinfo=UTC),
        ),
        SimpleNamespace(
            source="yt_dlp",
            video_id="hash",
            source_url="https://vimeo.com/123",
            title="Other media",
            status=JobStatus.DELIVERING,
            next_attempt_at=datetime(2026, 9, 19, 11, 45, tzinfo=UTC),
        ),
    ]
    for job in auto_jobs.jobs:
        job.job_type, job.source, job.source_url = "auto", "youtube", None
    for job in manual_jobs.jobs:
        job.job_type = "manual"
        if not hasattr(job, "source_url"):
            job.source_url = None
    auto_jobs.jobs += manual_jobs.jobs
    monkeypatch.setattr(summarizer_module, "SummaryJob", auto_jobs)
    interaction = FakeInteraction()

    await Summarizer.queue.callback(
        Summarizer(
            SimpleNamespace(
                video_work=VideoWorkCoordinator(), message_rate_limiter=None
            )
        ),
        interaction,
    )

    assert interaction.response.deferred == [{"thinking": True}]
    assert interaction.followup.messages == [
        (
            (
                "Active jobs:\n"
                "1. **Manual:** Manual video — <https://www.twitch.tv/videos/123456789> "
                "— **Generating** — <t:1789815620:S>\n"
                "2. **Manual:** Archived Space — <https://x.com/i/spaces/1DxleVnmlOmKL> "
                "— **Ready to deliver** — <t:1789817400:S>\n"
                "3. **Manual:** Other media — <https://vimeo.com/123> "
                "— **Delivering** — <t:1789818300:S>\n"
                "4. **Auto:** Auto video — <https://www.youtube.com/watch?v=dQw4w9WgXcQ> "
                "— **Queued** — <t:1789819200:S>"
            ),
            {},
        )
    ]
    assert auto_jobs.query == {
        "status": {"$in": ["queued", "generating", "ready_to_deliver", "delivering"]}
    }
    assert not hasattr(summarizer_module, "get_topic_list")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    ["[]", "not JSON", "{}", '[{"name": " "}]', json.dumps([{"name": "x" * 101}])],
)
async def test_topic_pipeline_rejects_invalid_drafts_before_review(answer):
    client = FakeNotebookClient(answers=[answer])

    with pytest.raises(InvalidSummaryResponse):
        await summarizer_util.get_topic_list(client, VIDEO_REF)

    assert client.deleted == ["notebook-1"]
    assert len(client.prompts) == 1
    assert FakeVideoRecord.records == []


def test_topic_prompt_requests_detailed_english_prose_without_lists():
    prompt = summarizer_util._topic_prompt(
        [Topic(name="Introduction"), Topic(name="Deep Dive"), Topic(name="Conclusion")],
        1,
    )

    assert "English" in prompt
    assert "450 words" in prompt
    assert "no minimum word count or fixed paragraph count" in prompt
    assert "brief topics" in prompt
    assert "short paragraph" in prompt
    assert "longer or more complex discussions" in prompt
    assert "additional words and paragraphs" in prompt
    assert "padding" in prompt
    assert "repetition" in prompt
    assert "150-200" not in prompt
    assert "2-3" not in prompt
    assert (
        "important claims, reasoning, examples, events, distinctions, and conclusions"
        in prompt
    )
    assert "Do not use bullets, numbered lists, headings" in prompt
    assert "'Introduction' ends" in prompt
    assert "'Conclusion' begins" in prompt
    assert "do not invent missing details" in prompt


def test_detail_prompt_uses_selected_language_and_keeps_length_and_boundaries():
    prompt = summarizer_util._topic_prompt(
        [Topic(name="First"), Topic(name="Middle"), Topic(name="Last")],
        1,
        output_language="Japanese",
    )
    assert "explanation in Japanese" in prompt
    assert "in English" not in prompt
    assert "450 words" in prompt
    assert "no minimum word count or fixed paragraph count" in prompt
    assert "'First' ends" in prompt
    assert "'Last' begins" in prompt


@pytest.mark.asyncio
async def test_detail_command_queues_youtube_summary_for_durable_delivery():
    manual_summary = SimpleNamespace(enqueue=AsyncMock())
    cog = Summarizer(
        SimpleNamespace(
            fetch_channel=None,
            message_rate_limiter=None,
            settings=None,
            video_work=VideoWorkCoordinator(),
            notebook_client=object(),
            get_cog=lambda name: manual_summary if name == "ManualSummary" else None,
        )
    )
    all_interaction = FakeInteraction()

    await Summarizer.detail.callback(cog, all_interaction, VIDEO_ID, None)

    assert all_interaction.followup.messages == [
        ("YouTube video summary is queued and will be posted here.", {}),
    ]
    manual_summary.enqueue.assert_awaited_once_with(
        ref=VIDEO_REF,
        operation=ManualSummaryOperation.DETAIL_ALL,
        topic_index=None,
        channel_id=123,
        requester_id=456,
    )

    one_interaction = FakeInteraction()

    await Summarizer.detail.callback(cog, one_interaction, VIDEO_ID, 2)

    assert one_interaction.followup.messages == [
        ("YouTube video summary is queued and will be posted here.", {}),
    ]
    assert manual_summary.enqueue.await_args_list[-1].kwargs == {
        "ref": VIDEO_REF,
        "operation": ManualSummaryOperation.DETAIL_ONE,
        "topic_index": 1,
        "channel_id": 123,
        "requester_id": 456,
    }
    assert not hasattr(summarizer_module, "get_topic_details")
    assert not hasattr(summarizer_module, "get_topic_details_all")


@pytest.mark.asyncio
async def test_detail_command_reports_unsupported_source_without_queueing():
    manual_summary = SimpleNamespace(enqueue=AsyncMock())
    cog = Summarizer(
        SimpleNamespace(
            fetch_channel=None,
            message_rate_limiter=None,
            settings=None,
            video_work=VideoWorkCoordinator(),
            notebook_client=object(),
            get_cog=lambda name: manual_summary if name == "ManualSummary" else None,
        )
    )
    interaction = FakeInteraction()

    await Summarizer.detail.callback(cog, interaction, "not-a-url", 1)

    assert interaction.followup.messages == [
        ("'not-a-url' is not a supported video source.", {}),
    ]
    manual_summary.enqueue.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_detail_job_preserves_invalid_topic_index_response(monkeypatch):
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        notebook_client=object(),
        video_work=VideoWorkCoordinator(),
    )
    manual_detail = make_manual_summary(bot)
    job = make_manual_detail_job(topic_index=3)
    monkeypatch.setattr(
        manualsummary_module,
        "get_topic_details",
        AsyncMock(side_effect=IndexError),
    )

    monkeypatch.setattr(
        manualsummary_module, "get_video_metadata", AsyncMock(return_value=None)
    )

    await manual_detail._generate(job)

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.delivery_chunks == ["Invalid topic index: 4"]


@pytest.mark.asyncio
async def test_manual_detail_waits_for_auto_summary_and_reuses_cached_details(
    monkeypatch,
):
    class BlockingNotebookClient(FakeNotebookClient):
        def __init__(self):
            super().__init__(
                answers=[
                    '[{"name": "Introduction"}, {"name": "Conclusion"}]',
                    '[{"name": "Introduction"}, {"name": "Conclusion"}]',
                    "The speaker introduces the subject.",
                    "The speaker closes the discussion.",
                ]
            )
            self.ask_started = asyncio.Event()
            self.release_ask = asyncio.Event()

        async def ask(self, notebook_id, prompt):
            self.ask_started.set()
            await self.release_ask.wait()
            return await super().ask(notebook_id, prompt)

    client = BlockingNotebookClient()
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        notebook_client=client,
        video_work=VideoWorkCoordinator(),
    )
    auto_summary = make_autosummary(bot)
    auto_summary.summary_service = NotebookLMSummaryService(client)
    item = FakeSummaryJob(
        video_id=VIDEO_ID,
        channel_id="UC1",
        channel_name="Test",
        title="A stream",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    manual_detail = make_manual_summary(bot)
    job = make_manual_detail_job()
    monkeypatch.setattr(
        manualsummary_module,
        "get_video_metadata",
        AsyncMock(
            return_value=SimpleNamespace(title="A stream", channel_name="Test Channel")
        ),
    )

    automatic_task = asyncio.create_task(auto_summary._generate_for_job(item))
    await client.ask_started.wait()
    detail_task = asyncio.create_task(manual_detail._generate(job))
    await asyncio.sleep(0)

    assert job.delivery_chunks == []

    client.release_ask.set()
    assert await automatic_task
    await detail_task

    assert job.status is JobStatus.READY_TO_DELIVER
    assert job.delivery_chunks == [
        (
            "**Video:** A stream\n**Channel:** Test Channel\n\n"
            "**Introduction**\nThe speaker introduces the subject."
        )
    ]
    assert len(client.created) == 1
    assert len(client.prompts) == 4


@pytest.mark.asyncio
async def test_auto_summary_waits_for_an_earlier_manual_detail_job(monkeypatch):
    detail_started = asyncio.Event()
    release_detail = asyncio.Event()

    async def blocked_detail(client, ref, topic_index, **kwargs):
        detail_started.set()
        await release_detail.wait()
        return {"topic_name": "Introduction", "detail": "Details"}

    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        notebook_client=object(),
        video_work=VideoWorkCoordinator(),
    )
    manual_detail = make_manual_summary(bot)
    job = make_manual_detail_job()
    auto_summary = make_autosummary(bot)
    auto_summary.summary_service = SimpleNamespace(
        summarize=AsyncMock(return_value=[Topic(name="Introduction", detail="Details")])
    )
    item = FakeSummaryJob(
        video_id=VIDEO_ID,
        channel_id="UC1",
        channel_name="Test",
        title="A stream",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    monkeypatch.setattr(manualsummary_module, "get_topic_details", blocked_detail)

    monkeypatch.setattr(
        manualsummary_module, "get_video_metadata", AsyncMock(return_value=None)
    )

    detail_task = asyncio.create_task(manual_detail._generate(job))
    await detail_started.wait()
    automatic_task = asyncio.create_task(auto_summary._generate_for_job(item))
    await asyncio.sleep(0)

    auto_summary.summary_service.summarize.assert_not_awaited()

    release_detail.set()
    await detail_task
    assert await automatic_task
    auto_summary.summary_service.summarize.assert_awaited_once()


@pytest.mark.asyncio
async def test_notebooklm_error_in_manual_detail_releases_work_for_auto_summary(
    monkeypatch,
):
    detail_started = asyncio.Event()
    release_detail = asyncio.Event()

    async def failing_detail(client, ref, topic_index, **kwargs):
        detail_started.set()
        await release_detail.wait()
        raise SourceAddError("no transcript")

    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        notebook_client=object(),
        video_work=VideoWorkCoordinator(),
    )
    manual_detail = make_manual_summary(bot)
    job = make_manual_detail_job()
    auto_summary = make_autosummary(bot)
    auto_summary.summary_service = SimpleNamespace(
        summarize=AsyncMock(return_value=[Topic(name="Introduction", detail="Details")])
    )
    item = FakeSummaryJob(
        video_id=VIDEO_ID,
        channel_id="UC1",
        channel_name="Test",
        title="A stream",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    monkeypatch.setattr(manualsummary_module, "get_topic_details", failing_detail)

    monkeypatch.setattr(
        manualsummary_module, "get_video_metadata", AsyncMock(return_value=None)
    )

    detail_task = asyncio.create_task(manual_detail._generate(job))
    await detail_started.wait()
    automatic_task = asyncio.create_task(auto_summary._generate_for_job(item))
    await asyncio.sleep(0)

    auto_summary.summary_service.summarize.assert_not_awaited()

    release_detail.set()
    await detail_task
    assert job.status is JobStatus.QUEUED
    assert await automatic_task
    auto_summary.summary_service.summarize.assert_awaited_once()


@pytest.mark.asyncio
async def test_bot_setup_loads_all_cogs_and_syncs_commands(monkeypatch):
    holodex = SimpleNamespace(aclose=AsyncMock())
    bot = Bot(notebook_client=object(), holodex=holodex)
    assert bot.holodex is holodex
    loaded = []
    load_extension = AsyncMock(side_effect=lambda cog: loaded.append(cog))
    sync = AsyncMock()
    monkeypatch.setattr(bot, "load_extension", load_extension)
    monkeypatch.setattr(bot.tree, "sync", sync)

    await bot.setup_hook()

    assert loaded == [
        "vsummary.cogs.misc.ping",
        "vsummary.cogs.manualsummary.manualsummary",
        "vsummary.cogs.summarizer.summarizer",
        "vsummary.cogs.autosummary.autosummary",
    ]
    sync.assert_awaited_once_with()
    await bot.close()


@pytest.mark.asyncio
async def test_ping_command_reports_latency_in_milliseconds():
    cog = Ping(SimpleNamespace(latency=0.1234))
    interaction = FakeInteraction()

    await Ping.ping.callback(cog, interaction)

    assert interaction.response.sent == "Pong! Latency: 123ms"


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [TransientHolodexError, PermanentHolodexError])
async def test_autosummary_add_channel_handles_success_duplicate_and_api_errors(
    monkeypatch,
    caplog,
    error_type,
):
    channel = HolodexChannel(id="UC123", name="Test Channel")
    cog = make_autosummary()
    interaction = FakeInteraction()
    cog.holodex.fetch_channel = AsyncMock(return_value=channel)
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
    cog.holodex.fetch_channel = AsyncMock(side_effect=ChannelNotFoundError())

    await Autosummary.addchannel.callback(cog, not_found_interaction, "missing")

    assert not_found_interaction.followup.messages == [
        ("Channel 'missing' was not found on Holodex.", {})
    ]

    error_interaction = FakeInteraction()
    cog.holodex.fetch_channel = AsyncMock(side_effect=error_type("lookup failed"))

    await Autosummary.addchannel.callback(cog, error_interaction, "broken")

    assert error_interaction.followup.messages == [
        ("An error occurred while fetching channel 'broken' from Holodex.", {})
    ]

    assert "Error fetching channel broken: lookup failed" in caplog.messages
    cog.holodex.fetch_channel.assert_awaited_once_with("broken")


@pytest.mark.asyncio
async def test_autosummary_remove_channel_deletes_pending_videos_and_handles_missing():
    cog = make_autosummary()
    followed = FakeFollowedChannel("UC123", "Test Channel")
    FakeFollowedChannel.find_one_result = followed
    interaction = FakeInteraction()

    await Autosummary.removechannel.callback(cog, interaction, "UC123")

    assert followed.deleted is True
    assert FakeSummaryJob.deleted_queries == [
        {
            "job_type": "auto",
            "channel_id": "UC123",
            "status": {"$in": ["queued", "ready_to_deliver"]},
        }
    ]
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
async def test_autosummary_list_channels_splits_messages_at_discord_limit():
    cog = make_autosummary()
    FakeFollowedChannel.records = [
        FakeFollowedChannel(f"UC{i}", "x" * 100) for i in range(30)
    ]
    interaction = FakeInteraction()

    await Autosummary.listchannels.callback(cog, interaction)

    assert len(interaction.followup.messages) > 1
    assert all(len(content) <= 2_000 for content, _ in interaction.followup.messages)


@pytest.mark.asyncio
async def test_autosummary_poll_and_process_loops_delegate_each_due_item():
    cog = make_autosummary()
    channel = FakeFollowedChannel("UC1", "First")
    FakeFollowedChannel.records = [channel]
    videos = [HolodexVideo("video", "Title", None, datetime.now(UTC), "First")]
    cog.holodex.fetch_channel_videos_since = AsyncMock(return_value=videos)
    cog._enqueue_new_video = AsyncMock()

    await cog.check_new_videos()

    cog.holodex.fetch_channel_videos_since.assert_awaited_once_with(
        "UC1", channel.added_at, page_size=50
    )
    cog._enqueue_new_video.assert_awaited_once_with(
        videos[0], FakeFollowedChannel.records[0]
    )

    due_item = FakeSummaryJob(
        video_id="due",
        channel_id="UC1",
        channel_name="First",
        title="Due",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    FakeSummaryJob.records = [due_item]
    cog._claim_due_jobs = AsyncMock(return_value=[due_item])
    cog._process_pending_video = AsyncMock()
    cog._get_auto_summary_channel = AsyncMock(return_value=object())

    await cog.process_due_videos()

    cog._process_pending_video.assert_awaited_once_with(due_item)


@pytest.mark.asyncio
async def test_autosummary_poll_continues_after_one_channel_fails():
    cog = make_autosummary()
    first = FakeFollowedChannel("UC1", "First")
    second = FakeFollowedChannel("UC2", "Second")
    FakeFollowedChannel.records = [first, second]
    video = HolodexVideo("video", "Title", None, datetime.now(UTC), "Second")
    cog.holodex.fetch_channel_videos_since = AsyncMock(
        side_effect=[RuntimeError("Holodex unavailable"), [video]]
    )
    cog._enqueue_new_video = AsyncMock()

    await cog.check_new_videos()

    assert cog.holodex.fetch_channel_videos_since.await_count == 2
    cog._enqueue_new_video.assert_awaited_once_with(video, second)


@pytest.mark.asyncio
async def test_poll_loop_waits_for_discord_ready_before_first_run():
    bot = SimpleNamespace(wait_until_ready=AsyncMock())
    cog = Autosummary.__new__(Autosummary)
    cog.settings = None
    cog.summary_service = None
    cog.notebook = None
    cog.bot = bot

    await cog.before_poll_loop()

    bot.wait_until_ready.assert_awaited_once_with()


def test_autosummary_poll_interval_is_fixed():
    settings = Settings(
        discord_token="token",
        mongodb_uri="mongodb://localhost",
        holodex_api_key="test-holodex-key",
    )
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
        settings=settings,
        holodex=SimpleNamespace(aclose=AsyncMock()),
        summary_service=object(),
    )
    cog = Autosummary(bot, start_polling=False)

    assert cog.settings is bot.settings
    assert cog.holodex is bot.holodex
    assert cog.summary_service is bot.summary_service

    assert cog.poll_loop.minutes == 30


@pytest.mark.asyncio
async def test_autosummary_leaves_shared_holodex_open_on_unload():
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
        settings=None,
        holodex=SimpleNamespace(aclose=AsyncMock()),
        summary_service=object(),
    )
    cog = Autosummary(bot, start_polling=False)

    cog.cog_unload()
    await cog.close()

    bot.holodex.aclose.assert_not_awaited()


@pytest.mark.asyncio
async def test_bot_reuses_holodex_managed_by_startup():
    holodex = SimpleNamespace(aclose=AsyncMock())
    bot = Bot(notebook_client=object(), holodex=holodex)
    try:
        assert bot.holodex is holodex
        assert bot.summary_service.client is bot.notebook_client
        assert bot.summary_service.config is not None
    finally:
        await bot.close()
    holodex.aclose.assert_not_awaited()


@pytest.mark.asyncio
async def test_auto_transcript_delay_uses_operational_config():
    cog = make_autosummary()
    cog.settings = RuntimeConfig(transcript_delay_seconds=60)
    published = datetime.now(UTC)
    channel = FakeFollowedChannel(
        "UC1", "Channel", added_at=published - timedelta(seconds=1)
    )
    video = HolodexVideo(
        id=VIDEO_ID,
        title="Title",
        topic_id=None,
        available_at=published,
        channel_name="Channel",
        duration=30,
    )
    await cog._enqueue_new_video(video, channel)
    assert FakeSummaryJob.records[0].next_attempt_at == published + timedelta(
        seconds=90
    )


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
    cog.holodex.fetch_channel_videos_since = AsyncMock(return_value=videos)
    cog._enqueue_new_video = AsyncMock()

    await cog.check_new_videos()

    cog._enqueue_new_video.assert_awaited_once_with(videos[1], channel)


@pytest.mark.asyncio
async def test_autosummary_enqueue_skips_ignored_and_duplicate_videos(monkeypatch):
    cog = make_autosummary()
    channel = FakeFollowedChannel("UC1", "Test")
    monkeypatch.setattr(
        autosummary_module, "policy_ignores_video", AsyncMock(return_value=True)
    )
    ignored = HolodexVideo(
        id="ignored",
        title="Ignored",
        topic_id="shorts",
        available_at=datetime.now(UTC) - timedelta(hours=3),
        channel_name="Test",
    )

    await cog._enqueue_new_video(ignored, channel)

    assert FakeSummaryJob.records == []

    existing = FakeSummaryJob(
        video_id=VIDEO_ID,
        channel_id="UC1",
        channel_name="Test",
        title="Existing",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    FakeSummaryJob.records = [existing]
    duplicate = HolodexVideo(
        id=VIDEO_ID,
        title="Duplicate",
        topic_id=None,
        available_at=datetime.now(UTC) - timedelta(hours=3),
        channel_name="Test",
    )

    await cog._enqueue_new_video(duplicate, channel)

    assert FakeSummaryJob.records == [existing]


@pytest.mark.asyncio
async def test_autosummary_only_enqueues_unignored_topic_videos_published_after_removal(
    monkeypatch,
):
    cog = make_autosummary()
    channel = FakeFollowedChannel("UC1", "Test")
    removed_at = datetime.now(UTC)
    monkeypatch.setattr(
        autosummary_module, "policy_ignores_video", AsyncMock(side_effect=[True, False])
    )
    before_removal = HolodexVideo(
        "before", "Before", "shorts", removed_at - timedelta(seconds=1), "Test"
    )
    after_removal = HolodexVideo(
        "after", "After", "shorts", removed_at + timedelta(seconds=1), "Test"
    )

    await cog._enqueue_new_video(before_removal, channel)
    await cog._enqueue_new_video(after_removal, channel)

    assert [job.video_id for job in FakeSummaryJob.records] == ["after"]


@pytest.mark.asyncio
async def test_addignoredtopic_does_not_require_manage_guild_permission(monkeypatch):
    """Discord integration permissions, not bot code, control topic commands."""
    cog = make_autosummary()
    interaction = FakeInteraction()
    interaction.guild_id = 123
    interaction.permissions = SimpleNamespace(manage_guild=False)
    policy = SimpleNamespace(
        topic_id="shorts", effective_at=datetime(2026, 9, 22, 12, tzinfo=UTC)
    )
    setter = AsyncMock(return_value=(True, policy))
    monkeypatch.setattr(autosummary_module, "set_auto_summary_topic_policy", setter)

    await Autosummary.addignoredtopic.callback(cog, interaction, "shorts")

    setter.assert_awaited_once_with("shorts", ignored=True)
    assert interaction.response.sent == (
        "Topic `shorts` will be ignored for videos published after <t:1790078400:F>."
    )
    assert interaction.response.sent_kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_removeignoredtopic_is_idempotent_for_an_already_allowed_topic(
    monkeypatch,
):
    cog = make_autosummary()
    interaction = FakeInteraction()
    monkeypatch.setattr(
        autosummary_module,
        "set_auto_summary_topic_policy",
        AsyncMock(return_value=(False, None)),
    )

    await Autosummary.removeignoredtopic.callback(cog, interaction, "unlisted")

    assert (
        interaction.response.sent
        == "Topic `unlisted` is already allowed for new videos."
    )
    assert interaction.response.sent_kwargs == {"ephemeral": True}


@pytest.mark.asyncio
async def test_autosummary_enqueue_sets_ready_and_delayed_attempt_times(monkeypatch):
    cog = make_autosummary()
    channel = FakeFollowedChannel("UC1", "Test")
    ready_at = datetime.now(UTC) - timedelta(hours=3)
    ready_video = HolodexVideo("ready", "Ready", None, ready_at, "Test")

    await cog._enqueue_new_video(ready_video, channel)

    ready_pending = FakeSummaryJob.records[-1]
    assert ready_pending.video_id == "ready"
    assert ready_pending.next_attempt_at <= datetime.now(UTC)

    delayed_at = datetime.now(UTC) - timedelta(minutes=30)
    delayed_video = HolodexVideo(
        "delayed", "Delayed", None, delayed_at, "Test", duration=1800
    )

    await cog._enqueue_new_video(delayed_video, channel)

    delayed_pending = FakeSummaryJob.records[-1]
    assert abs(
        delayed_pending.next_attempt_at
        - (delayed_at + timedelta(seconds=1800, hours=2))
    ) < timedelta(seconds=1)


@pytest.mark.asyncio
async def test_autosummary_processes_success_and_posts_summary(monkeypatch):
    target = FakeChannelTarget()
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
        get_channel=lambda channel_id: target if channel_id == 123 else None,
    )
    cog = make_autosummary(bot)
    item = FakeSummaryJob(
        video_id=VIDEO_ID,
        channel_id="UC1",
        channel_name="Test",
        title="A stream",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
    )
    cog.settings = RuntimeConfig(auto_summary_channel_id=123)
    monkeypatch.setattr(
        cog.summary_service,
        "summarize",
        AsyncMock(
            return_value=[
                {"topic_name": "Intro", "detail": "Details"},
                {"topic_name": "Outro", "detail": "More details"},
            ]
        ),
    )

    await cog._process_pending_video(item)

    assert item.status is JobStatus.COMPLETED
    assert target.messages == [
        "**Video:** A stream\n**Channel:** Test\n\n**Intro**\nDetails\n\n**Outro**\nMore details"
    ]


@pytest.mark.asyncio
async def test_autosummary_retries_then_marks_video_failed_and_posts_failure(
    monkeypatch,
):
    target = FakeChannelTarget()
    bot = SimpleNamespace(
        fetch_channel=None,
        message_rate_limiter=None,
        settings=None,
        video_work=VideoWorkCoordinator(),
        notebook_client=object(),
        get_channel=lambda _: target,
    )
    cog = make_autosummary(bot)
    cog.settings = RuntimeConfig(auto_summary_channel_id=123)

    retry_item = FakeSummaryJob(
        video_id="retry",
        channel_id="UC1",
        channel_name="Test",
        title="Retry me",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
        retry_count=0,
    )
    await cog._handle_source_error(retry_item)

    assert retry_item.status is JobStatus.QUEUED
    assert retry_item.retry_count == 1
    assert retry_item.next_attempt_at > datetime.now(UTC)
    assert target.messages == []

    failed_item = FakeSummaryJob(
        video_id="failed",
        channel_id="UC1",
        channel_name="Test",
        title="Failed video",
        available_at=datetime.now(UTC),
        next_attempt_at=datetime.now(UTC),
        retry_count=4,
    )
    await cog._handle_source_error(failed_item)

    assert failed_item.status is JobStatus.READY_TO_DELIVER
    assert target.messages == []
    await cog._deliver_for_job(failed_item)
    assert failed_item.status is JobStatus.FAILED
    assert len(target.messages) == 1
    assert "Failed to generate a summary after 5 attempts" in target.messages[0]


@pytest.mark.asyncio
async def test_autosummary_channel_lookup_skips_missing_or_invalid_configuration(
    monkeypatch,
):
    cog = make_autosummary(
        SimpleNamespace(
            fetch_channel=None,
            message_rate_limiter=None,
            settings=None,
            video_work=VideoWorkCoordinator(),
            notebook_client=object(),
            get_channel=lambda _: None,
        )
    )

    monkeypatch.delenv("AUTO_SUMMARY_CHANNEL_ID", raising=False)
    assert await cog._get_auto_summary_channel() is None

    monkeypatch.setenv("AUTO_SUMMARY_CHANNEL_ID", "not-an-integer")
    assert await cog._get_auto_summary_channel() is None

    cog.settings = RuntimeConfig(auto_summary_channel_id=123)
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
async def test_startup_requires_all_credentials(monkeypatch, caplog, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_module, "configure_logging", lambda _: None)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("HOLODEX_API_KEY", raising=False)
    mongo_client = Mock(side_effect=AssertionError("Startup reached MongoDB"))
    monkeypatch.setattr(main_module, "AsyncMongoClient", mongo_client)

    with pytest.raises(SystemExit) as missing_token:
        await main_module.async_main()

    assert missing_token.value.code == 1

    monkeypatch.setenv("DISCORD_TOKEN", "token")

    with pytest.raises(SystemExit) as missing_uri:
        await main_module.async_main()

    assert missing_uri.value.code == 1

    monkeypatch.setenv("MONGODB_URI", "mongodb://localhost")

    with pytest.raises(SystemExit) as missing_holodex_key:
        await main_module.async_main()

    assert missing_holodex_key.value.code == 1
    assert "HOLODEX_API_KEY is required" in caplog.text
    mongo_client.assert_not_called()


@pytest.mark.asyncio
async def test_startup_rejects_invalid_injected_holodex_key(monkeypatch, caplog):
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_module, "configure_logging", lambda _: None)
    mongo_client = Mock(side_effect=AssertionError("Startup reached MongoDB"))
    monkeypatch.setattr(main_module, "AsyncMongoClient", mongo_client)

    settings = Settings(
        discord_token="token",
        mongodb_uri="mongodb://localhost",
        holodex_api_key="private-key\ninvalid",
    )

    with pytest.raises(SystemExit) as error:
        await main_module.async_main(settings)

    assert error.value.code == 1
    assert "HOLODEX_API_KEY" in caplog.text
    assert "private-key" not in caplog.text
    mongo_client.assert_not_called()


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
            Settings(
                discord_token="token",
                mongodb_uri="mongodb://localhost",
                holodex_api_key="test-holodex-key",
            )
        )

    assert mongo_client is not None
    assert mongo_client.uri == "mongodb://localhost"
    assert mongo_client.kwargs == {"tz_aware": True}
    assert (
        main_module.AutoSummaryTopicPolicy
        in (main_module.init_beanie.await_args.kwargs["document_models"])
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bot_error", [None, RuntimeError("Discord failed")])
async def test_startup_wires_and_closes_external_clients(monkeypatch, bot_error):
    events = []
    database = object()

    class FakeMongoClient:
        close = AsyncMock()

        def __getitem__(self, name):
            return database

    mongo_client = FakeMongoClient()

    async def initialize(**kwargs):
        assert kwargs["database"] is database
        assert not kwargs.get("allow_index_dropping", False)
        events.append("initialize")

    @asynccontextmanager
    async def notebook_session(*, keepalive=None):
        assert keepalive == 600
        events.append("notebook")
        yield object()

    bot = SimpleNamespace(start=AsyncMock(side_effect=bot_error), close=AsyncMock())
    make_bot = Mock(return_value=bot)
    monkeypatch.setattr(main_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(main_module, "configure_logging", lambda _: None)
    monkeypatch.setattr(
        main_module, "AsyncMongoClient", lambda *args, **kwargs: mongo_client
    )
    monkeypatch.setattr(main_module, "init_beanie", initialize)
    monkeypatch.setattr(
        main_module, "NotebookLMClient", SimpleNamespace(from_storage=notebook_session)
    )
    monkeypatch.setattr(main_module, "Bot", make_bot)
    settings = Settings(
        discord_token="token",
        mongodb_uri="mongodb://localhost",
        holodex_api_key="test-holodex-key",
    )

    if bot_error is None:
        await main_module.async_main(settings)
    else:
        with pytest.raises(RuntimeError, match="Discord failed"):
            await main_module.async_main(settings)

    assert events == ["initialize", "notebook"]
    holodex = make_bot.call_args.kwargs["holodex"]
    assert holodex._client.headers["X-APIKEY"] == settings.holodex_api_key
    assert holodex._client.is_closed
    bot.start.assert_awaited_once_with("token")
    bot.close.assert_awaited_once()
    mongo_client.close.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("topics", [[], ["Art", "shorts"]])
async def test_listignoredtopics_uses_current_ignored_topics(monkeypatch, topics):
    cog = make_autosummary()
    interaction = FakeInteraction()
    get_topics = AsyncMock(return_value=topics)
    monkeypatch.setattr(
        autosummary_module, "get_ignored_auto_summary_topics", get_topics
    )

    await Autosummary.listignoredtopics.callback(cog, interaction)

    get_topics.assert_awaited_once_with()
    assert interaction.response.sent == (
        "Ignored auto-summary topics:\n- `Art`\n- `shorts`"
        if topics
        else "No topics are ignored for automatic summaries."
    )
    assert interaction.response.sent_kwargs == {"ephemeral": True}
