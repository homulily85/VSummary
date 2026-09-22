import asyncio
import logging
from logging.handlers import TimedRotatingFileHandler
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from rich.logging import RichHandler

from vsummary.logging import (
    DiscordLogHandler,
    LoggingConfigurationError,
    configure_logging,
)
from vsummary.settings import Settings


@pytest.fixture
def isolated_root_logger():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    root.handlers.clear()
    try:
        yield root
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers[:] = original_handlers
        root.setLevel(original_level)


def test_configure_logging_writes_utf8_tracebacks_to_daily_rotating_file(
    tmp_path, isolated_root_logger
):
    settings = Settings(
        discord_token="token",
        mongodb_uri="mongodb://localhost",
        log_file_path=tmp_path / "nested" / "vsummary.log",
        log_level="DEBUG",
    )

    discord_handler = configure_logging(settings)
    logger = logging.getLogger("vsummary.tests.logging")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logger.exception("Unable to process job", extra={"context": {"job_id": "42"}})

    file_handler = next(
        handler
        for handler in isolated_root_logger.handlers
        if isinstance(handler, TimedRotatingFileHandler)
    )
    assert discord_handler is None
    assert any(
        isinstance(handler, RichHandler) for handler in isolated_root_logger.handlers
    )
    assert file_handler.utc is True
    assert file_handler.backupCount == 14

    contents = settings.log_file_path.read_text(encoding="utf-8")
    assert "Unable to process job" in contents
    assert "job_id=42" in contents
    assert "Traceback" in contents
    assert "RuntimeError: boom" in contents


def test_configure_logging_fails_fast_when_log_file_cannot_be_opened(
    tmp_path, isolated_root_logger
):
    blocked_parent = tmp_path / "not-a-directory"
    blocked_parent.write_text("blocked", encoding="utf-8")
    settings = Settings(
        discord_token="token",
        mongodb_uri="mongodb://localhost",
        log_file_path=blocked_parent / "vsummary.log",
    )

    with pytest.raises(LoggingConfigurationError, match="file logging is required"):
        configure_logging(settings)


@pytest.mark.asyncio
async def test_discord_sink_queues_warning_after_ready_and_redacts_and_truncates():
    handler = DiscordLogHandler(channel_id=123, queue_size=2)
    logger = logging.getLogger("vsummary.tests.discord")
    info = logger.makeRecord(logger.name, logging.INFO, __file__, 0, "ignore", (), None)
    handler.handle(info)
    assert handler.queue.empty()

    try:
        raise RuntimeError("token=super-secret")
    except RuntimeError:
        warning = logger.makeRecord(
            logger.name,
            logging.WARNING,
            __file__,
            0,
            "%s",
            ("mongodb://user:password@example.test/db " + "x" * 3_000,),
            exc_info=__import__("sys").exc_info(),
            extra={
                "context": {
                    "source": "twitch",
                    "video_id": "123456789",
                    "api_key": "also-secret",
                }
            },
        )
    warning.created = 0
    handler.handle(warning)

    target = SimpleNamespace(send=AsyncMock())
    await handler.start(SimpleNamespace(get_channel=lambda channel_id: target))
    await handler.drain()
    await handler.stop()

    target.send.assert_awaited_once()
    message = target.send.await_args.args[0]
    assert message == (
        "Timestamp (UTC): 1970-01-01T00:00:00Z\n"
        "Local time: <t:0:F>\n"
        "Video: https://www.twitch.tv/videos/123456789\n"
        "Error: RuntimeError: token=<redacted>"
    )
    assert "Traceback" not in message
    assert "super-secret" not in message
    assert "also-secret" not in message
    assert "mongodb://user:password@" not in message
    assert len(message) <= 2_000


@pytest.mark.asyncio
async def test_discord_sink_disables_itself_for_an_invalid_channel_without_logging_loop():
    handler = DiscordLogHandler(channel_id=123)
    await handler.start(
        SimpleNamespace(
            get_channel=lambda channel_id: None,
            fetch_channel=AsyncMock(side_effect=RuntimeError("missing channel")),
        )
    )

    assert handler.disabled is True
    record = logging.getLogger("vsummary.tests.discord").makeRecord(
        "vsummary.tests.discord", logging.ERROR, __file__, 0, "later error", (), None
    )
    handler.handle(record)
    assert handler.queue.empty()


@pytest.mark.asyncio
async def test_discord_sink_reports_events_dropped_while_its_queue_is_full():
    release_first_send = asyncio.Event()
    sent = []

    class SlowTarget:
        async def send(self, message):
            sent.append(message)
            if len(sent) == 1:
                await release_first_send.wait()

    handler = DiscordLogHandler(channel_id=123, queue_size=1)
    logger = logging.getLogger("vsummary.tests.discord")
    await handler.start(SimpleNamespace(get_channel=lambda channel_id: SlowTarget()))
    handler.handle(
        logger.makeRecord(logger.name, logging.WARNING, __file__, 0, "first", (), None)
    )
    await asyncio.sleep(0)
    for message in ("queued", "dropped"):
        handler.handle(
            logger.makeRecord(
                logger.name, logging.WARNING, __file__, 0, message, (), None
            )
        )
    release_first_send.set()
    await handler.drain()
    await handler.stop()

    assert any("dropped 1 event(s)" in message for message in sent)
