"""Test the Discord command for the active NotebookLM account's quota."""

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from notebooklm import NotebookLMError
from notebooklm.types import (
    UsageSummary,
    UsageSummaryStatus,
    UsageWindow,
    UsageWindowKind,
)

from vsummary.bot import Bot


async def invoke_quota(get_usage):
    notebook_client = SimpleNamespace(settings=SimpleNamespace(get_usage=get_usage))
    holodex = SimpleNamespace(aclose=AsyncMock())
    response = SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock())
    followup = SimpleNamespace(send=AsyncMock())

    async with Bot(notebook_client=notebook_client, holodex=holodex) as bot:
        await bot.load_extension("vsummary.cogs.misc.quota")
        command = bot.tree.get_command("quota")
        assert command is not None
        assert command.parameters == []
        assert command.default_permissions is None
        await command.callback(
            command.binding, SimpleNamespace(response=response, followup=followup)
        )

    get_usage.assert_awaited_once_with()
    response.defer.assert_not_awaited()
    followup.send.assert_not_awaited()
    response.send_message.assert_awaited_once()
    reply = response.send_message.await_args
    assert not reply.kwargs.get("ephemeral", False)
    return reply.args[0]


async def test_quota_shows_both_windows_and_their_server_reset_times():
    five_hour_reset = datetime(2026, 11, 14, 18, tzinfo=UTC)
    weekly_reset = datetime(2026, 11, 16, 2, tzinfo=UTC)
    usage = UsageSummary(
        status=UsageSummaryStatus.READY,
        windows=(
            UsageWindow(UsageWindowKind.WEEKLY, 100, 0, weekly_reset),
            UsageWindow(UsageWindowKind.FIVE_HOUR, 12.3456, 87.6544, five_hour_reset),
        ),
    )

    result = await invoke_quota(AsyncMock(return_value=usage))

    assert result.splitlines() == [
        "**NotebookLM quota**",
        f"**5h** · 12.35% used · 87.65% remaining · Resets <t:{int(five_hour_reset.timestamp())}:F>",
        f"**7d** · 100.00% used · 0.00% remaining · Resets <t:{int(weekly_reset.timestamp())}:F>",
    ]


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (
            UsageSummaryStatus.DISABLED,
            "The current NotebookLM account does not have quota metering enabled.",
        ),
        (
            UsageSummaryStatus.SKIPPED,
            "NotebookLM quota is temporarily unavailable. Please try again.",
        ),
    ],
)
async def test_quota_handles_unavailable_snapshots(status, message):
    assert await invoke_quota(AsyncMock(return_value=UsageSummary(status))) == message


async def test_quota_handles_sdk_errors_without_exposing_details(caplog):
    error = NotebookLMError("private error details")
    result = await invoke_quota(AsyncMock(side_effect=error))

    assert result == "NotebookLM quota is temporarily unavailable. Please try again."
    assert "NotebookLMError" in caplog.text
    assert "private error details" not in caplog.text


async def test_quota_cancels_slow_lookup():
    cancelled = asyncio.Event()

    async def slow_lookup():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async with asyncio.timeout(3):
        result = await invoke_quota(AsyncMock(side_effect=slow_lookup))

    assert cancelled.is_set()
    assert result == "NotebookLM quota is temporarily unavailable. Please try again."
