import logging
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from notebooklm import ChatError, ServerError
from notebooklm.types import (
    UsageSummary,
    UsageSummaryStatus,
    UsageWindow,
    UsageWindowKind,
)

from vsummary.model.video import Topic
from vsummary.util import summarizer
from vsummary.util.summarizer import PermanentSummaryError
from vsummary.util.video import VideoRef

REF = VideoRef(source="youtube", video_id="dQw4w9WgXcQ")


@pytest.fixture
def client():
    return SimpleNamespace(
        chat=SimpleNamespace(ask=AsyncMock()),
        settings=SimpleNamespace(
            get_usage=AsyncMock(return_value=UsageSummary(UsageSummaryStatus.DISABLED))
        ),
    )


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


def test_review_uses_the_complete_previous_answer_without_resending_the_inventory():
    topics = [Topic(name=f"Segment {i:04}: " + "🎵" * 86) for i in range(1000)]
    prompt = summarizer._topic_list_prompt(topics, output_language="Japanese")

    assert len(prompt.encode("utf-8")) <= 4000
    assert "complete candidate topic list in your previous answer" in prompt
    assert all(topic.name not in prompt for topic in topics)
    assert "Retain supported discussions" in prompt
    assert "Do not target a fixed topic count" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt", ["a" * 4000, "🎵" * 1000], ids=["ascii-limit", "emoji-limit"]
)
async def test_chat_question_budget_accepts_boundary_values(prompt, client):
    response = SimpleNamespace(answer="done")
    client.chat.ask.return_value = response

    assert (
        await summarizer._ask_notebook(
            client, "notebook", prompt, phase="topic_review", ref=REF, topic_count=10
        )
        is response
    )
    client.chat.ask.assert_awaited_once_with("notebook", prompt)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt", ["a" * 4001, "🎵" * 1001], ids=["ascii-oversize", "emoji-oversize"]
)
async def test_chat_question_budget_blocks_oversized_requests_before_dispatch(
    prompt, client
):
    with pytest.raises(PermanentSummaryError, match="4000"):
        await summarizer._ask_notebook(
            client, "notebook", prompt, phase="topic_review", ref=REF, topic_count=10
        )
    client.chat.ask.assert_not_awaited()
    client.settings.get_usage.assert_not_awaited()


@pytest.mark.asyncio
async def test_chat_metrics_record_sizes_and_stage_without_prompt_content(
    caplog, client
):
    caplog.set_level(logging.INFO, logger=summarizer.__name__)
    prompt = "private content 語🎵"
    await summarizer._ask_notebook(
        client, "notebook", prompt, phase="topic_review", ref=REF, topic_count=17
    )

    context = next(
        record.context
        for record in caplog.records
        if getattr(record, "context", {}).get("phase") == "topic_review"
    )
    assert context["prompt_characters"] == len(prompt)
    assert context["prompt_utf8_bytes"] == len(prompt.encode("utf-8"))
    assert context["topic_count"] == 17
    assert context["video_id"] == REF.video_id
    assert "private content" not in caplog.text
    assert prompt not in str(context)


@pytest.mark.asyncio
async def test_status_3_is_permanent_without_replaying_the_request(client):
    error = ChatError("Chat request was rejected by the server (status 3). Shorten it.")
    client.chat.ask.side_effect = error
    with pytest.raises(PermanentSummaryError, match="status 3") as caught:
        await summarizer._ask_notebook(
            client,
            "notebook",
            "Review the previous answer.",
            phase="topic_review",
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
async def test_other_chat_failures_keep_existing_classification(message, client):
    error = ChatError(message)
    client.chat.ask.side_effect = error
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("five_hour_used", "weekly_used", "five_hour_reset", "expected_reset"),
    [
        (100, 90, 2, 2),
        (90, 100, 2, 48),
        (100, 100, 2, 48),
        (100, 90, 0, None),
        (99.9, 90, 2, None),
    ],
    ids=["five-hour", "weekly", "both", "at-reset", "below-limit"],
)
async def test_chat_checks_both_quota_windows_before_dispatch(
    client, monkeypatch, five_hour_used, weekly_used, five_hour_reset, expected_reset
):
    now = datetime(2026, 11, 11, tzinfo=UTC)
    monkeypatch.setattr(
        summarizer, "datetime", SimpleNamespace(now=lambda _: now), raising=False
    )
    client.settings.get_usage.return_value = UsageSummary(
        UsageSummaryStatus.READY,
        windows=(
            UsageWindow(
                UsageWindowKind.FIVE_HOUR,
                five_hour_used,
                100 - five_hour_used,
                now + timedelta(hours=five_hour_reset),
            ),
            UsageWindow(
                UsageWindowKind.WEEKLY,
                weekly_used,
                100 - weekly_used,
                now + timedelta(days=2),
            ),
        ),
    )
    options = {"phase": "topic_detail", "ref": REF, "topic_count": 1}

    if expected_reset is None:
        await summarizer._ask_notebook(client, "notebook", "Question", **options)
        client.chat.ask.assert_awaited_once()
    else:
        with pytest.raises(summarizer.SummaryServiceError, match="quota") as caught:
            await summarizer._ask_notebook(client, "notebook", "Question", **options)
        assert isinstance(caught.value, summarizer.NotebookLMQuotaExhausted)
        assert caught.value.resets_at == now + timedelta(hours=expected_reset)
        client.chat.ask.assert_not_awaited()
    client.settings.get_usage.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", [UsageSummaryStatus.DISABLED, UsageSummaryStatus.SKIPPED]
)
async def test_chat_proceeds_when_quota_meter_is_unavailable(client, status):
    client.settings.get_usage.return_value = UsageSummary(status)

    await summarizer._ask_notebook(
        client, "notebook", "Question", phase="topic_detail", ref=REF, topic_count=1
    )

    client.settings.get_usage.assert_awaited_once()
    client.chat.ask.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_proceeds_and_logs_when_quota_lookup_fails(client, caplog):
    client.settings.get_usage.side_effect = ServerError("Usage unavailable")

    await summarizer._ask_notebook(
        client, "notebook", "Question", phase="topic_detail", ref=REF, topic_count=1
    )

    client.settings.get_usage.assert_awaited_once()
    client.chat.ask.assert_awaited_once()
    assert any(
        record.levelno == logging.WARNING and "quota" in record.getMessage()
        for record in caplog.records
    )
