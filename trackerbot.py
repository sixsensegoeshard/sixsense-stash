"""
301secrets Tracker — production-ready improved version.

Implements critical fixes + major feature upgrades:
- correct file classification (loops / zips / files / media / messages)
- users table + profile updates
- no DB writes in read-only views
- global error handler
- scheduler guards + catch-up
- all-time vs current role cycle
- improved stats / top / profile / places / roles / reports / health / backup
"""

from __future__ import annotations

import asyncio
import calendar
import csv
import io
import logging
import os
import re
import sqlite3
import sys
import time
import traceback
import weakref
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from aiogram import BaseMiddleware, Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeChat,
    BotCommandScopeChatAdministrators,
    BotCommandScopeDefault,
    BufferedInputFile,
    ErrorEvent,
    Message,
    TelegramObject,
)
from tracker_ui import build_screen, deliver, register_ui, short, stats_card
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Integer,
    String,
    delete,
    func,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# ============================================================
# CONFIG
# ============================================================
APP_NAME = "301secrets Tracker"
DB_DEFAULT = "sqlite+aiosqlite:///tracker_v3.db"
BOT_TZ_DEFAULT = "Europe/Riga"

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", os.getenv("ADMIN_ID", "1268142648")).strip()
DB_URL = os.getenv("DB_URL", DB_DEFAULT).strip()
BOT_TIMEZONE = os.getenv("BOT_TIMEZONE", BOT_TZ_DEFAULT).strip()
TRACK_CHAT_ID_RAW = os.getenv("TRACK_CHAT_ID", "").strip()
REQUIRE_TRACK_CHAT_ID = os.getenv("REQUIRE_TRACK_CHAT_ID", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set. Put it into .env or export BOT_TOKEN='...'.")

try:
    ADMIN_IDS: set[int] = {int(x) for x in ADMIN_IDS_RAW.replace(" ", "").split(",") if x}
except ValueError as exc:
    raise RuntimeError(f"Invalid ADMIN_IDS: {exc}") from exc
if not ADMIN_IDS:
    raise RuntimeError("At least one ADMIN_ID is required.")

try:
    TRACK_CHAT_ID = int(TRACK_CHAT_ID_RAW) if TRACK_CHAT_ID_RAW else None
except ValueError as exc:
    raise RuntimeError(f"TRACK_CHAT_ID must be an integer: {exc}") from exc
if REQUIRE_TRACK_CHAT_ID and TRACK_CHAT_ID is None:
    raise RuntimeError(
        "TRACK_CHAT_ID is required for production. Set the exact private group/supergroup ID."
    )

try:
    LOCAL_TZ = ZoneInfo(BOT_TIMEZONE)
except Exception as exc:
    raise RuntimeError(f"Invalid BOT_TIMEZONE={BOT_TIMEZONE!r}: {exc}") from exc

try:
    SMALL_THRESHOLD = max(1, int(os.getenv("PLACEMENT_SMALL_THRESHOLD", "3")))
    LARGE_THRESHOLD = max(1, int(os.getenv("PLACEMENT_LARGE_THRESHOLD", "1")))
except ValueError as exc:
    raise RuntimeError(f"Placement thresholds must be integers: {exc}") from exc

NOTE_MAX_LEN = 500
THROTTLE_SECONDS = 1.5
UNDO_WINDOW_MINUTES = 15
OPPORTUNITY_TAGS = tuple(
    tag.strip().lower()
    for tag in os.getenv("OPPORTUNITY_HASHTAGS", "#opportunity").split(",")
    if tag.strip()
)
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", "backups"))
REPORT_CATCHUP = os.getenv("REPORT_CATCHUP", "1").strip().lower() not in {
    "0",
    "false",
    "no",
}
MSG_RETENTION_DAYS = int(os.getenv("MSG_RETENTION_DAYS", "0") or "0")  # 0 = keep forever

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("301tracker")


# ============================================================
# DATABASE MODELS
# ============================================================
class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    username: Mapped[str | None] = mapped_column(String, index=True)
    first_name: Mapped[str | None] = mapped_column(String)
    last_name: Mapped[str | None] = mapped_column(String)
    first_seen: Mapped[datetime] = mapped_column(DateTime, index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime, index=True)


class MsgLog(Base):
    __tablename__ = "msg_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[int | None] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String, index=True)
    first_name: Mapped[str | None] = mapped_column(String)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    thread_id: Mapped[int | None] = mapped_column(Integer, index=True)
    category: Mapped[str] = mapped_column(String, index=True)
    opportunity: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    text_preview: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class Placement(Base):
    __tablename__ = "placements"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String)
    tier: Mapped[str] = mapped_column(String, default="small", index=True)  # small / large
    artist: Mapped[str | None] = mapped_column(String)
    track: Mapped[str | None] = mapped_column(String)
    comment: Mapped[str | None] = mapped_column(String)
    proof: Mapped[str | None] = mapped_column(String)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    used_by_award_id: Mapped[int | None] = mapped_column(Integer, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class Note(Base):
    __tablename__ = "notes"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String)
    text: Mapped[str | None] = mapped_column(String)
    actor_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class MonthlySnapshot(Base):
    __tablename__ = "monthly_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String)
    period: Mapped[str] = mapped_column(String, index=True)
    messages: Mapped[int] = mapped_column(Integer, default=0)
    loops: Mapped[int] = mapped_column(Integer, default=0)
    zips: Mapped[int] = mapped_column(Integer, default=0)
    files: Mapped[int] = mapped_column(Integer, default=0)
    media: Mapped[int] = mapped_column(Integer, default=0)
    opportunities: Mapped[int] = mapped_column(Integer, default=0)
    active_days: Mapped[int] = mapped_column(Integer, default=0)
    total: Mapped[int] = mapped_column(Integer, default=0)


class PlacementProgress(Base):
    """Manual role-cycle markers only. Created on write, never on pure read."""

    __tablename__ = "placement_progress"

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    small_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    big_reset_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class RoleAward(Base):
    """History of manual role awards (moderator confirmed)."""

    __tablename__ = "role_awards"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, index=True)  # small / big
    actor_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    small_count: Mapped[int] = mapped_column(Integer, default=0)
    large_count: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(String)


engine_kwargs: dict = {"echo": False}
if DB_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"timeout": 30}
engine = create_async_engine(DB_URL, **engine_kwargs)
Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


# ============================================================
# APP CONSTANTS
# ============================================================
router = Router()
WRITE_LAST: dict[int, float] = {}
USER_WRITE_LOCKS: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()


def user_write_lock(uid: int) -> asyncio.Lock:
    """Serialize confirmed actions for a target, even from different admins."""
    lock = USER_WRITE_LOCKS.get(uid)
    if lock is None:
        lock = asyncio.Lock()
        USER_WRITE_LOCKS[uid] = lock
    return lock

LOOP_EXTS = {
    ".mp3",
    ".wav",
    ".flac",
    ".aif",
    ".aiff",
    ".ogg",
    ".m4a",
    ".aac",
    ".opus",
    ".wma",
    ".mid",
    ".midi",
}
ZIP_EXTS = {".zip", ".rar", ".7z", ".tar", ".gz", ".tgz", ".bz2"}

SORT_KEYS = {
    "total",
    "messages",
    "loops",
    "zips",
    "files",
    "media",
    "days",
    "opportunities",
}
PERIOD_KEYS = {"7d", "30d", "month", "all"}

MONTHS_EN = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}

EMPTY_STATS = {
    "un": None,
    "fn": None,
    "messages": 0,
    "loops": 0,
    "zips": 0,
    "files": 0,
    "media": 0,
    "total": 0,
    "opportunities": 0,
}


# ============================================================
# TIME / TEXT HELPERS
# ============================================================
def utc_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def utc_naive_from_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=LOCAL_TZ)
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def local_from_utc_naive(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc).astimezone(LOCAL_TZ)


def local_date_from_utc_naive(value: datetime) -> date:
    return local_from_utc_naive(value).date()


def escape_html(value) -> str:
    if value is None:
        return ""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def display_name(username: str | None, first_name: str | None) -> str:
    return f"@{username}" if username else (first_name or "Unknown")


def header(emoji: str, title: str) -> str:
    return f"{emoji} <b>{title}</b>"


def metric_line(parts: list[tuple[str, int | float]], *, hide_zero: bool = True) -> str:
    """Compact chips: 🎵 3 · 🖼 5 · 📅 2"""
    chunks: list[str] = []
    for label, value in parts:
        if hide_zero and not value:
            continue
        if isinstance(value, float):
            shown = f"{value:.1f}".rstrip("0").rstrip(".")
        else:
            shown = str(value)
        chunks.append(f"{label} {shown}")
    return " · ".join(chunks)


def activity_bits(u: dict, day_count: int = 0, *, hide_zero: bool = True) -> str:
    return metric_line(
        [
            ("🎵", u.get("loops", 0)),
            ("🗂", u.get("zips", 0)),
            ("📎", u.get("files", 0)),
            ("🖼", u.get("media", 0)),
            ("✏️", u.get("messages", 0)),
            ("🎯", u.get("opportunities", 0)),
            ("📅", day_count),
        ],
        hide_zero=hide_zero,
    )


def short_date(dt_value: datetime) -> str:
    return local_from_utc_naive(dt_value).strftime("%d.%m.%y")


def format_local(dt_value: datetime) -> str:
    return local_from_utc_naive(dt_value).strftime("%d.%m.%Y %H:%M")


def throttle(user_id: int) -> bool:
    now = time.monotonic()
    last = WRITE_LAST.get(user_id, 0.0)
    if now - last < THROTTLE_SECONDS:
        return True
    WRITE_LAST[user_id] = now
    return False


def is_admin(message: Message) -> bool:
    return bool(message.from_user and message.from_user.id in ADMIN_IDS)


def is_command_message(message: Message) -> bool:
    if not message.text:
        return False
    return bool(re.match(r"^/\w+(?:@\w+)?(?:\s|$)", message.text, flags=re.IGNORECASE))


def contains_opportunity_tag(message: Message) -> bool:
    if not OPPORTUNITY_TAGS:
        return False
    haystack = f"{message.text or ''}\n{message.caption or ''}".lower()
    return any(
        re.search(r"(?<!\w)" + re.escape(tag) + r"(?!\w)", haystack)
        for tag in OPPORTUNITY_TAGS
    )


# ============================================================
# CALENDAR RANGES
# ============================================================
def month_start_local(value: datetime) -> datetime:
    value = value.astimezone(LOCAL_TZ)
    return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month_local(value: datetime) -> datetime:
    value = value.astimezone(LOCAL_TZ)
    if value.month == 12:
        return value.replace(
            year=value.year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0
        )
    return value.replace(month=value.month + 1, day=1, hour=0, minute=0, second=0, microsecond=0)


def current_month_range() -> tuple[datetime, datetime]:
    start = month_start_local(local_now())
    end = next_month_local(start)
    return utc_naive_from_local(start), utc_naive_from_local(end)


def current_month_label() -> str:
    now = local_now()
    return f"{MONTHS_EN[now.month]} {now.year}"


def period_bounds(period: str) -> tuple[datetime | None, datetime, str]:
    now = utc_now()
    if period == "7d":
        return now - timedelta(days=7), now, "последние 7 дней"
    if period == "30d":
        return now - timedelta(days=30), now, "последние 30 дней"
    if period == "all":
        return None, now, "всё время"
    start, end = current_month_range()
    return start, end, current_month_label()


def previous_calendar_week_range() -> tuple[datetime, datetime, str]:
    now_local = local_now()
    this_monday = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    prev_monday = this_monday - timedelta(days=7)
    label = f"{prev_monday:%d.%m}–{(this_monday - timedelta(days=1)):%d.%m.%Y}"
    return utc_naive_from_local(prev_monday), utc_naive_from_local(this_monday), label


def previous_calendar_month_range() -> tuple[datetime, datetime, str]:
    current_start = month_start_local(local_now())
    previous_last = current_start - timedelta(days=1)
    previous_start = month_start_local(previous_last)
    label = previous_start.strftime("%Y-%m")
    return utc_naive_from_local(previous_start), utc_naive_from_local(current_start), label


# ============================================================
# MESSAGE CLASSIFICATION  (item 1)
# ============================================================
def get_category(message: Message) -> str | None:
    """Classify message into: message | loop | zip | files | media."""
    if message.document:
        filename = (message.document.file_name or "").lower()
        if any(filename.endswith(ext) for ext in LOOP_EXTS):
            return "loop"
        if any(filename.endswith(ext) for ext in ZIP_EXTS):
            return "zip"
        # other documents (pdf, txt, project files, etc.)
        return "files"
    if message.audio:
        return "loop"
    if message.voice:
        return "message"
    if message.photo or message.video or message.animation or message.video_note:
        return "media"
    if message.text:
        return "message"
    return None


def get_text_preview(message: Message) -> str | None:
    if message.text:
        return message.text[:200]
    if message.caption:
        return message.caption[:200]
    if message.document:
        return f"[File] {message.document.file_name or 'no name'}"
    if message.audio:
        return f"[Audio] {message.audio.title or message.audio.file_name or ''}"
    if message.photo:
        return "[Photo]"
    if message.video:
        return "[Video]"
    if message.animation:
        return "[GIF]"
    if message.voice:
        return "[Voice]"
    if message.video_note:
        return "[Video note]"
    return None


def is_service_message(message: Message) -> bool:
    return bool(
        message.new_chat_members
        or message.left_chat_member
        or message.new_chat_title
        or message.new_chat_photo
        or message.delete_chat_photo
        or message.group_chat_created
        or message.supergroup_chat_created
        or message.pinned_message
    )


# ============================================================
# DATABASE INIT / MIGRATIONS
# ============================================================
async def column_exists(conn, table: str, column: str) -> bool:
    result = await conn.execute(text(f"PRAGMA table_info({table})"))
    return any(row[1] == column for row in result.fetchall())


async def ensure_column(conn, table: str, column: str, ddl: str) -> None:
    if not await column_exists(conn, table, column):
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

        await ensure_column(conn, "msg_log", "message_id", "INTEGER")
        await ensure_column(conn, "msg_log", "opportunity", "BOOLEAN DEFAULT 0")
        await ensure_column(conn, "placements", "tier", "VARCHAR DEFAULT 'small'")
        await ensure_column(conn, "placements", "artist", "VARCHAR")
        await ensure_column(conn, "placements", "track", "VARCHAR")
        await ensure_column(conn, "placements", "proof", "VARCHAR")
        await ensure_column(conn, "placements", "actor_id", "BIGINT")
        await ensure_column(conn, "placements", "used_by_award_id", "INTEGER")
        await ensure_column(conn, "notes", "actor_id", "BIGINT")
        await ensure_column(conn, "monthly_snapshots", "opportunities", "INTEGER DEFAULT 0")
        await ensure_column(conn, "monthly_snapshots", "files", "INTEGER DEFAULT 0")

        await conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS placement_progress (
                user_id BIGINT PRIMARY KEY,
                small_reset_at DATETIME NULL,
                big_reset_at DATETIME NULL,
                updated_at DATETIME NOT NULL
            )
            """
            )
        )
        await conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS role_awards (
                id INTEGER PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username VARCHAR,
                kind VARCHAR NOT NULL,
                actor_id BIGINT,
                small_count INTEGER DEFAULT 0,
                large_count INTEGER DEFAULT 0,
                note VARCHAR,
                created_at DATETIME
            )
            """
            )
        )
        # Legacy / partial tables: ensure all columns exist before indexes.
        await ensure_column(conn, "role_awards", "username", "VARCHAR")
        await ensure_column(conn, "role_awards", "kind", "VARCHAR")
        await ensure_column(conn, "role_awards", "actor_id", "BIGINT")
        await ensure_column(conn, "role_awards", "small_count", "INTEGER DEFAULT 0")
        await ensure_column(conn, "role_awards", "large_count", "INTEGER DEFAULT 0")
        await ensure_column(conn, "role_awards", "note", "VARCHAR")
        await ensure_column(conn, "role_awards", "created_at", "DATETIME")

        await conn.execute(
            text(
                """
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username VARCHAR,
                first_name VARCHAR,
                last_name VARCHAR,
                first_seen DATETIME,
                last_seen DATETIME
            )
            """
            )
        )
        await ensure_column(conn, "users", "username", "VARCHAR")
        await ensure_column(conn, "users", "first_name", "VARCHAR")
        await ensure_column(conn, "users", "last_name", "VARCHAR")
        await ensure_column(conn, "users", "first_seen", "DATETIME")
        await ensure_column(conn, "users", "last_seen", "DATETIME")

        if DB_URL.startswith("sqlite"):
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA synchronous=NORMAL"))
            await conn.execute(text("PRAGMA foreign_keys=ON"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))

            await conn.execute(
                text(
                    """
                DELETE FROM msg_log
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM msg_log
                    WHERE message_id IS NOT NULL
                    GROUP BY chat_id, message_id
                )
                AND message_id IS NOT NULL
            """
                )
            )
            await conn.execute(
                text(
                    "CREATE UNIQUE INDEX IF NOT EXISTS ux_msg_log_chat_message "
                    "ON msg_log(chat_id, message_id) WHERE message_id IS NOT NULL"
                )
            )

        indexes = [
            "CREATE INDEX IF NOT EXISTS ix_msg_log_user_created ON msg_log(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_msg_log_user_chat_created ON msg_log(user_id, chat_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_msg_log_category_created ON msg_log(category, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_msg_log_opportunity_created ON msg_log(opportunity, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_placements_user_created ON placements(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_placements_user_tier_used ON placements(user_id, tier, used_by_award_id)",
            "CREATE INDEX IF NOT EXISTS ix_notes_user_created ON notes(user_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_snapshots_user_period ON monthly_snapshots(user_id, period)",
            "CREATE INDEX IF NOT EXISTS ix_users_username ON users(username)",
            "CREATE INDEX IF NOT EXISTS ix_users_last_seen ON users(last_seen)",
            "CREATE INDEX IF NOT EXISTS ix_role_awards_user ON role_awards(user_id, created_at)",
        ]
        for statement in indexes:
            try:
                await conn.execute(text(statement))
            except Exception as idx_exc:
                log.warning("Index skipped (%s): %s", statement, idx_exc)

    if DB_URL.startswith("sqlite") and not DB_URL.startswith("sqlite:///:memory:"):
        db_path = DB_URL.split("///", 1)[-1]
        try:
            os.chmod(db_path, 0o600)
        except OSError:
            pass


# ============================================================
# USERS TABLE HELPERS  (item 3)
# ============================================================
async def upsert_user(
    session: AsyncSession,
    user_id: int,
    username: str | None,
    first_name: str | None,
    last_name: str | None = None,
) -> None:
    now = utc_now()
    # A username can be removed or transferred to another Telegram ID.
    if username:
        await session.execute(
            update(User)
            .where(User.user_id != user_id, func.lower(User.username) == username.lower())
            .values(username=None)
        )
    if session.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert

        stmt = insert(User).values(
            user_id=user_id, username=username, first_name=first_name,
            last_name=last_name, first_seen=now, last_seen=now,
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[User.user_id],
            set_={
                "username": stmt.excluded.username,
                "first_name": stmt.excluded.first_name,
                "last_name": stmt.excluded.last_name,
                "last_seen": stmt.excluded.last_seen,
            },
        ).returning(User)
        # Atomic insert/update prevents concurrent first messages losing a log
        # to a users-PK collision; refresh identities already loaded in session.
        result = await session.scalars(stmt, execution_options={"populate_existing": True})
        result.all()
        return
    existing = await session.get(User, user_id)
    if existing is None:
        session.add(
            User(
                user_id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                first_seen=now,
                last_seen=now,
            )
        )
    else:
        existing.username = username
        existing.first_name = first_name
        existing.last_name = last_name
        existing.last_seen = now


# ============================================================
# TRACKING MIDDLEWARE
# ============================================================
async def track_message(session: AsyncSession, message: Message) -> None:
    if TRACK_CHAT_ID is not None and message.chat.id != TRACK_CHAT_ID:
        return
    if message.chat.type not in {"group", "supergroup"}:
        return
    if not message.from_user or message.from_user.is_bot:
        return
    if is_service_message(message) or is_command_message(message):
        return

    category = get_category(message)
    if category is None:
        return

    await upsert_user(
        session,
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
    )

    # Updates can be queued across day/month boundaries while the bot is down.
    created_at = message.date.astimezone(timezone.utc).replace(tzinfo=None)
    row = MsgLog(
        message_id=message.message_id,
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        chat_id=message.chat.id,
        thread_id=message.message_thread_id,
        category=category,
        opportunity=contains_opportunity_tag(message),
        text_preview=get_text_preview(message),
        created_at=created_at,
    )
    try:
        # Roll back only a duplicate log, preserving fresh user metadata.
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        duplicate = await session.scalar(
            select(MsgLog.id).where(
                MsgLog.chat_id == message.chat.id,
                MsgLog.message_id == message.message_id,
            )
        )
        if duplicate is None:
            await session.rollback()
            raise
    try:
        await session.commit()
    except Exception:
        await session.rollback()
        log.exception(
            "Message tracking failed: chat=%s msg=%s", message.chat.id, message.message_id
        )


class DatabaseMiddleware(BaseMiddleware):
    async def __call__(self, handler, event: TelegramObject, data: dict):
        async with Session() as session:
            data["session"] = session
            if isinstance(event, Message):
                try:
                    if is_command_message(event) and event.from_user and not event.from_user.is_bot:
                        if state := data.get("state"):
                            await state.clear()
                        await upsert_user(
                            session, event.from_user.id, event.from_user.username,
                            event.from_user.first_name, event.from_user.last_name,
                        )
                        await session.commit()
                    await track_message(session, event)
                except Exception:
                    await session.rollback()
                    log.exception("Tracking middleware failed")
            return await handler(event, data)


router.message.outer_middleware(DatabaseMiddleware())


# ============================================================
# USER LOOKUP  (items 3, 19)
# ============================================================
async def resolve_name(session: AsyncSession, uid: int) -> str | None:
    user = await session.get(User, uid)
    if user:
        return display_name(user.username, user.first_name)

    result = await session.execute(
        select(MsgLog.username, MsgLog.first_name)
        .where(MsgLog.user_id == uid)
        .order_by(MsgLog.created_at.desc())
        .limit(1)
    )
    row = result.one_or_none()
    if row:
        return display_name(row.username, row.first_name)

    result = await session.execute(
        select(Placement.username)
        .where(Placement.user_id == uid, Placement.username.is_not(None))
        .order_by(Placement.created_at.desc())
        .limit(1)
    )
    username = result.scalar_one_or_none()
    if username:
        return username if username.startswith("@") else display_name(username, None)

    result = await session.execute(
        select(Note.username)
        .where(Note.user_id == uid, Note.username.is_not(None))
        .order_by(Note.created_at.desc())
        .limit(1)
    )
    username = result.scalar_one_or_none()
    if username:
        return username if username.startswith("@") else display_name(username, None)
    return None


async def find_user(session: AsyncSession, message: Message, raw: str | None):
    raw = raw.strip() if raw else None
    if not raw and message.reply_to_message and message.reply_to_message.from_user:
        user = message.reply_to_message.from_user
        # Resolving a reply is a read operation. Its embedded user profile may
        # also be older than the latest tracked profile.
        name = await resolve_name(session, user.id)
        return user.id, name or display_name(user.username, user.first_name), None

    if not raw:
        return None, None, "💡 Укажи @username или ответь командой на сообщение пользователя."

    arg = raw.strip().lstrip("@")
    if arg.isdigit():
        uid = int(arg)
        if not 0 < uid < 2**63:
            return None, None, "Укажи положительный Telegram ID (до 19 цифр)."
        name = await resolve_name(session, uid)
        return uid, name or f"ID {uid}", None

    # Exact username from users table first
    exact_user = await session.execute(
        select(User).where(func.lower(User.username) == arg.lower()).limit(2)
    )
    exact_users = exact_user.scalars().all()
    if len(exact_users) > 1:
        return None, None, "🤔 Username неоднозначен. Ответь на сообщение пользователя или укажи Telegram ID."
    if exact_users:
        urow = exact_users[0]
        return urow.user_id, display_name(urow.username, urow.first_name), None

    exact = await session.execute(
        select(MsgLog.user_id, MsgLog.username, MsgLog.first_name)
        .outerjoin(User, User.user_id == MsgLog.user_id)
        .where(User.user_id.is_(None), func.lower(MsgLog.username) == arg.lower())
        .order_by(MsgLog.created_at.desc())
        .limit(1)
    )
    row = exact.one_or_none()
    if row:
        return row.user_id, display_name(row.username, row.first_name), None

    if raw.strip().startswith("@"):
        return None, None, "❌ Пользователь не найден. Попробуй ответом на его сообщение или укажи Telegram ID."

    escaped = arg.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = f"%{escaped}%"

    # Prefer users table for fuzzy
    fuzzy_users = await session.execute(
        select(User)
        .where(
            or_(
                func.lower(User.username).like(pattern, escape="\\"),
                func.lower(User.first_name).like(pattern, escape="\\"),
            )
        )
        .order_by(User.last_seen.desc())
        .limit(5)
    )
    urows = fuzzy_users.scalars().all()
    if len(urows) == 1:
        u = urows[0]
        return u.user_id, display_name(u.username, u.first_name), None
    if len(urows) > 1:
        names = ", ".join(escape_html(display_name(u.username, u.first_name)) for u in urows)
        return None, None, f"🤔 Нашёл несколько пользователей: {names}"

    result = await session.execute(
        select(MsgLog.user_id)
        .outerjoin(User, User.user_id == MsgLog.user_id)
        .where(
            User.user_id.is_(None),
            or_(
                func.lower(MsgLog.username).like(pattern, escape="\\"),
                func.lower(MsgLog.first_name).like(pattern, escape="\\"),
            )
        )
        .group_by(MsgLog.user_id)
        .order_by(func.max(MsgLog.created_at).desc())
        .limit(5)
    )
    rows = result.fetchall()
    if len(rows) == 1:
        r = rows[0]
        return r.user_id, await resolve_name(session, r.user_id), None
    if len(rows) > 1:
        names = ", ".join([
            escape_html(await resolve_name(session, r.user_id) or f"ID {r.user_id}")
            for r in rows
        ])
        return None, None, f"🤔 Нашёл несколько пользователей: {names}"
    return None, None, "❌ Пользователь не найден. Попробуй ответом на его сообщение или укажи Telegram ID."


async def resolve_command_user(message: Message, session: AsyncSession, command: CommandObject):
    tokens = command.args.split() if command.args else []
    return await find_user(session, message, " ".join(tokens))


# ============================================================
# ACTIVITY STATS  (items 1, 6, 8)
# ============================================================
def _blank_user(username=None, first_name=None) -> dict:
    return {
        "un": username,
        "fn": first_name,
        "messages": 0,
        "loops": 0,
        "zips": 0,
        "files": 0,
        "media": 0,
        "total": 0,
        "opportunities": 0,
    }


async def user_pivot(
    session: AsyncSession, start: datetime | None, end: datetime | None,
    *, user_id: int | None = None,
) -> dict[int, dict]:
    q = select(
        MsgLog.user_id,
        MsgLog.username,
        MsgLog.first_name,
        MsgLog.category,
        func.count(MsgLog.id),
    )
    if user_id is not None:
        q = q.where(MsgLog.user_id == user_id)
    if TRACK_CHAT_ID is not None:
        q = q.where(MsgLog.chat_id == TRACK_CHAT_ID)
    if start is not None:
        q = q.where(MsgLog.created_at >= start)
    if end is not None:
        q = q.where(MsgLog.created_at < end)
    q = q.group_by(MsgLog.user_id, MsgLog.username, MsgLog.first_name, MsgLog.category)

    users: dict[int, dict] = {}
    for uid, username, first_name, category, count in (await session.execute(q)).fetchall():
        user = users.setdefault(uid, _blank_user(username, first_name))
        if category == "message":
            user["messages"] += count
        elif category == "loop":
            user["loops"] += count
        elif category == "zip":
            user["zips"] += count
        elif category == "files":
            user["files"] += count
        elif category == "media":
            user["media"] += count
        else:
            # legacy / unknown → files bucket to avoid silent loss
            user["files"] += count
        user["total"] += count

    opp_q = select(MsgLog.user_id, func.count(MsgLog.id)).where(MsgLog.opportunity.is_(True))
    if user_id is not None:
        opp_q = opp_q.where(MsgLog.user_id == user_id)
    if TRACK_CHAT_ID is not None:
        opp_q = opp_q.where(MsgLog.chat_id == TRACK_CHAT_ID)
    if start is not None:
        opp_q = opp_q.where(MsgLog.created_at >= start)
    if end is not None:
        opp_q = opp_q.where(MsgLog.created_at < end)
    opp_q = opp_q.group_by(MsgLog.user_id)
    for uid, count in (await session.execute(opp_q)).fetchall():
        users.setdefault(uid, _blank_user())["opportunities"] = count
    if users:
        profiles = await session.execute(
            select(User.user_id, User.username, User.first_name).where(User.user_id.in_(users))
        )
        for uid, username, first_name in profiles:
            users[uid].update(un=username, fn=first_name)
    return users


async def user_days(
    session: AsyncSession, start: datetime | None, end: datetime | None,
    *, user_id: int | None = None,
) -> dict[int, int]:
    """Count distinct local calendar days per user.

    For SQLite we still pull timestamps (portable + correct TZ conversion).
    Indexed by (user_id, created_at) so this stays acceptable for typical private-group volumes.
    """
    q = select(MsgLog.user_id, MsgLog.created_at)
    if user_id is not None:
        q = q.where(MsgLog.user_id == user_id)
    if TRACK_CHAT_ID is not None:
        q = q.where(MsgLog.chat_id == TRACK_CHAT_ID)
    if start is not None:
        q = q.where(MsgLog.created_at >= start)
    if end is not None:
        q = q.where(MsgLog.created_at < end)
    rows = (await session.execute(q)).fetchall()
    dates: dict[int, set[date]] = defaultdict(set)
    for uid, created_at in rows:
        dates[uid].add(local_date_from_utc_naive(created_at))
    return {uid: len(days) for uid, days in dates.items()}


async def get_user_dates(
    session: AsyncSession, uid: int, start: datetime | None, end: datetime | None
) -> list[date]:
    q = select(MsgLog.created_at).where(MsgLog.user_id == uid)
    if TRACK_CHAT_ID is not None:
        q = q.where(MsgLog.chat_id == TRACK_CHAT_ID)
    if start is not None:
        q = q.where(MsgLog.created_at >= start)
    if end is not None:
        q = q.where(MsgLog.created_at < end)
    return [local_date_from_utc_naive(row[0]) for row in (await session.execute(q)).fetchall()]


def calc_streak(dates: list[date], reference: date | None = None) -> int:
    if not dates:
        return 0
    unique = sorted(set(dates), reverse=True)
    current = reference or local_now().date()
    if unique[0] not in {current, current - timedelta(days=1)}:
        return 0
    expected = unique[0]
    streak = 0
    for day in unique:
        if day != expected:
            break
        streak += 1
        expected -= timedelta(days=1)
    return streak


# ============================================================
# PLACEMENTS / ROLE CYCLE  (items 4, 11, 12, 13)
# ============================================================
async def placement_counts(session: AsyncSession, uid: int) -> dict:
    rows = await session.execute(
        select(Placement.tier, func.count(Placement.id))
        .where(Placement.user_id == uid)
        .group_by(Placement.tier)
    )
    result = {"small": 0, "large": 0, "total": 0}
    for tier, count in rows.fetchall():
        if tier == "large":
            result["large"] += int(count)
        else:
            result["small"] += int(count)
    result["total"] = result["small"] + result["large"]
    return result


async def get_placement_progress_state_readonly(
    session: AsyncSession, uid: int
) -> PlacementProgress | None:
    """Read-only: never creates a row."""
    return await session.get(PlacementProgress, uid)


async def get_or_create_placement_progress(
    session: AsyncSession, uid: int
) -> PlacementProgress:
    """Write path only (used by /role reset)."""
    state = await session.get(PlacementProgress, uid)
    if state is None:
        state = PlacementProgress(
            user_id=uid,
            small_reset_at=None,
            big_reset_at=None,
            updated_at=utc_now(),
        )
        session.add(state)
        await session.flush()
    return state


async def placement_progress(session: AsyncSession, uid: int) -> dict:
    """All-time counts + current role-cycle progress. No writes."""
    counts = await placement_counts(session, uid)
    state = await get_placement_progress_state_readonly(session, uid)

    small_q = select(func.count(Placement.id)).where(
        Placement.user_id == uid,
        Placement.tier != "large",
    )
    big_q = select(func.count(Placement.id)).where(
        Placement.user_id == uid,
        Placement.tier == "large",
    )
    if state and state.small_reset_at:
        small_q = small_q.where(Placement.created_at > state.small_reset_at)
    if state and state.big_reset_at:
        big_q = big_q.where(Placement.created_at > state.big_reset_at)

    small = int(await session.scalar(small_q) or 0)
    big = int(await session.scalar(big_q) or 0)

    return {
        **counts,
        "small_since": min(small, SMALL_THRESHOLD),
        "large_since": min(big, LARGE_THRESHOLD),
        "small_raw": small,
        "large_raw": big,
        "small_target": SMALL_THRESHOLD,
        "large_target": LARGE_THRESHOLD,
        "small_reset_at": state.small_reset_at if state else None,
        "large_reset_at": state.big_reset_at if state else None,
        "ready_small": small >= SMALL_THRESHOLD,
        "ready_big": big >= LARGE_THRESHOLD,
        "ready": small >= SMALL_THRESHOLD or big >= LARGE_THRESHOLD,
    }


async def reset_placement_progress(
    session: AsyncSession, uid: int, kind: str, actor_id: int | None, name: str | None
) -> dict:
    async with user_write_lock(uid):
        # End earlier read snapshots; a second admin must see the first reset.
        await session.commit()
        state = await session.get(PlacementProgress, uid)
        if state is not None:
            await session.refresh(state)
        p = await placement_progress(session, uid)
        if kind not in {"small", "big"}:
            raise ValueError("Укажи Small или Big.")
        if not p["ready_small" if kind == "small" else "ready_big"]:
            raise ValueError("Порог этой роли ещё не достигнут или роль уже подтверждена.")
        return await _reset_placement_progress(session, uid, kind, actor_id, name)


async def _reset_placement_progress(
    session: AsyncSession, uid: int, kind: str, actor_id: int | None, name: str | None
) -> dict:
    state = await get_or_create_placement_progress(session, uid)
    now = utc_now()
    p_before = await placement_progress(session, uid)

    if kind == "small":
        state.small_reset_at = now
    else:
        state.big_reset_at = now
    state.updated_at = now

    session.add(
        RoleAward(
            user_id=uid,
            username=name,
            kind=kind,
            actor_id=actor_id,
            small_count=p_before["small"],
            large_count=p_before["large"],
            note=f"manual role confirm + {kind} progress reset",
            created_at=now,
        )
    )
    await session.commit()
    return await placement_progress(session, uid)


# ============================================================
# STATS BUILDERS
# ============================================================
async def get_user_stats(
    session: AsyncSession, uid: int, period: str, include_placements: bool = False
) -> dict:
    start, end, label = period_bounds(period)
    users = await user_pivot(session, start, end, user_id=uid)
    days = await user_days(session, start, end, user_id=uid)
    dates = await get_user_dates(session, uid, None, utc_now())
    base = users.get(uid, _blank_user()).copy()
    if uid not in users:
        profile = await session.get(User, uid)
        if profile:
            base.update(un=profile.username, fn=profile.first_name)
    base["active_days"] = days.get(uid, 0)
    base["streak"] = calc_streak(dates)
    base["period_label"] = label
    if include_placements:
        placement = await placement_progress(session, uid)
        base.update(
            {
                "placements_total": placement["total"],
                "placements_small": placement["small"],
                "placements_large": placement["large"],
                "small_since": placement["small_since"],
                "large_since": placement["large_since"],
                "small_target": placement["small_target"],
                "large_target": placement["large_target"],
                "ready": placement["ready"],
            }
        )
    return base


async def build_stats_text(
    session: AsyncSession, uid: int, name: str | None, period: str, hints: bool
) -> str:
    if not name:
        name = await resolve_name(session, uid) or f"ID {uid}"
    s = await get_user_stats(session, uid, period)

    return stats_card(name, s)


async def build_top(session: AsyncSession, sort: str, period: str) -> str:
    start, end, label = period_bounds(period)
    users = await user_pivot(session, start, end)
    days = await user_days(session, start, end)
    if not users:
        return f"Нет активности за {escape_html(label)}."

    def sort_key(item):
        uid, user = item
        primary = days.get(uid, 0) if sort == "days" else user.get(sort, 0)
        # stable tie-breakers
        return (primary, user.get("total", 0), user.get("loops", 0), uid)

    users = {uid: user for uid, user in users.items() if (days.get(uid, 0) if sort == "days" else user.get(sort, 0)) > 0}
    if not users:
        return f"🏆 <b>Рейтинг</b>\n<i>{escape_html(label)}</i>\n\nПока нет активности в этой категории."
    current = sorted(users.items(), key=sort_key, reverse=True)[:10]
    arrows: dict[int, str] = {}
    if period in {"7d", "30d"}:
        window = 7 if period == "7d" else 30
        prev_end = start
        prev_start = prev_end - timedelta(days=window) if prev_end else None
        prev_users = await user_pivot(session, prev_start, prev_end)
        prev_days = await user_days(session, prev_start, prev_end)

        def prev_key(item):
            uid, user = item
            primary = prev_days.get(uid, 0) if sort == "days" else user.get(sort, 0)
            return (primary, user.get("total", 0), user.get("loops", 0), uid)

        prev_users = {uid: user for uid, user in prev_users.items() if (prev_days.get(uid, 0) if sort == "days" else user.get(sort, 0)) > 0}
        prev_rank = {
            uid: idx
            for idx, (uid, _) in enumerate(
                sorted(prev_users.items(), key=prev_key, reverse=True), start=1
            )
        }
        for idx, (uid, _) in enumerate(current, start=1):
            prev = prev_rank.get(uid)
            arrows[uid] = "🆕" if prev is None else "↑" if prev > idx else "↓" if prev < idx else "•"

    labels = {
        "total": "Вся активность", "messages": "Сообщения",
        "loops": "Лупы", "zips": "Архивы", "files": "Файлы",
        "media": "Медиа", "days": "Активные дни", "opportunities": "Возможности",
    }
    medals = ["🥇", "🥈", "🥉"]
    out = f"{header('🏆', f'Рейтинг · {labels.get(sort, sort)}')}\n<i>{escape_html(label)}</i>\n\n"
    for idx, (uid, user) in enumerate(current, start=1):
        marker = medals[idx - 1] if idx <= 3 else f"{idx}."
        name = escape_html(short(display_name(user["un"], user["fn"]), 55))
        value = days.get(uid, 0) if sort == "days" else user.get(sort, 0)
        out += f"{marker} {name} — <b>{value}</b> {arrows.get(uid, '')}\n"
    return out


async def build_activity_candidates(session: AsyncSession) -> str:
    start, end, _ = period_bounds("30d")
    users = await user_pivot(session, start, end)
    days = await user_days(session, start, end)
    if not users:
        return (
            "🌟 <b>Активные участники</b>\n"
            "<i>Последние 30 дней</i>\n\n"
            "Нет активности за последние 30 дней."
        )

    def score(uid: int, u: dict) -> tuple:
        return (
            u["loops"] * 3
            + u["zips"] * 2
            + u["files"] * 1.5
            + u["opportunities"] * 2
            + days.get(uid, 0)
            + u["media"]
            + u["messages"] * 0.25,
            u["loops"],
            u["zips"],
            u["opportunities"],
            days.get(uid, 0),
            u["total"],
        )

    ranked = sorted(users.items(), key=lambda item: score(item[0], item[1]), reverse=True)[:3]
    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for idx, (uid, u) in enumerate(ranked):
        name = escape_html(short(display_name(u["un"], u["fn"]), 55))
        points = score(uid, u)[0]
        lines.append(f"{medals[idx]} <b>{name}</b> · {points:g} баллов\n{u['total']} действий · {days.get(uid, 0)} активных дней")
    out = "🌟 <b>Активные участники</b>\n<i>Последние 30 дней</i>\n\n" + "\n\n".join(lines)
    out += "\n\n<i>Баллы: луп 3 · архив 2 · файл 1,5 · возможность 2 · медиа 1 · сообщение 0,25 · активный день 1. Возможность — дополнительный признак сообщения.</i>"
    return out

async def build_last_text(session: AsyncSession, uid: int, name: str | None) -> str:
    if not name:
        name = await resolve_name(session, uid) or f"ID {uid}"
    rows = (
        await session.execute(
            select(MsgLog)
            .where(MsgLog.user_id == uid)
            .order_by(MsgLog.created_at.desc())
            .limit(15)
        )
    ).scalars().all()
    if not rows:
        return f"🕐 <b>{escape_html(name)}</b>\n\nNo messages"
    out = f"🕐 <b>Last messages: {escape_html(name)}</b>\n\n"
    for row in rows:
        local_time = local_from_utc_naive(row.created_at)
        out += (
            f"<code>{local_time:%d.%m %H:%M}</code> · "
            f"<b>{escape_html(row.category)}</b> · "
            f"{escape_html(row.text_preview or '—')}\n"
        )
    return out


async def build_notes_text(session: AsyncSession, uid: int, name: str | None) -> str:
    if not name:
        name = await resolve_name(session, uid) or f"ID {uid}"
    rows = (
        await session.execute(
            select(Note).where(Note.user_id == uid).order_by(Note.created_at.desc()).limit(20)
        )
    ).scalars().all()
    out = f"📝 <b>Notes: {escape_html(name)}</b> ({len(rows)})\n\n"
    if not rows:
        return out + "Empty"
    for row in rows:
        out += f"• {short_date(row.created_at)} — {escape_html(row.text or '—')}\n"
    return out


async def build_history_text(session: AsyncSession, uid: int, name: str | None) -> str:
    if not name:
        name = await resolve_name(session, uid) or f"ID {uid}"
    rows = (
        await session.execute(
            select(MonthlySnapshot)
            .where(MonthlySnapshot.user_id == uid)
            .order_by(MonthlySnapshot.period.desc())
        )
    ).scalars().all()
    out = f"🗂 <b>History: {escape_html(name)}</b>\n\n"
    if not rows:
        return out + "No monthly archives yet"
    for row in rows:
        files = getattr(row, "files", 0) or 0
        out += (
            f"• {row.period} — total <b>{row.total}</b> "
            f"(M {row.messages} / L {row.loops} / Z {row.zips} / F {files} / PV {row.media}"
            f" / Opp {row.opportunities}) · days {row.active_days}\n"
        )
    return out


async def build_places_text(session: AsyncSession, uid: int, name: str | None) -> str:
    if not name:
        name = await resolve_name(session, uid) or f"ID {uid}"
    p = await placement_progress(session, uid)
    rows = (
        await session.execute(
            select(Placement)
            .where(Placement.user_id == uid)
            .order_by(Placement.created_at.desc())
            .limit(20)
        )
    ).scalars().all()

    ready = ""
    if p["ready_big"]:
        ready = "✅ Ready via Big"
    elif p["ready_small"]:
        ready = "✅ Ready via Small"
    else:
        need_s = max(0, p["small_target"] - p["small_raw"])
        ready = f"⏳ Need {need_s} Small or 1 Big"

    out = (
        f"🎯 <b>Placements · {escape_html(name)}</b>\n\n"
        f"<b>ALL TIME</b>\n"
        f"Small: <b>{p['small']}</b> · Big: <b>{p['large']}</b> · Total: <b>{p['total']}</b>\n\n"
        f"<b>CURRENT ROLE CYCLE</b>\n"
        f"Small: <b>{p['small_since']}/{p['small_target']}</b> "
        f"(raw {p['small_raw']})\n"
        f"Big: <b>{p['large_since']}/{p['large_target']}</b> "
        f"(raw {p['large_raw']})\n"
        f"{ready}\n"
    )
    if p["small_reset_at"]:
        out += f"\nSmall reset: <code>{format_local(p['small_reset_at'])}</code>"
    if p["large_reset_at"]:
        out += f"\nBig reset: <code>{format_local(p['large_reset_at'])}</code>"

    out += "\n\n<b>History</b>\n"
    if not rows:
        out += "Empty"
    else:
        for row in rows:
            tier = "🟣 BIG" if row.tier == "large" else "⚪️ small"
            body = ""
            if row.artist or row.track:
                body = f"{row.artist or '—'} — {row.track or '—'}"
            elif row.comment:
                body = row.comment
            else:
                body = "placement"
            out += f"• {short_date(row.created_at)} {tier} — {escape_html(body)}\n"
    out += "\n<i>/role @user small|big — confirm role &amp; reset cycle</i>"
    return out


async def build_role_dashboard(session: AsyncSession) -> str:
    """Ready / almost / progress board based on current cycle."""
    # Collect users who have any placements
    uid_rows = (await session.execute(select(Placement.user_id).distinct())).scalars().all()
    if not uid_rows:
        return "🎖 <b>Placement roles</b>\n\nПока нет placements."

    ready: list[tuple[str, dict]] = []
    almost: list[tuple[str, dict]] = []
    progress: list[tuple[str, dict]] = []

    for uid in uid_rows:
        p = await placement_progress(session, uid)
        name = await resolve_name(session, uid) or f"ID {uid}"
        entry = (name, p)
        if p["ready"]:
            ready.append(entry)
        elif p["small_raw"] >= max(1, p["small_target"] - 1) or p["large_raw"] >= 1:
            almost.append(entry)
        elif p["total"] > 0:
            progress.append(entry)

    def sort_key(item):
        _, p = item
        # nearest to goal first
        need_s = max(0, p["small_target"] - p["small_raw"])
        need_b = max(0, p["large_target"] - p["large_raw"])
        return (min(need_s, need_b * 3), -p["total"])

    ready.sort(key=lambda x: -x[1]["total"])
    almost.sort(key=sort_key)
    progress.sort(key=sort_key)

    out = "🎖 <b>Placement role board</b>\n\n"
    if ready:
        out += "<b>✅ Ready</b>\n"
        for name, p in ready[:15]:
            via = "Big" if p["ready_big"] else "Small"
            out += (
                f"• <b>{escape_html(name)}</b> — via {via} · "
                f"S {p['small_since']}/{p['small_target']} · B {p['large_since']}/{p['large_target']} "
                f"(all-time {p['total']})\n"
            )
        out += "\n"
    if almost:
        out += "<b>🟡 Almost</b>\n"
        for name, p in almost[:10]:
            out += (
                f"• <b>{escape_html(name)}</b> — "
                f"S {p['small_since']}/{p['small_target']} · B {p['large_since']}/{p['large_target']} "
                f"(all-time {p['total']})\n"
            )
        out += "\n"
    if progress:
        out += "<b>⏳ Progress</b>\n"
        for name, p in progress[:10]:
            out += (
                f"• <b>{escape_html(name)}</b> — "
                f"S {p['small_since']}/{p['small_target']} · B {p['large_since']}/{p['large_target']} "
                f"(all-time {p['total']})\n"
            )
    if not ready and not almost and not progress:
        out += "Нет данных."
    out += "\n<i>Role is manual. /role resets the cycle after you grant it.</i>"
    return out


async def build_roles_text(session: AsyncSession, uid: int, name: str | None) -> str:
    if not name:
        name = await resolve_name(session, uid) or f"ID {uid}"
    p = await placement_progress(session, uid)
    out = (
        f"🎖 <b>Placement Role · {escape_html(name)}</b>\n\n"
        f"<b>ALL TIME</b>\n"
        f"Small: <b>{p['small']}</b> · Big: <b>{p['large']}</b> · Total: <b>{p['total']}</b>\n\n"
        f"<b>CURRENT ROLE CYCLE</b>\n"
        f"Small · <b>{p['small_since']}/{p['small_target']}</b> (raw {p['small_raw']})\n"
        f"Big · <b>{p['large_since']}/{p['large_target']}</b> (raw {p['large_raw']})\n"
    )
    if p["ready_big"]:
        out += "\n✅ Ready via Big"
    elif p["ready_small"]:
        out += "\n✅ Ready via Small"
    else:
        out += f"\n⏳ Next: {max(0, p['small_target'] - p['small_raw'])} Small or 1 Big"
    if p["small_reset_at"]:
        out += f"\n\nLast Small reset: <code>{format_local(p['small_reset_at'])}</code>"
    if p["large_reset_at"]:
        out += f"\nLast Big reset: <code>{format_local(p['large_reset_at'])}</code>"

    awards = (
        await session.execute(
            select(RoleAward)
            .where(RoleAward.user_id == uid)
            .order_by(RoleAward.created_at.desc())
            .limit(5)
        )
    ).scalars().all()
    if awards:
        out += "\n\n<b>Recent role confirms</b>\n"
        for a in awards:
            out += f"• {short_date(a.created_at)} · {a.kind.upper()} (S{a.small_count}/B{a.large_count})\n"

    out += (
        "\n\n<i>/role @user small — reset Small after manual role\n"
        "/role @user big — reset Big after manual role</i>"
    )
    return out


async def build_profile_text(session: AsyncSession, uid: int, name: str | None) -> str:
    if not name:
        name = await resolve_name(session, uid) or f"ID {uid}"
    month = await get_user_stats(session, uid, "month", include_placements=True)
    d7 = await get_user_stats(session, uid, "7d")
    d30 = await get_user_stats(session, uid, "30d")
    all_time = await get_user_stats(session, uid, "all")
    p = await placement_progress(session, uid)

    month_bits = metric_line(
        [
            ("Loops", month["loops"]),
            ("Zips", month["zips"]),
            ("Files", month["files"]),
            ("Media", month["media"]),
            ("Msg", month["messages"]),
            ("Opps", month["opportunities"]),
        ],
        hide_zero=True,
    )
    out = (
        f"{header('🗂', f'Profile · {escape_html(name)}')}\n\n"
        f"<b>Этот месяц</b> · {escape_html(month['period_label'])}\n"
        f"Total <b>{month['total']}</b> · Days <b>{month['active_days']}</b> · "
        f"Streak <b>{month['streak']}</b>\n"
        f"{month_bits}\n\n"
        f"<b>7d</b>  {d7['total']} total · {d7['active_days']} days\n"
        f"<b>30d</b> {d30['total']} total · {d30['active_days']} days\n"
        f"<b>All</b>  {all_time['total']} total · {all_time['active_days']} days\n\n"
        f"<b>Placements</b>  Small {p['small']} · Big {p['large']} · Total {p['total']}\n"
        f"<b>Role cycle</b>  S {p['small_since']}/{p['small_target']} · "
        f"B {p['large_since']}/{p['large_target']}"
    )
    if p["ready"]:
        out += "\n✅ Ready for role"
    return out


# ============================================================
# REPORT / SNAPSHOT / EXPORT
# ============================================================
async def build_weekly_report(session: AsyncSession) -> str | None:
    start, end, label = previous_calendar_week_range()
    users = await user_pivot(session, start, end)
    days = await user_days(session, start, end)
    if not users:
        return f"{header('📋', 'Отчёт за неделю')}\n<i>{label}</i>\n\nЗа этот период ещё нет активности."

    ranked = sorted(users.items(), key=lambda item: item[1]["total"], reverse=True)[:10]
    loop_leader = max(users.items(), key=lambda item: item[1]["loops"])
    zip_leader = max(users.items(), key=lambda item: item[1]["zips"])
    files_leader = max(users.items(), key=lambda item: item[1]["files"])
    media_leader = max(users.items(), key=lambda item: item[1]["media"])
    day_leader = max(users.items(), key=lambda item: days.get(item[0], 0))
    opp_leader = max(users.items(), key=lambda item: item[1]["opportunities"])

    medals = ["🥇", "🥈", "🥉"]
    out = f"{header('📋', 'Отчёт за неделю')}\n<i>{label}</i>\n\n"
    for idx, (uid, user) in enumerate(ranked, start=1):
        marker = medals[idx - 1] if idx <= 3 else f"{idx}."
        out += f"{marker} {escape_html(short(display_name(user['un'], user['fn']), 55))} — <b>{user['total']}</b>\n"

    extras = []
    if loop_leader[1]["loops"]:
        extras.append(
            f"Лупы: <b>{escape_html(short(display_name(loop_leader[1]['un'], loop_leader[1]['fn']), 55))}</b> — {loop_leader[1]['loops']}"
        )
    if zip_leader[1]["zips"]:
        extras.append(
            f"Архивы: <b>{escape_html(short(display_name(zip_leader[1]['un'], zip_leader[1]['fn']), 55))}</b> — {zip_leader[1]['zips']}"
        )
    if files_leader[1]["files"]:
        extras.append(
            f"Файлы: <b>{escape_html(short(display_name(files_leader[1]['un'], files_leader[1]['fn']), 55))}</b> — {files_leader[1]['files']}"
        )
    if media_leader[1]["media"]:
        extras.append(
            f"Медиа: <b>{escape_html(short(display_name(media_leader[1]['un'], media_leader[1]['fn']), 55))}</b> — {media_leader[1]['media']}"
        )
    if days.get(day_leader[0], 0):
        extras.append(
            f"Активные дни: <b>{escape_html(short(display_name(day_leader[1]['un'], day_leader[1]['fn']), 55))}</b> — {days[day_leader[0]]}"
        )
    if opp_leader[1]["opportunities"]:
        extras.append(
            f"Возможности: <b>{escape_html(short(display_name(opp_leader[1]['un'], opp_leader[1]['fn']), 55))}</b> — {opp_leader[1]['opportunities']}"
        )
    if extras:
        out += "\n\n" + "\n".join(extras)
    return out


async def make_snapshot(session: AsyncSession) -> str:
    start, end, period = previous_calendar_month_range()
    await session.execute(delete(MonthlySnapshot).where(MonthlySnapshot.period == period))
    users = await user_pivot(session, start, end)
    days = await user_days(session, start, end)
    for uid, user in users.items():
        session.add(
            MonthlySnapshot(
                user_id=uid,
                username=user["un"],
                period=period,
                messages=user["messages"],
                loops=user["loops"],
                zips=user["zips"],
                files=user["files"],
                media=user["media"],
                opportunities=user["opportunities"],
                active_days=days.get(uid, 0),
                total=user["total"],
            )
        )
    await session.merge(Setting(key="snapshot_last_period", value=period))
    await session.commit()
    return period


async def build_export(session: AsyncSession) -> BufferedInputFile:
    users = await user_pivot(session, None, utc_now())
    days = await user_days(session, None, utc_now())
    placement_user_rows = (
        await session.execute(select(Placement.user_id, Placement.username).distinct())
    ).all()
    for uid, username in placement_user_rows:
        users.setdefault(uid, _blank_user(username))

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "user_id",
            "username",
            "first_name",
            "messages",
            "loops",
            "zips",
            "files",
            "photo_video",
            "opportunities",
            "total",
            "active_days",
            "small_placements",
            "large_placements",
            "total_placements",
            "small_progress",
            "big_progress",
            "role_ready",
        ]
    )
    for uid, user in sorted(users.items(), key=lambda item: item[1]["total"], reverse=True):
        p = await placement_progress(session, uid)
        writer.writerow(
            [
                uid,
                user["un"] or "",
                user["fn"] or "",
                user["messages"],
                user["loops"],
                user["zips"],
                user["files"],
                user["media"],
                user["opportunities"],
                user["total"],
                days.get(uid, 0),
                p["small"],
                p["large"],
                p["total"],
                p["small_since"],
                p["large_since"],
                int(p["ready"]),
            ]
        )
    return BufferedInputFile(
        buffer.getvalue().encode("utf-8-sig"),
        filename=f"301tracker_{local_now():%Y-%m-%d}.csv",
    )


# ============================================================
# DELIVERY HELPERS
# ============================================================
async def send_private_or_ack(message: Message, bot: Bot, text_value: str) -> None:
    if message.chat.type == "private":
        await message.answer(text_value)
        return
    try:
        await bot.send_message(message.from_user.id, text_value)
        await message.reply("📬 Отправил в личные сообщения.")
    except Exception:
        await message.reply("⚠️ Не смог написать в ЛС. Открой /start у бота и повтори.")


async def send_document_private_or_ack(
    message: Message, bot: Bot, document: BufferedInputFile, caption: str
) -> None:
    if message.chat.type == "private":
        await message.answer_document(document, caption=caption)
        return
    try:
        await bot.send_document(message.from_user.id, document, caption=caption)
        await message.reply("📬 Файл отправлен в личные сообщения.")
    except Exception:
        await message.reply("⚠️ Не смог написать в ЛС. Открой /start у бота и повтори.")


# ============================================================
# COMMANDS
# ============================================================
@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(message: Message, session: AsyncSession, state: FSMContext, bot: Bot):
    await state.clear()
    await upsert_user(session, message.from_user.id, message.from_user.username, message.from_user.first_name, message.from_user.last_name)
    await session.commit()
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id), bot)


@router.message(Command("help"))
async def cmd_help(message: Message, session: AsyncSession, state: FSMContext, bot: Bot):
    await state.clear()
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "help"), bot)


@router.message(Command("my"))
async def cmd_my(message: Message, session: AsyncSession, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    period = command.args.split()[-1].lower() if command.args else "month"
    if period not in PERIOD_KEYS:
        period = "month"
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "stats", message.from_user.id, period), bot)


@router.message(Command("stats"))
async def cmd_stats(message: Message, session: AsyncSession, command: CommandObject, state: FSMContext, bot: Bot):
    await state.clear()
    tokens = command.args.split() if command.args else []
    period = "month"
    if tokens and tokens[-1].lower() in PERIOD_KEYS:
        period = tokens.pop().lower()
    if not is_admin(message) and (tokens or message.reply_to_message):
        await message.answer("Для чужой статистики нужны права администратора. Твоя статистика — /my.")
        return
    if not tokens and not message.reply_to_message:
        uid, name, error = message.from_user.id, message.from_user.full_name, None
    else:
        uid, name, error = await find_user(session, message, " ".join(tokens))
    if error:
        await message.answer(error)
        return
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "stats", uid, period), bot)


@router.message(Command("top"))
async def cmd_top(message: Message, session: AsyncSession, command: CommandObject, state: FSMContext, bot: Bot):
    if not is_admin(message):
        await message.answer("Рейтинг доступен администратору. Твоя статистика — /my.")
        return
    await state.clear()
    period = "month"
    sort = "total"
    for token in command.args.split() if command.args else []:
        t = token.lower()
        if t in PERIOD_KEYS:
            period = t
        elif t in SORT_KEYS:
            sort = t
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "top", period=period, extra=sort), bot)


@router.message(Command("members"))
async def cmd_members(message: Message, session: AsyncSession, state: FSMContext, bot: Bot):
    if not is_admin(message):
        await message.answer("Участники доступны администратору. Твой профиль — /profile.")
        return
    await state.clear()
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "members"), bot)


@router.message(Command("candidates"))
async def cmd_candidates(message: Message, session: AsyncSession, bot: Bot):
    if not is_admin(message):
        return
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "candidates"), bot)


@router.message(Command("places"))
async def cmd_places(message: Message, session: AsyncSession, command: CommandObject, bot: Bot):
    if not is_admin(message):
        return
    uid, name, error = await resolve_command_user(message, session, command)
    if error:
        await message.answer(error)
        return
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "places", uid), bot)


@router.message(Command("last"))
async def cmd_last(message: Message, session: AsyncSession, command: CommandObject, bot: Bot):
    if not is_admin(message):
        return
    uid, name, error = await resolve_command_user(message, session, command)
    if error:
        await message.answer(error)
        return
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "last", uid), bot)


@router.message(Command("notes"))
async def cmd_notes(message: Message, session: AsyncSession, command: CommandObject, bot: Bot):
    if not is_admin(message):
        return
    uid, name, error = await resolve_command_user(message, session, command)
    if error:
        await message.answer(error)
        return
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "notes", uid), bot)


@router.message(Command("history"))
async def cmd_history(message: Message, session: AsyncSession, command: CommandObject, bot: Bot):
    if not is_admin(message):
        return
    uid, name, error = await resolve_command_user(message, session, command)
    if error:
        await message.answer(error)
        return
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "history", uid), bot)


@router.message(Command("roles"))
async def cmd_roles(message: Message, session: AsyncSession, command: CommandObject, bot: Bot):
    if not is_admin(message):
        return
    tokens = command.args.split() if command.args else []
    if tokens:
        uid, name, error = await resolve_command_user(message, session, command)
        if error:
            await message.answer(error)
            return
        await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "roles", uid), bot)
        return
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "board"), bot)


@router.message(Command("role"))
async def cmd_role(message: Message, session: AsyncSession, command: CommandObject):
    if not is_admin(message):
        return

    tokens = command.args.split() if command.args else []
    if message.reply_to_message and tokens and not tokens[0].startswith("@") and not tokens[0].isdigit():
        uid, name, error = await find_user(session, message, None)
        kind = tokens[0].lower()
    else:
        if len(tokens) < 2:
            await message.answer(
                "Формат: /role @user small\n"
                "или /role @user big\n"
                "Это подтверждает ручную выдачу роли и сбрасывает соответствующую шкалу."
            )
            return
        uid, name, error = await find_user(session, message, tokens[0])
        kind = tokens[1].lower()

    if error:
        await message.answer(error)
        return
    if kind not in {"small", "big"}:
        await message.answer("Укажи small или big.")
        return

    try:
        p = await reset_placement_progress(session, uid, kind, message.from_user.id, name)
    except ValueError as exc:
        await message.answer(str(exc))
        return
    await message.answer(
        f"✅ <b>Выдача роли подтверждена</b>\n\n"
        f"{escape_html(name)}\n"
        f"Роль: <b>{kind.title()}</b>\n\n"
        f"<b>За всё время</b>\n"
        f"Small · <b>{p['small']}</b> · Big · <b>{p['large']}</b>\n\n"
        f"<b>Новый цикл</b>\n"
        f"Small · <b>{p['small_since']}/{p['small_target']}</b>\n"
        f"Big · <b>{p['large_since']}/{p['large_target']}</b>\n\n"
        f"<i>Роль в Telegram выдаётся вручную.</i>"
    )


@router.message(Command("profile"))
async def cmd_profile(message: Message, session: AsyncSession, command: CommandObject, bot: Bot, state: FSMContext):
    await state.clear()
    if not is_admin(message) and (command.args or message.reply_to_message):
        await message.answer("Чужой профиль доступен администратору. Твой профиль — /profile без аргументов.")
        return
    if not command.args and not message.reply_to_message:
        uid, name, error = message.from_user.id, message.from_user.full_name, None
    else:
        uid, name, error = await resolve_command_user(message, session, command)
    if error:
        await message.answer(error)
        return
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "profile", uid), bot)


@router.message(Command("place"))
async def cmd_place(message: Message, session: AsyncSession, command: CommandObject):
    if not is_admin(message):
        return
    if throttle(message.from_user.id):
        await message.answer("⏳ Слишком быстро, подожди пару секунд.")
        return
    tokens = command.args.split() if command.args else []
    if not tokens:
        await message.answer(
            "Формат:\n"
            "/place @username small Артист | Трек\n"
            "/place @username big текст-комментарий\n"
            "Или ответь на сообщение: /place small Артист | Трек"
        )
        return
    if message.reply_to_message and (not tokens[0].startswith("@") and not tokens[0].isdigit()):
        uid, name, error = await find_user(session, message, None)
        tier = tokens[0].lower()
        payload = " ".join(tokens[1:])
    else:
        uid, name, error = await find_user(session, message, tokens[0])
        tier = tokens[1].lower() if len(tokens) > 1 else "small"
        payload = " ".join(tokens[2:])
    if error:
        await message.answer(error)
        return
    if tier in {"normal", "small"}:
        tier = "small"
    elif tier in {"large", "big"}:
        tier = "large"
    else:
        await message.answer("Тип placement: small или big.")
        return

    payload = payload.strip()
    artist = None
    track = None
    comment = None
    if "|" in payload:
        artist_part, track_part = payload.split("|", 1)
        artist = artist_part.strip() or None
        track = track_part.strip() or None
    elif " — " in payload:
        artist_part, track_part = payload.split(" — ", 1)
        artist = artist_part.strip() or None
        track = track_part.strip() or None
    elif " - " in payload:
        artist_part, track_part = payload.split(" - ", 1)
        artist = artist_part.strip() or None
        track = track_part.strip() or None
    else:
        comment = payload or None

    if not artist and not track and not comment:
        comment = "placement"

    # Render the same bounded values that are persisted in the database.
    artist = artist[:200] if artist else None
    track = track[:200] if track else None
    comment = comment[:NOTE_MAX_LEN] if comment else None

    # light de-dupe: same user + same tier + same body within 2 minutes
    recent_cut = utc_now() - timedelta(minutes=2)
    body_check = (artist or "") + "|" + (track or "") + "|" + (comment or "")
    recent = (
        await session.execute(
            select(Placement)
            .where(
                Placement.user_id == uid,
                Placement.tier == tier,
                Placement.created_at >= recent_cut,
            )
            .order_by(Placement.created_at.desc())
            .limit(3)
        )
    ).scalars().all()
    for r in recent:
        prev_body = (r.artist or "") + "|" + (r.track or "") + "|" + (r.comment or "")
        if prev_body == body_check:
            await message.answer("⚠️ Похожая запись уже добавлена только что. Проверь /places.")
            return

    session.add(
        Placement(
            user_id=uid,
            username=name,
            tier=tier,
            artist=(artist[:200] if artist else None),
            track=(track[:200] if track else None),
            comment=(comment[:NOTE_MAX_LEN] if comment else None),
            actor_id=message.from_user.id,
            created_at=utc_now(),
        )
    )
    await session.commit()
    p = await placement_progress(session, uid)

    if artist and track:
        details_line = f"🎤 {escape_html(artist)}\n🎵 {escape_html(track)}"
    else:
        details_line = f"📝 {escape_html(comment)}"
    await message.answer(
        f"{header('✅', 'Placement добавлен')}\n\n"
        f"👤 {escape_html(name)}　{'🟣 BIG' if tier == 'large' else '⚪️ small'}\n"
        f"{details_line}\n\n"
        f"<b>За всё время</b>: Small {p['small']} · Big {p['large']} · Всего {p['total']}\n"
        f"<b>Текущий цикл</b>: Small {p['small_since']}/{p['small_target']} · "
        f"Big {p['large_since']}/{p['large_target']}"
        + ("\n✅ Порог роли достигнут" if p["ready"] else "")
    )


@router.message(Command("note"))
async def cmd_note(message: Message, session: AsyncSession, command: CommandObject):
    if not is_admin(message):
        return
    if throttle(message.from_user.id):
        await message.answer("⏳ Слишком быстро, подожди пару секунд.")
        return
    tokens = command.args.split() if command.args else []
    if not tokens:
        await message.answer("Формат: /note @username текст\nИли ответь на сообщение: /note текст")
        return

    if message.reply_to_message and not tokens[0].startswith("@") and not tokens[0].isdigit():
        uid, name, error = await find_user(session, message, None)
        note_text = " ".join(tokens)
    else:
        uid, name, error = await find_user(session, message, tokens[0])
        note_text = " ".join(tokens[1:])

    if error:
        await message.answer(error)
        return
    if not note_text:
        await message.answer("❗ Текст заметки пустой.")
        return

    session.add(
        Note(
            user_id=uid,
            username=name,
            text=note_text[:NOTE_MAX_LEN],
            actor_id=message.from_user.id,
            created_at=utc_now(),
        )
    )
    await session.commit()
    await message.answer(f"📝 Заметка сохранена: {escape_html(name)}")


@router.message(Command("undo"))
async def cmd_undo(message: Message, session: AsyncSession):
    if not is_admin(message):
        return
    actor_id = message.from_user.id
    cutoff = utc_now() - timedelta(minutes=UNDO_WINDOW_MINUTES)

    last_note = await session.scalar(
        select(Note)
        .where(Note.actor_id == actor_id, Note.created_at >= cutoff)
        .order_by(Note.created_at.desc(), Note.id.desc())
        .limit(1)
    )
    last_placement = await session.scalar(
        select(Placement)
        .where(Placement.actor_id == actor_id, Placement.created_at >= cutoff)
        .order_by(Placement.created_at.desc(), Placement.id.desc())
        .limit(1)
    )
    candidates = [x for x in [last_note, last_placement] if x]
    if not candidates:
        await message.answer(f"Нечего отменять за последние {UNDO_WINDOW_MINUTES} минут.")
        return
    target = max(candidates, key=lambda x: (x.created_at, x.id))
    if isinstance(target, Placement) and not await placement_can_be_undone(session, target):
        await message.answer("По этому плейсменту уже подтверждена роль. Отмена недоступна.")
        return

    if isinstance(target, Note):
        preview = f"NOTE · {target.username or target.user_id} · {target.text or '—'}"
    else:
        body = target.comment or f"{target.artist or ''} {target.track or ''}".strip() or "placement"
        preview = f"PLACEMENT · {target.username or target.user_id} · {target.tier} · {body}"

    await session.delete(target)
    await session.commit()
    await message.answer(f"↩️ Отменено:\n<code>{escape_html(preview[:200])}</code>")


async def placement_can_be_undone(session: AsyncSession, placement: Placement) -> bool:
    state = await session.get(PlacementProgress, placement.user_id)
    if state is not None:
        await session.refresh(state)
    reset_at = (state.big_reset_at if placement.tier == "large" else state.small_reset_at) if state else None
    return not placement.used_by_award_id and (not reset_at or placement.created_at > reset_at)


@router.message(Command("export"))
async def cmd_export(message: Message, session: AsyncSession, bot: Bot):
    if not is_admin(message):
        return
    await send_document_private_or_ack(
        message, bot, await build_export(session), "📤 Full tracker export"
    )


@router.message(Command("report"))
async def cmd_report(message: Message, session: AsyncSession, bot: Bot):
    if not is_admin(message):
        return
    await deliver(message, await build_screen(sys.modules[__name__], session, message.from_user.id, "report"), bot)


@router.message(Command("setreport"))
async def cmd_setreport(message: Message, session: AsyncSession):
    if not is_admin(message):
        return
    if message.chat.type not in {"group", "supergroup"} or (TRACK_CHAT_ID is not None and message.chat.id != TRACK_CHAT_ID):
        await message.answer("Настрой отчёт в подключённой группе, ответив /setreport на сообщение в нужном топике.")
        return
    if not message.reply_to_message:
        await message.answer(
            "❗ Ответь /setreport на сообщение именно в том чате/топике, куда должен идти отчёт."
        )
        return
    await session.merge(Setting(key="report_chat", value=str(message.chat.id)))
    await session.merge(
        Setting(key="report_thread", value=str(message.reply_to_message.message_thread_id or 0))
    )
    await session.commit()
    await message.answer(f"✅ Отчёт настроен: каждый понедельник в 12:00 по {BOT_TIMEZONE}.")


@router.message(Command("snapshot"))
async def cmd_snapshot(message: Message, session: AsyncSession):
    if not is_admin(message):
        return
    period = await make_snapshot(session)
    await message.answer(f"📸 Архив за <b>{period}</b> сохранён.")


@router.message(Command("health"))
async def cmd_health(message: Message, session: AsyncSession, bot: Bot):
    if not is_admin(message):
        return
    await send_private_or_ack(message, bot, await build_health_text(session))


async def build_health_text(session: AsyncSession) -> str:
    counts = {
        "messages": await session.scalar(select(func.count(MsgLog.id))) or 0,
        "users": await session.scalar(select(func.count(User.user_id))) or 0,
        "placements": await session.scalar(select(func.count(Placement.id))) or 0,
        "notes": await session.scalar(select(func.count(Note.id))) or 0,
        "snapshots": await session.scalar(select(func.count(MonthlySnapshot.id))) or 0,
        "awards": await session.scalar(select(func.count(RoleAward.id))) or 0,
    }
    last_msg = await session.scalar(select(func.max(MsgLog.created_at)))
    last_snap = await session.scalar(select(Setting).where(Setting.key == "snapshot_last_period"))
    last_report = await session.scalar(select(Setting).where(Setting.key == "report_last_period"))
    last_backup = await session.scalar(select(Setting).where(Setting.key == "backup_last"))

    db_size = "—"
    if DB_URL.startswith("sqlite") and make_url(DB_URL).database not in {None, ":memory:"}:
        path = Path(make_url(DB_URL).database)
        if path.exists():
            db_size = f"{path.stat().st_size / 1024 / 1024:.2f} MB"

    text_value = (
        f"{header('🩺', 'Состояние трекера')}\n\n"
        f"База данных: доступна · {escape_html(db_size)}\n\n"
        f"Сообщения: <b>{counts['messages']}</b> · Участники: <b>{counts['users']}</b>\n"
        f"Плейсменты: {counts['placements']} · Заметки: {counts['notes']}\n"
        f"Месячные записи: {counts['snapshots']} · Выданные роли: {counts['awards']}\n\n"
        f"Часовой пояс: <code>{escape_html(BOT_TIMEZONE)}</code>\n"
        f"Отслеживаемый чат: <code>{TRACK_CHAT_ID if TRACK_CHAT_ID is not None else 'любая группа'}</code>\n"
        f"Администраторы: {len(ADMIN_IDS)}\n"
        f"Порог роли: {SMALL_THRESHOLD} Small или {LARGE_THRESHOLD} Big\n\n"
        f"Последняя активность: {format_local(last_msg) if last_msg else 'пока нет'}\n"
        f"Последний архив: {escape_html(last_snap.value if last_snap else 'пока нет')}\n"
        f"Последний отчёт: {escape_html(last_report.value if last_report else 'пока нет')}\n"
        f"Последняя копия: {escape_html(last_backup.value if last_backup else 'пока нет')}"
    )
    return text_value


@router.message(Command("backup"))
async def cmd_backup(message: Message, session: AsyncSession):
    if not is_admin(message):
        return
    try:
        path = await backup_database(session)
    except Exception:
        log.exception("Manual backup failed")
        await message.answer("❌ Backup не удалось создать. Смотри лог процесса.")
        return
    if not path:
        await message.answer("ℹ️ Backup доступен только для SQLite.")
        return
    await message.answer(f"💾 Backup создан: <code>{escape_html(path.name)}</code>")


# ============================================================
# BACKUP / CATCH-UP / RETENTION  (items 5, 24, 25, 26)
# ============================================================
async def backup_database(session: AsyncSession | None = None) -> Path | None:
    if not DB_URL.startswith("sqlite") or make_url(DB_URL).database in {None, ":memory:"}:
        return None
    db_path = make_url(DB_URL).database
    source = Path(db_path)
    if not source.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    target = BACKUP_DIR / f"tracker_{local_now():%Y-%m-%d_%H-%M-%S_%f}.db"

    def copy_database():
        connection = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)
        destination = sqlite3.connect(str(target), timeout=30)
        try:
            connection.backup(destination)
        finally:
            destination.close()
            connection.close()

    # SQLite backup can take seconds: keep tracking and button clicks responsive.
    await asyncio.to_thread(copy_database)

    try:
        os.chmod(target, 0o600)
    except OSError:
        pass

    backups = sorted(BACKUP_DIR.glob("tracker_*.db"), key=lambda x: x.stat().st_mtime, reverse=True)
    for old in backups[30:]:
        try:
            old.unlink()
        except OSError:
            pass

    if session is not None:
        await session.merge(Setting(key="backup_last", value=local_now().strftime("%Y-%m-%d %H:%M:%S")))
        await session.commit()
    else:
        async with Session() as s:
            await s.merge(Setting(key="backup_last", value=local_now().strftime("%Y-%m-%d %H:%M:%S")))
            await s.commit()
    return target


async def retention_cleanup(session: AsyncSession) -> int:
    if MSG_RETENTION_DAYS <= 0:
        return 0
    cutoff = utc_now() - timedelta(days=MSG_RETENTION_DAYS)
    result = await session.execute(delete(MsgLog).where(MsgLog.created_at < cutoff))
    await session.commit()
    return result.rowcount or 0


async def startup_catchup(bot: Bot) -> None:
    async with Session() as session:
        now_local = local_now()
        if now_local.day == 1 and now_local.hour == 0 and now_local.minute < 5:
            pass
        else:
            _, _, previous_period = previous_calendar_month_range()
            snapshot_marker = await session.scalar(
                select(Setting).where(Setting.key == "snapshot_last_period")
            )
            exists = await session.scalar(
                select(func.count(MonthlySnapshot.id)).where(
                    MonthlySnapshot.period == previous_period
                )
            )
            if not snapshot_marker or snapshot_marker.value != previous_period:
                if exists:
                    await session.merge(Setting(key="snapshot_last_period", value=previous_period))
                    await session.commit()
                else:
                    try:
                        await make_snapshot(session)
                        log.info("Startup catch-up snapshot created for %s", previous_period)
                    except Exception:
                        log.exception("Startup snapshot catch-up failed")

        if not REPORT_CATCHUP:
            return

        now_local = local_now()
        monday_noon = (now_local - timedelta(days=now_local.weekday())).replace(
            hour=12, minute=0, second=0, microsecond=0
        )
        if now_local < monday_noon:
            return

        start, end, week_label = previous_calendar_week_range()
        setting_chat = await session.scalar(select(Setting).where(Setting.key == "report_chat"))
        if not setting_chat:
            return
        last_sent = await session.scalar(select(Setting).where(Setting.key == "report_last_period"))
        if last_sent and last_sent.value == week_label:
            return

        report = await build_weekly_report(session)
        setting_thread = await session.scalar(select(Setting).where(Setting.key == "report_thread"))

    if report:
        kwargs = {}
        if setting_thread and int(setting_thread.value) > 0:
            kwargs["message_thread_id"] = int(setting_thread.value)
        try:
            await bot.send_message(int(setting_chat.value), report, **kwargs)
            async with Session() as session:
                await session.merge(Setting(key="report_last_period", value=week_label))
                await session.commit()
            log.info("Startup catch-up weekly report sent for %s", week_label)
        except Exception:
            log.exception("Startup weekly report catch-up failed")


async def backup_loop() -> None:
    while True:
        now = local_now()
        target = now.replace(hour=3, minute=30, second=0, microsecond=0)
        if target <= now:
            target = target + timedelta(days=1)
        await asyncio.sleep(max(1, (target - now).total_seconds()))
        try:
            path = await backup_database()
            if path:
                log.info("Database backup created: %s", path)
            async with Session() as session:
                deleted = await retention_cleanup(session)
                if deleted:
                    log.info("Retention cleanup removed %s old messages", deleted)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Database backup failed")


# ============================================================
# SCHEDULERS  (item 5)
# ============================================================
def next_weekly_report_target() -> datetime:
    now = local_now()
    this_monday_noon = (now - timedelta(days=now.weekday())).replace(
        hour=12, minute=0, second=0, microsecond=0
    )
    if this_monday_noon > now:
        return this_monday_noon
    return this_monday_noon + timedelta(days=7)


def next_snapshot_target() -> datetime:
    now = local_now()
    first = month_start_local(now)
    target = first.replace(hour=0, minute=5)
    if target <= now:
        target = next_month_local(first).replace(hour=0, minute=5)
    return target


async def report_loop(bot: Bot) -> None:
    while True:
        target = next_weekly_report_target()
        await asyncio.sleep(max(1, (target - local_now()).total_seconds()))
        try:
            async with Session() as session:
                setting_chat = await session.scalar(
                    select(Setting).where(Setting.key == "report_chat")
                )
                setting_thread = await session.scalar(
                    select(Setting).where(Setting.key == "report_thread")
                )
                _, _, week_label = previous_calendar_week_range()
                last_sent = await session.scalar(
                    select(Setting).where(Setting.key == "report_last_period")
                )
                if last_sent and last_sent.value == week_label:
                    continue
                report = await build_weekly_report(session)
            if setting_chat and report:
                kwargs = {}
                if setting_thread and int(setting_thread.value) > 0:
                    kwargs["message_thread_id"] = int(setting_thread.value)
                await bot.send_message(int(setting_chat.value), report, **kwargs)
                async with Session() as mark_session:
                    await mark_session.merge(Setting(key="report_last_period", value=week_label))
                    await mark_session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Weekly report failed")


async def snapshot_loop() -> None:
    while True:
        target = next_snapshot_target()
        await asyncio.sleep(max(1, (target - local_now()).total_seconds()))
        try:
            async with Session() as session:
                _, _, period = previous_calendar_month_range()
                last = await session.scalar(
                    select(Setting).where(Setting.key == "snapshot_last_period")
                )
                if last and last.value == period:
                    continue
                saved = await make_snapshot(session)
                log.info("Snapshot saved: %s", saved)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Snapshot failed")


# ============================================================
# BOT COMMAND MENU
# ============================================================
async def setup_commands(bot: Bot) -> None:
    public = [
        BotCommand(command="menu", description="Открыть меню"),
        BotCommand(command="my", description="Моя статистика"),
        BotCommand(command="profile", description="Мой профиль"),
        BotCommand(command="help", description="Помощь"),
    ]
    await bot.set_my_commands(public, scope=BotCommandScopeDefault())
    await bot.set_my_commands(public, scope=BotCommandScopeAllGroupChats())

    admin = public + [BotCommand(command="members", description="Участники и поиск"), BotCommand(command="top", description="Рейтинг группы")]
    for admin_id in ADMIN_IDS:
        try:
            await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=admin_id))
        except Exception:
            log.warning("Could not set private command menu for an admin")
    if TRACK_CHAT_ID is not None:
        try:
            await bot.set_my_commands(
                public,
                scope=BotCommandScopeChatAdministrators(chat_id=TRACK_CHAT_ID),
            )
        except Exception:
            log.warning(
                "Could not set admin command menu for chat administrators in TRACK_CHAT_ID=%s",
                TRACK_CHAT_ID,
            )


# ============================================================
# GLOBAL ERROR HANDLER  (item 2)
# ============================================================
async def global_error_handler(event: ErrorEvent, bot: Bot):
    err = event.exception
    log.error("Unhandled error: %s\n%s", err, "".join(traceback.format_exception(type(err), err, err.__traceback__)))
    # Raw exceptions can contain token-bearing URLs; keep details in process logs.
    message = event.update.message
    query = event.update.callback_query
    try:
        if query:
            await query.answer("Не получилось выполнить действие. Открой /menu и повтори.", show_alert=True)
        elif message and (message.chat.type == "private" or is_command_message(message)):
            await message.answer("Не получилось выполнить действие. Открой /menu и повтори.")
    except Exception:
        log.warning("Could not deliver error notice")
    return True


# ============================================================
# STARTUP
# ============================================================
async def main() -> None:
    await init_db()
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    weekly_task = None
    snapshot_task = None
    backup_task = None
    try:
        me = await bot.get_me()
        log.info(
            "Started @%s id=%s tz=%s db=%s track_chat=%s admins=%s",
            me.username,
            me.id,
            BOT_TIMEZONE,
            DB_URL,
            TRACK_CHAT_ID,
            len(ADMIN_IDS),
        )
        if TRACK_CHAT_ID is not None:
            try:
                chat = await bot.get_chat(TRACK_CHAT_ID)
                log.info("Tracking chat: %s (%s)", chat.title, TRACK_CHAT_ID)
                if not me.can_read_all_group_messages:
                    member = await bot.get_chat_member(TRACK_CHAT_ID, me.id)
                    if str(member.status) not in {"administrator", "creator"}:
                        log.warning(
                            "Bot privacy mode is enabled and bot is not an admin in the tracking chat; "
                            "ordinary messages may not be visible."
                        )
            except Exception as exc:
                raise RuntimeError(
                    f"TRACK_CHAT_ID={TRACK_CHAT_ID} cannot be verified. Is the bot in the chat? {exc}"
                ) from exc

        await setup_commands(bot)
        await startup_catchup(bot)
        dp = Dispatcher(events_isolation=SimpleEventIsolation())
        dp.include_router(router)
        dp.errors.register(global_error_handler)

        weekly_task = asyncio.create_task(report_loop(bot))
        snapshot_task = asyncio.create_task(snapshot_loop())
        backup_task = asyncio.create_task(backup_loop())
        try:
            await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
        finally:
            for task in (weekly_task, snapshot_task, backup_task):
                if task:
                    task.cancel()
            await asyncio.gather(
                *(x for x in (weekly_task, snapshot_task, backup_task) if x),
                return_exceptions=True,
            )
    finally:
        await bot.session.close()
        await engine.dispose()


register_ui(router, sys.modules[__name__])


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Bot stopped")
