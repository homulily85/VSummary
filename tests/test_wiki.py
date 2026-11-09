"""Tests for evidence-backed YouTube name corrections."""

from datetime import UTC, date, datetime

import pytest

from vsummary.model.wiki import Validity
from vsummary.settings import RuntimeConfig, SettingsError
from vsummary.util.video_metadata import VideoMetadata
from vsummary.util.wiki import Mention, correction_table, normalize_name


def test_temporal_validity_requires_positive_historical_support():
    period = Validity(start=date(2020, 1, 1), end=date(2023, 1, 1))
    assert period.covers(date(2021, 1, 1))
    assert not period.covers(date(2023, 1, 1))
    assert not period.covers(None)
    assert not Validity().covers(date(2021, 1, 1))


def test_config_is_opt_in():
    assert not RuntimeConfig().wiki_enabled
    assert RuntimeConfig.from_mapping({"wiki": {"enabled": True}}).wiki_enabled


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("channels", []),
        ("official_hosts", []),
        ("max_queries", 3),
        ("research_seconds", 120),
        ("max_sources", 2),
    ],
)
def test_wiki_settings_reject_fixed_defaults(key, value):
    with pytest.raises(
        SettingsError, match=f"Unknown configuration option: wiki.{key}"
    ):
        RuntimeConfig.from_mapping({"wiki": {key: value}})


def test_correction_source_contains_only_accepted_data():
    accepted = Mention(
        original="Bee",
        quote="I met Bee at the event.",
        kind="person",
        canonical="Bé",
        evidence_ids=["source"],
    )
    unknown = Mention(original="Unknown", quote="I met Unknown.", kind="person")
    table = correction_table([accepted, unknown])
    assert "Bé" in table
    assert "Unknown" not in table
    assert "source" not in table


def test_normalize_name_preserves_non_latin_names():
    assert normalize_name("  Ｂé　雪  ") == "bé 雪"


def test_metadata_does_not_invent_video_date():
    metadata = VideoMetadata("Old recording", "Channel", published_on=date(2024, 3, 4))
    assert metadata.content_date == date(2024, 3, 4)
    assert VideoMetadata("Old recording", "Channel").content_date is None
    assert VideoMetadata(
        "Live",
        "Channel",
        published_on=date(2024, 3, 4),
        aired_at=datetime(2021, 2, 3, tzinfo=UTC),
    ).content_date == date(2021, 2, 3)


@pytest.mark.asyncio
async def test_wiki_failure_keeps_original_summary_prompt(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from vsummary.util import summarizer
    from vsummary.util.video import VideoRef

    ref = VideoRef(source="youtube", video_id="dQw4w9WgXcQ")
    notebook = summarizer.SummaryNotebook("summary", "video")
    client = SimpleNamespace(chat=SimpleNamespace(ask=AsyncMock()))
    service = SimpleNamespace(prepare=AsyncMock(side_effect=TimeoutError))
    monkeypatch.setattr(summarizer, "WikiVerificationService", lambda *a: service)

    result = await summarizer._prepare_wiki(client, ref, notebook, None)
    assert not result.corrections
    assert notebook.source_ids is None
    await summarizer._ask_summary(
        client,
        notebook,
        "Existing prompt",
        ref=ref,
        phase="topic_detail",
        topic_count=1,
    )
    client.chat.ask.assert_awaited_once_with("summary", "Existing prompt")


@pytest.mark.asyncio
async def test_wiki_resolves_old_name_and_blocks_conflicting_identities(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from vsummary.util import wiki

    historical = Validity(start=date(2020, 1, 1), end=date(2023, 1, 1))
    alias = SimpleNamespace(
        validity=historical,
        entity_id="person-b",
        status="verified",
        evidence_ids=["alias-proof"],
        channel_id="channel-a",
    )
    aliases = MagicMock()
    aliases.find.return_value.limit.return_value.to_list = AsyncMock(
        return_value=[alias]
    )
    facts = MagicMock()
    facts.find.return_value.limit.return_value.to_list = AsyncMock(
        return_value=[
            SimpleNamespace(
                validity=historical,
                value="Name B",
                status="verified",
                evidence_ids=["old-name"],
            ),
            SimpleNamespace(
                validity=Validity(start=date(2023, 1, 1)),
                value="New Name B",
                status="verified",
                evidence_ids=["new-name"],
            ),
        ]
    )
    monkeypatch.setattr(wiki, "WikiAlias", aliases)
    monkeypatch.setattr(wiki, "WikiFact", facts)

    mention = Mention(original="Bee", quote="I met Bee.", kind="person")
    result = await wiki.resolve_alias(mention, "channel-a", date(2021, 1, 1))
    assert result.entity_id == "person-b"
    assert result.canonical == "Name B"
    assert aliases.find.call_args.args[0]["status"][chr(36) + "in"] == [
        "verified",
        "conflicted",
    ]
    assert not (
        await wiki.resolve_alias(mention, "channel-a", date(2019, 1, 1))
    ).accepted

    aliases.find.return_value.limit.return_value.to_list.return_value.append(
        SimpleNamespace(
            validity=historical,
            entity_id="other-b",
            status="verified",
            channel_id="channel-a",
        )
    )
    assert not (
        await wiki.resolve_alias(mention, "channel-a", date(2021, 1, 1))
    ).accepted


def test_official_page_needs_dated_evidence_or_named_video_link():
    from vsummary.util.wiki_verification import NameProposal, confirm_name, official_url

    url = "https://www.youtube.com/@official"
    metadata = VideoMetadata(
        "Panel",
        "Channel A",
        description=f"Guest Alicia Kim {url}",
        links=(url,),
        published_on=date(2021, 5, 1),
    )
    mention = Mention(original="Alisia Kim", quote="Welcome Alisia Kim.", kind="person")
    proof = NameProposal(canonical="Alicia Kim", excerpt="Alicia Kim official profile")

    assert official_url(url, metadata) == url
    assert official_url("https://random.test/official", metadata) is None
    assert confirm_name(proof, mention, "Different text", url, metadata) is None
    assert confirm_name(proof, mention, proof.excerpt, url, None) is None
    period, alias = confirm_name(proof, mention, proof.excerpt, url, metadata)
    assert period.covers(date(2021, 5, 1))
    assert not period.covers(date(2021, 5, 2))
    assert not alias


@pytest.mark.asyncio
async def test_research_imports_only_authorized_sources_and_checks_task(monkeypatch):
    from types import SimpleNamespace as NS
    from unittest.mock import AsyncMock

    from notebooklm.types import ResearchStatus

    from vsummary.util import wiki_verification as verification
    from vsummary.util.video import VideoRef

    ref = VideoRef(source="youtube", video_id="dQw4w9WgXcQ")
    url = "https://www.youtube.com/@official"
    metadata = VideoMetadata(
        "Panel",
        "Channel A",
        channel_id="UC" + "a" * 22,
        description=f"Guest Alicia Kim {url}",
        links=(url,),
        published_on=date(2021, 5, 1),
    )
    mention = Mention(original="Alisia Kim", quote="Welcome Alisia Kim.", kind="person")
    good = NS(url=url)
    bad = NS(url="https://random.test/official")
    task = NS(task_id="unrelated", status=ResearchStatus.COMPLETED, sources=(bad, good))
    client = NS(
        research=NS(
            start=AsyncMock(return_value=NS(task_id="task")),
            wait_for_completion=AsyncMock(return_value=task),
            import_sources=AsyncMock(),
        ),
        sources=NS(
            list=AsyncMock(return_value=[NS(id="profile", url=url)]),
            wait_until_ready=AsyncMock(),
            get_fulltext=AsyncMock(
                return_value=NS(url=url, content="Alicia Kim official profile")
            ),
        ),
    )
    ask = AsyncMock(
        return_value=NS(
            answer='{"canonical":"Alicia Kim","excerpt":"Alicia Kim official profile"}'
        )
    )
    learned = AsyncMock(
        return_value=mention.model_copy(
            update={"canonical": "Alicia Kim", "evidence_ids": ["proof"]}
        )
    )
    monkeypatch.setattr(verification, "learn_name", learned)
    service = verification.WikiVerificationService(client, ask, AsyncMock())

    assert not (
        await service.verify_name("check", ref, metadata, mention, "query")
    ).accepted
    client.research.import_sources.assert_not_awaited()

    task.task_id = "task"
    assert (
        await service.verify_name("check", ref, metadata, mention, "query")
    ).accepted
    client.research.import_sources.assert_awaited_once_with("check", "task", [good])
    assert ask.call_args.kwargs["source_ids"] == ["profile"]
    assert learned.call_args.kwargs["attested_alias"] is False


@pytest.mark.asyncio
async def test_wiki_reuses_complete_metadata_hint(monkeypatch):
    from unittest.mock import AsyncMock

    from vsummary.util import wiki_verification as verification
    from vsummary.util.video import VideoRef

    ref = VideoRef(source="youtube", video_id="dQw4w9WgXcQ")
    metadata = VideoMetadata("Panel", "Channel", published_on=date(2021, 5, 1))
    extractor = AsyncMock()
    monkeypatch.setattr(verification, "get_video_metadata", extractor)
    assert await verification.load_wiki_metadata(ref, metadata) is metadata
    extractor.assert_not_awaited()
