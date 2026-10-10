"""Regression tests using synthetic Telegram updates and SQLite in memory only."""

import asyncio
import importlib
import os
from pathlib import Path
import sys
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiogram.types import Message
from sqlalchemy import event, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Disable dotenv itself before importing application code; clearing the process
# environment prevents inherited credentials or DB URLs being used as well.
with patch.dict(os.environ, {
    "BOT_TOKEN": "123456:synthetic-test-token",
    "ADMIN_IDS": "900",
    "TRACK_CHAT_ID": "-100123",
    "BOT_TIMEZONE": "Europe/Moscow",
    "DB_URL": "sqlite+aiosqlite:///:memory:",
}, clear=True), patch("dotenv.load_dotenv", return_value=False):
    bot = importlib.import_module("trackerbot")


CHAT_ID = -100123
NOW = datetime(2026, 10, 10, 12)


def message(message_id=1, uid=10, username="artist", **changes):
    values = {
        "message_id": message_id,
        "date": NOW.replace(tzinfo=timezone.utc),
        "chat": {"id": CHAT_ID, "type": "supergroup"},
        "from_user": {"id": uid, "is_bot": False, "first_name": "Artist",
                      "username": username},
        "text": "hello",
    }
    values.update(changes)
    return Message.model_validate(values)


class TrackingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(bot.Base.metadata.create_all)
            await connection.execute(text(
                "CREATE UNIQUE INDEX ux_test_message ON msg_log(chat_id, message_id) "
                "WHERE message_id IS NOT NULL"
            ))
        self.patches = [
            patch.object(bot, "TRACK_CHAT_ID", CHAT_ID),
            patch.object(bot, "LOCAL_TZ", ZoneInfo("Europe/Moscow")),
            patch.object(bot, "utc_now", return_value=NOW),
            patch.object(bot, "local_now", return_value=NOW.replace(
                tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Moscow"))),
            patch.object(bot, "OPPORTUNITY_TAGS", ("#opportunity",)),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()

    async def seed_log(self, session, uid=10, username="artist", first_name="Artist",
                       category="message", created_at=None, chat_id=CHAT_ID,
                       opportunity=False):
        session.add(bot.MsgLog(
            user_id=uid, username=username, first_name=first_name,
            chat_id=chat_id, category=category, opportunity=opportunity,
            created_at=created_at or NOW - timedelta(hours=1),
        ))

    async def test_delayed_message_keeps_telegram_date(self):
        sent = datetime(2026, 9, 30, 20, 59, tzinfo=timezone.utc)
        async with self.sessions() as session:
            await bot.track_message(session, message(date=sent))
            row = await session.scalar(select(bot.MsgLog))
            self.assertEqual(row.created_at, sent.replace(tzinfo=None))
            self.assertEqual(bot.local_date_from_utc_naive(row.created_at).isoformat(),
                             "2026-09-30")
            self.assertEqual((await bot.get_user_stats(session, 10, "month"))["total"], 0)

    async def test_duplicate_does_not_discard_updated_user_profile(self):
        async with self.sessions() as session:
            await bot.track_message(session, message())
            await bot.track_message(session, message(username="renamed"))
            self.assertEqual(await session.scalar(select(func.count(bot.MsgLog.id))), 1)
            self.assertEqual((await session.get(bot.User, 10)).username, "renamed")

    async def test_private_commands_refresh_sender_without_recording_activity(self):
        async with self.sessions() as session:
            await bot.upsert_user(session, 10, "old_name", "Old name")
            await session.commit()
        incoming = message(username="current_name", text="/start",
                           chat={"id": 10, "type": "private"})
        async def handler(event, data):
            self.assertEqual(await bot.resolve_name(data["session"], 10), "@current_name")
            stats = await bot.get_user_stats(data["session"], 10, "all")
            self.assertEqual(stats["un"], "current_name")
            self.assertEqual(stats["total"], 0)
            return "handled"
        with patch.object(bot, "Session", self.sessions):
            self.assertEqual(await bot.DatabaseMiddleware()(handler, incoming, {}), "handled")
        async with self.sessions() as session:
            self.assertEqual((await session.get(bot.User, 10)).username, "current_name")
            self.assertEqual(await session.scalar(select(func.count(bot.MsgLog.id))), 0)

    async def test_private_noncommand_input_is_not_activity_or_profile_write(self):
        incoming = message(chat={"id": 10, "type": "private"})
        async def handler(event, data):
            return "handled"
        with patch.object(bot, "Session", self.sessions):
            await bot.DatabaseMiddleware()(handler, incoming, {})
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(bot.MsgLog.id))), 0)
            self.assertIsNone(await session.get(bot.User, 10))

    async def test_username_and_last_name_removal_refresh_loaded_identity(self):
        async with self.sessions() as session:
            await bot.upsert_user(session, 10, "artist", "Artist", "Surname")
            await session.commit()
            user = await session.get(bot.User, 10)
            first_seen = user.first_seen
            await bot.upsert_user(session, 10, None, "New name", None)
            await session.commit()
            self.assertIsNone(user.username)
            self.assertIsNone(user.last_name)
            self.assertEqual(user.first_name, "New name")
            self.assertEqual(user.first_seen, first_seen)

    async def test_concurrent_upserts_for_new_user_do_not_collide(self):
        async with self.sessions() as left, self.sessions() as right:
            # Both sessions observe an absent user, reproducing the original
            # read-then-insert race without using any file-backed database.
            self.assertIsNone(await left.get(bot.User, 10))
            self.assertIsNone(await right.get(bot.User, 10))
            await asyncio.gather(
                bot.upsert_user(left, 10, None, "Artist"),
                bot.upsert_user(right, 10, None, "Artist"),
            )
            await left.commit()
            await right.commit()
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(bot.User.user_id))), 1)

    async def test_username_transfer_resolves_to_new_owner(self):
        async with self.sessions() as session:
            await bot.upsert_user(session, 10, "artist", "Old owner")
            await self.seed_log(session)
            await session.commit()
            await bot.upsert_user(session, 20, "ARTIST", "New owner")
            await session.commit()
            uid, _, error = await bot.find_user(session, message(), "@artist")
            self.assertEqual(uid, 20)
            self.assertIsNone(error)
            self.assertIsNone((await session.get(bot.User, 10)).username)

    async def test_historical_username_cannot_override_current_profile(self):
        async with self.sessions() as session:
            await self.seed_log(session, username="old_name")
            await bot.upsert_user(session, 10, "new_name", "Artist")
            await session.commit()
            uid, _, error = await bot.find_user(session, message(), "@old_name")
            self.assertIsNone(uid)
            self.assertIsNotNone(error)

    async def test_explicit_username_does_not_fuzzy_match_another_person(self):
        async with self.sessions() as session:
            await bot.upsert_user(session, 10, "artist_extra", "Artist")
            await session.commit()
            uid, _, error = await bot.find_user(session, message(), "@artist")
            self.assertIsNone(uid)
            self.assertIsNotNone(error)
            self.assertEqual((await bot.find_user(session, message(), "artist"))[0], 10)

    async def test_duplicate_exact_profiles_require_disambiguation(self):
        async with self.sessions() as session:
            for uid in (10, 20):
                session.add(bot.User(user_id=uid, username="artist", first_name="Artist",
                                     first_seen=NOW, last_seen=NOW))
            await session.commit()
            uid, _, error = await bot.find_user(session, message(), "@artist")
            self.assertIsNone(uid)
            self.assertIsNotNone(error)

    async def test_reply_lookup_does_not_write_or_overwrite_current_name(self):
        async with self.sessions() as session:
            await bot.upsert_user(session, 10, "current_name", "Artist")
            await session.commit()
            incoming = message(uid=900, reply_to_message=message(username="old_name"))
            with patch.object(session, "commit", side_effect=AssertionError("unexpected write")):
                self.assertEqual((await bot.find_user(session, incoming, None))[:2],
                                 (10, "@current_name"))
                unknown = message(uid=900, reply_to_message=message(uid=30))
                self.assertEqual((await bot.find_user(session, unknown, None))[0], 30)
            self.assertIsNone(await session.get(bot.User, 30))

    async def test_legacy_fuzzy_lookup_groups_by_user_not_old_names(self):
        async with self.sessions() as session:
            await self.seed_log(session, username="producer_old", first_name="Old")
            await self.seed_log(session, username="producer_new", first_name="New",
                                created_at=NOW - timedelta(minutes=1))
            await session.commit()
            self.assertEqual((await bot.find_user(session, message(), "producer"))[:2],
                             (10, "@producer_new"))

    async def test_legacy_fuzzy_ambiguity_returns_error(self):
        async with self.sessions() as session:
            await self.seed_log(session, uid=10, username="producer_one")
            await self.seed_log(session, uid=20, username="producer_two")
            await session.commit()
            uid, _, error = await bot.find_user(session, message(), "producer")
            self.assertIsNone(uid)
            self.assertIn("producer_one", error)
            self.assertIn("producer_two", error)

    async def test_whitespace_lookup_is_not_a_match_all_search(self):
        async with self.sessions() as session:
            await bot.upsert_user(session, 10, "artist", "Artist")
            await session.commit()
            self.assertIsNone((await bot.find_user(session, message(), "  "))[0])

    async def test_stats_are_scoped_to_user_chat_and_half_open_period(self):
        start, end, _ = bot.period_bounds("month")
        async with self.sessions() as session:
            await bot.upsert_user(session, 10, "current_name", "Current")
            for category in ("message", "loop", "zip", "files", "media", "legacy"):
                await self.seed_log(session, category=category,
                                    opportunity=category == "loop")
            await self.seed_log(session, created_at=start)
            await self.seed_log(session, created_at=end)
            await self.seed_log(session, uid=20)
            await self.seed_log(session, chat_id=-100999, opportunity=True)
            await session.commit()
            statements = []
            def capture(connection, cursor, statement, parameters, context, many):
                statements.append(statement)
            event.listen(self.engine.sync_engine, "before_cursor_execute", capture)
            try:
                stats = await bot.get_user_stats(session, 10, "month")
            finally:
                event.remove(self.engine.sync_engine, "before_cursor_execute", capture)
            self.assertEqual(stats["total"], 7)
            self.assertEqual(stats["messages"], 2)
            self.assertEqual(stats["files"], 2)
            self.assertEqual(stats["opportunities"], 1)
            self.assertEqual((stats["un"], stats["fn"]), ("current_name", "Current"))
            for statement in statements:
                if "FROM msg_log" in statement:
                    self.assertIn("msg_log.user_id =", statement)
                    self.assertIn("msg_log.chat_id =", statement)

    async def test_local_days_streak_ignore_other_chat_and_future_rows(self):
        async with self.sessions() as session:
            for stamp in (datetime(2026, 10, 8, 22), datetime(2026, 10, 9, 1),
                          datetime(2026, 10, 9, 22)):
                await self.seed_log(session, created_at=stamp)
            await self.seed_log(session, created_at=NOW + timedelta(days=1), chat_id=-100999)
            await self.seed_log(session, created_at=NOW + timedelta(days=2))
            await session.commit()
            stats = await bot.get_user_stats(session, 10, "all")
            self.assertEqual(stats["active_days"], 2)
            self.assertEqual(stats["streak"], 2)

    async def test_empty_user_stats_remain_zero(self):
        async with self.sessions() as session:
            await self.seed_log(session, uid=20)
            await session.commit()
            stats = await bot.get_user_stats(session, 10, "all")
            self.assertEqual((stats["total"], stats["active_days"], stats["streak"]), (0, 0, 0))

    async def test_filters_skip_bots_commands_services_and_other_chats(self):
        cases = [
            message(chat={"id": -100999, "type": "supergroup"}),
            message(chat={"id": 10, "type": "private"}),
            message(from_user={"id": 10, "first_name": "Bot", "is_bot": True}),
            message(text="/stats@tracker artist"),
            message(text=None, new_chat_title="Renamed"),
        ]
        async with self.sessions() as session:
            for incoming in cases:
                await bot.track_message(session, incoming)
            self.assertEqual(await session.scalar(select(func.count(bot.MsgLog.id))), 0)
            self.assertEqual(await session.scalar(select(func.count(bot.User.user_id))), 0)

    async def test_opportunity_tags_match_whole_tags_in_text_and_captions(self):
        for value in ("#Opportunity!", "hello #opportunity", "(#opportunity)"):
            self.assertTrue(bot.contains_opportunity_tag(message(text=value)))
        self.assertTrue(bot.contains_opportunity_tag(message(text=None, caption="#opportunity")))
        for value in ("#opportunity_extra", "#opportunity2", "prefix#opportunity", "ordinary"):
            self.assertFalse(bot.contains_opportunity_tag(message(text=value)))

    async def test_video_notes_are_media_and_documents_keep_extension_rules(self):
        self.assertEqual(bot.get_category(message(text=None, video_note={
            "file_id": "video", "file_unique_id": "video_unique", "length": 10,
            "duration": 1,
        })), "media")
        for filename, expected in (("LOOP.WAV", "loop"), ("pack.ZIP", "zip"),
                                   ("project.flp", "files")):
            self.assertEqual(bot.get_category(message(text=None, document={
                "file_id": "doc", "file_unique_id": "doc_unique", "file_name": filename,
            })), expected)

    async def test_voice_counts_as_message_audio_as_loop_and_video_note_as_media(self):
        voice = message(1, text=None, voice={
            "file_id": "voice", "file_unique_id": "voice_unique", "duration": 3,
        })
        audio = message(2, text=None, audio={
            "file_id": "audio", "file_unique_id": "audio_unique", "duration": 3,
            "file_name": "music.mp3",
        })
        video_note = message(3, text=None, video_note={
            "file_id": "video", "file_unique_id": "video_unique", "length": 10,
            "duration": 3,
        })
        self.assertEqual(bot.get_category(voice), "message")
        self.assertEqual(bot.get_category(audio), "loop")
        self.assertEqual(bot.get_category(video_note), "media")
        async with self.sessions() as session:
            for incoming in (voice, audio, video_note):
                await bot.track_message(session, incoming)
            stats = await bot.get_user_stats(session, 10, "month")
            self.assertEqual((stats["messages"], stats["loops"], stats["media"], stats["total"]),
                             (1, 1, 1, 3))

    async def test_placement_only_name_does_not_get_double_at_sign(self):
        async with self.sessions() as session:
            session.add(bot.Placement(user_id=10, username="@artist", tier="small",
                                      created_at=NOW))
            await session.commit()
            self.assertEqual(await bot.resolve_name(session, 10), "@artist")


if __name__ == "__main__":
    unittest.main()
