import re
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils import send_log, truncate

NO_REASON = "No reason provided"
MAX_TIMEOUT = timedelta(days=28)
DURATION_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
DURATION_RE = re.compile(r"(\d+)\s*([smhdw])", re.IGNORECASE)


def parse_duration(text: str) -> timedelta | None:
    """Parse strings like '10m', '2h30m', '1d' into a timedelta."""
    text = text.strip()
    matches = DURATION_RE.findall(text)
    # Reject leftovers like '10x' or plain '10' so typos don't silently become something else.
    if not matches or DURATION_RE.sub("", text).strip():
        return None
    seconds = sum(int(amount) * DURATION_UNITS[unit.lower()] for amount, unit in matches)
    return timedelta(seconds=seconds) if seconds > 0 else None


def format_duration(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    parts = []
    for name, size in (("d", 86400), ("h", 3600), ("m", 60), ("s", 1)):
        value, seconds = divmod(seconds, size)
        if value:
            parts.append(f"{value}{name}")
    return " ".join(parts)


def can_act(interaction: discord.Interaction, target: discord.Member, check_bot: bool = True) -> str | None:
    """Return an error message if the moderator or bot may not act on target, else None."""
    guild = interaction.guild
    moderator = interaction.user
    if target.id == moderator.id:
        return "You can't use this on yourself."
    if target.id == guild.me.id:
        return "I can't use this on myself."
    if target.id == guild.owner_id:
        return "You can't use this on the server owner."
    if moderator.id != guild.owner_id and target.top_role >= moderator.top_role:
        return "That member's top role is the same as or higher than yours."
    if check_bot and target.top_role >= guild.me.top_role:
        return "That member's top role is the same as or higher than mine. Move my role above theirs."
    return None


async def try_dm(user: discord.abc.User, text: str):
    try:
        await user.send(text)
    except discord.HTTPException:
        pass


async def send_modlog(
    guild: discord.Guild,
    action: str,
    target: discord.abc.User,
    moderator: discord.abc.User,
    reason: str,
    color: discord.Color,
    **extra: str,
):
    embed = discord.Embed(title=action, color=color, timestamp=discord.utils.utcnow())
    embed.add_field(name="User", value=f"{target.mention} (`{target.id}`)")
    embed.add_field(name="Moderator", value=moderator.mention)
    for name, value in extra.items():
        embed.add_field(name=name.replace("_", " ").title(), value=value)
    embed.add_field(name="Reason", value=truncate(reason), inline=False)
    await send_log(guild, "modlog_channel_id", embed)


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(description="Kick a member from the server.")
    @app_commands.guild_only()
    @app_commands.default_permissions(kick_members=True)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(self, interaction: discord.Interaction, member: discord.Member, reason: str = NO_REASON):
        if error := can_act(interaction, member):
            return await interaction.response.send_message(error, ephemeral=True)

        await try_dm(member, f"You were kicked from **{interaction.guild.name}**. Reason: {reason}")
        await member.kick(reason=f"{interaction.user}: {reason}")
        await interaction.response.send_message(f"Kicked {member.mention}. Reason: {reason}")
        await send_modlog(interaction.guild, "Member kicked", member, interaction.user, reason, discord.Color.orange())

    @app_commands.command(description="Ban a user (they don't need to be in the server).")
    @app_commands.describe(delete_days="Delete this many days of their recent messages (0-7)")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = NO_REASON,
        delete_days: app_commands.Range[int, 0, 7] = 0,
    ):
        member = interaction.guild.get_member(user.id)
        if member and (error := can_act(interaction, member)):
            return await interaction.response.send_message(error, ephemeral=True)

        if member:
            await try_dm(member, f"You were banned from **{interaction.guild.name}**. Reason: {reason}")
        await interaction.guild.ban(
            user,
            reason=f"{interaction.user}: {reason}",
            delete_message_seconds=delete_days * 86400,
        )
        await interaction.response.send_message(f"Banned {user.mention}. Reason: {reason}")
        await send_modlog(interaction.guild, "User banned", user, interaction.user, reason, discord.Color.red())

    @app_commands.command(description="Unban a user by their ID.")
    @app_commands.describe(user_id="The ID of the banned user")
    @app_commands.guild_only()
    @app_commands.default_permissions(ban_members=True)
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str = NO_REASON):
        if not user_id.isdigit():
            return await interaction.response.send_message("That isn't a valid user ID.", ephemeral=True)
        try:
            ban = await interaction.guild.fetch_ban(discord.Object(id=int(user_id)))
        except discord.NotFound:
            return await interaction.response.send_message("That user isn't banned.", ephemeral=True)

        await interaction.guild.unban(ban.user, reason=f"{interaction.user}: {reason}")
        await interaction.response.send_message(f"Unbanned {ban.user.mention}. Reason: {reason}")
        await send_modlog(interaction.guild, "User unbanned", ban.user, interaction.user, reason, discord.Color.green())

    @app_commands.command(description="Time out a member so they can't chat or join voice.")
    @app_commands.describe(duration="How long, e.g. 10m, 2h, 1d, 1h30m (max 28d)")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def timeout(
        self, interaction: discord.Interaction, member: discord.Member, duration: str, reason: str = NO_REASON
    ):
        if error := can_act(interaction, member):
            return await interaction.response.send_message(error, ephemeral=True)
        delta = parse_duration(duration)
        if delta is None:
            return await interaction.response.send_message(
                "Invalid duration. Use something like `10m`, `2h`, `1d` or `1h30m`.", ephemeral=True
            )
        if delta > MAX_TIMEOUT:
            return await interaction.response.send_message("Timeouts can be at most 28 days.", ephemeral=True)

        await member.timeout(delta, reason=f"{interaction.user}: {reason}")
        pretty = format_duration(delta)
        await interaction.response.send_message(f"Timed out {member.mention} for {pretty}. Reason: {reason}")
        await send_modlog(
            interaction.guild, "Member timed out", member, interaction.user, reason,
            discord.Color.orange(), duration=pretty,
        )

    @app_commands.command(description="Remove a member's timeout.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    async def untimeout(self, interaction: discord.Interaction, member: discord.Member, reason: str = NO_REASON):
        if not member.is_timed_out():
            return await interaction.response.send_message("That member isn't timed out.", ephemeral=True)
        if error := can_act(interaction, member):
            return await interaction.response.send_message(error, ephemeral=True)

        await member.timeout(None, reason=f"{interaction.user}: {reason}")
        await interaction.response.send_message(f"Removed timeout from {member.mention}.")
        await send_modlog(interaction.guild, "Timeout removed", member, interaction.user, reason, discord.Color.green())

    @app_commands.command(description="Give a member a warning.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(self, interaction: discord.Interaction, member: discord.Member, reason: str):
        if member.bot:
            return await interaction.response.send_message("You can't warn bots.", ephemeral=True)
        # Warnings are stored in the DB only, so the bot's own role position doesn't matter.
        if error := can_act(interaction, member, check_bot=False):
            return await interaction.response.send_message(error, ephemeral=True)

        warn_id = await db.add_warning(interaction.guild.id, member.id, interaction.user.id, reason)
        count = len(await db.list_warnings(interaction.guild.id, member.id))
        await try_dm(member, f"You were warned in **{interaction.guild.name}**. Reason: {reason}")
        await interaction.response.send_message(
            f"Warned {member.mention} (warning #{warn_id}, {count} total). Reason: {reason}"
        )
        await send_modlog(
            interaction.guild, "Member warned", member, interaction.user, reason,
            discord.Color.yellow(), warning_id=str(warn_id), total_warnings=str(count),
        )

    @app_commands.command(description="List a member's warnings.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings(self, interaction: discord.Interaction, member: discord.Member):
        rows = await db.list_warnings(interaction.guild.id, member.id)
        if not rows:
            return await interaction.response.send_message(f"{member.mention} has no warnings.", ephemeral=True)

        embed = discord.Embed(title=f"Warnings for {member}", color=discord.Color.yellow())
        # Embeds allow 25 fields; show the most recent ones.
        for row in rows[-25:]:
            created = discord.utils.format_dt(discord.utils.parse_time(row["created_at"]), "d")
            embed.add_field(
                name=f"#{row['id']} - {created}",
                value=truncate(f"{row['reason']}\n*by <@{row['moderator_id']}>*"),
                inline=False,
            )
        embed.set_footer(text=f"{len(rows)} total")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(description="Delete a single warning by its ID.")
    @app_commands.describe(warning_id="The warning number shown in /warnings")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def delwarn(self, interaction: discord.Interaction, warning_id: int):
        if await db.delete_warning(interaction.guild.id, warning_id):
            await interaction.response.send_message(f"Deleted warning #{warning_id}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"No warning #{warning_id} in this server.", ephemeral=True)

    @app_commands.command(description="Remove all of a member's warnings.")
    @app_commands.guild_only()
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.checks.has_permissions(moderate_members=True)
    async def clearwarns(self, interaction: discord.Interaction, member: discord.Member):
        count = await db.clear_warnings(interaction.guild.id, member.id)
        await interaction.response.send_message(f"Cleared {count} warning(s) for {member.mention}.", ephemeral=True)
        if count:
            await send_modlog(
                interaction.guild, "Warnings cleared", member, interaction.user, NO_REASON,
                discord.Color.green(), cleared=str(count),
            )

    @app_commands.command(description="Bulk delete recent messages in this channel.")
    @app_commands.describe(
        amount="How many recent messages to scan (1-100)",
        user="Only delete messages from this user",
    )
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True, read_message_history=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[int, 1, 100],
        user: discord.User | None = None,
    ):
        await interaction.response.defer(ephemeral=True, thinking=True)
        check = (lambda m: m.author.id == user.id) if user else (lambda m: True)
        deleted = await interaction.channel.purge(limit=amount, check=check, reason=f"Purge by {interaction.user}")
        await interaction.followup.send(f"Deleted {len(deleted)} message(s).", ephemeral=True)

        target = user or interaction.user
        await send_modlog(
            interaction.guild, "Messages purged", target, interaction.user, NO_REASON,
            discord.Color.blurple(), channel=interaction.channel.mention, deleted=str(len(deleted)),
        )

    @app_commands.command(description="Set slowmode for a channel (0 to disable).")
    @app_commands.describe(seconds="Seconds between messages (0-21600)", channel="Defaults to this channel")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_channels=True)
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        seconds: app_commands.Range[int, 0, 21600],
        channel: discord.TextChannel | None = None,
    ):
        channel = channel or interaction.channel
        await channel.edit(slowmode_delay=seconds, reason=f"Slowmode by {interaction.user}")
        if seconds:
            await interaction.response.send_message(f"Slowmode in {channel.mention} set to {seconds}s.")
        else:
            await interaction.response.send_message(f"Slowmode disabled in {channel.mention}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
