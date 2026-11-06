"""NotebookLM topic caching, detail generation, and failure classification."""

import json
import logging
from collections.abc import Awaitable, Callable, Sequence

import httpx
from notebooklm import ChatError, NotebookLMClient, SourceAddError
from notebooklm.types import AskResult, Notebook

from vsummary.logging import log_event
from vsummary.model.video import Topic, VideoSummary
from vsummary.settings import DEFAULT_CONFIG, RuntimeConfig, runtime_config
from vsummary.util.generic_media import download_generic_audio
from vsummary.util.twitch import download_twitch_audio
from vsummary.util.video import VideoRef, build_video_url
from vsummary.util.x_space import download_x_space_audio

logger = logging.getLogger(__name__)
# Conservative application budget, not a claimed Google API limit. UTF-8 bytes
# also bound character count and leave headroom for multilingual topic names.
_MAX_CHAT_QUESTION_BYTES = 4000


class SummaryServiceError(Exception):
    """Base error raised by the NotebookLM summary service."""


class TransientSummaryError(SummaryServiceError):
    """Raised when a summary can be retried safely."""


class PermanentSummaryError(SummaryServiceError):
    """Raised when a request is invalid or NotebookLM returned an unusable result."""


class InvalidSummaryResponse(TransientSummaryError):
    """Raised when NotebookLM returns malformed topic data that may succeed on retry."""


async def _ask_notebook(
    client: NotebookLMClient,
    notebook_id: str,
    prompt: str,
    *,
    phase: str,
    ref: VideoRef,
    topic_count: int,
) -> AskResult:
    """Bound outgoing chat questions and classify invalid-argument rejections."""
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
    """Apply the configured immediate retry budget to malformed responses."""
    for _ in range(retries):
        try:
            return await operation()
        except InvalidSummaryResponse:
            pass
    return await operation()


class NotebookLMSummaryService:
    """NotebookLM-backed summary generation with durable cache reuse."""

    def __init__(
        self, client: NotebookLMClient, *, config: RuntimeConfig | None = None
    ):
        """Retain the authenticated NotebookLM client owned by the application."""
        self.client = client
        self.config = config

    async def close(self) -> None:
        """Close the client when it exposes the optional asynchronous close hook."""
        close = getattr(self.client, "aclose", None)
        if close is not None:
            await close()

    async def summarize(
        self, ref: VideoRef, *, use_youtube_audio: bool = False
    ) -> list[Topic]:
        """Generate all topic details and map backend failures to workflow errors."""
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
    """Name the topics adjacent to ``topic_index`` for prompt context."""
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
    """Validate names used both in persisted data and Discord select labels."""
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
    """Decode NotebookLM's topic-list JSON and validate Discord-safe names."""
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


async def _create_notebook_with_source(
    client: NotebookLMClient,
    ref: VideoRef,
    *,
    use_youtube_audio: bool = False,
    config: RuntimeConfig | None = None,
) -> "Notebook":
    """Create a temporary notebook with the video source attached.

    The caller is responsible for deleting the returned notebook when it is
    no longer needed. If the source cannot be added, the notebook is deleted
    here before the error is re-raised.

    ``use_youtube_audio`` uploads downloaded audio for YouTube references while
    retaining the original reference for cache and concurrency coordination.
    """
    video_link = build_video_url(ref)
    options = {"config": config} if config is not None else {}
    upload_timeout = runtime_config(config).source_upload_timeout_seconds
    notebook = await client.notebooks.create(video_link)
    try:
        if ref.source == "twitch":
            async with download_twitch_audio(ref, **options) as audio:
                await client.sources.add_file(
                    notebook.id,
                    audio,
                    mime_type="audio/mp4",
                    wait=True,
                    wait_timeout=upload_timeout,
                    title=f"Twitch VOD {ref.video_id}",
                )
        elif ref.source == "x_space":
            async with download_x_space_audio(ref, **options) as audio:
                await client.sources.add_file(
                    notebook.id,
                    audio,
                    mime_type="audio/mp4",
                    wait=True,
                    wait_timeout=upload_timeout,
                    title=f"X Space {ref.video_id}",
                )
        elif ref.source == "yt_dlp" or (ref.source == "youtube" and use_youtube_audio):
            async with download_generic_audio(ref, **options) as audio:
                await client.sources.add_file(
                    notebook.id,
                    audio,
                    mime_type="audio/mp4",
                    wait=True,
                    wait_timeout=upload_timeout,
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
    """Build the prompt that asks for a detailed single-topic explanation."""
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
        f"Return only the prose summary."
    )


def _topic_list_prompt(
    candidates: Sequence[Topic] | None = None,
    *,
    output_language: str = DEFAULT_CONFIG.output_language,
) -> str:
    """Build extraction or same-conversation coverage-review instructions."""
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
    """
    Get the topic list for a video, creating it if it doesn't exist.
    :param client: The NotebookLMClient instance to use for interacting with the notebook service
    :param ref: The video reference (source + id) for which to get or create the topic list
    :return: The video object with its topic list, and the notebook used to generate
             it (or None when the topics were already cached and no notebook was needed)
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
        client, ref, use_youtube_audio=use_youtube_audio, config=config
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
    """Return cached topics or create them from a temporary NotebookLM notebook."""
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
    """Return one topic's cached or newly generated detail for ``ref``."""
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
                client, ref, use_youtube_audio=use_youtube_audio, config=config
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
        topics[topic_index].detail = response.answer
        await video.save()
        logger.info("Topic detail saved to database.")
        return {
            "topic_name": topics[topic_index].name,
            "detail": topics[topic_index].detail,
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
    """Return every topic detail, generating and saving only missing details."""
    video, notebook = await _get_or_create_topic_list(
        client, ref, use_youtube_audio=use_youtube_audio, config=config
    )
    topics = _validated_topics(video.topics or [])

    if notebook is None and any(t.detail is None for t in topics):
        notebook = await _create_notebook_with_source(
            client, ref, use_youtube_audio=use_youtube_audio, config=config
        )

    results = []
    needs_db_save = False
    try:
        for i, topic in enumerate(topics):
            if topic.detail:
                results.append({"topic_name": topic.name, "detail": topic.detail})
            else:
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
                topic.detail = response.answer
                needs_db_save = True

                results.append({"topic_name": topic.name, "detail": topic.detail})

        if needs_db_save:
            logger.info(
                f"Saving all newly fetched topic details to database for video {ref} in a "
                f"single batch..."
            )
            await video.save()
            logger.info("Batch save complete.")

        return results
    finally:
        if notebook is not None:
            await _safe_delete_notebook(client, notebook.id)
            logger.info("Deleted temporary notebook used for topic details.")


async def _safe_delete_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    """Best-effort cleanup that logs failures without hiding the original error."""
    try:
        await client.notebooks.delete(notebook_id)
    except Exception:
        logger.exception(
            "Failed to delete temporary NotebookLM resource %s",
            notebook_id,
            extra={"context": {"notebook_id": notebook_id}},
        )
