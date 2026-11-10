"""Recognize X URLs, verify archived Spaces, and prepare replay audio."""

import asyncio
import logging
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import yt_dlp

from vsummary.util.audio import MAX_AUDIO_BYTES, audio_options

if TYPE_CHECKING:
    from vsummary.util.video import VideoRef


logger = logging.getLogger(__name__)
_X_HOSTS = {
    "x.com",
    "www.x.com",
    "m.x.com",
    "mobile.x.com",
    "twitter.com",
    "www.twitter.com",
    "m.twitter.com",
    "mobile.twitter.com",
}
_SPACE_ID_RE = re.compile(r"[A-Za-z0-9]{13}")


class XSpaceResolutionError(ValueError):
    """Signal that an X URL cannot be resolved to a playable archived Space."""


class XPostWithoutSpaceError(XSpaceResolutionError):
    """Signal a status post without an associated Space."""


class XSpaceAudioError(RuntimeError):
    """Signal a failure while preparing an archived Space for upload."""


class XSpaceAudioUnavailableError(XSpaceAudioError):
    """Signal an inaccessible replay or an unsupported conversion or size."""


class XSpaceInputKind(str, Enum):
    """Distinguish direct Space links from links to status posts."""

    SPACE = "space"
    STATUS = "status"


@dataclass(frozen=True)
class XSpaceInput:
    """Carry a recognized X link before remote replay validation.

    Attributes:
        kind: Whether the input points to a Space or a status post.
        input_id: ID from the input URL.
        url: Canonical X URL passed to yt-dlp.
        original_url: User-supplied URL with surrounding whitespace removed.
    """

    kind: XSpaceInputKind
    input_id: str
    url: str
    original_url: str


def parse_x_space_input(value: str) -> XSpaceInput | None:
    """Recognize direct Space and status-post URLs without making requests.

    Args:
        value: Candidate HTTP(S) URL on a recognized X or Twitter host.

    Returns:
        Normalized input for a recognized URL shape, or ``None`` otherwise.
    """
    original_url = value.strip()
    try:
        parsed = urlparse(original_url)
        hostname = (parsed.hostname or "").lower()
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or hostname not in _X_HOSTS:
        return None

    segments = [segment for segment in parsed.path.split("/") if segment]
    if (
        len(segments) == 3
        and segments[:2] == ["i", "spaces"]
        and _SPACE_ID_RE.fullmatch(segments[2])
    ):
        space_id = segments[2]
        return XSpaceInput(
            original_url=original_url,
            kind=XSpaceInputKind.SPACE,
            input_id=space_id,
            url=f"https://x.com/i/spaces/{space_id}",
        )
    if len(segments) == 3 and segments[1] == "status" and segments[2].isdigit():
        return XSpaceInput(
            original_url=original_url,
            kind=XSpaceInputKind.STATUS,
            input_id=segments[2],
            url=f"https://x.com/{segments[0]}/status/{segments[2]}",
        )
    return None


async def resolve_parsed_x_space(x_input: XSpaceInput) -> "VideoRef":
    """Resolve a recognized X link to a playable Space replay.

    Args:
        x_input: Direct Space or status-post link classified by the local parser.

    Returns:
        Video reference keyed by the resolved Space ID.

    Raises:
        XPostWithoutSpaceError: If the post contains no Space.
        XSpaceResolutionError: If extraction fails or the Space has no playable archive.
    """
    logger.info("Resolving X Space from %s URL.", x_input.kind.value)
    try:
        info = await asyncio.to_thread(_extract_x_space_info, x_input.url)
    except XSpaceResolutionError:
        raise
    except Exception as exc:  # yt-dlp exposes site errors as varied exception types
        raise _resolution_error_from_exception(exc, x_input.kind) from exc
    ref = _ref_from_info(info, x_input.kind)
    logger.info("Resolved archived X Space %s.", ref.video_id)
    return ref


def _extract_x_space_info(url: str) -> dict:
    """Read X metadata with yt-dlp in inspection mode.

    Args:
        url: Canonical Space or status-post URL.

    Returns:
        Raw metadata dictionary.

    Raises:
        XSpaceResolutionError: If yt-dlp returns no metadata dictionary.
        yt_dlp.utils.DownloadError: If yt-dlp cannot extract the metadata.
    """
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
    }
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(url, download=False)
    if not isinstance(info, dict):
        raise XSpaceResolutionError(
            "This X Space is unavailable or cannot be accessed."
        )
    return info


def _ref_from_info(info: dict, input_kind: XSpaceInputKind) -> "VideoRef":
    """Validate Space metadata and build its replay reference.

    Args:
        info: Extracted yt-dlp metadata.
        input_kind: URL kind used to classify a result without a Space.

    Returns:
        Video reference containing the validated Space ID.

    Raises:
        XPostWithoutSpaceError: If the input is a post and the result contains no Space.
        XSpaceResolutionError: If the result has an invalid Space ID or no playable
            archive.
    """
    if not _is_x_space_result(info):
        if input_kind is XSpaceInputKind.STATUS:
            raise XPostWithoutSpaceError("This X post does not contain an X Space.")
        raise XSpaceResolutionError(
            "This X Space is unavailable or cannot be accessed."
        )

    space_id = info.get("id")
    if not isinstance(space_id, str) or not _SPACE_ID_RE.fullmatch(space_id):
        raise XSpaceResolutionError(
            "This X Space is unavailable or cannot be accessed."
        )

    _require_archived_replay(info)
    from vsummary.util.video import VideoRef

    return VideoRef(source="x_space", video_id=space_id)


def _is_x_space_result(info: dict) -> bool:
    """Identify metadata from yt-dlp's Twitter Spaces extractor.

    Args:
        info: Metadata dictionary returned by yt-dlp.

    Returns:
        Whether either extractor field identifies Twitter Spaces.
    """
    extractor_key = info.get("extractor_key")
    extractor = info.get("extractor")
    return extractor_key == "TwitterSpaces" or extractor == "twitter:spaces"


def _require_archived_replay(info: dict) -> None:
    """Require a finished Space with replay formats.

    Args:
        info: Extracted Space metadata.

    Raises:
        XSpaceResolutionError: If the Space is upcoming, live, processing, or lacks an
            archived replay.
    """
    live_status = info.get("live_status")
    if live_status == "is_upcoming":
        raise XSpaceResolutionError("This X Space has not started yet.")
    if live_status == "is_live":
        raise XSpaceResolutionError(
            "This X Space is currently live; only archived Spaces are supported."
        )
    if live_status == "post_live":
        raise XSpaceResolutionError(
            "This X Space has ended but has not been archived yet."
        )
    if live_status != "was_live" or not info.get("formats"):
        raise XSpaceResolutionError("This X Space has ended but is not archived.")


def _resolution_error_from_exception(
    exc: Exception, input_kind: XSpaceInputKind
) -> XSpaceResolutionError:
    """Translate a yt-dlp error into a user-facing Space resolution error.

    Args:
        exc: Exception raised while inspecting the X URL.
        input_kind: URL kind used to recognize a status post without a Space.

    Returns:
        Specific resolution error for recognized failures, or a generic access error.
    """
    message = str(exc).lower()
    if "not started yet" in message:
        return XSpaceResolutionError("This X Space has not started yet.")
    if "ended but not downloadable yet" in message:
        return XSpaceResolutionError(
            "This X Space has ended but has not been archived yet."
        )
    if "replay is disabled" in message:
        return XSpaceResolutionError("This X Space has ended but is not archived.")
    if input_kind is XSpaceInputKind.STATUS and "no video could be found" in message:
        return XPostWithoutSpaceError("This X post does not contain an X Space.")
    return XSpaceResolutionError("This X Space is unavailable or cannot be accessed.")


def _download(url: str, directory: str) -> Path:
    """Revalidate an archived Space and convert its replay to M4A.

    Args:
        url: Canonical archived Space URL.
        directory: Directory for the converted file.

    Returns:
        Path to the single validated M4A file.

    Raises:
        XSpaceAudioUnavailableError: If FFmpeg is missing, the archive cannot be
            validated or converted, or the size limit is exceeded.
        XSpaceAudioError: If the expected single M4A output is missing or ambiguous.
        OSError: If the output cannot be inspected on disk.
    """
    if shutil.which("ffmpeg") is None:
        raise XSpaceAudioUnavailableError("ffmpeg is required to process X Space audio")
    options = audio_options(Path(directory) / "%(id)s.%(ext)s")
    try:
        with yt_dlp.YoutubeDL(options) as downloader:
            info = downloader.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise XSpaceResolutionError(
                    "This X Space is unavailable or cannot be accessed."
                )
            ref = _ref_from_info(info, XSpaceInputKind.SPACE)
            logger.info("Downloading archived X Space audio for %s.", ref.video_id)
            downloader.download([url])
    except XSpaceResolutionError as exc:
        raise XSpaceAudioUnavailableError(str(exc)) from exc
    except (yt_dlp.utils.DownloadError, yt_dlp.utils.PostProcessingError) as exc:
        raise XSpaceAudioUnavailableError(
            f"X Space audio download or conversion failed: {exc}"
        ) from exc

    paths = list(Path(directory).glob("*.m4a"))
    if len(paths) != 1:
        raise XSpaceAudioError("X Space audio download did not produce an M4A file")
    if paths[0].stat().st_size > MAX_AUDIO_BYTES:
        raise XSpaceAudioUnavailableError(
            "X Space audio exceeds NotebookLM's upload limit"
        )
    logger.info("Prepared X Space audio for %s.", paths[0].stem)
    return paths[0]


@asynccontextmanager
async def download_x_space_audio(ref: "VideoRef") -> AsyncIterator[Path]:
    """Prepare replay audio for an archived Space in a temporary directory.

    Args:
        ref: Normalized X Space reference.

    Yields:
        Path to the M4A file, available until the context exits.

    Raises:
        ValueError: If the reference is for another source.
        XSpaceAudioUnavailableError: If the replay cannot be accessed or converted, or
            exceeds the size limit.
        XSpaceAudioError: If the downloader does not produce one M4A file.
        OSError: If a temporary-file operation fails.
    """
    if ref.source != "x_space":
        raise ValueError("X Space audio can only be downloaded for X Space references")
    with tempfile.TemporaryDirectory(prefix="vsummary-x-space-") as directory:
        path = await asyncio.to_thread(
            _download,
            f"https://x.com/i/spaces/{ref.video_id}",
            directory,
        )
        yield path
