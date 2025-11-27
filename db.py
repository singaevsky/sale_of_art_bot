import asyncio
import aiosqlite
import os
from typing import Optional, Tuple

DB_PATH = os.getenv("DB_PATH", "bot.sqlite3")

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_seen INTEGER
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS promo_codes (
            code TEXT PRIMARY KEY,
            used_by INTEGER,
            used_at INTEGER
        );
        """)
        await db.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        await db.commit()

async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        await db.commit()

async def upsert_user(user_id: int, username: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users(user_id, username, first_seen) VALUES(?,?,strftime('%s','now'))",
            (user_id, username)
        )
        await db.commit()

async def count_available_codes() -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM promo_codes WHERE used_by IS NULL")
        row = await cur.fetchone()
        return row[0] if row else 0

async def take_code_for_user(user_id: int) -> Optional[Tuple[str, int]]:
    # Возвращает (code, used_at)
    async with aiosqlite.connect(DB_PATH) as db:
        # пытаемся взять любой неиспользованный код
        cur = await db.execute("SELECT code FROM promo_codes WHERE used_by IS NULL LIMIT 1")
        row = await cur.fetchone()
        if not row:
            return None
        code = row[0]
        await db.execute("UPDATE promo_codes SET used_by=?, used_at=strftime('%s','now') WHERE code=?", (user_id, code))
        await db.commit()
        return code, await get_unix_now()

async def add_codes(codes: list[str]):
    if not codes:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany("INSERT OR IGNORE INTO promo_codes(code) VALUES(?)", [(c,) for c in codes])
        await db.commit()

async def get_unix_now() -> int:
    # используем системное время, БД тоже хранит в unixtime
    import time
    return int(time.time())

async def export_remaining_codes(limit: int | None = None) -> list[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        if limit:
            cur = await db.execute("SELECT code FROM promo_codes WHERE used_by IS NULL LIMIT ?", (limit,))
        else:
            cur = await db.execute("SELECT code FROM promo_codes WHERE used_by IS NULL")
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def mark_gift_sent(user_id: int):
    # запасной лог, если захотите статистику по Star Gifts
    key = f"gift_sent_to_{user_id}"
    await set_setting(key, str(await get_unix_now()))
