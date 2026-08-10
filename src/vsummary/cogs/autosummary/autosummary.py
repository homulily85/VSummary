import logging
import os
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from vsummary.model.channel import Channel, PendingVideo, PendingVideoStatus
from vsummary.util.holodex import (
    MAX_RETRIES,
    ChannelNotFoundError,
    HolodexClient,
    HolodexVideo,
    exponential_backoff_hours,
    is_ignored,
    is_transcript_ready,
)
from vsummary.util.summarizer import get_topic_details_all
from vsummary.util.video import VideoRef

logger = logging.getLogger(__name__)


class Autosummary(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.holodex = HolodexClient()
        self.notebook = None
        self.poll_loop.start()

    def cog_unload(self):
        self.poll_loop.cancel()

    async def cog_load(self):
        self.notebook = self.bot.notebook_client

    @tasks.loop(minutes=30)
    async def poll_loop(self):
        await self.check_new_videos()
        await self.process_due_videos()

    async def check_new_videos(self):
        channels = await Channel.find().to_list()
        for channel in channels:
            videos = await self.holodex.get_channel_videos(channel.channel_id)
            for video in videos:
                await self._enqueue_new_video(video, channel)

    async def _enqueue_new_video(
        self,
        video: HolodexVideo,
        channel: Channel,
    ):
        if is_ignored(video.topic_id):
            logger.info(f"Skipping ignored video {video.id} (topic {video.topic_id})")
            return

        existing = await PendingVideo.find_one(
            PendingVideo.video_id == video.id,
        )
        if existing:
            return

        ready = is_transcript_ready(video.available_at)
        next_attempt = (
            datetime.now(UTC) if ready else video.available_at + timedelta(hours=2)
        )
        pending = PendingVideo(
            video_id=video.id,
            channel_id=channel.channel_id,
            channel_name=channel.name,
            title=video.title,
            available_at=video.available_at,
            next_attempt_at=next_attempt,
        )
        await pending.save()
        logger.info(f"Queued video {video.id} for auto-summary.")

    async def process_due_videos(self):
        due = await PendingVideo.find(
            PendingVideo.status == PendingVideoStatus.QUEUED,
            PendingVideo.next_attempt_at <= datetime.now(UTC),
        ).to_list()
        for item in due:
            await self._process_pending_video(item)

    async def _process_pending_video(self, item: PendingVideo):
        ref = VideoRef(source="youtube", id=item.video_id)
        try:
            details = await get_topic_details_all(self.notebook, ref)
        except Exception as exc:  # noqa: BLE001 - treat any failure as retryable
            logger.error(f"Unexpected error summarizing video {item.video_id}: {exc}")
            await self._handle_source_error(item)
            return

        item.status = PendingVideoStatus.DONE
        await item.save()
        await self._post_summary(item, details)

    async def _handle_source_error(self, item: PendingVideo):
        item.retry_count += 1
        if item.retry_count >= MAX_RETRIES:
            item.status = PendingVideoStatus.FAILED
            await item.save()
            await self._post_failure(item)
            return
        backoff = exponential_backoff_hours(item.retry_count)
        item.next_attempt_at = datetime.now(UTC) + timedelta(hours=backoff)
        item.status = PendingVideoStatus.QUEUED
        await item.save()
        logger.info(
            f"Transcript unavailable for video {item.video_id}; "
            f"retrying in {backoff} hour(s) (attempt {item.retry_count})."
        )

    async def _post_summary(self, item: PendingVideo, details: list[dict]):
        channel = await self._get_auto_summary_channel()
        if channel is None:
            return
        header = f"**Video:** {item.title}\n**Channel:** {item.channel_name}"
        for detail in details:
            header += f"\n\n**{detail['topic_name']}**\n{detail['detail']}"
        await self._send_to_channel(channel, header)

    async def _post_failure(self, item: PendingVideo):
        channel = await self._get_auto_summary_channel()
        if channel is None:
            return
        message = (
            f"**Video:** {item.title}\n**Channel:** {item.channel_name}\n\n"
            f"Failed to generate a summary after {MAX_RETRIES} attempts; "
            f"no transcript could be fetched for this video."
        )
        await channel.send(message)

    async def _get_auto_summary_channel(self):
        channel_id = os.getenv("AUTO_SUMMARY_CHANNEL_ID")
        if not channel_id:
            logger.error(
                "AUTO_SUMMARY_CHANNEL_ID is not set; skipping auto-summary post."
            )
            return None
        try:
            channel = self.bot.get_channel(int(channel_id))
        except ValueError:
            logger.error("AUTO_SUMMARY_CHANNEL_ID is not a valid integer.")
            return None
        if channel is None:
            logger.error(f"Auto-summary channel {channel_id} not found; skipping post.")
            return None
        return channel

    async def _send_to_channel(self, channel, text: str):
        if len(text) <= 2000:
            await channel.send(text)
            return
        while len(text) > 2000:
            split_index = text.rfind("\n", 0, 2000)
            if split_index == -1:
                split_index = text.rfind(".", 0, 2000)
                if split_index == -1:
                    split_index = 2000
            await channel.send(text[:split_index])
            text = text[split_index:].lstrip()
        if text:
            await channel.send(text)

    @app_commands.command(
        name="addchannel",
        description="Add a Vtuber channel to follow for automatic summaries.",
    )
    @app_commands.describe(
        channel_id="The Holodex/YouTube channel ID (e.g. UCQ0UDLQCjY0rmuxCDE38FGg)."
    )
    async def addchannel(
        self,
        interaction: discord.Interaction,
        channel_id: str,
    ):
        await interaction.response.defer(thinking=True)
        try:
            channel = await self.holodex.get_channel(channel_id)
        except ChannelNotFoundError:
            await interaction.followup.send(
                f"Channel '{channel_id}' was not found on Holodex."
            )
            return

        if channel is None:
            await interaction.followup.send(
                f"An error occurred while fetching channel '{channel_id}' from Holodex."
            )
            return

        existing = await Channel.find_one(Channel.channel_id == channel_id)
        if existing:
            await interaction.followup.send(
                f"Channel **{channel.name}** (`{channel_id}`) is already being followed."
            )
            return

        record = Channel(
            channel_id=channel_id,
            name=channel.name,
        )
        await record.save()
        await interaction.followup.send(
            f"Added channel **{channel.name}** (`{channel_id}`) for automatic summaries."
        )

    @app_commands.command(
        name="removechannel",
        description="Stop following a channel for automatic summaries.",
    )
    @app_commands.describe(
        channel_id="The Holodex/YouTube channel ID to stop following."
    )
    async def removechannel(
        self,
        interaction: discord.Interaction,
        channel_id: str,
    ):
        await interaction.response.defer(thinking=True)
        result = await Channel.find_one(Channel.channel_id == channel_id)
        if result is None:
            await interaction.followup.send(
                f"Channel '{channel_id}' is not being followed."
            )
            return
        await result.delete()
        await PendingVideo.delete_one(PendingVideo.channel_id == channel_id)
        await interaction.followup.send(
            f"Removed channel '{channel_id}' and its pending videos."
        )

    @app_commands.command(
        name="listchannels",
        description="List channels followed for automatic summaries.",
    )
    async def listchannels(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        channels = await Channel.find().to_list()
        if not channels:
            await interaction.followup.send(
                "No channels are currently followed for automatic summaries."
            )
            return
        lines = [f"**{channel.name}** (`{channel.channel_id}`)" for channel in channels]
        await interaction.followup.send("Followed channels:\n" + "\n".join(lines))


async def setup(bot):
    await bot.add_cog(Autosummary(bot))
