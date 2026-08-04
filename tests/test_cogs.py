from unittest.mock import AsyncMock, MagicMock

from vsummary.cogs.misc.ping import Ping
from vsummary.cogs.summarizer.summarizer import Summarizer


class TestSendChunkedMessage:
    def _make_interaction(self):
        interaction = MagicMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_single_short_message(self):
        interaction = self._make_interaction()

        await Summarizer.send_chunked_message(interaction, "hello")
        interaction.followup.send.assert_awaited_once_with("hello")

    async def test_long_message_split_on_newline(self):
        interaction = self._make_interaction()
        # 5000 chars, newline every 1000 chars -> should split into 5 chunks.
        text = "\n".join("x" * 1000 for _ in range(5))

        await Summarizer.send_chunked_message(interaction, text)
        calls = [c.args[0] for c in interaction.followup.send.await_args_list]
        # All chunks stay under Discord's 2000-char limit.
        assert all(len(call) <= 2000 for call in calls)
        # Splits on newlines, so each chunk is a full line (1000 chars here).
        assert interaction.followup.send.await_count == 5
        assert all(len(call) == 1000 for call in calls)

    async def test_long_message_without_newlines_falls_back(self):
        interaction = self._make_interaction()
        text = "abcdef" * 400  # 2400 chars, no newlines

        await Summarizer.send_chunked_message(interaction, text)
        assert interaction.followup.send.await_count >= 2
        calls = [c.args[0] for c in interaction.followup.send.await_args_list]
        assert all(len(call) <= 2000 for call in calls)
        assert sum(len(call) for call in calls) == len(text)

    async def test_no_send_for_empty_text(self):
        interaction = self._make_interaction()

        await Summarizer.send_chunked_message(interaction, "")
        interaction.followup.send.assert_not_awaited()


class TestTopicsCommand:
    def _make_cog(self):
        bot = MagicMock()
        bot.notebook_client = MagicMock()
        return Summarizer(bot)

    def _make_interaction(self):
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_replies_with_topic_list(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        topics = [MagicMock(name="alpha"), MagicMock(name="beta")]
        topics[0].name = "alpha"
        topics[1].name = "beta"

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.to_video_url", return_value="url"
        )
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_list",
            AsyncMock(return_value=topics),
        )

        await cog.topics.callback(
            cog, interaction, video="https://youtu.be/dQw4w9WgXcQ"
        )

        interaction.response.defer.assert_awaited_once()
        send = interaction.followup.send
        sent = " ".join(c.args[0] for c in send.await_args_list)
        assert "1: alpha" in sent
        assert "2: beta" in sent

    async def test_invalid_url_sends_error(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()

        from vsummary.util.youtube import InvalidYouTubeInputError

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.to_video_url",
            side_effect=InvalidYouTubeInputError("bad"),
        )

        await cog.topics.callback(cog, interaction, video="bad!")

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with("bad")


class TestPing:
    async def test_ping_reports_latency(self):
        bot = MagicMock()
        bot.latency = 0.05
        cog = Ping(bot)
        interaction = MagicMock()
        interaction.response.send_message = AsyncMock()

        await cog.ping.callback(cog, interaction)

        interaction.response.send_message.assert_awaited_once_with(
            "Pong! Latency: 50ms"
        )


class TestDetailCommand:
    def _make_cog(self):
        bot = MagicMock()
        bot.notebook_client = MagicMock()
        return Summarizer(bot)

    def _make_interaction(self):
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        return interaction

    async def test_single_topic_index_fetches_one_detail(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.to_video_url", return_value="url"
        )
        get_details = mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details",
            AsyncMock(return_value={"topic_name": "alpha", "detail": "some detail"}),
        )

        await cog.detail.callback(cog, interaction, video="dQw4w9WgXcQ", topic_index=2)

        # topic_index is converted to 0-based (2 -> 1).
        get_details.assert_awaited_once_with(cog.bot.notebook_client, "url", 1)
        interaction.followup.send.assert_awaited_once_with("**alpha**\nsome detail")

    async def test_all_topics_when_index_is_none(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.to_video_url", return_value="url"
        )
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details_all",
            AsyncMock(
                return_value=[
                    {"topic_name": "alpha", "detail": "one"},
                    {"topic_name": "beta", "detail": "two"},
                ]
            ),
        )

        await cog.detail.callback(
            cog, interaction, video="dQw4w9WgXcQ", topic_index=None
        )

        interaction.response.defer.assert_awaited_once()
        calls = [c.args[0] for c in interaction.followup.send.await_args_list]
        assert calls == [
            "**1. alpha**\none",
            "**2. beta**\ntwo",
        ]

    async def test_invalid_url_sends_error(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()

        from vsummary.util.youtube import InvalidYouTubeInputError

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.to_video_url",
            side_effect=InvalidYouTubeInputError("bad"),
        )

        await cog.detail.callback(cog, interaction, video="bad!", topic_index=None)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with("bad")

    async def test_invalid_topic_index_sends_message(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.to_video_url", return_value="url"
        )
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details",
            AsyncMock(side_effect=IndexError("bad index")),
        )

        await cog.detail.callback(cog, interaction, video="dQw4w9WgXcQ", topic_index=3)

        interaction.followup.send.assert_awaited_once_with("Invalid topic index: 3")

    async def test_source_add_error_sends_hint(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()

        from notebooklm import SourceAddError

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.to_video_url", return_value="url"
        )
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details_all",
            AsyncMock(side_effect=SourceAddError("http://example.com")),
        )

        await cog.detail.callback(
            cog, interaction, video="dQw4w9WgXcQ", topic_index=None
        )

        interaction.followup.send.assert_awaited_once_with(
            "Please ensure the link is correct and try again."
        )
