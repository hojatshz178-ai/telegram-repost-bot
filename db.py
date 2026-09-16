from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_type TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_message_id TEXT NOT NULL,
    source_url TEXT,
    published_at TEXT NOT NULL,
    ingested_at TEXT NOT NULL,
    raw_text TEXT NOT NULL DEFAULT '',
    media_json TEXT NOT NULL DEFAULT '[]',
    content_hash TEXT NOT NULL,
    processed INTEGER NOT NULL DEFAULT 0,
    UNIQUE(source_type, source_name, source_message_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_key TEXT NOT NULL UNIQUE,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    title_hint TEXT NOT NULL DEFAULT '',
    summary_hint TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    published_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    message_ids_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    priority INTEGER NOT NULL DEFAULT 0,
    urgency TEXT NOT NULL DEFAULT 'normal',
    is_military INTEGER NOT NULL DEFAULT 0,
    is_ad INTEGER NOT NULL DEFAULT 0,
    country_region TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL DEFAULT '',
    media_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    ready_at TEXT,
    scheduled_for TEXT,
    published_message_ids_json TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY(event_id) REFERENCES events(id)
);

CREATE INDEX IF NOT EXISTS idx_candidates_queue ON candidates(status, priority DESC, created_at ASC);
CREATE INDEX IF NOT EXISTS idx_messages_hash ON messages(content_hash);
CREATE INDEX IF NOT EXISTS idx_messages_published ON messages(published_at);

CREATE TABLE IF NOT EXISTS state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS gemini_keys (
    key_value TEXT PRIMARY KEY,
    cooldown_until TEXT,
    fail_count INTEGER NOT NULL DEFAULT 0,
    last_used TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class DB:
    def __init__(self, path: Path):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn:
            await self.conn.close()

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        assert self.conn
        await self.conn.execute(sql, params)
        await self.conn.commit()

    async def fetchone(self, sql: str, params: tuple[Any, ...] = ()) -> aiosqlite.Row | None:
        assert self.conn
        cur = await self.conn.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params: tuple[Any, ...] = ()) -> list[aiosqlite.Row]:
        assert self.conn
        cur = await self.conn.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return rows

    async def state_get(self, key: str, default: str | None = None) -> str | None:
        row = await self.fetchone("SELECT value FROM state WHERE key=?", (key,))
        return row["value"] if row else default

    async def state_set(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    async def insert_message(self, data: dict[str, Any]) -> int | None:
        try:
            cur = await self.conn.execute(
                """INSERT INTO messages(source_type,source_name,source_message_id,source_url,published_at,ingested_at,raw_text,media_json,content_hash)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    data["source_type"], data["source_name"], data["source_message_id"], data.get("source_url"),
                    data["published_at"], data.get("ingested_at", now_iso()), data.get("raw_text", ""),
                    json.dumps(data.get("media", []), ensure_ascii=False), data["content_hash"],
                ),
            )
            await self.conn.commit()
            return cur.lastrowid
        except aiosqlite.IntegrityError:
            return None

    async def find_recent_candidates(self, since_iso: str) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM candidates WHERE created_at >= ? ORDER BY created_at DESC", (since_iso,)
        )

    async def queue_count(self) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS n FROM candidates WHERE status='queued'")
        return int(row["n"] if row else 0)
