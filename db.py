"""Small SQLite storage layer shared by all cogs."""

from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

DB_PATH = Path(__file__).parent / "bot.db"

CONFIG_FIELDS = {"log_channel_id", "modlog_channel_id", "autorole_human_id", "autorole_bot_id", "sticky_roles", "rolerequest_channel_id"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_config (
    guild_id          INTEGER PRIMARY KEY,
    log_channel_id    INTEGER,
    modlog_channel_id INTEGER,
    autorole_human_id INTEGER,
    autorole_bot_id   INTEGER,
    sticky_roles      INTEGER,
    rolerequest_channel_id INTEGER
);

CREATE TABLE IF NOT EXISTS warnings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    moderator_id INTEGER NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_warnings_member ON warnings (guild_id, user_id);

CREATE TABLE IF NOT EXISTS role_panels (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    channel_id  INTEGER NOT NULL,
    message_id  INTEGER,
    title       TEXT    NOT NULL,
    description TEXT,
    style       TEXT    NOT NULL,
    approval    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS role_panel_roles (
    panel_id INTEGER NOT NULL,
    role_id  INTEGER NOT NULL,
    emoji    TEXT,
    PRIMARY KEY (panel_id, role_id)
);

CREATE TABLE IF NOT EXISTS sticky_roles (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    role_ids TEXT    NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS role_requests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    role_id     INTEGER NOT NULL,
    reason      TEXT,
    status      TEXT    NOT NULL DEFAULT 'pending',
    reviewer_id INTEGER,
    created_at  TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_role_requests_member ON role_requests (guild_id, user_id, status);
"""


def _connect():
    return aiosqlite.connect(DB_PATH)


async def init():
    async with _connect() as conn:
        await conn.executescript(SCHEMA)
        await _migrate(conn)
        await conn.commit()


async def _migrate(conn: aiosqlite.Connection):
    """Add columns introduced after a database was first created."""
    await _add_missing_columns(conn, "guild_config", {field: "INTEGER" for field in CONFIG_FIELDS})
    await _add_missing_columns(conn, "role_panels", {"approval": "INTEGER NOT NULL DEFAULT 0"})


async def _add_missing_columns(conn: aiosqlite.Connection, table: str, columns: dict[str, str]):
    async with conn.execute(f"PRAGMA table_info({table})") as cur:
        existing = {row[1] for row in await cur.fetchall()}
    for name in sorted(columns.keys() - existing):
        await conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {columns[name]}")


async def get_config(guild_id: int) -> dict:
    async with _connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return {field: None for field in CONFIG_FIELDS}
    return dict(row)


async def set_config(guild_id: int, field: str, value: int | None):
    if field not in CONFIG_FIELDS:
        raise ValueError(f"Unknown config field: {field}")
    async with _connect() as conn:
        await conn.execute(
            f"INSERT INTO guild_config (guild_id, {field}) VALUES (?, ?) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {field} = excluded.{field}",
            (guild_id, value),
        )
        await conn.commit()


async def add_warning(guild_id: int, user_id: int, moderator_id: int, reason: str) -> int:
    async with _connect() as conn:
        cur = await conn.execute(
            "INSERT INTO warnings (guild_id, user_id, moderator_id, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, moderator_id, reason, datetime.now(timezone.utc).isoformat()),
        )
        await conn.commit()
        return cur.lastrowid


async def list_warnings(guild_id: int, user_id: int) -> list[dict]:
    async with _connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM warnings WHERE guild_id = ? AND user_id = ? ORDER BY id",
            (guild_id, user_id),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def delete_warning(guild_id: int, warn_id: int) -> bool:
    async with _connect() as conn:
        cur = await conn.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND id = ?", (guild_id, warn_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def clear_warnings(guild_id: int, user_id: int) -> int:
    async with _connect() as conn:
        cur = await conn.execute(
            "DELETE FROM warnings WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        )
        await conn.commit()
        return cur.rowcount


# ---------- Role panels ----------


async def create_panel(
    guild_id: int, channel_id: int, title: str, description: str | None, style: str, approval: bool
) -> int:
    async with _connect() as conn:
        cur = await conn.execute(
            "INSERT INTO role_panels (guild_id, channel_id, title, description, style, approval) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, title, description, style, int(approval)),
        )
        await conn.commit()
        return cur.lastrowid


async def set_panel_message(panel_id: int, message_id: int):
    async with _connect() as conn:
        await conn.execute("UPDATE role_panels SET message_id = ? WHERE id = ?", (message_id, panel_id))
        await conn.commit()


async def get_panel(guild_id: int, panel_id: int) -> dict | None:
    async with _connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM role_panels WHERE guild_id = ? AND id = ?", (guild_id, panel_id)
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def list_panels(guild_id: int) -> list[dict]:
    async with _connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM role_panels WHERE guild_id = ? ORDER BY id", (guild_id,)) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def delete_panel(guild_id: int, panel_id: int) -> bool:
    async with _connect() as conn:
        cur = await conn.execute("DELETE FROM role_panels WHERE guild_id = ? AND id = ?", (guild_id, panel_id))
        if cur.rowcount:
            await conn.execute("DELETE FROM role_panel_roles WHERE panel_id = ?", (panel_id,))
        await conn.commit()
        return cur.rowcount > 0


async def add_panel_role(panel_id: int, role_id: int, emoji: str | None):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO role_panel_roles (panel_id, role_id, emoji) VALUES (?, ?, ?) "
            "ON CONFLICT(panel_id, role_id) DO UPDATE SET emoji = excluded.emoji",
            (panel_id, role_id, emoji),
        )
        await conn.commit()


async def remove_panel_role(panel_id: int, role_id: int) -> bool:
    async with _connect() as conn:
        cur = await conn.execute(
            "DELETE FROM role_panel_roles WHERE panel_id = ? AND role_id = ?", (panel_id, role_id)
        )
        await conn.commit()
        return cur.rowcount > 0


async def get_panel_roles(panel_id: int) -> list[dict]:
    async with _connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM role_panel_roles WHERE panel_id = ? ORDER BY rowid", (panel_id,)
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


# ---------- Sticky roles ----------


async def save_sticky(guild_id: int, user_id: int, role_ids: list[int]):
    async with _connect() as conn:
        await conn.execute(
            "INSERT INTO sticky_roles (guild_id, user_id, role_ids) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET role_ids = excluded.role_ids",
            (guild_id, user_id, ",".join(map(str, role_ids))),
        )
        await conn.commit()


async def pop_sticky(guild_id: int, user_id: int) -> list[int]:
    """Return and forget the roles saved when this member last left."""
    async with _connect() as conn:
        async with conn.execute(
            "SELECT role_ids FROM sticky_roles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return []
        await conn.execute("DELETE FROM sticky_roles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        await conn.commit()
    return [int(r) for r in row[0].split(",") if r]


# ---------- Role requests ----------


async def create_role_request(guild_id: int, user_id: int, role_id: int, reason: str | None) -> int:
    async with _connect() as conn:
        cur = await conn.execute(
            "INSERT INTO role_requests (guild_id, user_id, role_id, reason, created_at) VALUES (?, ?, ?, ?, ?)",
            (guild_id, user_id, role_id, reason, datetime.now(timezone.utc).isoformat()),
        )
        await conn.commit()
        return cur.lastrowid


async def get_role_request(request_id: int) -> dict | None:
    async with _connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute("SELECT * FROM role_requests WHERE id = ?", (request_id,)) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def pending_role_requests(guild_id: int, user_id: int) -> list[dict]:
    async with _connect() as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM role_requests WHERE guild_id = ? AND user_id = ? AND status = 'pending'",
            (guild_id, user_id),
        ) as cur:
            return [dict(row) for row in await cur.fetchall()]


async def resolve_role_request(request_id: int, status: str, reviewer_id: int) -> bool:
    """Mark a pending request accepted/declined. False if someone already handled it."""
    async with _connect() as conn:
        cur = await conn.execute(
            "UPDATE role_requests SET status = ?, reviewer_id = ? WHERE id = ? AND status = 'pending'",
            (status, reviewer_id, request_id),
        )
        await conn.commit()
        return cur.rowcount > 0


async def cancel_role_request(request_id: int) -> bool:
    async with _connect() as conn:
        cur = await conn.execute(
            "UPDATE role_requests SET status = 'cancelled' WHERE id = ? AND status = 'pending'", (request_id,)
        )
        await conn.commit()
        return cur.rowcount > 0
