"""Integration tests: real aiogram dispatcher + memory DB + fake Telegram API."""

import asyncio
import re
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import timedelta
from html import unescape
from unittest.mock import AsyncMock, patch

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, EditMessageText, GetMe, SendDocument, SendMessage
from aiogram.types import Message, Update, User
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_tracking import bot as app, message, NOW
from tracker_ui import Screen, build_screen, button, edit_screen, keyboard, parse_payload, save_entry


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.messages = {}
        self.sequence = 100

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs):
        yield b""

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, GetMe):
            return User(id=123456, is_bot=True, first_name="Tracker", username="synthetic_tracker_bot")
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, (SendMessage, EditMessageText, SendDocument)):
            if isinstance(method, EditMessageText):
                msg_id = method.message_id
            else:
                self.sequence += 1
                msg_id = self.sequence
            msg = Message.model_validate({
                "message_id": msg_id, "date": NOW,
                "chat": {"id": method.chat_id, "type": "private" if method.chat_id > 0 else "supergroup"},
                "from_user": {"id": 123456, "is_bot": True, "first_name": "Tracker"},
                "text": getattr(method, "text", None) or getattr(method, "caption", None),
                "reply_markup": getattr(method, "reply_markup", None),
            }, context={"bot": bot})
            self.messages[msg_id] = msg
            return msg
        raise AssertionError(f"Unexpected Telegram API method: {type(method).__name__}")


class UITests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.dp = Dispatcher(events_isolation=SimpleEventIsolation())
        cls.dp.include_router(app.router)

    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(app.Base.metadata.create_all)
        self.patches = [patch.object(app, "Session", self.sessions), patch.object(app, "ADMIN_IDS", {900})]
        for item in self.patches:
            item.start()
        self.api = FakeTelegram()
        self.telegram = Bot("123456:synthetic-test-token", session=self.api, default=DefaultBotProperties(parse_mode="HTML"))
        self.update_id = 0
        async with self.sessions() as session:
            await app.upsert_user(session, 10, "artist", "Artist")
            await app.upsert_user(session, 900, "admin", "Admin")
            await session.commit()

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.dp.storage.set_state(self.dp.fsm.get_context(bot=self.telegram, chat_id=900, user_id=900).key, None)
        await self.dp.storage.set_data(self.dp.fsm.get_context(bot=self.telegram, chat_id=900, user_id=900).key, {})
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()
        await self.telegram.session.close()

    def last_screen(self):
        screens = [method for method in self.api.calls if isinstance(method, (SendMessage, EditMessageText))]
        method = screens[-1]
        if isinstance(method, EditMessageText):
            return self.api.messages[method.message_id]
        return self.api.messages[self.api.sequence]

    async def command(self, text, actor=900, chat_id=None):
        self.update_id += 1
        msg = message(message_id=self.update_id, uid=actor, username="admin" if actor == 900 else "artist", text=text,
                      chat={"id": chat_id or actor, "type": "private" if (chat_id or actor) > 0 else "supergroup"})
        await self.dp.feed_update(self.telegram, Update(update_id=self.update_id, message=msg))
        return self.last_screen()

    async def click(self, screen, label, actor=900, raw=None):
        self.update_id += 1
        if raw is None:
            matches = [b for row in screen.reply_markup.inline_keyboard for b in row if label in b.text]
            self.assertEqual(len(matches), 1, label)
            raw = matches[0].callback_data
        update = Update.model_validate({
            "update_id": self.update_id,
            "callback_query": {"id": str(self.update_id), "chat_instance": "synthetic", "data": raw,
                "from_user": {"id": actor, "is_bot": False, "first_name": "Tester"}, "message": screen.model_dump()},
        })
        await self.dp.feed_update(self.telegram, update)
        return self.last_screen()

    async def test_start_and_period_navigation_edit_one_message(self):
        home = await self.command("/start")
        self.assertIn("301 / TRACKER", home.text)
        stats = await self.click(home, "Моя статистика")
        self.assertEqual(stats.message_id, home.message_id)
        changed = await self.click(stats, "7 дней")
        self.assertIn("последние 7 дней", changed.text)
        self.assertEqual(changed.message_id, home.message_id)
        self.assertTrue(any(isinstance(call, AnswerCallbackQuery) for call in self.api.calls))

    async def test_stats_without_arguments_returns_own_card(self):
        stats = await self.command("/stats", actor=10)
        self.assertIn("@artist", stats.text)
        self.assertIn("Статистика", stats.text)

    async def test_profile_without_arguments_works_for_regular_user(self):
        profile = await self.command("/profile", actor=10)
        self.assertIn("@artist", profile.text)
        self.assertFalse(any("Заметка" in b.text for row in profile.reply_markup.inline_keyboard for b in row))

    async def test_other_person_cannot_operate_admin_menu(self):
        home = await self.command("/start")
        await self.click(home, "Управление", actor=10)
        call = self.api.calls[-1]
        self.assertIsInstance(call, AnswerCallbackQuery)
        self.assertTrue(call.show_alert)
        self.assertNotIn("Управление</b>", self.last_screen().text)

    async def test_forged_owner_cannot_open_another_profile(self):
        home = await self.command("/start", actor=10)
        await self.click(home, "", actor=10, raw=button(10, "", "profile", 900).callback_data)
        self.assertIsInstance(self.api.calls[-1], AnswerCallbackQuery)
        self.assertTrue(self.api.calls[-1].show_alert)

    async def test_build_screen_checks_permissions(self):
        async with self.sessions() as session:
            for view in ("notes", "admin", "members", "health"):
                with self.assertRaises(PermissionError):
                    await build_screen(app, session, 10, view)
            with self.assertRaises(PermissionError):
                await build_screen(app, session, 10, "stats", 900)

    async def test_guided_placement_requires_confirmation_and_is_idempotent(self):
        members = await self.click(await self.command("/start"), "Участники")
        profile = await self.click(members, "@artist")
        choice = await self.click(profile, "＋ Плейсмент")
        await self.click(choice, "Small")
        preview = await self.command("Artist <A> | Track & B")
        self.assertIn("&lt;A&gt;", preview.text)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(app.Placement.id))), 0)
        saved = await self.click(preview, "Сохранить")
        self.assertIn("Плейсмент сохранён", saved.text)
        await self.click(preview, "Сохранить")
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(app.Placement.id))), 1)
            item = await session.scalar(select(app.Placement))
            self.assertEqual((item.user_id, item.tier, item.artist, item.track), (10, "small", "Artist <A>", "Track & B"))

    async def test_note_can_be_cancelled_without_write(self):
        profile = await self.command("/profile @artist")
        await self.click(profile, "＋ Заметка")
        preview = await self.command("private test note")
        await self.command("/cancel")
        await self.click(preview, "Сохранить")
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 0)

    async def test_confirm_rejects_button_from_old_form(self):
        profile = await self.command("/profile @artist")
        await self.click(profile, "＋ Заметка")
        old = await self.command("old note")
        await self.command("/cancel")
        profile = await self.command("/profile @artist")
        await self.click(profile, "＋ Заметка")
        new = await self.command("new note")
        await self.click(old, "Сохранить")
        await self.click(new, "Сохранить")
        async with self.sessions() as session:
            notes = (await session.scalars(select(app.Note))).all()
            self.assertEqual([note.text for note in notes], ["new note"])

    async def test_cancel_from_old_form_does_not_cancel_new_form(self):
        profile = await self.command("/profile @artist")
        await self.click(profile, "＋ Заметка")
        old = await self.command("old note")
        await self.command("/cancel")
        profile = await self.command("/profile @artist")
        await self.click(profile, "＋ Заметка")
        new = await self.command("new note")
        await self.click(old, "Отмена")
        await self.click(new, "Сохранить")
        async with self.sessions() as session:
            self.assertEqual([note.text for note in (await session.scalars(select(app.Note))).all()], ["new note"])

    async def test_navigation_command_cancels_pending_form(self):
        profile = await self.command("/profile @artist")
        await self.click(profile, "＋ Заметка")
        await self.command("/roles")
        result = await self.command("Should not be a note")
        self.assertIn("301 / TRACKER", result.text)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 0)

    async def test_invalid_numeric_search_returns_error_without_overflow(self):
        async with self.sessions() as session:
            for raw in ("0", "9" * 80):
                uid, _, error = await app.find_user(session, message(), raw)
                self.assertIsNone(uid)
                self.assertIn("Telegram ID", error)

    async def test_note_only_member_is_in_picker(self):
        async with self.sessions() as session:
            session.add(app.Note(user_id=777, actor_id=900, text="note", created_at=app.utc_now()))
            await session.commit()
            screen = await build_screen(app, session, 900, "members")
            self.assertTrue(any(parse_payload(b.callback_data)[2] == 777 for row in screen.markup.inline_keyboard for b in row))

    async def test_long_legacy_placement_response_is_bounded(self):
        result = await self.command("/place @artist small " + "X" * 4070)
        self.assertLess(len(result.text), 4096)
        async with self.sessions() as session:
            placement = await session.scalar(select(app.Placement))
            self.assertEqual(len(placement.comment), 500)

    async def test_search_text_in_private_chat_opens_profile(self):
        members = await self.click(await self.command("/start"), "Участники")
        await self.click(members, "Найти")
        result = await self.command("@artist")
        self.assertIn("Карточка участника", result.text)
        self.assertIn("@artist", result.text)

    async def test_empty_and_long_lists_are_bounded_and_paginated(self):
        async with self.sessions() as session:
            empty = await build_screen(app, session, 900, "notes", 10)
            self.assertIn("Пока нет записей", empty.text)
            for index in range(16):
                session.add(app.Note(user_id=10, actor_id=900, text="<&>" * 166, created_at=app.utc_now() - timedelta(minutes=index)))
            await session.commit()
            first = await build_screen(app, session, 900, "notes", 10)
            second = await build_screen(app, session, 900, "notes", 10, extra="1")
            last = await build_screen(app, session, 900, "notes", 10, extra="999")
            self.assertIn("страница 1/4", first.text)
            self.assertIn("страница 2/4", second.text)
            self.assertIn("страница 4/4", last.text)
            visible = unescape(re.sub(r"</?(?:b|i|code|a)(?: [^>]*)?>", "", first.text))
            self.assertLessEqual(len(visible.encode("utf-16-le")) // 2, 4096)

    async def test_empty_category_ranking_does_not_list_zero_scores(self):
        async with self.sessions() as session:
            session.add(app.MsgLog(user_id=10, chat_id=app.TRACK_CHAT_ID, category="message", created_at=app.utc_now() - timedelta(minutes=1)))
            await session.commit()
            top = await build_screen(app, session, 900, "top", extra="loops")
            self.assertIn("нет активности в этой категории", top.text)

    async def test_regular_users_see_only_four_commands(self):
        api_bot = AsyncMock()
        await app.setup_commands(api_bot)
        public = api_bot.set_my_commands.call_args_list[0].args[0]
        self.assertEqual([c.command for c in public], ["menu", "my", "profile", "help"])
        admin = api_bot.set_my_commands.call_args_list[2].args[0]
        self.assertEqual(len(admin), 6)

    async def test_role_confirmation_does_not_delete_history(self):
        async with self.sessions() as session:
            for _ in range(app.SMALL_THRESHOLD):
                session.add(app.Placement(user_id=10, tier="small", actor_id=900, created_at=app.utc_now() - timedelta(minutes=1)))
            await session.commit()
        profile = await self.command("/profile @artist")
        roles = await self.click(profile, "Прогресс")
        preview = await self.click(roles, "Подтвердить Small")
        await self.click(preview, "Подтвердить")
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(app.RoleAward.id))), 1)
            self.assertEqual(await session.scalar(select(func.count(app.Placement.id))), app.SMALL_THRESHOLD)
            self.assertEqual((await app.placement_progress(session, 10))["small_raw"], 0)

    async def test_two_admins_cannot_confirm_same_role_cycle(self):
        async with self.sessions() as session:
            for _ in range(app.SMALL_THRESHOLD):
                session.add(app.Placement(user_id=10, tier="small", created_at=app.utc_now() - timedelta(minutes=1)))
            await session.commit()
        async with self.sessions() as left, self.sessions() as right:
            results = await asyncio.gather(
                app.reset_placement_progress(left, 10, "small", 900, "artist"),
                app.reset_placement_progress(right, 10, "small", 901, "artist"),
                return_exceptions=True,
            )
            self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(app.RoleAward.id))), 1)

    async def test_confirmed_role_prevents_placement_undo(self):
        async with self.sessions() as session:
            for _ in range(app.SMALL_THRESHOLD):
                session.add(app.Placement(user_id=10, tier="small", actor_id=900, created_at=app.utc_now() - timedelta(minutes=1)))
            await session.commit()
            await app.reset_placement_progress(session, 10, "small", 900, "artist")
            target = await session.scalar(select(app.Placement).order_by(app.Placement.id.desc()).limit(1))
            with self.assertRaises(ValueError):
                await save_entry(app, session, 900, {"kind": "undo", "uid": 10, "target": f"place-{target.id}"})
            self.assertEqual(await session.scalar(select(func.count(app.Placement.id))), app.SMALL_THRESHOLD)

    async def test_cancelled_service_confirmation_does_not_mutate(self):
        admin = await self.click(await self.command("/start"), "Управление")
        preview = await self.click(admin, "Архив месяца")
        await self.command("/menu")
        with patch.object(app, "make_snapshot", new_callable=AsyncMock) as snapshot:
            await self.click(preview, "Подтвердить")
            snapshot.assert_not_awaited()

    async def test_all_read_screens_render_without_telegram_requests(self):
        views = {"home": "0", "stats": "0", "profile": "0", "places": "0", "notes": "0", "last": "0", "history": "0", "members": "0", "roles": "0", "board": "0", "report": "0", "top": "total", "candidates": "0", "admin": "0", "help": "0", "health": "0"}
        async with self.sessions() as session:
            for view, extra in views.items():
                with self.subTest(view=view):
                    screen = await build_screen(app, session, 900, view, 10, extra=extra)
                    self.assertTrue(screen.text)
                    self.assertLess(len(unescape(re.sub("<[^>]+>", "", screen.text))), 4096)
                    for row in screen.markup.inline_keyboard:
                        for b in row:
                            self.assertLessEqual(len(b.callback_data.encode()), 64)

    async def test_sqlite_backup_keeps_data_and_uses_unique_filenames(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.db"
            with sqlite3.connect(source) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.execute("INSERT INTO sample VALUES ('synthetic')")
            with patch.object(app, "DB_URL", "sqlite+aiosqlite:///" + str(source)), patch.object(app, "BACKUP_DIR", Path(directory) / "backups"):
                left = await app.backup_database()
                right = await app.backup_database()
                self.assertNotEqual(left, right)
                with sqlite3.connect(left) as connection:
                    self.assertEqual(connection.execute("SELECT value FROM sample").fetchone()[0], "synthetic")
                    self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    async def test_save_entry_rechecks_admin_and_duplicate_content(self):
        data = {"kind": "note", "uid": 10, "text": "test note"}
        async with self.sessions() as session:
            with self.assertRaises(PermissionError):
                await save_entry(app, session, 10, data)
            await save_entry(app, session, 900, data)
            await save_entry(app, session, 900, data)
            self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 1)

    async def test_malformed_callback_returns_helpful_notice(self):
        home = await self.command("/start")
        await self.click(home, "", raw="ui:broken")
        self.assertTrue(self.api.calls[-1].show_alert)

    async def test_group_menu_goes_to_private_chat(self):
        result = await self.command("/menu", chat_id=app.TRACK_CHAT_ID)
        self.assertIn("личные сообщения", result.text)
        self.assertTrue(result.reply_markup.inline_keyboard[0][0].url.startswith("https://t.me/"))
        self.assertTrue(any(isinstance(call, SendMessage) and call.chat_id == 900 and "301 / TRACKER" in call.text for call in self.api.calls))

    def test_callback_lengths_and_validation(self):
        for period in ("7d", "30d", "month", "all"):
            payload = button(9999999999, "test", "awardsmall", 9999999999, period).callback_data
            self.assertLessEqual(len(payload.encode()), 64)
            self.assertEqual(parse_payload(payload)[0], 9999999999)
        for payload in ("ui:x:home:0:month:0", "ui:10:home:-1:month:0", "ui:10:home:0:bad:0"):
            with self.assertRaises(ValueError):
                parse_payload(payload)


if __name__ == "__main__":
    unittest.main()
