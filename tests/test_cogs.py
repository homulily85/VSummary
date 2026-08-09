from unittest.mock import AsyncMock, MagicMock

import discord

from vsummary.cogs.misc.ping import Ping
from vsummary.cogs.summarizer.summarizer import Summarizer, TopicsView
from vsummary.util.video import UnsupportedVideoSource, VideoRef


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

    def _make_topic(self, name):
        topic = MagicMock(name=name)
        topic.name = name
        return topic

    async def test_replies_with_topic_list(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        topics = [self._make_topic("alpha"), self._make_topic("beta")]

        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source", return_value=ref
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

    async def test_replies_with_interactive_view(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        topics = [self._make_topic("alpha"), self._make_topic("beta")]

        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source", return_value=ref
        )
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_list",
            AsyncMock(return_value=topics),
        )

        await cog.topics.callback(
            cog, interaction, video="https://youtu.be/dQw4w9WgXcQ"
        )

        first_call = interaction.followup.send.await_args_list[0]
        view = first_call.kwargs.get("view")
        assert isinstance(view, TopicsView)
        assert isinstance(view.topic_select, discord.ui.Select)
        assert [o.label for o in view.topic_select.options] == ["alpha", "beta"]
        assert [o.value for o in view.topic_select.options] == ["1", "2"]
        assert any(
            isinstance(child, discord.ui.Button) and child.label == "All topics"
            for child in view.children
        )

    async def test_view_disabled_after_completion(self):
        view = TopicsView(
            bot=MagicMock(),
            ref=VideoRef(source="youtube", id="dQw4w9WgXcQ"),
            topics=[self._make_topic("alpha")],
        )

        view.disable_all_components()

        assert view.topic_select.disabled
        for child in view.children:
            assert child.disabled

    async def test_more_than_25_topics_notes_detail_command(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        topics = [self._make_topic(f"topic-{i + 1}") for i in range(30)]

        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source", return_value=ref
        )
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_list",
            AsyncMock(return_value=topics),
        )

        await cog.topics.callback(
            cog, interaction, video="https://youtu.be/dQw4w9WgXcQ"
        )

        view = interaction.followup.send.await_args_list[0].kwargs["view"]
        assert len(view.topic_select.options) == 25
        sent = " ".join(c.args[0] for c in interaction.followup.send.await_args_list)
        assert "use /detail" in sent.lower()

    async def test_25_or_fewer_topics_has_no_note(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        topics = [self._make_topic(f"topic-{i + 1}") for i in range(25)]

        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source", return_value=ref
        )
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_list",
            AsyncMock(return_value=topics),
        )

        await cog.topics.callback(
            cog, interaction, video="https://youtu.be/dQw4w9WgXcQ"
        )

        sent = " ".join(c.args[0] for c in interaction.followup.send.await_args_list)
        assert "use /detail" not in sent


class TestTopicsViewCallbacks:
    def _make_cog(self):
        bot = MagicMock()
        bot.notebook_client = MagicMock()
        return Summarizer(bot)

    def _make_interaction(self):
        interaction = MagicMock()
        interaction.response.defer = AsyncMock()
        interaction.followup.send = AsyncMock()
        interaction.edit_original_response = AsyncMock()
        return interaction

    def _make_view(self, cog, ref, topics):
        return TopicsView(bot=cog.bot, ref=ref, topics=topics)

    def _select_topic(self, view, value):
        """Mark the select as having ``value`` selected, as discord.py would."""
        view.topic_select._values = [value]

    async def test_select_fetches_single_topic_detail(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        topics = [MagicMock(name="alpha"), MagicMock(name="beta")]
        topics[0].name = "alpha"
        topics[1].name = "beta"
        view = self._make_view(cog, ref, topics)

        get_details = mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details",
            AsyncMock(return_value={"topic_name": "beta", "detail": "some detail"}),
        )

        select = view.topic_select
        self._select_topic(view, "2")
        await view.topic_select.callback(interaction)

        # value "2" is 1-based -> 0-based index 1.
        get_details.assert_awaited_once_with(cog.bot.notebook_client, ref, 1)
        interaction.response.defer.assert_awaited_once()
        interaction.edit_original_response.assert_awaited_once_with(view=view)
        interaction.followup.send.assert_awaited_once_with("**beta**\nsome detail")
        assert select.disabled

    async def test_all_button_fetches_all_details(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        topics = [MagicMock(name="alpha"), MagicMock(name="beta")]
        topics[0].name = "alpha"
        topics[1].name = "beta"
        view = self._make_view(cog, ref, topics)

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details_all",
            AsyncMock(
                return_value=[
                    {"topic_name": "alpha", "detail": "one"},
                    {"topic_name": "beta", "detail": "two"},
                ]
            ),
        )

        all_button = next(
            child
            for child in view.children
            if isinstance(child, discord.ui.Button) and child.label == "All topics"
        )
        await all_button.callback(interaction)

        calls = [c.args[0] for c in interaction.followup.send.await_args_list]
        assert calls == ["**alpha**\none", "**beta**\ntwo"]
        interaction.edit_original_response.assert_awaited_once_with(view=view)
        assert all_button.disabled

    async def test_select_sends_invalid_index_message(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        topics = [MagicMock(name="alpha")]
        topics[0].name = "alpha"
        view = self._make_view(cog, ref, topics)

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details",
            AsyncMock(side_effect=IndexError("bad index")),
        )

        self._select_topic(view, "9")
        await view.topic_select.callback(interaction)

        interaction.followup.send.assert_awaited_once_with("Invalid topic index: 9")

    async def test_all_button_sends_source_add_error_message(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        topics = [MagicMock(name="alpha")]
        topics[0].name = "alpha"
        view = self._make_view(cog, ref, topics)

        from notebooklm import SourceAddError

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details_all",
            AsyncMock(side_effect=SourceAddError("http://example.com")),
        )

        all_button = next(
            child
            for child in view.children
            if isinstance(child, discord.ui.Button) and child.label == "All topics"
        )
        await all_button.callback(interaction)

        interaction.followup.send.assert_awaited_once_with(
            "Provided link or id is invalid or no transcript available."
        )

    async def test_select_sends_invalid_video_message(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()
        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        topics = [MagicMock(name="alpha")]
        topics[0].name = "alpha"
        view = self._make_view(cog, ref, topics)

        from notebooklm import SourceAddError

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details",
            AsyncMock(side_effect=SourceAddError("http://example.com")),
        )

        self._select_topic(view, "1")
        await view.topic_select.callback(interaction)

        interaction.followup.send.assert_awaited_once_with(
            "Provided link or id is invalid or no transcript available."
        )

    async def test_invalid_url_sends_error(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source",
            side_effect=UnsupportedVideoSource("bad"),
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

        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source", return_value=ref
        )
        get_details = mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details",
            AsyncMock(return_value={"topic_name": "alpha", "detail": "some detail"}),
        )

        await cog.detail.callback(cog, interaction, video="dQw4w9WgXcQ", topic_index=2)

        # topic_index is converted to 0-based (2 -> 1).
        get_details.assert_awaited_once_with(cog.bot.notebook_client, ref, 1)
        interaction.followup.send.assert_awaited_once_with("**alpha**\nsome detail")

    async def test_all_topics_when_index_is_none(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()

        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source", return_value=ref
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

        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source",
            side_effect=UnsupportedVideoSource("bad"),
        )

        await cog.detail.callback(cog, interaction, video="bad!", topic_index=None)

        interaction.response.defer.assert_awaited_once()
        interaction.followup.send.assert_awaited_once_with("bad")

    async def test_invalid_topic_index_sends_message(self, mocker):
        cog = self._make_cog()
        interaction = self._make_interaction()

        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source", return_value=ref
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

        ref = VideoRef(source="youtube", id="dQw4w9WgXcQ")
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.parse_video_source", return_value=ref
        )
        mocker.patch(
            "vsummary.cogs.summarizer.summarizer.get_topic_details_all",
            AsyncMock(side_effect=SourceAddError("http://example.com")),
        )

        await cog.detail.callback(
            cog, interaction, video="dQw4w9WgXcQ", topic_index=None
        )

        interaction.followup.send.assert_awaited_once_with(
            "Provided link or id is invalid or no transcript available."
        )
