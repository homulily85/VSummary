"""Console and file logging with optional queued delivery to Discord."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich.logging import RichHandler

from vsummary.settings import DEFAULT_CONFIG, Settings, runtime_config
from vsummary.util.discord import send_limited
from vsummary.util.video import UnsupportedVideoSource, VideoRef, build_video_url

if TYPE_CHECKING:
    from vsummary.bot import Bot

DISCORD_MESSAGE_LIMIT = 2_000
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
    """Signal an invalid log level or a failure to open the required log file."""


class ContextFormatter(logging.Formatter):
    """Format standard log records with a stable representation of their context."""

    def format(self, record: logging.LogRecord) -> str:
        """Add rendered structured context to a record and format it.

        Args:
            record: Log record to update with a ``context_text`` attribute.

        Returns:
            Formatted record text, including any exception text handled by the base
                formatter.
        """
        record.context_text = _format_context(getattr(record, "context", None))
        return super().format(record)


class DiscordLogHandler(logging.Handler):
    """Forward qualifying log events through a bounded asynchronous queue.

    Events can be dropped under load. The sink reports the drop count when space becomes
    available and disables itself on a channel or delivery failure.
    """

    def __init__(
        self,
        channel_id: int,
        *,
        queue_size: int = DEFAULT_CONFIG.discord_log_queue_size,
        min_level: str = DEFAULT_CONFIG.discord_log_min_level,
    ):
        """Create the queue for a Discord log destination.

        Args:
            channel_id: Discord channel ID for log messages.
            queue_size: Maximum number of queued events. Defaults to the runtime queue
                size.
            min_level: Minimum severity name. Defaults to the runtime Discord log level.

        Raises:
            ValueError: If the severity name is unrecognized.
        """
        super().__init__(min_level)
        self.channel_id = channel_id
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=queue_size)
        self.disabled = False
        self._channel: Any | None = None
        self._bot: Any | None = None
        self._task: asyncio.Task[None] | None = None
        self._dropped_count = 0

    def emit(self, record: logging.LogRecord) -> None:
        """Render and queue a log event without waiting for a Discord send.

        Skip low-severity events and records that cannot be formatted. Track drops if
        the queue is full.

        Args:
            record: Record supplied by Python logging.
        """
        if self.disabled or record.levelno < self.level:
            return
        try:
            message = self.format_record(record)
        except Exception:  # noqa: BLE001 - logging must not break application work
            return
        self._enqueue(message)

    async def start(self, bot: Bot) -> None:
        """Resolve the destination and schedule delivery once Discord is ready.

        Use the channel cache before fetching the channel. Disable the sink if
        resolution fails or the channel cannot send messages.

        Args:
            bot: Connected bot used for channel lookup and rate-limited sends.
        """
        if self.disabled or self._task is not None:
            return
        try:
            channel = bot.get_channel(self.channel_id)
        except Exception as exc:  # noqa: BLE001 - Discord cache access is external
            self._disable(f"could not resolve channel {self.channel_id}: {exc}")
            return
        if channel is None:
            fetch_channel = bot.fetch_channel
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
        """Cancel the delivery task and release its bot and channel references."""
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._channel = None
        self._bot = None

    def format_record(self, record: logging.LogRecord) -> str:
        """Render one record for the Discord log destination.

        Args:
            record: Event with an optional exception and video context.

        Returns:
            Discord-sized text with a UTC timestamp, a viewer-local timestamp, an
                optional video link, and a redacted error message.
        """
        timestamp = (
            datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z")
        )
        lines = [
            f"Timestamp (UTC): {timestamp}",
            f"Local time: <t:{int(record.created)}:F>",
        ]
        video_url = _video_url_from_context(getattr(record, "context", None))
        if video_url is not None:
            lines.append(f"Video: {video_url}")
        lines.append(f"Error: {_redact_text(_record_error(record))}")
        return _truncate("\n".join(lines), DISCORD_MESSAGE_LIMIT)

    async def _deliver(self) -> None:
        """Drain the queue through the shared Discord send limiter.

        Disable the sink on a send failure and acknowledge each dequeued item.

        Raises:
            asyncio.CancelledError: If the delivery task is cancelled.
        """
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
        """Queue an event or count it as dropped if capacity is exhausted.

        Args:
            message: Preformatted Discord log text.
        """
        if self.queue.full():
            self._dropped_count += 1
            return
        self._enqueue_dropped_summary()
        if self.queue.full():
            self._dropped_count += 1
            return
        self.queue.put_nowait(message)

    def _enqueue_dropped_summary(self) -> None:
        """Queue one warning for accumulated drops when space becomes available."""
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
        """Disable this sink and log the reason through the remaining handlers.

        Args:
            reason: Description of the channel resolution or send failure.
        """
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
    """Install application handlers on the root logger.

    Replace and close previously installed VSummary handlers. Configure file rotation at
    midnight UTC.

    Args:
        settings: Settings for severity, file rotation, and optional Discord delivery.
            When omitted, use runtime defaults.

    Returns:
        Discord handler to start once the bot is ready, or ``None`` if disabled.

    Raises:
        LoggingConfigurationError: If the log level is invalid or the required file
            handler cannot be opened.
    """
    config = runtime_config(settings)
    log_path = Path(config.log_file_path)
    level_name = config.log_level
    level = logging.getLevelName(level_name)
    if not isinstance(level, int):
        raise LoggingConfigurationError(
            f"invalid configured logging.level: {level_name}"
        )

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
            interval=config.log_rotation_interval_days,
            backupCount=config.log_backup_count,
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
    discord_channel_id = config.discord_log_channel_id
    if discord_channel_id is not None:
        discord_handler = DiscordLogHandler(
            discord_channel_id,
            queue_size=config.discord_log_queue_size,
            min_level=config.discord_log_min_level,
        )
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
    """Log a message with fields available to the configured formatters.

    Args:
        logger: Destination logger.
        level: Numeric logging severity.
        message: Message format string.
        *args: Values interpolated into the message by Python logging.
        **context: Structured fields attached to the record.
    """
    logger.log(level, message, *args, extra={"context": context})


def _format_context(value: object, *, redact: bool = False) -> str:
    """Render structured context in a stable order.

    Args:
        value: Context mapping to format.
        redact: Whether to mask recognized credential keys and text patterns.

    Returns:
        Space-prefixed key-value text, or an empty string for absent or non-mapping
            context.
    """
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
    """Recover a canonical video URL from structured log fields.

    Args:
        value: Context expected to contain a video ID and optionally a source. A missing
            source defaults to YouTube.

    Returns:
        Video URL, or ``None`` if the context cannot be turned into a supported URL.
    """
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
    """Extract the short error text for a log record.

    Args:
        record: Event that may carry exception information.

    Returns:
        Exception type and message when present, otherwise the formatted log message.
    """
    if record.exc_info and record.exc_info[1] is not None:
        exception_type, exception, _ = record.exc_info
        return f"{exception_type.__name__}: {exception}"
    return record.getMessage()


def _redact_text(value: str) -> str:
    """Mask common credential assignments and URI credentials.

    Args:
        value: Text to redact before external delivery.

    Returns:
        Text with recognized credentials replaced by redaction markers.
    """
    value = _URI_CREDENTIALS.sub(r"\1<redacted>@", value)
    return _SECRET_ASSIGNMENT.sub(r"\1<redacted>", value)


def _truncate(value: str, limit: int) -> str:
    """Fit text within a character limit.

    Args:
        value: Text to shorten.
        limit: Maximum character count. Non-positive limits produce empty text.

    Returns:
        Original text if it fits, or a shortened string ending in an ellipsis.
    """
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"
