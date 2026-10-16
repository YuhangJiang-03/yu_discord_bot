import logging
from typing import Literal

import discord
from discord import app_commands
from discord.ext import commands

import db
from utils import send_log, truncate

log = logging.getLogger(__name__)

MAX_PANEL_ROLES = 25
MEMBERS_PAGE = 20
# Roles anyone could grant themselves (panels) or get automatically (auto-role, sticky) must not carry these.
DANGEROUS = discord.Permissions(
    administrator=True,
    manage_guild=True,
    manage_roles=True,
    manage_channels=True,
    manage_messages=True,
    manage_webhooks=True,
    kick_members=True,
    ban_members=True,
    moderate_members=True,
)
AUTOROLE_FIELDS = {"humans": "autorole_human_id", "bots": "autorole_bot_id"}
REVIEW_FIELD = "rolerequest_channel_id"
MAX_PENDING = 3


def role_error(member: discord.Member, role: discord.Role) -> str | None:
    """Return an error message if member (or the bot) may not manage role, else None."""
    guild = role.guild
    if role.is_default():
        return "That's the @everyone role."
    if role.managed:
        return f"{role.mention} is managed by an integration or bot and can't be assigned manually."
    if role >= guild.me.top_role:
        return f"{role.mention} is at or above my highest role. Move my role above it."
    if member.id != guild.owner_id and role >= member.top_role:
        return f"{role.mention} is at or above your highest role."
    return None


def is_dangerous(role: discord.Role) -> bool:
    return bool(role.permissions.value & DANGEROUS.value)


def dangerous_names(role: discord.Role) -> str:
    """Human-readable list of the mod-level permissions a role grants, e.g. 'Manage Messages, Kick Members'."""
    granted = discord.Permissions(role.permissions.value & DANGEROUS.value)
    return ", ".join(name.replace("_", " ").title() for name, value in granted if value)


def is_assignable(role: discord.Role, allow_dangerous: bool = False) -> bool:
    """Whether the bot can hand this role out (dangerous roles only when staff approve each request)."""
    if role.is_default() or role.managed or role >= role.guild.me.top_role:
        return False
    return allow_dangerous or not is_dangerous(role)


# ---------- Role panels ----------


async def build_panel(guild: discord.Guild, panel: dict) -> tuple[discord.Embed, discord.ui.View]:
    entries = []
    for row in await db.get_panel_roles(panel["id"]):
        role = guild.get_role(row["role_id"])
        if role is not None:
            entries.append((role, row["emoji"]))

    lines = [f"{emoji + ' ' if emoji else ''}{role.mention}" for role, emoji in entries]
    body = "\n".join(filter(None, [panel["description"], "\n".join(lines) or "*No roles yet.*"]))
    embed = discord.Embed(title=panel["title"], description=truncate(body, 4096), color=discord.Color.blurple())
    if panel["approval"]:
        footer = "Pick a role to request it. Staff will review your request."
    elif panel["style"] == "buttons":
        footer = "Click again to remove a role."
    else:
        footer = "Pick your roles below."
    embed.set_footer(text=footer)

    view = discord.ui.View(timeout=None)
    if panel["style"] == "buttons":
        for role, emoji in entries:
            view.add_item(PanelButton(panel["id"], role.id, label=truncate(role.name, 80), emoji=emoji))
    elif entries:
        options = [
            discord.SelectOption(label=truncate(role.name, 100), value=str(role.id), emoji=emoji)
            for role, emoji in entries
        ]
        view.add_item(PanelSelect(panel["id"], options))
    return embed, view


async def load_panel(interaction: discord.Interaction, panel_id: int) -> tuple[dict, set[int]] | None:
    """A panel in this guild and its role IDs, or None (after replying) if the click can't be handled."""
    if not interaction.guild.me.guild_permissions.manage_roles:
        await interaction.response.send_message("I'm missing the Manage Roles permission.", ephemeral=True)
        return None
    panel = await db.get_panel(interaction.guild.id, panel_id)
    if panel is None:
        await interaction.response.send_message("This role panel no longer exists.", ephemeral=True)
        return None
    return panel, {row["role_id"] for row in await db.get_panel_roles(panel_id)}


# ---------- Role requests (approval panels) ----------


async def request_roles(member: discord.Member, roles: list[discord.Role]) -> str:
    """Send roles to the staff review channel. Returns a summary for the member."""
    guild = member.guild
    channel_id = (await db.get_config(guild.id)).get(REVIEW_FIELD)
    channel = guild.get_channel(channel_id) if channel_id else None
    if channel is None:
        return "Role requests aren't set up yet. Ask staff to run `/rolepanel reviewchannel`."

    pending = await db.pending_role_requests(guild.id, member.id)
    pending_roles = {r["role_id"] for r in pending}
    sent, already, failed = [], [], []
    for role in roles:
        if role.id in pending_roles:
            already.append(role)
            continue
        if len(pending) + len(sent) >= MAX_PENDING:
            return (
                f"You can have at most {MAX_PENDING} pending requests. Wait for staff to review them."
                + (f" Sent: {', '.join(r.mention for r in sent)}." if sent else "")
            )
        request_id = await db.create_role_request(guild.id, member.id, role.id, None)
        embed = discord.Embed(
            title="Role request",
            description=f"{member.mention} wants {role.mention}",
            color=discord.Color.gold(),
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        if is_dangerous(role):
            embed.add_field(name="⚠️ Grants mod permissions", value=dangerous_names(role), inline=False)
        embed.set_footer(text=f"Request #{request_id} · User ID: {member.id}")
        view = discord.ui.View(timeout=None)
        view.add_item(AcceptButton(request_id))
        view.add_item(DeclineButton(request_id))
        try:
            await channel.send(embed=embed, view=view)
            sent.append(role)
        except discord.HTTPException:
            await db.cancel_role_request(request_id)
            failed.append(role)

    parts = []
    if sent:
        parts.append(f"Requested {', '.join(r.mention for r in sent)}. You'll get a DM when staff review it.")
    if already:
        parts.append(f"You already have a pending request for {', '.join(r.mention for r in already)}.")
    if failed:
        parts.append("I couldn't post some requests. Let staff know the review channel is broken.")
    return " ".join(parts)


async def resolve_request(interaction: discord.Interaction, request_id: int, accept: bool, note: str | None = None):
    """Handle Accept, or the Decline modal, on a review message."""
    guild = interaction.guild
    reviewer = interaction.user
    request = await db.get_role_request(request_id)
    if request is None or request["guild_id"] != guild.id:
        return await interaction.response.send_message("That request no longer exists.", ephemeral=True)
    if not reviewer.guild_permissions.manage_roles:
        return await interaction.response.send_message("You need Manage Roles to review requests.", ephemeral=True)

    role = guild.get_role(request["role_id"])
    member = guild.get_member(request["user_id"])
    if accept:
        if role is None:
            return await interaction.response.send_message("That role was deleted. Decline the request instead.", ephemeral=True)
        if error := role_error(reviewer, role):
            return await interaction.response.send_message(error, ephemeral=True)
        if is_dangerous(role) and not reviewer.guild_permissions.administrator:
            return await interaction.response.send_message(
                f"Only an admin can approve this role because it grants: {dangerous_names(role)}.", ephemeral=True
            )
        if member is None:
            return await interaction.response.send_message("That member left the server. Decline the request instead.", ephemeral=True)

    # Conditional update, so two reviewers clicking at once can't both handle it.
    if not await db.resolve_role_request(request_id, "accepted" if accept else "declined", reviewer.id):
        return await interaction.response.send_message("Someone already handled this request.", ephemeral=True)
    if accept:
        await member.add_roles(role, reason=f"Role request #{request_id} accepted by {reviewer}")

    embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else discord.Embed()
    embed.color = discord.Color.green() if accept else discord.Color.red()
    embed.add_field(name="Accepted by" if accept else "Declined by", value=reviewer.mention, inline=False)
    if note:
        embed.add_field(name="Reason", value=truncate(note), inline=False)
    await interaction.response.edit_message(embed=embed, view=None)

    if member is not None:
        role_name = role.name if role else "a deleted role"
        text = f"Your request for **{role_name}** in **{guild.name}** was {'accepted' if accept else 'declined'}."
        if note:
            text += f"\nReason: {note}"
        try:
            await member.send(text)
        except discord.HTTPException:
            pass


class AcceptButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rolereq:accept:(?P<id>\d+)"):
    def __init__(self, request_id: int):
        self.request_id = request_id
        super().__init__(
            discord.ui.Button(label="Accept", style=discord.ButtonStyle.success, custom_id=f"rolereq:accept:{request_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await resolve_request(interaction, self.request_id, accept=True)


class DeclineModal(discord.ui.Modal, title="Decline role request"):
    note = discord.ui.TextInput(
        label="Reason (sent to the member)", style=discord.TextStyle.paragraph, required=False, max_length=500
    )

    def __init__(self, request_id: int):
        super().__init__()
        self.request_id = request_id

    async def on_submit(self, interaction: discord.Interaction):
        await resolve_request(interaction, self.request_id, accept=False, note=self.note.value.strip() or None)


class DeclineButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rolereq:decline:(?P<id>\d+)"):
    def __init__(self, request_id: int):
        self.request_id = request_id
        super().__init__(
            discord.ui.Button(label="Decline", style=discord.ButtonStyle.danger, custom_id=f"rolereq:decline:{request_id}")
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_roles:
            return await interaction.response.send_message("You need Manage Roles to review requests.", ephemeral=True)
        await interaction.response.send_modal(DeclineModal(self.request_id))


class PanelButton(discord.ui.DynamicItem[discord.ui.Button], template=r"rolepanel:(?P<panel>\d+):(?P<role>\d+)"):
    def __init__(self, panel_id: int, role_id: int, label: str | None = None, emoji: str | None = None):
        self.panel_id = panel_id
        self.role_id = role_id
        super().__init__(
            discord.ui.Button(
                label=label,
                emoji=emoji,
                style=discord.ButtonStyle.secondary,
                custom_id=f"rolepanel:{panel_id}:{role_id}",
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match):
        return cls(int(match["panel"]), int(match["role"]))

    async def callback(self, interaction: discord.Interaction):
        loaded = await load_panel(interaction, self.panel_id)
        if loaded is None:
            return
        panel, role_ids = loaded
        role = interaction.guild.get_role(self.role_id)
        if self.role_id not in role_ids or role is None or not is_assignable(role, bool(panel["approval"])):
            return await interaction.response.send_message("That role can't be assigned anymore.", ephemeral=True)

        member = interaction.user
        if role in member.roles:
            # Giving up a role never needs approval.
            await member.remove_roles(role, reason="Role panel")
            await interaction.response.send_message(f"Removed {role.mention}.", ephemeral=True)
        elif panel["approval"]:
            await interaction.response.defer(ephemeral=True, thinking=True)
            await interaction.followup.send(await request_roles(member, [role]), ephemeral=True)
        else:
            await member.add_roles(role, reason="Role panel")
            await interaction.response.send_message(f"Gave you {role.mention}.", ephemeral=True)


class PanelSelect(discord.ui.DynamicItem[discord.ui.Select], template=r"rolepanel-select:(?P<panel>\d+)"):
    def __init__(self, panel_id: int, options: list[discord.SelectOption]):
        self.panel_id = panel_id
        super().__init__(
            discord.ui.Select(
                custom_id=f"rolepanel-select:{panel_id}",
                placeholder="Pick your roles",
                min_values=0,
                max_values=len(options),
                options=options,
            )
        )

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Select, match):
        return cls(int(match["panel"]), item.options)

    async def callback(self, interaction: discord.Interaction):
        loaded = await load_panel(interaction, self.panel_id)
        if loaded is None:
            return
        panel, role_ids = loaded
        selected = {int(v) for v in interaction.data.get("values", [])}
        member = interaction.user
        guild = interaction.guild

        panel_roles = [
            r for r in map(guild.get_role, role_ids) if r is not None and is_assignable(r, bool(panel["approval"]))
        ]
        to_add = [r for r in panel_roles if r.id in selected and r not in member.roles]
        to_remove = [r for r in panel_roles if r.id not in selected and r in member.roles]
        await interaction.response.defer(ephemeral=True, thinking=True)
        if to_remove:
            await member.remove_roles(*to_remove, reason="Role panel")

        parts = []
        if to_remove:
            parts.append("Removed " + ", ".join(r.mention for r in to_remove) + ".")
        if to_add and panel["approval"]:
            parts.append(await request_roles(member, to_add))
        elif to_add:
            await member.add_roles(*to_add, reason="Role panel")
            parts.append("Added " + ", ".join(r.mention for r in to_add) + ".")
        await interaction.followup.send(" ".join(parts) or "No changes.", ephemeral=True)


class Roles(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.bulk_jobs: set[int] = set()

    async def cog_load(self):
        # Panels are looked up by custom_id, so their buttons keep working after a restart.
        self.bot.add_dynamic_items(PanelButton, PanelSelect, AcceptButton, DeclineButton)

    async def cog_unload(self):
        self.bot.remove_dynamic_items(PanelButton, PanelSelect, AcceptButton, DeclineButton)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Every slash command in this cog is for staff. (Panel clicks don't pass through here.)
        if not interaction.user.guild_permissions.manage_roles:
            raise app_commands.MissingPermissions(["manage_roles"])
        if not interaction.guild.me.guild_permissions.manage_roles:
            raise app_commands.BotMissingPermissions(["manage_roles"])
        return True

    async def modlog(self, interaction: discord.Interaction, title: str, description: str):
        embed = discord.Embed(
            title=title, description=description, color=discord.Color.orange(), timestamp=discord.utils.utcnow()
        )
        embed.set_footer(text=f"By {interaction.user} ({interaction.user.id})")
        await send_log(interaction.guild, "modlog_channel_id", embed)

    # ---------- /role ----------

    role = app_commands.Group(
        name="role",
        description="Manage roles.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    @role.command(name="add", description="Give a member a role.")
    async def role_add(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if error := role_error(interaction.user, role):
            return await interaction.response.send_message(error, ephemeral=True)
        if role in member.roles:
            return await interaction.response.send_message(f"{member.mention} already has {role.mention}.", ephemeral=True)
        await member.add_roles(role, reason=f"By {interaction.user}")
        await interaction.response.send_message(
            f"Gave {member.mention} {role.mention}.", allowed_mentions=discord.AllowedMentions.none()
        )

    @role.command(name="remove", description="Take a role from a member.")
    async def role_remove(self, interaction: discord.Interaction, member: discord.Member, role: discord.Role):
        if error := role_error(interaction.user, role):
            return await interaction.response.send_message(error, ephemeral=True)
        if role not in member.roles:
            return await interaction.response.send_message(f"{member.mention} doesn't have {role.mention}.", ephemeral=True)
        await member.remove_roles(role, reason=f"By {interaction.user}")
        await interaction.response.send_message(
            f"Removed {role.mention} from {member.mention}.", allowed_mentions=discord.AllowedMentions.none()
        )

    @role.command(name="info", description="Show details about a role.")
    async def role_info(self, interaction: discord.Interaction, role: discord.Role):
        perms = [name.replace("_", " ").title() for name, value in role.permissions if value]
        if role.permissions.administrator:
            perms = ["Administrator (all permissions)"]
        embed = discord.Embed(title=role.name, color=role.color)
        embed.add_field(name="ID", value=str(role.id))
        embed.add_field(name="Members", value=str(len(role.members)))
        embed.add_field(name="Position", value=str(role.position))
        embed.add_field(name="Color", value=str(role.color))
        embed.add_field(name="Shown separately", value="Yes" if role.hoist else "No")
        embed.add_field(name="Mentionable", value="Yes" if role.mentionable else "No")
        embed.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "R"))
        embed.add_field(name="Managed", value="Yes" if role.managed else "No")
        embed.add_field(name="Permissions", value=truncate(", ".join(perms) or "None"), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @role.command(name="members", description="List the members who have a role.")
    async def role_members(
        self, interaction: discord.Interaction, role: discord.Role, page: app_commands.Range[int, 1] = 1
    ):
        members = sorted(role.members, key=lambda m: m.display_name.lower())
        if not members:
            return await interaction.response.send_message(f"Nobody has {role.mention}.", ephemeral=True)
        pages = -(-len(members) // MEMBERS_PAGE)
        page = min(page, pages)
        chunk = members[(page - 1) * MEMBERS_PAGE : page * MEMBERS_PAGE]
        embed = discord.Embed(
            title=f"Members with {role.name}", description="\n".join(m.mention for m in chunk), color=role.color
        )
        embed.set_footer(text=f"{len(members)} members · page {page}/{pages}")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @role.command(name="create", description="Create a new role (with no permissions).")
    @app_commands.describe(color="Hex color like #ff8800", hoist="Show members with this role separately")
    async def role_create(
        self,
        interaction: discord.Interaction,
        name: app_commands.Range[str, 1, 100],
        color: str | None = None,
        hoist: bool = False,
        mentionable: bool = False,
    ):
        colour = discord.Color.default()
        if color:
            try:
                colour = discord.Color.from_str(color if color.startswith(("#", "0x", "rgb")) else f"#{color}")
            except ValueError:
                return await interaction.response.send_message("That isn't a valid color. Use hex like `#ff8800`.", ephemeral=True)
        role = await interaction.guild.create_role(
            name=name, color=colour, hoist=hoist, mentionable=mentionable, reason=f"By {interaction.user}"
        )
        await interaction.response.send_message(f"Created {role.mention}.", allowed_mentions=discord.AllowedMentions.none())
        await self.modlog(interaction, "Role created", f"{role.mention} (`{role.name}`)")

    @role.command(name="edit", description="Change a role's name, color or display settings.")
    @app_commands.describe(color="Hex color like #ff8800", hoist="Show members with this role separately")
    async def role_edit(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        name: app_commands.Range[str, 1, 100] | None = None,
        color: str | None = None,
        hoist: bool | None = None,
        mentionable: bool | None = None,
    ):
        if error := role_error(interaction.user, role):
            return await interaction.response.send_message(error, ephemeral=True)
        changes = {}
        if name is not None:
            changes["name"] = name
        if color is not None:
            try:
                changes["color"] = discord.Color.from_str(color if color.startswith(("#", "0x", "rgb")) else f"#{color}")
            except ValueError:
                return await interaction.response.send_message("That isn't a valid color. Use hex like `#ff8800`.", ephemeral=True)
        if hoist is not None:
            changes["hoist"] = hoist
        if mentionable is not None:
            changes["mentionable"] = mentionable
        if not changes:
            return await interaction.response.send_message("Nothing to change.", ephemeral=True)

        old_name = role.name
        await role.edit(**changes, reason=f"By {interaction.user}")
        summary = ", ".join(f"{key} → {value}" for key, value in changes.items())
        await interaction.response.send_message(
            f"Updated {role.mention}: {summary}", allowed_mentions=discord.AllowedMentions.none()
        )
        await self.modlog(interaction, "Role edited", f"{role.mention} (was `{old_name}`)\n{summary}")

    @role.command(name="delete", description="Delete a role.")
    async def role_delete(self, interaction: discord.Interaction, role: discord.Role):
        if error := role_error(interaction.user, role):
            return await interaction.response.send_message(error, ephemeral=True)
        name, count = role.name, len(role.members)
        await role.delete(reason=f"By {interaction.user}")
        await interaction.response.send_message(f"Deleted **{name}** (it had {count} members).")
        await self.modlog(interaction, "Role deleted", f"`{name}` ({count} members)")

    @role.command(name="all", description="Add or remove a role for every member.")
    @app_commands.describe(target="Which members to change")
    async def role_all(
        self,
        interaction: discord.Interaction,
        action: Literal["add", "remove"],
        role: discord.Role,
        target: Literal["everyone", "humans", "bots"] = "humans",
    ):
        if error := role_error(interaction.user, role):
            return await interaction.response.send_message(error, ephemeral=True)
        guild = interaction.guild
        if guild.id in self.bulk_jobs:
            return await interaction.response.send_message("A bulk role change is already running here.", ephemeral=True)

        await interaction.response.defer(thinking=True)
        if not guild.chunked:
            await guild.chunk()
        members = [
            m
            for m in guild.members
            if (target == "everyone" or m.bot == (target == "bots")) and (role in m.roles) == (action == "remove")
        ]
        if not members:
            return await interaction.followup.send("Nobody needs changing.")

        verb = "Adding" if action == "add" else "Removing"
        await interaction.followup.send(
            f"{verb} {role.mention} for {len(members)} members. This can take a while; I'll post here when done.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.bulk_jobs.add(guild.id)
        done = failed = 0
        try:
            for member in members:
                try:
                    if action == "add":
                        await member.add_roles(role, reason=f"Bulk by {interaction.user}")
                    else:
                        await member.remove_roles(role, reason=f"Bulk by {interaction.user}")
                    done += 1
                except discord.HTTPException:
                    failed += 1
        finally:
            self.bulk_jobs.discard(guild.id)

        result = f"Finished: {'added' if action == 'add' else 'removed'} {role.mention} for {done} members"
        result += f", {failed} failed." if failed else "."
        try:
            # Interaction tokens expire after 15 minutes, so post to the channel instead.
            await interaction.channel.send(result, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass
        await self.modlog(interaction, "Bulk role change", f"{action} {role.mention} for {target}: {done} done, {failed} failed")

    # ---------- /autorole, /stickyroles ----------

    autorole = app_commands.Group(
        name="autorole",
        description="Give new members a role automatically.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    @autorole.command(name="set", description="Set the role new members get when they join.")
    async def autorole_set(
        self, interaction: discord.Interaction, target: Literal["humans", "bots"], role: discord.Role
    ):
        if error := role_error(interaction.user, role):
            return await interaction.response.send_message(error, ephemeral=True)
        if is_dangerous(role):
            return await interaction.response.send_message(
                f"{role.mention} grants mod permissions ({dangerous_names(role)}), so I won't hand it out automatically. "
                "Turn those off in the role's settings if it doesn't need them.",
                ephemeral=True,
            )
        await db.set_config(interaction.guild.id, AUTOROLE_FIELDS[target], role.id)
        await interaction.response.send_message(f"New {target} will get {role.mention}.", ephemeral=True)

    @autorole.command(name="clear", description="Stop giving new members a role.")
    async def autorole_clear(self, interaction: discord.Interaction, target: Literal["humans", "bots"]):
        await db.set_config(interaction.guild.id, AUTOROLE_FIELDS[target], None)
        await interaction.response.send_message(f"Auto-role for {target} disabled.", ephemeral=True)

    @app_commands.command(name="stickyroles", description="Give members their roles back when they rejoin.")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_roles=True)
    async def stickyroles(self, interaction: discord.Interaction, enabled: bool):
        await db.set_config(interaction.guild.id, "sticky_roles", int(enabled))
        if enabled:
            msg = "Sticky roles on. Members who leave will get their roles back when they rejoin (except mod/admin roles)."
        else:
            msg = "Sticky roles off."
        await interaction.response.send_message(msg, ephemeral=True)

    async def apply_join_roles(self, member: discord.Member):
        guild = member.guild
        if not guild.me.guild_permissions.manage_roles:
            return
        cfg = await db.get_config(guild.id)
        role_ids = []
        if autorole := cfg.get(AUTOROLE_FIELDS["bots" if member.bot else "humans"]):
            role_ids.append(autorole)
        if cfg.get("sticky_roles"):
            role_ids += await db.pop_sticky(guild.id, member.id)

        roles = {r for r in map(guild.get_role, role_ids) if r is not None and is_assignable(r) and r not in member.roles}
        if not roles:
            return
        try:
            await member.add_roles(*roles, reason="Auto-role / sticky roles")
        except discord.HTTPException:
            log.exception("Couldn't give join roles to %s in guild %s", member.id, guild.id)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Members still on the rules screen get their roles once they accept (see on_member_update).
        if not member.pending:
            await self.apply_join_roles(member)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.pending and not after.pending:
            await self.apply_join_roles(after)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        cfg = await db.get_config(member.guild.id)
        if not cfg.get("sticky_roles"):
            return
        role_ids = [r.id for r in member.roles if not r.is_default() and not r.managed]
        if role_ids:
            await db.save_sticky(member.guild.id, member.id, role_ids)

    # ---------- /rolepanel ----------

    rolepanel = app_commands.Group(
        name="rolepanel",
        description="Messages where members pick their own roles.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_roles=True),
    )

    async def panel_autocomplete(self, interaction: discord.Interaction, current: str):
        panels = await db.list_panels(interaction.guild.id)
        return [
            app_commands.Choice(name=truncate(f"#{p['id']} {p['title']}", 100), value=p["id"])
            for p in panels
            if current.lower() in f"{p['id']} {p['title']}".lower()
        ][:25]

    async def refresh_panel(self, guild: discord.Guild, panel: dict) -> str | None:
        """Re-render a panel's message. Returns an error message on failure."""
        channel = guild.get_channel(panel["channel_id"])
        if channel is None or panel["message_id"] is None:
            return "The panel's channel is gone. Delete it with `/rolepanel delete`."
        embed, view = await build_panel(guild, panel)
        try:
            await channel.get_partial_message(panel["message_id"]).edit(embed=embed, view=view)
        except discord.NotFound:
            return "The panel's message was deleted. Delete the panel with `/rolepanel delete`."
        except discord.HTTPException as e:
            return f"Discord rejected the panel update ({e.text}). Check the emoji is valid."
        return None

    @rolepanel.command(name="create", description="Post a new role panel.")
    @app_commands.describe(
        style="Buttons (one per role) or a dropdown menu",
        approval="Staff must accept each request before the role is given",
        channel="Where to post it (default: here)",
    )
    async def rolepanel_create(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 256],
        style: Literal["buttons", "dropdown"] = "buttons",
        approval: bool = False,
        description: app_commands.Range[str, 1, 1000] | None = None,
        channel: discord.TextChannel | None = None,
    ):
        channel = channel or interaction.channel
        perms = channel.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            return await interaction.response.send_message(
                f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True
            )
        panel_id = await db.create_panel(interaction.guild.id, channel.id, title, description, style, approval)
        panel = await db.get_panel(interaction.guild.id, panel_id)
        embed, view = await build_panel(interaction.guild, panel)
        message = await channel.send(embed=embed, view=view)
        await db.set_panel_message(panel_id, message.id)
        msg = f"Created panel **#{panel_id}** in {channel.mention}. Add roles with `/rolepanel addrole`."
        if approval and not (await db.get_config(interaction.guild.id)).get(REVIEW_FIELD):
            msg += "\nThis panel needs approval, so set where requests go with `/rolepanel reviewchannel`."
        await interaction.response.send_message(msg, ephemeral=True)

    @rolepanel.command(name="addrole", description="Add a role to a panel.")
    @app_commands.describe(panel="The panel to change", emoji="Optional emoji shown next to the role")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def rolepanel_addrole(
        self, interaction: discord.Interaction, panel: int, role: discord.Role, emoji: str | None = None
    ):
        data = await db.get_panel(interaction.guild.id, panel)
        if data is None:
            return await interaction.response.send_message("That panel doesn't exist.", ephemeral=True)
        if error := role_error(interaction.user, role):
            return await interaction.response.send_message(error, ephemeral=True)
        if is_dangerous(role) and not data["approval"]:
            return await interaction.response.send_message(
                f"{role.mention} grants mod permissions ({dangerous_names(role)}), so it can't go on an instant panel. "
                "Turn those off in the role's settings, or put it on a panel made with `/rolepanel create approval:true` "
                "so an admin approves each request.",
                ephemeral=True,
            )
        existing = {row["role_id"] for row in await db.get_panel_roles(panel)}
        if role.id not in existing and len(existing) >= MAX_PANEL_ROLES:
            return await interaction.response.send_message(f"Panels can hold at most {MAX_PANEL_ROLES} roles.", ephemeral=True)

        await db.add_panel_role(panel, role.id, emoji.strip() if emoji else None)
        if error := await self.refresh_panel(interaction.guild, data):
            if role.id not in existing:
                await db.remove_panel_role(panel, role.id)
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.send_message(f"Added {role.mention} to panel #{panel}.", ephemeral=True)

    @rolepanel.command(name="removerole", description="Remove a role from a panel.")
    @app_commands.describe(panel="The panel to change")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def rolepanel_removerole(self, interaction: discord.Interaction, panel: int, role: discord.Role):
        data = await db.get_panel(interaction.guild.id, panel)
        if data is None:
            return await interaction.response.send_message("That panel doesn't exist.", ephemeral=True)
        if not await db.remove_panel_role(panel, role.id):
            return await interaction.response.send_message(f"{role.mention} isn't on that panel.", ephemeral=True)
        if error := await self.refresh_panel(interaction.guild, data):
            return await interaction.response.send_message(error, ephemeral=True)
        await interaction.response.send_message(f"Removed {role.mention} from panel #{panel}.", ephemeral=True)

    @rolepanel.command(name="delete", description="Delete a panel and its message.")
    @app_commands.describe(panel="The panel to delete")
    @app_commands.autocomplete(panel=panel_autocomplete)
    async def rolepanel_delete(self, interaction: discord.Interaction, panel: int):
        data = await db.get_panel(interaction.guild.id, panel)
        if data is None:
            return await interaction.response.send_message("That panel doesn't exist.", ephemeral=True)
        channel = interaction.guild.get_channel(data["channel_id"])
        if channel is not None and data["message_id"]:
            try:
                await channel.get_partial_message(data["message_id"]).delete()
            except discord.HTTPException:
                pass
        await db.delete_panel(interaction.guild.id, panel)
        await interaction.response.send_message(f"Deleted panel #{panel}.", ephemeral=True)

    @rolepanel.command(name="list", description="List this server's role panels.")
    async def rolepanel_list(self, interaction: discord.Interaction):
        panels = await db.list_panels(interaction.guild.id)
        if not panels:
            return await interaction.response.send_message("No role panels yet. Make one with `/rolepanel create`.", ephemeral=True)
        lines = []
        for p in panels:
            count = len(await db.get_panel_roles(p["id"]))
            link = f"https://discord.com/channels/{p['guild_id']}/{p['channel_id']}/{p['message_id']}"
            mode = " · needs approval" if p["approval"] else ""
            lines.append(f"**#{p['id']}** [{truncate(p['title'], 60)}]({link}) · {p['style']} · {count} roles{mode}")
        embed = discord.Embed(title="Role panels", description=truncate("\n".join(lines), 4096), color=discord.Color.blurple())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @rolepanel.command(name="reviewchannel", description="Set where requests from approval panels go for staff review.")
    @app_commands.describe(channel="Staff-only channel (leave empty to stop accepting requests)")
    async def rolepanel_reviewchannel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if channel is not None:
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links):
                return await interaction.response.send_message(
                    f"I need View Channel, Send Messages and Embed Links in {channel.mention}.", ephemeral=True
                )
        await db.set_config(interaction.guild.id, REVIEW_FIELD, channel.id if channel else None)
        if channel:
            msg = f"Role requests will be posted in {channel.mention}. Make sure only staff can see it."
        else:
            msg = "Role requests turned off. Approval panels will tell members requests aren't set up."
        await interaction.response.send_message(msg, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(Roles(bot))
