import asyncio
import logging
from collections import deque
from dataclasses import dataclass, field

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from utils import truncate

log = logging.getLogger(__name__)

MAX_QUEUE = 100
QUEUE_PAGE = 10
# Seconds to wait before leaving a voice channel that has no listeners.
IDLE_LEAVE_DELAY = 30

# Searching only needs title/duration/link, so skip resolving formats (much faster).
SEARCH_OPTS = {"quiet": True, "no_warnings": True, "noplaylist": True, "extract_flat": "in_playlist"}
STREAM_OPTS = {"quiet": True, "no_warnings": True, "noplaylist": True, "format": "bestaudio/best"}
FFMPEG_OPTS = {
    # Stream URLs can drop mid-song; let ffmpeg reconnect instead of ending the track.
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


@dataclass
class Track:
    title: str
    url: str
    duration: int | None
    requester: discord.abc.User


@dataclass
class GuildPlayer:
    queue: deque[Track] = field(default_factory=deque)
    current: Track | None = None
    text_channel: discord.abc.Messageable | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def format_time(seconds: int | None) -> str:
    if seconds is None:
        return "live"
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def track_line(track: Track) -> str:
    return f"[{truncate(track.title, 80)}]({track.url}) `{format_time(track.duration)}`"


def search_youtube(query: str) -> dict | None:
    """Return info for a YouTube URL, or the top search result for anything else. Blocking."""
    target = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(SEARCH_OPTS) as ydl:
        info = ydl.extract_info(target, download=False)
    if info and "entries" in info:
        info = next(iter(info["entries"]), None)
    return info


def get_stream_url(url: str) -> str:
    """Resolve a playable audio URL. Blocking. Done right before playing since these URLs expire."""
    with yt_dlp.YoutubeDL(STREAM_OPTS) as ydl:
        return ydl.extract_info(url, download=False)["url"]


class Music(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.players: dict[int, GuildPlayer] = {}

    async def cog_unload(self):
        for vc in list(self.bot.voice_clients):
            await vc.disconnect(force=True)
        self.players.clear()

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        return self.players.setdefault(guild.id, GuildPlayer())

    def voice_error(self, interaction: discord.Interaction) -> str | None:
        """Return an error if the user can't control playback right now, else None."""
        vc = interaction.guild.voice_client
        if vc is None:
            return "I'm not in a voice channel."
        if not interaction.user.voice or interaction.user.voice.channel != vc.channel:
            return f"You need to be in {vc.channel.mention} to do that."
        return None

    async def play_next(self, guild: discord.Guild):
        player = self.players.get(guild.id)
        vc = guild.voice_client
        if player is None or vc is None or not vc.is_connected():
            return

        async with player.lock:
            if vc.is_playing() or vc.is_paused():
                return
            player.current = None
            while player.queue:
                track = player.queue.popleft()
                try:
                    stream_url = await asyncio.to_thread(get_stream_url, track.url)
                except Exception:
                    log.exception("Failed to load %s", track.url)
                    await self.announce(player, f"Couldn't play **{track.title}**, skipping it.")
                    continue
                if not vc.is_connected():
                    return
                player.current = track
                vc.play(discord.FFmpegPCMAudio(stream_url, **FFMPEG_OPTS), after=lambda e: self.on_track_end(guild, e))
                await self.announce(player, f"Now playing {track_line(track)} (requested by {track.requester.mention})")
                return

    def on_track_end(self, guild: discord.Guild, error: Exception | None):
        # Called from the voice thread, so hop back onto the event loop.
        if error:
            log.error("Player error in guild %s: %s", guild.id, error)
        asyncio.run_coroutine_threadsafe(self.play_next(guild), self.bot.loop)

    async def announce(self, player: GuildPlayer, text: str):
        if player.text_channel is None:
            return
        try:
            await player.text_channel.send(text, allowed_mentions=discord.AllowedMentions.none(), suppress_embeds=True)
        except discord.HTTPException:
            pass

    @app_commands.command(description="Search YouTube and play the closest match (or add it to the queue).")
    @app_commands.describe(query="Song name or YouTube link")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str):
        await self.enqueue(interaction, query)

    @app_commands.command(description="Search YouTube and add the closest match to the queue.")
    @app_commands.describe(query="Song name or YouTube link")
    @app_commands.guild_only()
    async def add(self, interaction: discord.Interaction, query: str):
        await self.enqueue(interaction, query)

    async def enqueue(self, interaction: discord.Interaction, query: str):
        if not interaction.user.voice or interaction.user.voice.channel is None:
            return await interaction.response.send_message("Join a voice channel first.", ephemeral=True)
        channel = interaction.user.voice.channel
        vc = interaction.guild.voice_client
        player = self.get_player(interaction.guild)
        busy = vc is not None and (vc.is_playing() or vc.is_paused() or player.queue)
        if vc is not None and vc.channel != channel and busy:
            return await interaction.response.send_message(
                f"I'm already playing in {vc.channel.mention}.", ephemeral=True
            )
        if len(player.queue) >= MAX_QUEUE:
            return await interaction.response.send_message(f"The queue is full ({MAX_QUEUE} songs).", ephemeral=True)
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.connect and perms.speak):
            return await interaction.response.send_message(
                f"I need Connect and Speak permissions in {channel.mention}.", ephemeral=True
            )

        # Searching takes a few seconds, longer than Discord's 3s reply window.
        await interaction.response.defer(thinking=True)
        try:
            info = await asyncio.to_thread(search_youtube, query)
        except yt_dlp.utils.DownloadError as e:
            log.warning("Search failed for %r: %s", query, e)
            info = None
        if not info:
            return await interaction.followup.send(f"No results found for **{truncate(query, 100)}**.")

        track = Track(
            title=info.get("title") or "Unknown title",
            url=info.get("webpage_url") or info.get("url"),
            duration=info.get("duration"),
            requester=interaction.user,
        )

        # Re-check: another /play may have connected while we were searching.
        vc = interaction.guild.voice_client
        if vc is None:
            vc = await channel.connect(self_deaf=True)
        elif vc.channel != channel:
            await vc.move_to(channel)

        player.text_channel = interaction.channel
        player.queue.append(track)
        if vc.is_playing() or vc.is_paused() or player.lock.locked():
            await interaction.followup.send(f"Added {track_line(track)} to the queue at position **{len(player.queue)}**.", suppress_embeds=True)
        else:
            await interaction.followup.send(f"Found {track_line(track)}", suppress_embeds=True)
            await self.play_next(interaction.guild)

    @app_commands.command(description="Show the current song and the queue.")
    @app_commands.describe(page="Page of the queue to show")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1] = 1):
        player = self.players.get(interaction.guild.id)
        if player is None or (player.current is None and not player.queue):
            return await interaction.response.send_message("The queue is empty.", ephemeral=True)

        pages = max(1, -(-len(player.queue) // QUEUE_PAGE))
        page = min(page, pages)
        start = (page - 1) * QUEUE_PAGE
        lines = [
            f"`{i}.` {track_line(track)} · {track.requester.mention}"
            for i, track in enumerate(list(player.queue)[start : start + QUEUE_PAGE], start=start + 1)
        ]

        embed = discord.Embed(title="Music queue", color=discord.Color.blurple())
        vc = interaction.guild.voice_client
        if player.current:
            status = " (paused)" if vc and vc.is_paused() else ""
            embed.add_field(name=f"Now playing{status}", value=track_line(player.current), inline=False)
        embed.add_field(name="Up next", value=truncate("\n".join(lines) or "Nothing queued."), inline=False)
        known = [t.duration for t in player.queue if t.duration]
        embed.set_footer(text=f"{len(player.queue)} queued · {format_time(sum(known))} total · page {page}/{pages}")
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Remove a song from the queue by its position.")
    @app_commands.describe(position="Position shown in /queue")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1]):
        if error := self.voice_error(interaction):
            return await interaction.response.send_message(error, ephemeral=True)
        player = self.get_player(interaction.guild)
        if position > len(player.queue):
            return await interaction.response.send_message(
                f"There's no song at position {position}. The queue has {len(player.queue)}.", ephemeral=True
            )
        track = player.queue[position - 1]
        del player.queue[position - 1]
        await interaction.response.send_message(f"Removed {track_line(track)} from the queue.", suppress_embeds=True)

    @app_commands.command(description="Pause the current song.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction):
        if error := self.voice_error(interaction):
            return await interaction.response.send_message(error, ephemeral=True)
        vc = interaction.guild.voice_client
        if not vc.is_playing():
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        vc.pause()
        await interaction.response.send_message("Paused. Use `/resume` to continue.")

    @app_commands.command(description="Continue playing a paused song.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction):
        if error := self.voice_error(interaction):
            return await interaction.response.send_message(error, ephemeral=True)
        vc = interaction.guild.voice_client
        if not vc.is_paused():
            return await interaction.response.send_message("Nothing is paused.", ephemeral=True)
        vc.resume()
        await interaction.response.send_message("Resumed.")

    @app_commands.command(description="Skip to the next song in the queue.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction):
        if error := self.voice_error(interaction):
            return await interaction.response.send_message(error, ephemeral=True)
        vc = interaction.guild.voice_client
        if not (vc.is_playing() or vc.is_paused()):
            return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
        player = self.get_player(interaction.guild)
        skipped = player.current
        # Stopping fires the after-callback, which starts the next song.
        vc.stop()
        name = f"**{skipped.title}**" if skipped else "the current song"
        await interaction.response.send_message(f"Skipped {name}.")

    @app_commands.command(description="Stop playing, clear the queue and leave the voice channel.")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction):
        if error := self.voice_error(interaction):
            return await interaction.response.send_message(error, ephemeral=True)
        self.players.pop(interaction.guild.id, None)
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("Stopped and cleared the queue.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        guild = member.guild
        if member.id == self.bot.user.id:
            if after.channel is None:
                # Disconnected (by /stop, a moderator, or a dropped connection).
                self.players.pop(guild.id, None)
            return

        vc = guild.voice_client
        if vc is None or before.channel != vc.channel or after.channel == vc.channel:
            return
        if any(not m.bot for m in vc.channel.members):
            return
        await asyncio.sleep(IDLE_LEAVE_DELAY)
        vc = guild.voice_client
        if vc is not None and not any(not m.bot for m in vc.channel.members):
            player = self.players.get(guild.id)
            if player:
                await self.announce(player, "Everyone left the voice channel, so I stopped the music.")
            await vc.disconnect()


async def setup(bot: commands.Bot):
    await bot.add_cog(Music(bot))
