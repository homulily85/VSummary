"""Resolve, validate, and download archived public X Spaces."""

from __future__ import annotations

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

if TYPE_CHECKING:
    from vsummary.util.video import VideoRef


MAX_AUDIO_BYTES = 190 * 1024 * 1024
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
    """The supplied X URL does not resolve to an archived Space."""


class XSpaceAudioError(RuntimeError):
    """An archived X Space could not be prepared for NotebookLM."""


class XSpaceAudioUnavailableError(XSpaceAudioError):
    """The X Space audio is unavailable or cannot be uploaded."""


class XSpaceInputKind(str, Enum):
    """The supported X URL shapes accepted before remote resolution."""

    SPACE = "space"
    STATUS = "status"


@dataclass(frozen=True)
class XSpaceInput:
    """A supported X URL, normalized before requesting X through yt-dlp."""

    kind: XSpaceInputKind
    input_id: str
    url: str


def parse_x_space_input(value: str) -> XSpaceInput | None:
    """Recognize direct X Space and status-post URLs without network access."""
    try:
        parsed = urlparse(value.strip())
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
            kind=XSpaceInputKind.SPACE,
            input_id=space_id,
            url=f"https://x.com/i/spaces/{space_id}",
        )
    if len(segments) == 3 and segments[1] == "status" and segments[2].isdigit():
        return XSpaceInput(
            kind=XSpaceInputKind.STATUS,
            input_id=segments[2],
            url=f"https://x.com/{segments[0]}/status/{segments[2]}",
        )
    return None


def is_x_space_input(value: str) -> bool:
    """Return whether ``value`` has one of the X URL shapes we support."""
    return parse_x_space_input(value) is not None


async def resolve_x_space(value: str) -> VideoRef:
    """Resolve a user URL to a downloadable, archived X Space reference."""
    parsed = parse_x_space_input(value)
    if parsed is None:
        raise XSpaceResolutionError("This is not a supported X Space URL.")

    logger.info("Resolving X Space from %s URL.", parsed.kind.value)
    try:
        info = await asyncio.to_thread(_extract_x_space_info, parsed.url)
    except XSpaceResolutionError:
        raise
    except Exception as exc:  # yt-dlp exposes site errors as varied exception types
        raise _resolution_error_from_exception(exc, parsed.kind) from exc
    ref = _ref_from_info(info, parsed.kind)
    logger.info("Resolved archived X Space %s.", ref.video_id)
    return ref


def _extract_x_space_info(url: str) -> dict:
    """Ask yt-dlp for X metadata without downloading audio."""
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


def _ref_from_info(info: dict, input_kind: XSpaceInputKind) -> VideoRef:
    """Validate yt-dlp metadata and create an archived X Space reference."""
    if not _is_x_space_result(info):
        if input_kind is XSpaceInputKind.STATUS:
            raise XSpaceResolutionError("This X post does not contain an X Space.")
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
    """Identify metadata emitted by yt-dlp's Twitter Spaces extractor."""
    extractor_key = info.get("extractor_key")
    extractor = info.get("extractor")
    return extractor_key == "TwitterSpaces" or extractor == "twitter:spaces"


def _require_archived_replay(info: dict) -> None:
    """Reject upcoming, live, and ended-but-unarchived Space states."""
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
    """Translate variable yt-dlp failures into actionable user-facing messages."""
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
        return XSpaceResolutionError("This X post does not contain an X Space.")
    return XSpaceResolutionError("This X Space is unavailable or cannot be accessed.")


def _download(url: str, directory: str) -> Path:
    """Download, transcode, size-check, and return one archived Space M4A file."""
    if shutil.which("ffmpeg") is None:
        raise XSpaceAudioUnavailableError("ffmpeg is required to process X Space audio")
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "format": "bestaudio/best",
        "outtmpl": str(Path(directory) / "%(id)s.%(ext)s"),
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "m4a",
                "preferredquality": "64",
            }
        ],
    }
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
async def download_x_space_audio(ref: VideoRef) -> AsyncIterator[Path]:
    """Download an archived X Space as a temporary M4A audio file."""
    if ref.source != "x_space":
        raise ValueError("X Space audio can only be downloaded for X Space references")
    with tempfile.TemporaryDirectory(prefix="vsummary-x-space-") as directory:
        path = await asyncio.to_thread(
            _download, f"https://x.com/i/spaces/{ref.video_id}", directory
        )
        yield path
