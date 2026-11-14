"""Generate, validate, and cache NotebookLM topics and detailed summaries."""

import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence

import httpx
from notebooklm import ChatError, NotebookLMClient, SourceAddError
from notebooklm.types import AskResult, Notebook

from vsummary.logging import log_event
from vsummary.model.video import Topic, VideoSummary
from vsummary.services.notebook_quota import quota_guard
from vsummary.settings import DEFAULT_CONFIG, RuntimeConfig, runtime_config
from vsummary.util.generic_media import download_generic_audio
from vsummary.util.twitch import download_twitch_audio
from vsummary.util.video import VideoRef, build_video_url
from vsummary.util.x_space import download_x_space_audio

logger = logging.getLogger(__name__)
_SOURCE_UPLOAD_TIMEOUT_SECONDS = 600
# Conservative application budget, not a claimed Google API limit. UTF-8 bytes
# also bound character count and leave headroom for multilingual topic names.
_MAX_CHAT_QUESTION_BYTES = 4000
# Match numeric markers while preserving escaped brackets and numeric link labels.
_CITATION_MARKER = (
    r"(?<!\\)(?<!\[)\[[ \t]*[0-9]+(?:[ \t]*,[ \t]*[0-9]+)*[ \t]*\](?!\(|\])"
)
_CITATION_RUN = re.compile(
    rf"(?<![ \t])([ \t]*){_CITATION_MARKER}(?:[ \t]*{_CITATION_MARKER})*([ \t]*)"
)


class SummaryServiceError(Exception):
    """Base class for errors classified by the summary workflow."""


class TransientSummaryError(SummaryServiceError):
    """Signal a generation failure eligible for retry."""


class PermanentSummaryError(SummaryServiceError):
    """Signal an invalid request or result that should end generation retries."""


class InvalidSummaryResponse(TransientSummaryError):
    """Signal malformed NotebookLM output eligible for an immediate retry."""


async def _ask_notebook(
    client: NotebookLMClient,
    notebook_id: str,
    prompt: str,
    *,
    phase: str,
    ref: VideoRef,
    topic_count: int,
) -> AskResult:
    """Send a bounded question in the notebook's current conversation.

    Log the question's size and phase without logging its text.

    Args:
        client: Authenticated NotebookLM client.
        notebook_id: ID of the notebook whose current conversation to use.
        prompt: Question text within the 4,000-byte UTF-8 safety budget.
        phase: Workflow label for logs and errors.
        ref: Source reference for request logging.
        topic_count: Number of topics to record in request metrics.

    Returns:
        NotebookLM answer and associated metadata.

    Raises:
        PermanentSummaryError: If the question exceeds the local byte budget or
            NotebookLM rejects it with status 3.
        notebooklm.NotebookLMError: If another NotebookLM request error propagates.
    """
    character_count = len(prompt)
    byte_count = len(prompt.encode("utf-8"))
    log_event(
        logger,
        logging.INFO,
        "NotebookLM %s request: %s characters, %s UTF-8 bytes, %s topics.",
        phase,
        character_count,
        byte_count,
        topic_count,
        source=ref.source,
        video_id=ref.video_id,
        notebook_id=notebook_id,
        phase=phase,
        prompt_characters=character_count,
        prompt_utf8_bytes=byte_count,
        topic_count=topic_count,
    )
    if byte_count > _MAX_CHAT_QUESTION_BYTES:
        raise PermanentSummaryError(
            f"NotebookLM {phase} prompt exceeds the {_MAX_CHAT_QUESTION_BYTES}-byte "
            f"UTF-8 safety limit ({byte_count} bytes). Shorten the prompt or "
            "summary.output_language."
        )
    try:
        # Omitted conversation_id continues the notebook's server-side thread.
        # The review must follow extraction without resending its large answer.
        async with quota_guard(client):
            return await client.chat.ask(notebook_id, prompt)
    except ChatError as exc:
        # notebooklm-py 0.8.0 exposes this rejection only in its message. Match
        # the entire status marker, not rate-limit details or status 13 / 30.
        if "(status 3)" in str(exc):
            raise PermanentSummaryError(
                f"NotebookLM rejected the {phase} request (status 3: "
                f"INVALID_ARGUMENT; {character_count} characters, {byte_count} "
                "UTF-8 bytes). The request must be changed before retrying."
            ) from exc
        raise


async def retry_invalid_summary_response[SummaryResult](
    operation: Callable[[], Awaitable[SummaryResult]],
    *,
    retries: int = DEFAULT_CONFIG.invalid_summary_retries,
) -> SummaryResult:
    """Retry malformed output immediately within the specified budget.

    Args:
        operation: Asynchronous callable to run afresh on each attempt.
        retries: Number of additional attempts after the first. Defaults to the
            configured default retry budget.

    Returns:
        Result from the first successful call.

    Raises:
        InvalidSummaryResponse: If the final attempt still returns malformed output.

    Other operation exceptions propagate immediately.
    """
    for _ in range(retries):
        try:
            return await operation()
        except InvalidSummaryResponse:
            pass
    return await operation()


class NotebookLMSummaryService:
    """Provide the workers with typed NotebookLM summaries and shared cache reuse."""

    def __init__(
        self, client: NotebookLMClient, *, config: RuntimeConfig | None = None
    ):
        """Bind the summary service to an authenticated client and runtime options.

        Args:
            client: NotebookLM client whose lifecycle is managed by the application.
            config: Output language and source preparation settings. Defaults to runtime
                settings.
        """
        self.client = client
        self.config = config

    async def summarize(
        self, ref: VideoRef, *, use_youtube_audio: bool = False
    ) -> list[Topic]:
        """Return detailed topics for a recording, reusing cached output when available.

        Args:
            ref: Media reference identifying the source and its cache entry.
            use_youtube_audio: Whether to upload YouTube audio instead of attaching the
                watch URL when a notebook is needed.

        Returns:
            Ordered topics containing names and generated details.

        Raises:
            TransientSummaryError: If source attachment, transport, or an OS operation
                fails, or a NotebookLM answer needs to be retried.
            PermanentSummaryError: If the request is invalid or generation produces
                unusable data.
            TwitchAudioError: If Twitch audio cannot be prepared.
            XSpaceAudioError: If Space replay audio cannot be prepared.
            GenericMediaError: If generic or fallback audio cannot be prepared.
            notebooklm.NotebookLMError: If an unclassified NotebookLM error propagates.
            pymongo.errors.PyMongoError: If a cache read or write fails.
        """
        try:
            details = await get_topic_details_all(
                self.client,
                ref,
                use_youtube_audio=use_youtube_audio,
                config=self.config,
            )
        except SourceAddError as exc:
            log_event(
                logger,
                logging.WARNING,
                "NotebookLM could not add source for %s.",
                ref.video_id,
                source=ref.source,
                video_id=ref.video_id,
                error_type=type(exc).__name__,
            )
            raise TransientSummaryError(str(exc)) from exc
        except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
            log_event(
                logger,
                logging.WARNING,
                "NotebookLM request failed for %s.",
                ref.video_id,
                source=ref.source,
                video_id=ref.video_id,
                error_type=type(exc).__name__,
            )
            raise TransientSummaryError(str(exc)) from exc
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            log_event(
                logger,
                logging.ERROR,
                "NotebookLM returned an invalid summary for %s.",
                ref.video_id,
                source=ref.source,
                video_id=ref.video_id,
                error_type=type(exc).__name__,
            )
            raise PermanentSummaryError(str(exc)) from exc
        return [
            Topic(name=item["topic_name"], detail=item["detail"]) for item in details
        ]


def _topic_boundaries(topics: list[Topic], topic_index: int) -> dict[str, str]:
    """Describe the neighbors of a topic for a detail prompt.

    Args:
        topics: Chronologically ordered topic list.
        topic_index: Valid zero-based index of the requested topic.

    Returns:
        Mapping with ``prev`` and ``next`` descriptions. Use the start or end of the
            video where a neighbor is absent.
    """
    return {
        "prev": topics[topic_index - 1].name
        if topic_index > 0
        else "the start of the video",
        "next": (
            topics[topic_index + 1].name
            if topic_index + 1 < len(topics)
            else "the end of the video"
        ),
    }


def _validated_topics(topics: Sequence[Topic]) -> list[Topic]:
    """Validate and trim topic names for caching and Discord display.

    Args:
        topics: Candidate topics. Their names are trimmed in place during validation.

    Returns:
        Ordered list containing the validated topic objects.

    Raises:
        InvalidSummaryResponse: If the list is empty or a name is blank or exceeds 100
            characters.
    """
    if not topics:
        raise InvalidSummaryResponse("NotebookLM returned an empty topic list")

    validated: list[Topic] = []
    for topic in topics:
        name = topic.name.strip() if isinstance(topic.name, str) else ""
        if not name:
            raise InvalidSummaryResponse("NotebookLM returned a blank topic name")
        if len(name) > 100:
            raise InvalidSummaryResponse(
                "NotebookLM returned a topic name longer than Discord allows"
            )
        topic.name = name
        validated.append(topic)
    return validated


def _parse_topics(answer: str) -> list[Topic]:
    """Decode a NotebookLM topic-list response.

    Args:
        answer: JSON text containing a list of objects with string ``name`` fields.

    Returns:
        Ordered topics with validated, trimmed names.

    Raises:
        InvalidSummaryResponse: If the JSON, list structure, or topic names are invalid.
    """
    try:
        payload = json.loads(answer)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidSummaryResponse(
            "NotebookLM did not return JSON topic data"
        ) from exc

    if not isinstance(payload, list):
        raise InvalidSummaryResponse("NotebookLM topic data must be a list")

    topics: list[Topic] = []
    for item in payload:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise InvalidSummaryResponse("NotebookLM topic data is missing a name")
        topics.append(Topic(name=item["name"]))
    return _validated_topics(topics)


def _clean_topic_detail(answer: str) -> str:
    """Strip numeric citation runs from generated detail text.

    Preserve Markdown links and adjust whitespace around each removed citation run.

    Args:
        answer: Detail text from NotebookLM.

    Returns:
        Detail text with citation runs removed.

    Raises:
        InvalidSummaryResponse: If removing citations leaves only whitespace.
    """

    def remove_citations(match: re.Match[str]) -> str:
        """Choose replacement spacing for one matched citation run.

        Args:
            match: Citation run with adjacent horizontal whitespace captured.

        Returns:
            Replacement whitespace or an empty string, depending on punctuation and line
                boundaries.
        """
        before, after = match.groups()
        start, end = match.span()
        if end == len(answer):
            return ""
        following = answer[end]
        if following in "\r\n":
            # Keep explicit Markdown hard breaks.
            return after if after.endswith("  ") else ""
        if start == 0 or answer[start - 1] in "\r\n":
            return before
        if following in ".,!?;:…)]}。，！？、；：）】" or answer[start - 1] in "([{":
            return ""
        return " " if before or after else ""

    detail, removed = _CITATION_RUN.subn(remove_citations, answer)
    if removed and not detail.strip():
        raise InvalidSummaryResponse(
            "NotebookLM returned only citations for topic detail"
        )
    return detail


async def _create_notebook_with_source(
    client: NotebookLMClient,
    ref: VideoRef,
    *,
    use_youtube_audio: bool = False,
) -> "Notebook":
    """Open a temporary notebook and attach the requested source.

    Twitch, X Spaces, and generic media use a temporary audio upload. On source
    attachment failure, attempt to delete the notebook before propagating the error.

    Args:
        client: Authenticated NotebookLM client.
        ref: Media reference used for the URL and source selection.
        use_youtube_audio: Whether to upload downloaded YouTube audio while retaining
            the original reference.

    Returns:
        Notebook with its source attached. The caller owns its deletion.

    Raises:
        UnsupportedVideoSource: If the reference cannot be turned into a source URL.
        notebooklm.NotebookLMError: If notebook creation or source attachment fails.
        TwitchAudioError: If Twitch VOD audio cannot be prepared.
        XSpaceAudioError: If Space replay audio cannot be prepared.
        GenericMediaError: If generic or fallback audio cannot be prepared.
        OSError: If a file operation fails.
    """
    video_link = build_video_url(ref)
    async with quota_guard(client):
        notebook = await client.notebooks.create(video_link)
        try:
            if ref.source == "twitch":
                async with download_twitch_audio(ref) as audio:
                    await client.sources.add_file(
                        notebook.id,
                        audio,
                        mime_type="audio/mp4",
                        wait=True,
                        wait_timeout=_SOURCE_UPLOAD_TIMEOUT_SECONDS,
                        title=f"Twitch VOD {ref.video_id}",
                    )
            elif ref.source == "x_space":
                async with download_x_space_audio(ref) as audio:
                    await client.sources.add_file(
                        notebook.id,
                        audio,
                        mime_type="audio/mp4",
                        wait=True,
                        wait_timeout=_SOURCE_UPLOAD_TIMEOUT_SECONDS,
                        title=f"X Space {ref.video_id}",
                    )
            elif ref.source == "yt_dlp" or (
                ref.source == "youtube" and use_youtube_audio
            ):
                async with download_generic_audio(ref) as audio:
                    await client.sources.add_file(
                        notebook.id,
                        audio,
                        mime_type="audio/mp4",
                        wait=True,
                        wait_timeout=_SOURCE_UPLOAD_TIMEOUT_SECONDS,
                        title=f"YouTube video {ref.video_id}"
                        if ref.source == "youtube"
                        else "Media",
                    )
            else:
                await client.sources.add_url(notebook.id, video_link)
        except Exception:
            await _safe_delete_notebook(client, notebook.id)
            raise
        return notebook


def _topic_prompt(
    topics: list[Topic],
    topic_index: int,
    *,
    output_language: str = DEFAULT_CONFIG.output_language,
) -> str:
    """Build a detailed-summary question for one topic.

    Args:
        topics: Chronological topic list providing the target and its immediate
            neighbors.
        topic_index: Valid zero-based index of the target topic.
        output_language: Language requested for the generated prose. Defaults to
            English.

    Returns:
        NotebookLM prompt scoped to the selected topic and its boundaries.

    Raises:
        IndexError: If topic indexing runs outside the list.
    """
    boundaries = _topic_boundaries(topics, topic_index)
    return (
        f"Summarize topic {topic_index + 1} of {len(topics)} in chronological order. "
        f"Write a detailed, coherent explanation in {output_language.strip()} of only the topic "
        f"'{topics[topic_index].name}'. "
        f"Start after the previous topic "
        f"'{boundaries['prev']}' ends and stop before "
        f"the next topic '{boundaries['next']}' begins. "
        f"Match the length and paragraph count to the amount of substantive source "
        f"material, with no minimum word count or fixed paragraph count. For brief "
        f"topics, a short paragraph may be enough; give longer or more complex "
        f"discussions additional words and paragraphs as needed. Write no more than "
        f"450 words for this topic. Avoid padding and repetition. Explain the flow "
        f"of the discussion and preserve the important claims, reasoning, examples, "
        f"events, distinctions, and conclusions that the speaker makes when they are "
        f"present in the video. Integrate related subtopics into a readable narrative; "
        f"do not merely list them. "
        f"Do not use bullets, numbered lists, headings, labels such as 'Subtopic:', "
        f"a title, or meta-commentary. Do not discuss adjacent topics. Use only "
        f"information supported by the video and do not invent missing details. "
        "Return only the prose summary. End immediately after the final sentence "
        "of the summary. Do not append follow-up questions, suggested questions, "
        "or invitations to continue the discussion."
    )


def _topic_list_prompt(
    candidates: Sequence[Topic] | None = None,
    *,
    output_language: str = DEFAULT_CONFIG.output_language,
) -> str:
    """Build instructions for topic extraction or coverage review.

    Args:
        candidates: Existing candidate list to select review mode, or ``None`` for
            extraction. Review refers to the previous answer in the same conversation.
        output_language: Language requested for topic names. Defaults to English.

    Returns:
        NotebookLM prompt requiring chronological JSON topic data.
    """
    output_language = output_language.strip()
    requirements = (
        "Include every substantive discussion in chronological order, including "
        "brief discussions, secondary topics, audience questions, announcements, "
        "and anecdotes. Separate distinct discussions rather than combining them "
        "under broad categories. Represent later returns to a topic separately "
        "when they contain new information, using specific names. Exclude filler "
        "and incidental mentions. Do not target a fixed topic count. "
        "Use only information supported by the video and do not invent topics. "
        f"Write every topic name in {output_language}, regardless of the language spoken in "
        "the video or used in its transcript. Translate descriptive topic names "
        f"from other languages into {output_language}. Keep each name within 100 characters. "
        'Return a JSON list using this structure: [{"name": "topic name"}]. '
        "Return JSON only, do not include any other text in your response."
    )
    if candidates is None:
        return "Create a comprehensive topic list for this video. " + requirements
    return (
        "Compare the complete candidate topic list in your previous answer "
        "against the entire source from beginning "
        "to end. Identify omitted discussions, overly broad entries, and incorrectly "
        "merged recurring discussions. Return the complete corrected list. "
        "Retain supported discussions from the candidate list and add only "
        "source-supported omissions. Correct grouping and chronological order "
        "where needed. " + requirements
    )


async def _get_or_create_topic_list(
    client: NotebookLMClient,
    ref: VideoRef,
    *,
    use_youtube_audio: bool = False,
    config: RuntimeConfig | None = None,
) -> tuple[VideoSummary, "Notebook | None"]:
    """Load cached topics or generate and save a reviewed list.

    Look up topics by source, video ID, and trimmed, case-folded language. On a cache
    miss, extract topics and review coverage in the same conversation before saving.

    Args:
        client: Authenticated NotebookLM client.
        ref: Media reference identifying the source and cache key.
        use_youtube_audio: Whether to upload audio if a YouTube notebook is needed.
        config: Language and source preparation options. Defaults to runtime settings.

    Returns:
        Pair containing the cache record and the temporary notebook, or ``None`` for the
            notebook on a cache hit. The caller owns deletion of a returned notebook.

    Raises:
        InvalidSummaryResponse: If cached or generated topic data is invalid.
        PermanentSummaryError: If an extraction or review question is rejected as
            invalid.
        UnsupportedVideoSource: If the reference cannot be turned into a source URL.
        notebooklm.NotebookLMError: If a NotebookLM request fails.
        TwitchAudioError: If Twitch audio cannot be prepared.
        XSpaceAudioError: If Space audio cannot be prepared.
        GenericMediaError: If generic or fallback audio cannot be prepared.
        pymongo.errors.PyMongoError: If a cache read or write fails.
        OSError: If a file operation fails.
    """
    video_link = build_video_url(ref)
    output_language = runtime_config(config).output_language.strip()
    language_key = output_language.casefold()

    logger.info(f"Checking if video {video_link} is already in database...")
    video = await VideoSummary.find_one(
        VideoSummary.source == ref.source,
        VideoSummary.video_id == ref.video_id,
        VideoSummary.output_language == language_key,
    )
    if video and video.topics:
        video.topics = _validated_topics(video.topics)
        return video, None

    logger.info(
        f"Video {video_link} not found in database, creating notebook and adding source..."
    )

    notebook = await _create_notebook_with_source(
        client, ref, use_youtube_audio=use_youtube_audio
    )
    try:
        logger.info(f"Asking notebook {notebook.id} for topics...")
        response = await _ask_notebook(
            client,
            notebook.id,
            _topic_list_prompt(output_language=output_language),
            phase="topic_extraction",
            ref=ref,
            topic_count=0,
        )
        candidates = _parse_topics(response.answer)

        logger.info(f"Reviewing topic coverage in notebook {notebook.id}...")
        response = await _ask_notebook(
            client,
            notebook.id,
            _topic_list_prompt(candidates, output_language=output_language),
            phase="topic_review",
            ref=ref,
            topic_count=len(candidates),
        )
        topics_objects = _parse_topics(response.answer)

        logger.info("Saving reviewed topics to database...")
        if video:
            video.topics = topics_objects
        else:
            video = VideoSummary(
                source=ref.source,
                video_id=ref.video_id,
                output_language=language_key,
                topics=topics_objects,
            )

        await video.save()
        logger.info(f"Topics saved to database for video {video_link}.")
        return video, notebook
    except Exception:
        await _safe_delete_notebook(client, notebook.id)
        raise


async def get_topic_list(
    client: NotebookLMClient,
    ref: VideoRef,
    *,
    use_youtube_audio: bool = False,
    config: RuntimeConfig | None = None,
):
    """Return the ordered topics for one video and configured language.

    On a cache miss, save the reviewed list and attempt to delete the temporary
    notebook.

    Args:
        client: Authenticated NotebookLM client.
        ref: Source and video identity to summarize.
        use_youtube_audio: Whether to upload audio if a YouTube notebook is needed.
        config: Language and source preparation options. Defaults to runtime settings.

    Returns:
        List of ``Topic`` objects in source order, including any cached details.

    Raises:
        InvalidSummaryResponse: If cached or generated topics are invalid.
        PermanentSummaryError: If a generation question is invalid or exceeds the byte
            budget.
        UnsupportedVideoSource: If the reference cannot be turned into a source URL.
        notebooklm.NotebookLMError: If a NotebookLM request fails.
        TwitchAudioError: If Twitch audio cannot be prepared.
        XSpaceAudioError: If Space audio cannot be prepared.
        GenericMediaError: If generic or fallback audio cannot be prepared.
        pymongo.errors.PyMongoError: If a cache read or write fails.
        OSError: If a file operation fails.
    """
    logger.info(f"Getting topics from {ref}")
    video, notebook = await _get_or_create_topic_list(
        client, ref, use_youtube_audio=use_youtube_audio, config=config
    )
    if notebook is None:
        return video.topics
    try:
        return video.topics
    finally:
        await _safe_delete_notebook(client, notebook.id)
        logger.info("Deleted temporary notebook used for topic list.")


async def get_topic_details(
    client: NotebookLMClient,
    ref: VideoRef,
    topic_index: int,
    *,
    use_youtube_audio: bool = False,
    config: RuntimeConfig | None = None,
):
    """Read or generate the explanation for a single topic.

    Save newly generated detail in the language-specific cache and attempt to delete any
    temporary notebook when done.

    Args:
        client: Authenticated NotebookLM client.
        ref: Source and video identity to summarize.
        topic_index: Zero-based index within the ordered topic list.
        use_youtube_audio: Whether to upload audio if a YouTube notebook is needed.
        config: Language and source preparation options. Defaults to runtime settings.

    Returns:
        Mapping with ``topic_name`` and ``detail`` strings for the selected topic.

    Raises:
        IndexError: If the topic index falls outside the topic list.
        InvalidSummaryResponse: If topics are invalid or the generated detail contains
            only citations.
        PermanentSummaryError: If a generation question is rejected or exceeds the byte
            budget.
        UnsupportedVideoSource: If the reference cannot be turned into a source URL.
        notebooklm.NotebookLMError: If a NotebookLM request fails.
        TwitchAudioError: If Twitch audio cannot be prepared.
        XSpaceAudioError: If Space audio cannot be prepared.
        GenericMediaError: If generic or fallback audio cannot be prepared.
        pymongo.errors.PyMongoError: If a cache read or write fails.
        OSError: If a file operation fails.
    """
    video, notebook = await _get_or_create_topic_list(
        client, ref, use_youtube_audio=use_youtube_audio, config=config
    )
    topics = _validated_topics(video.topics or [])

    try:
        if topic_index < 0 or topic_index >= len(topics):
            raise IndexError(f"Invalid topic index: {topic_index}")

        if topics[topic_index].detail:
            logger.info("Topic details already exist in database, returning...")
            return {
                "topic_name": topics[topic_index].name,
                "detail": topics[topic_index].detail,
            }

        if notebook is None:
            notebook = await _create_notebook_with_source(
                client, ref, use_youtube_audio=use_youtube_audio
            )
        prompt = _topic_prompt(
            topics, topic_index, output_language=runtime_config(config).output_language
        )
        logger.info("Asking notebook for topic details...")
        response = await _ask_notebook(
            client,
            notebook.id,
            prompt,
            phase="topic_detail",
            ref=ref,
            topic_count=len(topics),
        )

        logger.info("Parsing response and saving details to database...")
        detail = _clean_topic_detail(response.answer)
        await video.set({f"topics.{topic_index}.detail": detail})
        logger.info("Topic detail saved to database.")
        return {
            "topic_name": topics[topic_index].name,
            "detail": detail,
        }
    finally:
        if notebook is not None:
            await _safe_delete_notebook(client, notebook.id)
            logger.info("Deleted temporary notebook used for topic details.")


async def get_topic_details_all(
    client: NotebookLMClient,
    ref: VideoRef,
    *,
    use_youtube_audio: bool = False,
    config: RuntimeConfig | None = None,
):
    """Return details for all topics, filling in missing explanations.

    Reuse saved details and write newly generated details in one update, even if
    generation fails partway through. Attempt to delete any temporary notebook when done.

    Args:
        client: Authenticated NotebookLM client.
        ref: Source and video identity to summarize.
        use_youtube_audio: Whether to upload audio if a YouTube notebook is needed.
        config: Language and source preparation options. Defaults to runtime settings.

    Returns:
        Ordered list of mappings with ``topic_name`` and ``detail`` strings.

    Raises:
        InvalidSummaryResponse: If topics are invalid or a generated detail contains
            only citations.
        PermanentSummaryError: If a generation question is rejected or exceeds the byte
            budget.
        UnsupportedVideoSource: If the reference cannot be turned into a source URL.
        notebooklm.NotebookLMError: If a NotebookLM request fails.
        TwitchAudioError: If Twitch audio cannot be prepared.
        XSpaceAudioError: If Space audio cannot be prepared.
        GenericMediaError: If generic or fallback audio cannot be prepared.
        pymongo.errors.PyMongoError: If a cache read or write fails.
        OSError: If a file operation fails.
    """
    video, notebook = await _get_or_create_topic_list(
        client, ref, use_youtube_audio=use_youtube_audio, config=config
    )
    topics = _validated_topics(video.topics or [])

    if notebook is None and any(not topic.detail for topic in topics):
        notebook = await _create_notebook_with_source(
            client, ref, use_youtube_audio=use_youtube_audio
        )

    results = []
    detail_updates: dict[str, str] = {}
    generation_complete = False
    try:
        for i, topic in enumerate(topics):
            detail = topic.detail
            if not detail:
                prompt = _topic_prompt(
                    topics, i, output_language=runtime_config(config).output_language
                )
                logger.info(f"Asking notebook for details on topic: '{topic.name}'...")

                response = await _ask_notebook(
                    client,
                    notebook.id,
                    prompt,
                    phase="topic_detail",
                    ref=ref,
                    topic_count=len(topics),
                )
                detail = _clean_topic_detail(response.answer)
                detail_updates[f"topics.{i}.detail"] = detail

            results.append({"topic_name": topic.name, "detail": detail})

        generation_complete = True
        return results
    finally:
        try:
            if detail_updates:
                await video.set(detail_updates)
        except Exception:
            if generation_complete:
                raise
            logger.exception(
                "Failed to save partial topic details for %s.", ref.video_id
            )
        finally:
            if notebook is not None:
                await _safe_delete_notebook(client, notebook.id)
                logger.info("Deleted temporary notebook used for topic details.")


async def _safe_delete_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    """Attempt to delete a temporary notebook and log API failures.

    Args:
        client: Authenticated NotebookLM client.
        notebook_id: Temporary notebook to remove.
    """
    try:
        await client.notebooks.delete(notebook_id)
    except Exception:
        logger.exception(
            "Failed to delete temporary NotebookLM resource %s",
            notebook_id,
            extra={"context": {"notebook_id": notebook_id}},
        )
