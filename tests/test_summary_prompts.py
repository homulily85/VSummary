import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from notebooklm import ChatError

from vsummary.model.video import Topic
from vsummary.util import summarizer
from vsummary.util.summarizer import PermanentSummaryError
from vsummary.util.video import VideoRef

REF = VideoRef(source="youtube", video_id="dQw4w9WgXcQ")


@pytest.mark.parametrize(("text", "index"), [("a", 0), ("🎵", 500), ("語", 999)])
def test_detail_prompt_stays_small_and_names_only_the_selected_segment(text, index):
    topics = [Topic(name=f"Segment {i:04}: " + text * 86) for i in range(1000)]
    prompt = summarizer._topic_prompt(topics, index, output_language="Japanese")

    assert len(prompt.encode("utf-8")) <= 4000
    assert topics[index].name in prompt
    assert (topics[index - 1].name if index else "the start of the video") in prompt
    assert (topics[index + 1].name if index < 999 else "the end of the video") in prompt
    assert topics[(index + 100) % 1000].name not in prompt
    assert "in Japanese" in prompt
    assert "450 words" in prompt


def test_detail_prompt_ends_with_instruction_to_omit_follow_up_questions():
    prompt = summarizer._topic_prompt([Topic(name="Discussion")], 0)

    assert prompt.endswith(
        "End immediately after the final sentence of the summary. "
        "Do not append follow-up questions, suggested questions, "
        "or invitations to continue the discussion."
    )


def test_topic_prompt_groups_continuous_discussions_in_chronological_order():
    prompt = summarizer._topic_list_prompt(output_language="Japanese")

    assert len(prompt.encode("utf-8")) <= 4000
    assert "continuous discussion with one shared focus" in prompt
    assert (
        "Keep directly related explanations, examples, causes, consequences" in prompt
    )
    assert "Split only when the focus shifts to an independent subject" in prompt
    assert "When the boundary is unclear, keep adjacent material together" in prompt
    assert "A → B → A" in prompt
    assert "brief standalone announcements" in prompt
    assert "Exclude filler" in prompt
    assert "chronological order" in prompt
    assert "Do not target a fixed topic count" in prompt
    assert "in Japanese" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt", ["a" * 4000, "🎵" * 1000], ids=["ascii-limit", "emoji-limit"]
)
async def test_chat_question_budget_accepts_boundary_values(prompt):
    response = SimpleNamespace(answer="done")
    client = SimpleNamespace(
        settings=SimpleNamespace(
            get_usage=AsyncMock(return_value=SimpleNamespace(enabled=False))
        ),
        chat=SimpleNamespace(ask=AsyncMock(return_value=response)),
    )

    assert (
        await summarizer._ask_notebook(
            client,
            "notebook",
            prompt,
            phase="topic_extraction",
            ref=REF,
            topic_count=10,
        )
        is response
    )
    client.chat.ask.assert_awaited_once_with("notebook", prompt)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt", ["a" * 4001, "🎵" * 1001], ids=["ascii-oversize", "emoji-oversize"]
)
async def test_chat_question_budget_blocks_oversized_requests_before_dispatch(prompt):
    client = SimpleNamespace(
        settings=SimpleNamespace(
            get_usage=AsyncMock(return_value=SimpleNamespace(enabled=False))
        ),
        chat=SimpleNamespace(ask=AsyncMock()),
    )
    with pytest.raises(PermanentSummaryError, match="4000"):
        await summarizer._ask_notebook(
            client,
            "notebook",
            prompt,
            phase="topic_extraction",
            ref=REF,
            topic_count=10,
        )
    client.chat.ask.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_metrics_record_sizes_and_stage_without_prompt_content(caplog):
    caplog.set_level(logging.INFO, logger=summarizer.__name__)
    prompt = "private content 語🎵"
    client = SimpleNamespace(
        settings=SimpleNamespace(
            get_usage=AsyncMock(return_value=SimpleNamespace(enabled=False))
        ),
        chat=SimpleNamespace(ask=AsyncMock()),
    )
    await summarizer._ask_notebook(
        client, "notebook", prompt, phase="topic_extraction", ref=REF, topic_count=17
    )

    context = next(
        record.context
        for record in caplog.records
        if getattr(record, "context", {}).get("phase") == "topic_extraction"
    )
    assert context["prompt_characters"] == len(prompt)
    assert context["prompt_utf8_bytes"] == len(prompt.encode("utf-8"))
    assert context["topic_count"] == 17
    assert context["video_id"] == REF.video_id
    assert "private content" not in caplog.text
    assert prompt not in str(context)


@pytest.mark.asyncio
async def test_status_3_is_permanent_without_replaying_the_request():
    error = ChatError("Chat request was rejected by the server (status 3). Shorten it.")
    client = SimpleNamespace(
        settings=SimpleNamespace(
            get_usage=AsyncMock(return_value=SimpleNamespace(enabled=False))
        ),
        chat=SimpleNamespace(ask=AsyncMock(side_effect=error)),
    )
    with pytest.raises(PermanentSummaryError, match="status 3") as caught:
        await summarizer._ask_notebook(
            client,
            "notebook",
            "Review the previous answer.",
            phase="topic_extraction",
            ref=REF,
            topic_count=17,
        )
    assert caught.value.__cause__ is error
    client.chat.ask.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [
        "Chat request was rejected by the server (status 13).",
        "Chat request was rejected by the server (status 30).",
        "Chat request was rate limited or rejected by the API.",
    ],
)
async def test_other_chat_failures_keep_existing_classification(message):
    error = ChatError(message)
    client = SimpleNamespace(
        settings=SimpleNamespace(
            get_usage=AsyncMock(return_value=SimpleNamespace(enabled=False))
        ),
        chat=SimpleNamespace(ask=AsyncMock(side_effect=error)),
    )
    with pytest.raises(ChatError) as caught:
        await summarizer._ask_notebook(
            client,
            "notebook",
            "Question",
            phase="topic_detail",
            ref=REF,
            topic_count=17,
        )
    assert caught.value is error
