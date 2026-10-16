import discord
from discord import app_commands
from discord.ext import commands

import db
from utils import send_log, truncate

LOG = "log_channel_id"


def user_embed(title: str, user: discord.abc.User, color: discord.Color) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=discord.utils.utcnow())
    embed.set_author(name=str(user), icon_url=user.display_avatar.url)
    embed.set_footer(text=f"User ID: {user.id}")
    return embed


class Logs(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ---------- /config ----------

    config = app_commands.Group(
        name="config",
        description="Configure the bot for this server.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    async def _set_channel(
        self, interaction: discord.Interaction, field: str, label: str, channel: discord.TextChannel | None
    ):
        if channel is not None:
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                return await interaction.response.send_message(
                    f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True
                )
        await db.set_config(interaction.guild.id, field, channel.id if channel else None)
        if channel:
            await interaction.response.send_message(f"{label} will be sent to {channel.mention}.", ephemeral=True)
        else:
            await interaction.response.send_message(f"{label} disabled.", ephemeral=True)

    @config.command(name="logs", description="Set the channel for server event logs (leave empty to disable).")
    async def config_logs(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await self._set_channel(interaction, "log_channel_id", "Server logs", channel)

    @config.command(name="modlog", description="Set the channel for moderation logs (leave empty to disable).")
    async def config_modlog(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        await self._set_channel(interaction, "modlog_channel_id", "Moderation logs", channel)

    @config.command(name="show", description="Show the current configuration.")
    async def config_show(self, interaction: discord.Interaction):
        cfg = await db.get_config(interaction.guild.id)

        def fmt(channel_id):
            return f"<#{channel_id}>" if channel_id else "*not set*"

        embed = discord.Embed(title="Server configuration", color=discord.Color.blurple())
        embed.add_field(name="Server logs", value=fmt(cfg["log_channel_id"]))
        embed.add_field(name="Moderation logs", value=fmt(cfg["modlog_channel_id"]))

        def fmt_role(role_id):
            return f"<@&{role_id}>" if role_id else "*not set*"

        embed.add_field(name="Auto-role (humans)", value=fmt_role(cfg["autorole_human_id"]))
        embed.add_field(name="Auto-role (bots)", value=fmt_role(cfg["autorole_bot_id"]))
        embed.add_field(name="Sticky roles", value="On" if cfg["sticky_roles"] else "Off")
        embed.add_field(name="Role requests", value=fmt(cfg["rolerequest_channel_id"]))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ---------- Messages ----------

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        embed = user_embed("Message deleted", message.author, discord.Color.red())
        embed.add_field(name="Channel", value=message.channel.mention)
        embed.add_field(name="Content", value=truncate(message.content), inline=False)
        if message.attachments:
            embed.add_field(
                name="Attachments",
                value=truncate("\n".join(a.filename for a in message.attachments)),
                inline=False,
            )
        await send_log(message.guild, LOG, embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        # Embeds unfurling also fires this event without a content change.
        if after.guild is None or after.author.bot or before.content == after.content:
            return
        embed = user_embed("Message edited", after.author, discord.Color.gold())
        embed.add_field(name="Channel", value=after.channel.mention)
        embed.add_field(name="Jump", value=f"[Go to message]({after.jump_url})")
        embed.add_field(name="Before", value=truncate(before.content), inline=False)
        embed.add_field(name="After", value=truncate(after.content), inline=False)
        await send_log(after.guild, LOG, embed)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        embed = discord.Embed(
            title="Messages bulk deleted",
            description=f"{len(payload.message_ids)} messages deleted in <#{payload.channel_id}>",
            color=discord.Color.red(),
            timestamp=discord.utils.utcnow(),
        )
        await send_log(guild, LOG, embed)

    # ---------- Members ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        embed = user_embed("Member joined", member, discord.Color.green())
        embed.description = member.mention
        embed.add_field(name="Account created", value=discord.utils.format_dt(member.created_at, "R"))
        embed.add_field(name="Member count", value=str(member.guild.member_count))
        await send_log(member.guild, LOG, embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        embed = user_embed("Member left", member, discord.Color.dark_grey())
        embed.description = member.mention
        if member.joined_at:
            embed.add_field(name="Joined", value=discord.utils.format_dt(member.joined_at, "R"))
        roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
        if roles:
            embed.add_field(name="Roles", value=truncate(" ".join(roles)), inline=False)
        await send_log(member.guild, LOG, embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        guild = after.guild

        if before.nick != after.nick:
            embed = user_embed("Nickname changed", after, discord.Color.blurple())
            embed.add_field(name="Before", value=before.nick or "*none*")
            embed.add_field(name="After", value=after.nick or "*none*")
            await send_log(guild, LOG, embed)

        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if added or removed:
            embed = user_embed("Roles updated", after, discord.Color.blurple())
            embed.description = after.mention
            if added:
                embed.add_field(name="Added", value=truncate(" ".join(r.mention for r in added)))
            if removed:
                embed.add_field(name="Removed", value=truncate(" ".join(r.mention for r in removed)))
            await send_log(guild, LOG, embed)

        if before.timed_out_until != after.timed_out_until:
            if after.is_timed_out():
                embed = user_embed("Member timed out", after, discord.Color.orange())
                embed.add_field(name="Until", value=discord.utils.format_dt(after.timed_out_until, "f"))
            else:
                embed = user_embed("Timeout removed", after, discord.Color.green())
            embed.description = after.mention
            await send_log(guild, LOG, embed)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        embed = user_embed("Member banned", user, discord.Color.red())
        embed.description = user.mention
        await send_log(guild, LOG, embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        embed = user_embed("Member unbanned", user, discord.Color.green())
        embed.description = user.mention
        await send_log(guild, LOG, embed)

    # ---------- Voice ----------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        if before.channel == after.channel:
            return  # mute/deafen/stream changes
        if before.channel is None:
            embed = user_embed("Joined voice", member, discord.Color.green())
            embed.description = f"{member.mention} joined {after.channel.mention}"
        elif after.channel is None:
            embed = user_embed("Left voice", member, discord.Color.dark_grey())
            embed.description = f"{member.mention} left {before.channel.mention}"
        else:
            embed = user_embed("Moved voice channel", member, discord.Color.blurple())
            embed.description = f"{member.mention} moved {before.channel.mention} → {after.channel.mention}"
        await send_log(member.guild, LOG, embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Logs(bot))
