import pytest

from vsummary.cogs.summarizer.summarizer import TopicsView
from vsummary.model.video import Topic
from vsummary.util.discord import split_message
from vsummary.util.video import VideoRef


def test_split_message_respects_limit_and_preserves_content():
    text = "a" * 1999 + "\n" + "b" * 10

    chunks = split_message(text)

    assert chunks == ["a" * 1999, "b" * 10]
    assert all(0 < len(chunk) <= 2000 for chunk in chunks)


def test_split_message_handles_long_lines_and_exact_boundary():
    text = "x" * 2000 + "y"

    assert split_message(text) == ["x" * 2000, "y"]


def test_split_message_never_emits_empty_chunks():
    assert split_message("\n" * 5 + "hello") == ["hello"]
    assert split_message("") == []


def test_topics_view_rejects_an_empty_or_overlong_discord_select_label():
    with pytest.raises(ValueError, match="at least one"):
        TopicsView(object(), VideoRef(source="youtube", id="dQw4w9WgXcQ"), [])

    with pytest.raises(ValueError, match="100"):
        TopicsView(
            object(),
            VideoRef(source="youtube", id="dQw4w9WgXcQ"),
            [Topic(name="x" * 101)],
        )
