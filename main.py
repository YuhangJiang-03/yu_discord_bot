import logging
import os
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

import db

log = logging.getLogger("bot")

COGS_DIR = Path(__file__).parent / "cogs"


class Bot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.message_content = True
        # Prefix is unused (slash commands only) but commands.Bot requires one.
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

    async def setup_hook(self):
        await db.init()

        for file in sorted(COGS_DIR.glob("*.py")):
            if file.stem.startswith("_"):
                continue
            await self.load_extension(f"cogs.{file.stem}")
            log.info("Loaded cog %s", file.stem)

        self.tree.on_error = self.on_app_command_error

        dev_guild_id = os.getenv("DEV_GUILD_ID")
        if dev_guild_id:
            # Guild sync is instant; global sync can take a while to show up.
            guild = discord.Object(id=int(dev_guild_id))
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            log.info("Synced %d commands to dev guild %s", len(synced), dev_guild_id)
        else:
            synced = await self.tree.sync()
            log.info("Synced %d commands globally", len(synced))

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ):
        if isinstance(error, app_commands.MissingPermissions):
            msg = f"You need these permissions: {', '.join(error.missing_permissions)}"
        elif isinstance(error, app_commands.BotMissingPermissions):
            msg = f"I need these permissions: {', '.join(error.missing_permissions)}"
        elif isinstance(error, app_commands.NoPrivateMessage):
            msg = "This command can only be used in a server."
        elif isinstance(error, app_commands.CommandInvokeError) and isinstance(
            error.original, discord.Forbidden
        ):
            msg = "Discord refused that action. Check my role position and permissions."
        else:
            log.exception("Unhandled command error", exc_info=error)
            msg = "Something went wrong running that command."

        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    # override=True: .env wins over stale DISCORD_TOKEN values left in the terminal environment.
    load_dotenv(override=True)
    discord.utils.setup_logging()
    Bot().run(os.environ["DISCORD_TOKEN"], log_handler=None)
