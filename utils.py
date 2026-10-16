"""Helpers shared between cogs."""

import discord

import db


def truncate(text: str, limit: int = 1024) -> str:
    """Fit text into an embed field (max 1024 chars)."""
    if not text:
        return "*(empty)*"
    return text if len(text) <= limit else text[: limit - 3] + "..."


async def send_log(guild: discord.Guild, field: str, embed: discord.Embed):
    """Send an embed to the channel configured under `field` (log_channel_id / modlog_channel_id)."""
    config = await db.get_config(guild.id)
    channel_id = config.get(field)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel is None:
        return
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        # Missing access or channel deleted; logging must never break the caller.
        pass
