"""Application logging sinks and safe Discord log delivery."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from rich.logging import RichHandler

from vsummary.settings import Settings
from vsummary.util.discord import send_limited
from vsummary.util.video import UnsupportedVideoSource, VideoRef, build_video_url

DISCORD_MESSAGE_LIMIT = 2_000
DISCORD_QUEUE_SIZE = 1_000
_HANDLER_MARKER = "_vsummary_logging_handler"
_SENSITIVE_CONTEXT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "discord_token",
    "mongodb_uri",
    "password",
    "secret",
    "session",
    "token",
}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(\b(?:api[_-]?key|authorization|cookie|password|passwd|secret|session|token)\b"
    r"[\"']?\s*[:=]\s*[\"']?)([^\s,;\]\}\"']+)"
)
_URI_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]+@")


class LoggingConfigurationError(RuntimeError):
    """Raised when the required application file log cannot be configured."""


class ContextFormatter(logging.Formatter):
    """Formatter that renders optional structured log context consistently."""

    def format(self, record: logging.LogRecord) -> str:
        record.context_text = _format_context(getattr(record, "context", None))
        return super().format(record)


class DiscordLogHandler(logging.Handler):
    """A non-blocking, warning-only logging sink delivered by a Discord bot."""

    def __init__(self, channel_id: int, *, queue_size: int = DISCORD_QUEUE_SIZE):
        super().__init__(logging.WARNING)
        self.channel_id = channel_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.disabled = False
        self._channel: Any | None = None
        self._bot: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._dropped_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Queue a pre-rendered event without ever blocking an application task."""
        if self.disabled or record.levelno < logging.WARNING:
            return
        try:
            message = self.format_record(record)
        except Exception:  # noqa: BLE001 - logging must not break application work
            return
        self._enqueue(message)

    async def start(self, bot: Any) -> None:
        """Resolve the configured channel and start delivery after Discord is ready."""
        if self.disabled or self._task is not None:
            return
        try:
            channel = bot.get_channel(self.channel_id)
        except Exception as exc:  # noqa: BLE001 - Discord cache access is external
            self._disable(f"could not resolve channel {self.channel_id}: {exc}")
            return
        if channel is None:
            fetch_channel = getattr(bot, "fetch_channel", None)
            if fetch_channel is not None:
                try:
                    channel = await fetch_channel(self.channel_id)
                except Exception as exc:  # noqa: BLE001 - Discord-specific errors vary
                    self._disable(f"could not resolve channel {self.channel_id}: {exc}")
                    return
        if channel is None or not callable(getattr(channel, "send", None)):
            self._disable(f"configured channel {self.channel_id} is unavailable")
            return
        self._channel = channel
        self._bot = bot
        self._task = asyncio.create_task(
            self._deliver(), name="vsummary-discord-log-delivery"
        )

    async def stop(self) -> None:
        """Stop delivery without logging from shutdown paths."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._channel = None
        self._bot = None

    async def drain(self) -> None:
        """Wait until messages currently queued for a live sink have been sent."""
        if not self.disabled and self._task is not None:
            await self.queue.join()

    def format_record(self, record: logging.LogRecord) -> str:
        """Render the timestamp, video link, and concise error for Discord."""
        timestamp = (
            datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        lines = [f"Timestamp: {timestamp}"]
        video_url = _video_url_from_context(getattr(record, "context", None))
        if video_url is not None:
            lines.append(f"Video: {video_url}")
        lines.append(f"Error: {_redact_text(_record_error(record))}")
        return _truncate("\n".join(lines), DISCORD_MESSAGE_LIMIT)

    async def _deliver(self) -> None:
        while True:
            message = await self.queue.get()
            try:
                await send_limited(self._bot, self._channel.send, message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - Discord-specific errors vary
                self._disable(f"sending to channel {self.channel_id} failed: {exc}")
                return
            finally:
                self.queue.task_done()
            self._enqueue_dropped_summary()

    def _enqueue(self, message: str) -> None:
        if self.queue.full():
            self._dropped_count += 1
            return
        self._enqueue_dropped_summary()
        if self.queue.full():
            self._dropped_count += 1
            return
        self.queue.put_nowait(message)

    def _enqueue_dropped_summary(self) -> None:
        if not self._dropped_count or self.queue.full() or self.disabled:
            return
        summary = _truncate(
            f"[{datetime.now(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')}] "
            f"WARNING vsummary.logging Discord log queue dropped "
            f"{self._dropped_count} event(s)",
            DISCORD_MESSAGE_LIMIT,
        )
        self.queue.put_nowait(summary)
        self._dropped_count = 0

    def _disable(self, reason: str) -> None:
        if self.disabled:
            return
        self.disabled = True
        self.setLevel(logging.CRITICAL + 1)
        logging.getLogger(__name__).error(
            "Discord log sink disabled: %s",
            reason,
            extra={"context": {"discord_log_channel_id": self.channel_id}},
        )


def configure_logging(settings: Settings | None = None) -> DiscordLogHandler | None:
    """Configure the required console/file sinks and optional Discord sink."""
    log_path = Path(getattr(settings, "log_file_path", Path("logs/vsummary.log")))
    level_name = getattr(settings, "log_level", "INFO")
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        raise LoggingConfigurationError(f"invalid configured LOG_LEVEL: {level_name}")

    formatter = ContextFormatter(
        "%(asctime)s %(levelname)-8s %(name)s %(message)s%(context_text)s"
    )
    formatter.converter = time.gmtime
    console_handler = RichHandler(rich_tracebacks=True, markup=False)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",
            interval=1,
            backupCount=14,
            encoding="utf-8",
            utc=True,
        )
    except (OSError, ValueError) as exc:
        raise LoggingConfigurationError(
            f"Startup cannot continue: file logging is required but {log_path} "
            f"could not be opened: {exc}"
        ) from exc
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)

    discord_handler = None
    discord_channel_id = getattr(settings, "discord_log_channel_id", None)
    if discord_channel_id is not None:
        discord_handler = DiscordLogHandler(discord_channel_id)
        discord_handler.setFormatter(formatter)

    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)
    for handler in (console_handler, file_handler, discord_handler):
        if handler is not None:
            setattr(handler, _HANDLER_MARKER, True)
            root.addHandler(handler)
    return discord_handler


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *args: object,
    **context: object,
) -> None:
    """Emit a log record with structured context shared by all configured sinks."""
    logger.log(level, message, *args, extra={"context": context})


def _format_context(value: object, *, redact: bool = False) -> str:
    if not isinstance(value, Mapping) or not value:
        return ""
    parts = []
    for key, item in sorted(value.items(), key=lambda pair: str(pair[0])):
        name = str(key)
        if redact and name.lower() in _SENSITIVE_CONTEXT_KEYS:
            rendered = "<redacted>"
        else:
            rendered = _redact_text(str(item)) if redact else str(item)
        parts.append(f"{name}={rendered}")
    return " " + _truncate(" ".join(parts), 500)


def _video_url_from_context(value: object) -> str | None:
    """Build a video URL from log context when one is available."""
    if not isinstance(value, Mapping):
        return None
    video_id = value.get("video_id")
    if not isinstance(video_id, str) or not video_id:
        return None
    source = value.get("source", "youtube")
    if not isinstance(source, str):
        return None
    try:
        return build_video_url(VideoRef(source=source, video_id=video_id))
    except (UnsupportedVideoSource, ValueError):
        return None


def _record_error(record: logging.LogRecord) -> str:
    """Extract the exception text without its traceback when available."""
    if record.exc_info and record.exc_info[1] is not None:
        exception_type, exception, _ = record.exc_info
        return f"{exception_type.__name__}: {exception}"
    return record.getMessage()


def _redact_text(value: str) -> str:
    value = _URI_CREDENTIALS.sub(r"\1<redacted>@", value)
    return _SECRET_ASSIGNMENT.sub(r"\1<redacted>", value)


def _truncate(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"
