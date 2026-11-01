"""Concrete click-path/checkpoint regressions; synthetic data and temporary files only.

These assertions protect audited click paths. No live configuration, database
or Telegram API is used.
"""

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.types import Update
from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_tracking import CHAT_ID, NOW, bot as app, message
from test_ui import FakeTelegram
from tools import checkpoint
from tracker_ui import Entry, build_screen, button, keyboard, parse_payload, register_ui, save_entry


class _DatabaseAuditCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(app.Base.metadata.create_all)
        self.patches = [
            patch.object(app, "Session", self.sessions),
            patch.object(app, "ADMIN_IDS", {900, 901}),
            patch.object(app, "TRACK_CHAT_ID", CHAT_ID),
            patch.object(app, "BOT_TIMEZONE", "Europe/Moscow"),
            patch.object(app, "LOCAL_TZ", ZoneInfo("Europe/Moscow")),
            patch.object(app, "utc_now", return_value=NOW),
            patch.object(app, "local_now", return_value=NOW.replace(
                tzinfo=timezone.utc).astimezone(ZoneInfo("Europe/Moscow"))),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()


class ClickPathAuditTests(_DatabaseAuditCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        # A fresh UI router avoids attaching/reparenting the production router
        # used by test_ui.UITests when running the full suite.
        parent = Router()
        parent.message.outer_middleware(app.DatabaseMiddleware())
        register_ui(parent, app)
        self.dp = Dispatcher(events_isolation=SimpleEventIsolation())
        self.dp.include_router(parent)
        self.api = FakeTelegram()
        self.telegram = Bot("123456:synthetic-test-token", session=self.api,
                            default=DefaultBotProperties(parse_mode="HTML"))
        self.state = self.dp.fsm.get_context(bot=self.telegram, chat_id=900, user_id=900)
        self.update_id = 0

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.dp.fsm.events_isolation.close()
        await self.telegram.session.close()
        await super().asyncTearDown()

    async def show(self, view, uid=10):
        async with self.sessions() as session:
            screen = await build_screen(app, session, 900, view, uid)
        return await self.telegram.send_message(900, screen.text, reply_markup=screen.markup)

    @staticmethod
    def callback(screen, action):
        matches = [item.callback_data for row in screen.reply_markup.inline_keyboard
                   for item in row if item.callback_data
                   and parse_payload(item.callback_data)[1] == action]
        if len(matches) != 1:
            raise AssertionError(f"Expected one {action!r} button, got {matches!r}")
        return matches[0]

    async def click(self, screen, action):
        self.update_id += 1
        update = Update.model_validate({
            "update_id": self.update_id,
            "callback_query": {
                "id": str(self.update_id), "chat_instance": "flow-audit",
                "data": self.callback(screen, action),
                "from_user": {"id": 900, "is_bot": False, "first_name": "Admin"},
                "message": screen.model_dump(),
            },
        })
        await self.dp.feed_update(self.telegram, update)
        return self.api.messages[screen.message_id]

    async def type_text(self, text):
        self.update_id += 1
        incoming = message(message_id=self.update_id, uid=900, text=text,
                           chat={"id": 900, "type": "private"})
        await self.dp.feed_update(self.telegram,
                                  Update(update_id=self.update_id, message=incoming))
        return self.api.messages[self.api.sequence]

    async def test_queued_tier_cancel_preserves_the_current_details_prompt(self):
        launcher = await self.telegram.send_message(
            900, "Participant", reply_markup=keyboard([[button(900, "Add", "addplace", 10)]]))
        tier = await self.click(launcher, "addplace")
        details = await self.click(tier, "small")
        before = await self.state.get_data()

        # Both screens reuse the same Telegram message. A queued tap on the
        # previous keyboard must not overwrite its newer replacement.
        await self.click(tier, "cancel")

        self.assertEqual(await self.state.get_state(), Entry.details.state)
        self.assertEqual(await self.state.get_data(), before)
        current = self.api.messages[details.message_id]
        self.assertEqual(current.text, details.text,
                         "Stale cancel erased the active details prompt")
        self.assertEqual(current.reply_markup, details.reply_markup)

    async def test_queued_save_preserves_the_revise_prompt(self):
        launcher = await self.telegram.send_message(
            900, "Participant", reply_markup=keyboard([[button(900, "Add", "addnote", 10)]]))
        await self.click(launcher, "addnote")
        preview = await self.type_text("Original draft")
        revised_prompt = await self.click(preview, "revise")
        before = await self.state.get_data()

        await self.click(preview, "save")

        self.assertEqual(await self.state.get_state(), Entry.details.state)
        self.assertEqual(await self.state.get_data(), before)
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 0)
        current = self.api.messages[revised_prompt.message_id]
        self.assertEqual(current.text, revised_prompt.text,
                         "Stale save erased the active revise prompt")
        self.assertEqual(current.reply_markup, revised_prompt.reply_markup)

    async def test_role_confirmation_is_bound_to_the_previewed_cycle(self):
        async with self.sessions() as session:
            session.add(app.Placement(user_id=10, tier="large", comment="First cycle",
                                      created_at=NOW - timedelta(minutes=1)))
            await session.commit()
        preview = await self.click(await self.show("roles"), "awardbig")

        async with self.sessions() as session:
            await save_entry(app, session, 901, {"kind": "big", "uid": 10})
            session.add(app.Placement(user_id=10, tier="large", comment="New cycle",
                                      created_at=NOW + timedelta(seconds=1)))
            await session.commit()
        with patch.object(app, "utc_now", return_value=NOW + timedelta(seconds=2)):
            await self.click(preview, "save")

        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(app.RoleAward.id))), 1,
                             "Old confirmation awarded a different, newly earned cycle")
            self.assertTrue((await app.placement_progress(session, 10))["ready_big"])


class SnapshotAuditTests(_DatabaseAuditCase):
    async def test_recalculation_cannot_destroy_an_archive_after_retention(self):
        await self.assert_archive_survives_retention(days=7, expected_deleted=2)

    async def test_recalculation_cannot_replace_a_complete_archive_with_partial_logs(self):
        await self.assert_archive_survives_retention(days=15, expected_deleted=1)

    async def assert_archive_survives_retention(self, days, expected_deleted):
        start, end, period = app.previous_calendar_month_range()
        async with self.sessions() as session:
            for stamp in (start + timedelta(days=1), end - timedelta(days=5)):
                session.add(app.MsgLog(user_id=10, chat_id=CHAT_ID, category="message",
                                       created_at=stamp))
            await session.commit()
            await app.make_snapshot(session)
            archived = await session.scalar(select(app.MonthlySnapshot).where(
                app.MonthlySnapshot.period == period))
            self.assertEqual((archived.total, archived.active_days), (2, 2))
            with patch.object(app, "MSG_RETENTION_DAYS", days):
                self.assertEqual(await app.retention_cleanup(session), expected_deleted)
                self.assertEqual(await session.scalar(select(func.count(app.MsgLog.id))),
                                 2 - expected_deleted)
                try:
                    await app.make_snapshot(session)
                except ValueError:
                    # Refusing to recalculate an incomplete source is also safe.
                    await session.rollback()
            async with self.sessions() as fresh:
                archived = await fresh.scalar(select(app.MonthlySnapshot).where(
                    app.MonthlySnapshot.period == period))
                self.assertIsNotNone(archived, "Recalculation deleted the only archived copy")
                self.assertEqual((archived.total, archived.active_days), (2, 2))

    async def test_retention_keeps_the_complete_week_until_the_scheduled_report(self):
        monday_noon = datetime(2026, 10, 12, 12, tzinfo=ZoneInfo("Europe/Moscow"))
        noon_utc = monday_noon.astimezone(timezone.utc).replace(tzinfo=None)
        cleanup_utc = noon_utc - timedelta(hours=8, minutes=30)  # Monday 03:30 local.
        with patch.object(app, "local_now", return_value=monday_noon), patch.object(
                app, "utc_now", return_value=noon_utc):
            start, end, _ = app.previous_calendar_week_range()
            async with self.sessions() as session:
                session.add(app.Setting(key="report_chat", value=str(CHAT_ID)))
                for stamp in (start + timedelta(minutes=30), end - timedelta(minutes=30)):
                    session.add(app.MsgLog(user_id=10, username="artist", chat_id=CHAT_ID,
                                           category="message", created_at=stamp))
                await session.commit()
                complete_report = await app.build_weekly_report(session)
                self.assertIn("2 действия", complete_report)
                with patch.object(app, "MSG_RETENTION_DAYS", 7), patch.object(
                        app, "utc_now", return_value=cleanup_utc):
                    await app.retention_cleanup(session)
                self.assertEqual(await app.build_weekly_report(session), complete_report,
                                 "03:30 cleanup truncated the week reported at 12:00")


class CheckpointAuditTests(unittest.TestCase):
    def create_checkpoint(self, root, relative_database="tracker_v3.db", with_config=True):
        project = root / "project"
        project.mkdir()
        database = project / relative_database
        database.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE evidence(value TEXT)")
            connection.execute("INSERT INTO evidence VALUES ('checkpoint contents')")
        (project / "trackerbot.py").write_text("# synthetic project\n")
        if with_config:
            (project / ".env").write_text(f"DB_URL=sqlite+aiosqlite:///{relative_database}\n")
        with patch.object(checkpoint, "ROOT", project), patch.object(
                checkpoint.subprocess, "check_output",
                side_effect=["a" * 40 + "\n", b"trackerbot.py\0"]):
            return checkpoint.create(database, root / "checkpoint")

    def test_restore_places_database_at_the_configured_path(self):
        for relative_database in ("tracker_v3.db", "data/custom.db"):
            with self.subTest(database=relative_database), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                folder = self.create_checkpoint(root, relative_database)
                restored = root / "restored"
                checkpoint.restore(folder, restored)
                config = (restored / ".env").read_text().strip().split("=", 1)[1]
                configured_path = restored / make_url(config).database
                self.assertTrue(configured_path.is_file(),
                                "Restored DB_URL opens an empty/missing database")
                with sqlite3.connect(configured_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
                    self.assertEqual(db.execute("SELECT value FROM evidence").fetchone(),
                                     ("checkpoint contents",))

    def test_restore_does_not_copy_configuration_outside_the_manifest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = self.create_checkpoint(root, with_config=False)
            (folder / "config.env").write_text("DB_URL=sqlite+aiosqlite:///unverified.db\n")
            target = root / "restored"
            try:
                checkpoint.restore(folder, target)
            except ValueError:
                return
            self.assertFalse((target / ".env").exists(),
                             "restore copied config.env although its hash was never verified")


if __name__ == "__main__":
    unittest.main()
