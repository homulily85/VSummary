import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from notebooklm import SourceAddError as NotebookLM_SourceAddError

from vsummary.model.video import Topic
from vsummary.util import summarizer as summarizer_module
from vsummary.util.video import VideoRef

REF = VideoRef(source="youtube", id="dQw4w9WgXcQ")


class FakeVideo:
    """Stand-in for the beanie Video document (no DB initialization needed)."""

    def __init__(self, source="youtube", video_id="vid", topics=None):
        self.source = source
        self.video_id = video_id
        self.topics = topics or []
        self.save = AsyncMock()


class FakeVideoClass:
    """Fake Video model class patched in place of the real beanie model."""

    source = MagicMock()
    video_id = MagicMock()
    find_one = AsyncMock()

    def __init__(self, find_result):
        self.find_one = AsyncMock(return_value=find_result)

    def __call__(self, *args, **kwargs):
        return FakeVideo(*args, **kwargs)


def patch_video(mocker, find_result):
    """Patch ``vsummary.util.summarizer.Video`` with a controllable fake."""
    fake = FakeVideoClass(find_result)
    mocker.patch.object(summarizer_module, "Video", fake)
    return fake


class FakeNotebook:
    def __init__(self, notebook_id="nb-42"):
        self.id = notebook_id


class FakeNotebooks:
    def __init__(self):
        self.create = AsyncMock(return_value=FakeNotebook())
        self.delete = AsyncMock()
        self.get = AsyncMock(return_value=FakeNotebook())


class FakeSources:
    def __init__(self, raise_on_add=False):
        self.add_url = AsyncMock()
        if raise_on_add:
            self.add_url = AsyncMock(
                side_effect=NotebookLM_SourceAddError(url="http://example.com")
            )


class FakeChat:
    def __init__(self, answer=""):
        self.ask = AsyncMock(return_value=type("AskResult", (), {"answer": answer})())


class FakeClient:
    def __init__(self, answer=""):
        self.notebooks = FakeNotebooks()
        self.sources = FakeSources()
        self.chat = FakeChat(answer)


class TestGetTopicList:
    async def test_returns_cached_topics_when_present(self, mocker):
        topics = [Topic(name="alpha"), Topic(name="beta")]
        video = FakeVideo(topics=topics)
        client = FakeClient()
        patch_video(mocker, video)

        result = await summarizer_module.get_topic_list(client, REF)
        assert result == topics
        # Cached path should not create a notebook nor save.
        assert not client.notebooks.create.await_count
        assert not client.sources.add_url.await_count
        assert not video.save.await_count

    async def test_creates_and_saves_new_video(self, mocker):
        client = FakeClient(answer=json.dumps([{"name": "alpha"}]))
        patch_video(mocker, None)

        await summarizer_module.get_topic_list(client, REF)
        assert client.notebooks.create.await_count == 1
        assert client.sources.add_url.await_count == 1
        assert client.notebooks.delete.await_count == 1

    async def test_deletes_notebook_on_source_add_error(self, mocker):
        client = FakeClient()
        client.sources.add_url = AsyncMock(
            side_effect=NotebookLM_SourceAddError(url="http://example.com")
        )
        patch_video(mocker, None)

        with pytest.raises(NotebookLM_SourceAddError):
            await summarizer_module.get_topic_list(client, REF)
        assert client.notebooks.create.await_count == 1
        assert client.notebooks.delete.await_count == 1

    async def test_updates_existing_video_in_place(self, mocker):
        video = FakeVideo(topics=[])
        client = FakeClient(answer=json.dumps([{"name": "alpha"}]))
        patch_video(mocker, video)

        await summarizer_module.get_topic_list(client, REF)
        assert video.save.await_count == 1
        assert not hasattr(video, "notebook_id")
        assert [t.name for t in video.topics] == ["alpha"]


class TestGetTopicDetails:
    async def test_returns_details_from_db_when_present(self, mocker):
        video = FakeVideo(topics=[Topic(name="alpha", detail="cached")])
        client = FakeClient()
        patch_video(mocker, video)

        result = await summarizer_module.get_topic_details(client, REF, 0)
        assert result == {"topic_name": "alpha", "detail": "cached"}
        assert not client.chat.ask.await_count
        assert not video.save.await_count

    async def test_fetches_details_when_missing(self, mocker):
        video = FakeVideo(topics=[Topic(name="alpha", detail=None)])
        client = FakeClient(answer="fresh detail")
        patch_video(mocker, video)

        result = await summarizer_module.get_topic_details(client, REF, 0)
        assert result == {"topic_name": "alpha", "detail": "fresh detail"}
        assert not client.notebooks.get.await_count
        assert client.notebooks.create.await_count == 1
        assert client.notebooks.delete.await_count == 1
        assert video.save.await_count == 1
        assert video.topics[0].detail == "fresh detail"

    @pytest.mark.parametrize("index", [-1, 5])
    async def test_invalid_index_raises(self, index, mocker):
        video = FakeVideo(topics=[Topic(name="alpha")])
        client = FakeClient()
        patch_video(mocker, video)

        with pytest.raises(IndexError):
            await summarizer_module.get_topic_details(client, REF, index)


class TestGetTopicDetailsAll:
    async def test_batch_fetches_and_saves_once(self, mocker):
        video = FakeVideo(
            topics=[Topic(name="a", detail="d1"), Topic(name="b", detail=None)]
        )
        client = FakeClient(answer="fetched")
        patch_video(mocker, video)

        results = await summarizer_module.get_topic_details_all(client, REF)
        assert results == [
            {"topic_name": "a", "detail": "d1"},
            {"topic_name": "b", "detail": "fetched"},
        ]
        # Single notebook created and deleted; single batched save.
        assert not client.notebooks.get.await_count
        assert client.notebooks.create.await_count == 1
        assert client.notebooks.delete.await_count == 1
        assert video.save.await_count == 1

    async def test_no_save_when_all_cached(self, mocker):
        video = FakeVideo(topics=[Topic(name="a", detail="d1")])
        client = FakeClient()
        patch_video(mocker, video)

        results = await summarizer_module.get_topic_details_all(client, REF)
        assert results == [{"topic_name": "a", "detail": "d1"}]
        assert not client.notebooks.get.await_count
        assert not video.save.await_count


class TestTopicOverlapRegression:
    """The detail prompts must request non-overlapping topic boundaries."""

    async def test_detail_prompt_names_prev_and_next_topics(self, mocker):
        video = FakeVideo(
            topics=[Topic(name="before"), Topic(name="target"), Topic(name="after")]
        )
        client = FakeClient(answer="fresh detail")
        patch_video(mocker, video)

        await summarizer_module.get_topic_details(client, REF, 1)

        prompt = client.chat.ask.await_args.args[1]
        prompt_lower = prompt.lower()
        assert "before" in prompt_lower
        assert "target" in prompt_lower
        assert "after" in prompt_lower

    async def test_detail_all_prompt_names_next_topic(self, mocker):
        video = FakeVideo(topics=[Topic(name="first"), Topic(name="second")])
        client = FakeClient(answer="fetched")
        patch_video(mocker, video)

        await summarizer_module.get_topic_details_all(client, REF)

        prompt = client.chat.ask.await_args.args[1]
        prompt_lower = prompt.lower()
        assert "first" in prompt_lower
        assert "second" in prompt_lower


class TestNotebookLifecycle:
    """Once cached, topic queries must not need a stored notebook_id.

    Every call that actually queries NotebookLM must create a fresh notebook
    and delete it when finished, so no notebook handle is ever persisted.
    """

    async def test_topic_list_cached_skips_notebook_creation(self, mocker):
        topics = [Topic(name="alpha")]
        video = FakeVideo(topics=topics)
        client = FakeClient()
        patch_video(mocker, video)

        await summarizer_module.get_topic_list(client, REF)
        assert not client.notebooks.create.await_count
        assert not client.notebooks.delete.await_count

    async def test_topic_list_miss_deletes_created_notebook(self, mocker):
        client = FakeClient(answer=json.dumps([{"name": "alpha"}]))
        patch_video(mocker, None)

        result = await summarizer_module.get_topic_list(client, REF)
        assert client.notebooks.create.await_count == 1
        assert client.notebooks.delete.await_count == 1
        assert [t.name for t in result] == ["alpha"]

    async def test_topic_details_cached_skips_notebook_creation(self, mocker):
        video = FakeVideo(topics=[Topic(name="alpha", detail="cached")])
        client = FakeClient()
        patch_video(mocker, video)

        result = await summarizer_module.get_topic_details(client, REF, 0)
        assert result == {"topic_name": "alpha", "detail": "cached"}
        assert not client.notebooks.create.await_count
        assert not client.notebooks.delete.await_count

    async def test_topic_details_miss_deletes_created_notebook(self, mocker):
        video = FakeVideo(topics=[Topic(name="alpha", detail=None)])
        client = FakeClient(answer="fresh detail")
        patch_video(mocker, video)

        result = await summarizer_module.get_topic_details(client, REF, 0)
        assert result == {"topic_name": "alpha", "detail": "fresh detail"}
        assert client.notebooks.create.await_count == 1
        assert client.notebooks.delete.await_count == 1
        assert (
            client.notebooks.create.return_value.id
            == client.notebooks.delete.await_args.args[0]
        )

    async def test_topic_details_index_error_deletes_created_notebook(self, mocker):
        # No cached topics means topic extraction created a temporary notebook;
        # an invalid index afterward must still clean it up.
        client = FakeClient(answer=json.dumps([{"name": "alpha"}]))
        patch_video(mocker, None)

        with pytest.raises(IndexError):
            await summarizer_module.get_topic_details(client, REF, 5)
        assert client.notebooks.create.await_count == 1
        assert client.notebooks.delete.await_count == 1

    async def test_topic_details_does_not_use_notebooks_get(self, mocker):
        video = FakeVideo(topics=[Topic(name="alpha", detail=None)])
        client = FakeClient(answer="fresh detail")
        patch_video(mocker, video)

        await summarizer_module.get_topic_details(client, REF, 0)
        assert not client.notebooks.get.await_count

    async def test_topic_details_all_reuses_created_notebook(self, mocker):
        video = FakeVideo(
            topics=[Topic(name="a", detail=None), Topic(name="b", detail=None)]
        )
        client = FakeClient(answer="fetched")
        patch_video(mocker, video)

        results = await summarizer_module.get_topic_details_all(client, REF)
        assert results == [
            {"topic_name": "a", "detail": "fetched"},
            {"topic_name": "b", "detail": "fetched"},
        ]
        # One notebook supports the whole query, then is deleted.
        assert client.notebooks.create.await_count == 1
        assert client.notebooks.delete.await_count == 1
        assert not client.notebooks.get.await_count

    async def test_topic_details_all_cached_skips_notebook_creation(self, mocker):
        video = FakeVideo(topics=[Topic(name="a", detail="d1")])
        client = FakeClient()
        patch_video(mocker, video)

        results = await summarizer_module.get_topic_details_all(client, REF)
        assert results == [{"topic_name": "a", "detail": "d1"}]
        assert not client.notebooks.create.await_count
        assert not client.notebooks.delete.await_count
