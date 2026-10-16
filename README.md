# yu_discord_bot
Yuhang's personal discord bot

## Setup
1. `pip install -r requirements.txt` (inside `bot-env`)
2. Copy `.env.example` to `.env` and fill in `DISCORD_TOKEN` (and `DEV_GUILD_ID` for instant slash-command sync in your test server).
3. In the Developer Portal, enable the **Server Members** and **Message Content** privileged intents.
4. Invite the bot with the `bot` + `applications.commands` scopes and the permissions: Kick Members, Ban Members, Moderate Members, Manage Messages, Manage Channels, Manage Roles, Read Message History, Send Messages, Embed Links, View Audit Log, Connect, Speak.
5. Install [FFmpeg](https://ffmpeg.org/download.html) and make sure `ffmpeg` is on your PATH (needed for music).
6. In Server Settings → Roles, drag the bot's role above every role you want it to manage.
7. `py main.py`

Command to run watchdog: watchmedo auto-restart --patterns="*.py" -- py main.py

## Structure
- `main.py` — bot startup; auto-loads every `cogs/*.py` and syncs slash commands
- `db.py` — SQLite storage (`bot.db`)
- `utils.py` — shared helpers
- `cogs/moderation.py` — `/kick /ban /unban /timeout /untimeout /warn /warnings /delwarn /clearwarns /purge /slowmode`
- `cogs/logs.py` — event logging + `/config logs|modlog|show`
- `cogs/roles.py` — `/role add|remove|info|members|create|edit|delete|all`, `/autorole set|clear`, `/stickyroles`, `/rolepanel create|addrole|removerole|delete|list|reviewchannel` (button/dropdown self-roles, optionally staff-approved)
- `cogs/music.py` — YouTube music: `/play /add /queue /remove /pause /resume /skip /stop`

To add a feature, create a new file in `cogs/` with an `async def setup(bot)` function.
