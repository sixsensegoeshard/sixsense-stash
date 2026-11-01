"""Click-path regressions: fresh router, synthetic Telegram and in-memory DB only.

Some tests intentionally expose audit findings and may fail until main fixes
the application. Do not xfail them: they describe the intended user outcome.
"""

import unittest
from datetime import timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# test_tracking disables dotenv and substitutes synthetic configuration before
# importing the application. Never import trackerbot directly in this module.
from test_tracking import CHAT_ID, NOW, bot as app, message
from test_ui import FakeTelegram
from tracker_records import modify_record, record_version
from tracker_ui import Entry, build_screen, parse_payload, register_ui


class NavigationAuditTests(unittest.IsolatedAsyncioTestCase):
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
            patch.object(app, "SMALL_THRESHOLD", 5),
            patch.object(app, "LARGE_THRESHOLD", 1),
            patch.object(app, "UNDO_WINDOW_MINUTES", 15),
        ]
        for item in self.patches:
            item.start()
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
        async with self.sessions() as session:
            for uid, username in ((10, "artist"), (900, "admin"), (901, "other_admin")):
                await app.upsert_user(session, uid, username, username)
            await session.commit()

    async def asyncTearDown(self):
        await self.dp.storage.close()
        await self.dp.fsm.events_isolation.close()
        await self.telegram.session.close()
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()

    @staticmethod
    def buttons(screen, action=None):
        return [item for row in screen.reply_markup.inline_keyboard for item in row
                if item.callback_data and (action is None
                    or parse_payload(item.callback_data)[1] == action)]

    async def show(self, view="home", uid=10, *, extra="0", period="month"):
        async with self.sessions() as session:
            screen = await build_screen(app, session, 900, view, uid, period, extra)
        return await self.telegram.send_message(900, screen.text, reply_markup=screen.markup)

    async def tap(self, screen, action, *, uid=None, extra=None, label=None,
                  actor=900, first=False):
        matches = [item for item in self.buttons(screen, action)
                   if (uid is None or parse_payload(item.callback_data)[2] == uid)
                   and (extra is None or parse_payload(item.callback_data)[4] == extra)
                   and (label is None or label in item.text)]
        if not first:
            self.assertEqual(len(matches), 1, (action, uid, extra, label))
        self.assertTrue(matches, action)
        self.update_id += 1
        update = Update.model_validate({
            "update_id": self.update_id,
            "callback_query": {
                "id": str(self.update_id), "chat_instance": "navigation-audit",
                "data": matches[0].callback_data,
                "from_user": {"id": actor, "is_bot": False, "first_name": "Tester"},
                "message": screen.model_dump(),
            },
        })
        await self.dp.feed_update(self.telegram, update)
        return self.api.messages[screen.message_id]

    async def type_text(self, text):
        self.update_id += 1
        incoming = message(message_id=self.update_id, uid=900, username="admin", text=text,
                           chat={"id": 900, "type": "private"})
        await self.dp.feed_update(self.telegram, Update(update_id=self.update_id, message=incoming))
        return self.api.messages[self.api.sequence]

    async def count(self, model):
        async with self.sessions() as session:
            return await session.scalar(select(func.count()).select_from(model))

    async def seed_note(self, text="Original note", *, uid=10, age=1, actor=900):
        async with self.sessions() as session:
            item = app.Note(user_id=uid, actor_id=actor, username=f"ID {uid}", text=text,
                            created_at=NOW - timedelta(minutes=age))
            session.add(item)
            await session.commit()
            return item.id

    async def seed_place(self, *, uid=10, tier="large", age=1):
        async with self.sessions() as session:
            item = app.Placement(user_id=uid, actor_id=900, username=f"ID {uid}",
                                 tier=tier, comment="Synthetic release",
                                 created_at=NOW - timedelta(minutes=age))
            session.add(item)
            await session.commit()
            return item.id

    async def configure_report(self, chat=CHAT_ID, thread=7):
        async with self.sessions() as session:
            for key, value in (("report_chat", chat), ("report_thread", thread)):
                await session.merge(app.Setting(key=key, value=str(value)))
            await session.commit()

    async def report_settings(self):
        async with self.sessions() as session:
            return {row.key: row.value for row in (await session.scalars(
                select(app.Setting).where(app.Setting.key.in_(
                    ["report_chat", "report_thread"])))).all()}

    async def note_preview(self, text="Draft", *, uid=10):
        profile = await self.show("profile", uid)
        more = await self.tap(profile, "more")
        await self.tap(more, "addnote")
        return await self.type_text(text)

    async def filtered_second_page(self):
        async with self.sessions() as session:
            for uid in range(1000, 1012):
                await app.upsert_user(session, uid, f"match{uid}", f"Match {uid}")
            await session.commit()
        members = await self.tap(await self.show(), "members")
        await self.tap(members, "search")
        filtered = await self.type_text("match")
        by_name = await self.tap(filtered, "members", label="По имени")
        second = await self.tap(by_name, "members", label="Далее")
        self.assertIn("страница 2/2", second.text)
        return second

    async def operation_preview(self, kind):
        if kind == "reportoff":
            await self.configure_report()
            return await self.tap(await self.show("reports"), "reportoff")
        if kind == "undo":
            await self.seed_note()
            return await self.tap(await self.show("admin"), "undo")
        if kind == "big":
            await self.seed_place()
            return await self.tap(await self.show("roles"), "awardbig")
        raise AssertionError(kind)

    async def assert_operation_not_applied(self, kind):
        if kind == "reportoff":
            self.assertEqual(await self.report_settings(), {
                "report_chat": str(CHAT_ID), "report_thread": "7"})
        elif kind == "undo":
            self.assertGreater(await self.count(app.Note), 0)
            self.assertEqual(await self.count(app.RecordChange), 0)
        else:
            self.assertEqual(await self.count(app.RoleAward), 0)

    async def test_cancel_search_restores_query_sort_and_page(self):
        second = await self.filtered_second_page()
        old_context = await self.state.get_data()
        prompt = await self.tap(second, "search")
        returned = await self.tap(prompt, "cancel")
        self.assertEqual(await self.state.get_data(), old_context)
        self.assertIn("Поиск: <b>match</b>", returned.text,
                      "Cancel rendered an unfiltered directory despite retaining filtered FSM data")
        self.assertIn("страница 2/2", returned.text)
        self.assertIn("✓ По имени", [b.text for b in self.buttons(returned)])

    async def test_form_cancel_and_revise_save_keep_directory_context(self):
        second = await self.filtered_second_page()
        profile = await self.tap(second, "profile", first=True)
        uid = parse_payload(self.buttons(profile, "addplace")[0].callback_data)[2]
        context = await self.state.get_data()
        tier = await self.tap(profile, "addplace")
        profile = await self.tap(tier, "cancel")
        self.assertEqual(await self.state.get_data(), context)
        more = await self.tap(profile, "more")
        await self.tap(more, "addnote")
        draft = await self.type_text("First wording")
        await self.tap(draft, "revise")
        revised = await self.type_text("Final wording")
        saved = await self.tap(revised, "save")
        self.assertEqual(await self.state.get_data(), context)
        back = await self.tap(saved, "members")
        self.assertIn("Поиск: <b>match</b>", back.text)
        self.assertIn("страница 2/2", back.text)
        self.assertIn("✓ По имени", [b.text for b in self.buttons(back)])
        async with self.sessions() as session:
            note = await session.scalar(select(app.Note))
            self.assertEqual((note.user_id, note.text), (uid, "Final wording"))

    async def test_old_form_save_revise_and_cancel_leave_new_preview_usable(self):
        old = await self.note_preview("Old draft")
        new = await self.note_preview("New draft", uid=901)
        before = await self.state.get_data()
        for action in ("save", "revise", "cancel"):
            with self.subTest(action=action):
                await self.tap(old, action)
                self.assertEqual(await self.state.get_state(), Entry.confirm.state)
                self.assertEqual(await self.state.get_data(), before)
                self.assertEqual(self.api.messages[new.message_id].reply_markup, new.reply_markup)
        await self.tap(new, "save")
        async with self.sessions() as session:
            notes = (await session.scalars(select(app.Note))).all()
            self.assertEqual([(item.user_id, item.text) for item in notes], [(901, "New draft")])

    async def test_old_tier_choice_cannot_redirect_a_new_form(self):
        old = await self.tap(await self.show("profile"), "addplace")
        current = await self.tap(await self.show("profile", 901), "addplace")
        before = await self.state.get_data()
        await self.tap(old, "large")
        self.assertEqual(await self.state.get_state(), Entry.tier.state)
        self.assertEqual(await self.state.get_data(), before)
        await self.tap(current, "small")
        preview = await self.type_text("Artist | Track")
        await self.tap(preview, "save")
        async with self.sessions() as session:
            place = await session.scalar(select(app.Placement))
            self.assertEqual((place.user_id, place.tier, place.track), (901, "small", "Track"))

    async def test_preview_nonce_is_also_bound_to_its_message(self):
        preview = await self.note_preview()
        copy = await self.telegram.send_message(900, preview.text, reply_markup=preview.reply_markup)
        before = await self.state.get_data()
        await self.tap(copy, "save")
        self.assertEqual(await self.state.get_data(), before)
        self.assertEqual(await self.count(app.Note), 0)
        await self.tap(preview, "save")
        self.assertEqual(await self.count(app.Note), 1)

    async def test_invalid_placement_text_keeps_form_then_valid_input_saves(self):
        tier = await self.tap(await self.show("profile"), "addplace")
        await self.tap(tier, "large")
        before = await self.state.get_data()
        error = await self.type_text("Artist |")
        self.assertIn("Укажи артиста", error.text)
        self.assertEqual(await self.state.get_state(), Entry.details.state)
        self.assertEqual(await self.state.get_data(), before)
        preview = await self.type_text("Artist | Correct track")
        await self.tap(preview, "save")
        async with self.sessions() as session:
            place = await session.scalar(select(app.Placement))
            self.assertEqual((place.user_id, place.tier, place.track), (10, "large", "Correct track"))

    async def test_regular_user_or_other_admin_cannot_confirm_owners_form(self):
        preview = await self.note_preview()
        before = await self.state.get_data()
        for actor in (10, 901):
            with self.subTest(actor=actor):
                await self.tap(preview, "save", actor=actor)
                self.assertIsInstance(self.api.calls[-1], AnswerCallbackQuery)
                self.assertTrue(self.api.calls[-1].show_alert)
                self.assertEqual(await self.state.get_data(), before)
                self.assertEqual(await self.count(app.Note), 0)
        await self.tap(preview, "save")
        self.assertEqual(await self.count(app.Note), 1)

    async def test_revoked_admin_cannot_confirm_report_undo_or_role(self):
        for kind in ("reportoff", "undo", "big"):
            with self.subTest(kind=kind):
                preview = await self.operation_preview(kind)
                with patch.object(app, "ADMIN_IDS", {901}):
                    await self.tap(preview, "save")
                self.assertIsInstance(self.api.calls[-1], AnswerCallbackQuery)
                self.assertTrue(self.api.calls[-1].show_alert)
                await self.assert_operation_not_applied(kind)

    async def test_expired_report_undo_and_role_confirmations_do_not_write(self):
        for kind in ("reportoff", "undo", "big"):
            with self.subTest(kind=kind):
                preview = await self.operation_preview(kind)
                with patch.object(app, "utc_now", return_value=NOW + timedelta(minutes=16)):
                    result = await self.tap(preview, "save")
                self.assertIn("устарел", result.text)
                self.assertIsNone(await self.state.get_state())
                await self.assert_operation_not_applied(kind)

    async def test_navigation_away_invalidates_report_undo_and_role_confirmations(self):
        for kind in ("reportoff", "undo", "big"):
            with self.subTest(kind=kind):
                home = await self.show()
                preview = await self.operation_preview(kind)
                await self.tap(home, "help")
                self.assertIsNone(await self.state.get_state())
                await self.tap(preview, "save")
                await self.assert_operation_not_applied(kind)

    async def test_report_cancel_and_manual_views_leave_destination_intact(self):
        await self.configure_report()
        expected = await self.report_settings()
        reports = await self.show("reports")
        preview = await self.tap(reports, "reportoff")
        reports = await self.tap(preview, "cancel")
        self.assertIn("Автоотчёт включён", reports.text)
        report = await self.tap(reports, "report")
        self.assertIn("Прошлая неделя", report.text)
        reports = await self.tap(report, "reports")
        setup = await self.tap(reports, "reportsetup")
        returned = await self.tap(setup, "reports")
        self.assertIn("Автоотчёт включён", returned.text)
        self.assertEqual(await self.report_settings(), expected)
        self.assertTrue(all(call.chat_id == 900 for call in self.api.calls
                            if isinstance(call, (SendMessage, EditMessageText))))

    async def test_report_confirmation_disables_only_after_save_and_returns_to_reports(self):
        preview = await self.operation_preview("reportoff")
        self.assertEqual(len(await self.report_settings()), 2)
        result = await self.tap(preview, "save")
        self.assertEqual(await self.report_settings(), {})
        self.assertIn("Автоотчёт выключен", result.text)
        self.assertTrue(self.buttons(result, "reportsetup"))
        self.assertFalse(self.buttons(result, "reportoff"))

    async def test_report_disable_rejects_destination_changed_after_preview(self):
        for chat, thread in ((CHAT_ID, 99), (-100999, 7)):
            with self.subTest(chat=chat, thread=thread):
                preview = await self.operation_preview("reportoff")
                # Another administrator completed /setreport while this form was open.
                # Simulate its DB effect without invoking a group handler or sending.
                await self.configure_report(chat, thread)
                expected = await self.report_settings()
                await self.tap(preview, "save")
                self.assertEqual(await self.report_settings(), expected,
                                 "Old confirmation disabled another administrator's new destination")

    async def test_confirmation_hint_names_buttons_that_exist(self):
        for kind in ("reportoff", "undo", "big"):
            with self.subTest(kind=kind):
                preview = await self.operation_preview(kind)
                hint = await self.type_text("What next?")
                self.assertTrue(self.buttons(preview, "save"))
                self.assertFalse(self.buttons(preview, "revise"))
                self.assertIn("Подтвердить", hint.text)
                self.assertNotIn("Изменить текст", hint.text)

    async def test_duplicate_save_keeps_success_screen_and_return_buttons(self):
        preview = await self.note_preview("Saved once")
        saved = await self.tap(preview, "save")
        self.assertEqual(await self.count(app.Note), 1)
        await self.tap(preview, "save")
        self.assertEqual(await self.count(app.Note), 1)
        self.assertEqual(self.api.messages[saved.message_id].text, saved.text,
                         "Queued duplicate save replaced the successful profile with an expiry screen")
        self.assertEqual(self.api.messages[saved.message_id].reply_markup, saved.reply_markup)

    async def test_undo_confirmation_deletes_exact_preview_and_records_history(self):
        await self.seed_note("Older", age=2)
        target = await self.seed_note("Latest", age=1)
        preview = await self.tap(await self.show("admin"), "undo")
        self.assertIn("Latest", preview.text)
        self.assertEqual(await self.count(app.Note), 2)
        result = await self.tap(preview, "save")
        self.assertIn("Заметка удалена", result.text)
        self.assertTrue(self.buttons(result, "reports"))
        async with self.sessions() as session:
            notes = (await session.scalars(select(app.Note))).all()
            self.assertEqual([item.text for item in notes], ["Older"])
            change = await session.scalar(select(app.RecordChange))
            self.assertEqual((change.record_id, change.action, change.actor_id), (target, "delete", 900))

    async def test_undo_cancel_from_addition_returns_to_that_profile(self):
        saved = await self.tap(await self.note_preview("Keep this"), "save")
        undo = await self.tap(saved, "undo")
        returned = await self.tap(undo, "cancel")
        self.assertEqual(await self.count(app.Note), 1)
        self.assertEqual(await self.count(app.RecordChange), 0)
        self.assertTrue(self.buttons(returned, "addplace"),
                        "Cancel on the profile's undo shortcut unexpectedly opened administration")

    async def test_saved_record_undo_shortcut_cannot_target_another_newer_record(self):
        saved = await self.tap(await self.note_preview("First member note"), "save")
        await self.seed_note("Other member's newer note", uid=901, age=-1)
        returned = await self.tap(saved, "undo")
        self.assertEqual(await self.count(app.Note), 2)
        self.assertFalse(self.buttons(returned, "save"))
        self.assertIn("прежней записи", returned.text)
        self.assertEqual(parse_payload(self.buttons(returned, "addplace")[0].callback_data)[2], 10)

    async def test_undo_rejects_a_newer_record_instead_of_deleting_either(self):
        preview = await self.operation_preview("undo")
        await self.seed_note("New latest", uid=901, age=0)
        result = await self.tap(preview, "save")
        self.assertIn("появилась новая", result.text)
        self.assertEqual(await self.count(app.Note), 2)
        self.assertEqual(await self.count(app.RecordChange), 0)

    async def test_undo_rechecks_record_version_after_preview(self):
        preview = await self.operation_preview("undo")
        async with self.sessions() as session:
            note = await session.scalar(select(app.Note))
            note.text = "Another admin's correction"
            await session.commit()
        result = await self.tap(preview, "save")
        self.assertIn("уже изменена", result.text)
        async with self.sessions() as session:
            self.assertEqual((await session.scalar(select(app.Note))).text,
                             "Another admin's correction")
        self.assertEqual(await self.count(app.RecordChange), 0)

    async def test_undo_rechecks_time_window_while_confirmation_is_still_fresh(self):
        await self.seed_note(age=14)
        preview = await self.tap(await self.show("admin"), "undo")
        with patch.object(app, "utc_now", return_value=NOW + timedelta(minutes=2)):
            result = await self.tap(preview, "save")
        self.assertIn("Повтори действие", result.text)
        self.assertEqual(await self.count(app.Note), 1)
        self.assertEqual(await self.count(app.RecordChange), 0)

    async def test_undo_rechecks_role_consumption_after_preview(self):
        await self.seed_place()
        preview = await self.tap(await self.show("admin"), "undo")
        async with self.sessions() as session:
            await app.reset_placement_progress(session, 10, "big", 901, "artist")
        result = await self.tap(preview, "save")
        self.assertIn("уже подтверждена роль", result.text)
        self.assertEqual(await self.count(app.Placement), 1)
        self.assertEqual(await self.count(app.RecordChange), 0)

    async def test_role_cancel_returns_to_progress_without_awarding(self):
        preview = await self.operation_preview("big")
        returned = await self.tap(preview, "cancel")
        self.assertEqual(await self.count(app.RoleAward), 0)
        self.assertIn("Прогресс к роли", returned.text,
                      "Cancelling role confirmation discarded the progress view")
        self.assertTrue(self.buttons(returned, "awardbig"))

    async def test_role_save_returns_to_progress_showing_new_cycle(self):
        preview = await self.operation_preview("big")
        returned = await self.tap(preview, "save")
        self.assertEqual(await self.count(app.RoleAward), 1)
        self.assertEqual(await self.count(app.Placement), 1)
        async with self.sessions() as session:
            self.assertFalse((await app.placement_progress(session, 10))["ready_big"])
        self.assertIn("Прогресс к роли", returned.text)
        self.assertIn("Подтверждённые роли", returned.text)
        self.assertFalse(self.buttons(returned, "awardbig"))

    async def test_role_rechecks_threshold_after_another_admin_deletes_placement(self):
        preview = await self.operation_preview("big")
        async with self.sessions() as session:
            place = await session.scalar(select(app.Placement))
            await modify_record(app, session, 901, 10, "place", place.id,
                                record_version(place), "delete")
        result = await self.tap(preview, "save")
        self.assertIn("Порог этой роли ещё не достигнут", result.text)
        self.assertEqual(await self.count(app.RoleAward), 0)

    async def test_role_preview_does_not_award_a_newly_earned_different_cycle(self):
        preview = await self.operation_preview("big")
        async with self.sessions() as session:
            await app.reset_placement_progress(session, 10, "big", 901, "artist")
            session.add(app.Placement(user_id=10, tier="large", comment="New cycle",
                                      created_at=NOW + timedelta(seconds=1)))
            await session.commit()
        with patch.object(app, "utc_now", return_value=NOW + timedelta(seconds=2)):
            returned = await self.tap(preview, "save")
        self.assertIn("цикл роли уже подтверждён", returned.text)
        self.assertEqual(await self.count(app.RoleAward), 1)
        async with self.sessions() as session:
            self.assertTrue((await app.placement_progress(session, 10))["ready_big"])

    async def test_confirming_other_role_scale_does_not_invalidate_big_preview(self):
        for _ in range(app.SMALL_THRESHOLD):
            await self.seed_place(tier="small")
        preview = await self.operation_preview("big")
        async with self.sessions() as session:
            await app.reset_placement_progress(session, 10, "small", 901, "artist")
        await self.tap(preview, "save")
        async with self.sessions() as session:
            awards = (await session.scalars(select(app.RoleAward.kind))).all()
            self.assertCountEqual(awards, ["small", "big"])

    async def test_role_board_return_restores_all_filter_and_second_page(self):
        for uid in range(20, 30):
            await self.seed_place(uid=uid, tier="small")
        board = await self.show("board", extra="all.1")
        self.assertIn("страница 2/2", board.text)
        progress = await self.tap(board, "roles", first=True)
        self.assertTrue(self.buttons(progress, "board"),
                        "Progress opened from the role board has no return to its filtered page")
        returned = await self.tap(progress, "board")
        self.assertIn("страница 2/2", returned.text)
        self.assertIn("✓ Все", [b.text for b in self.buttons(returned)])

    async def test_record_detail_back_retains_list_page(self):
        for index in range(6):
            await self.seed_note(f"Note {index}", age=index + 1)
        second = await self.show("notes", extra="1")
        self.assertIn("страница 2/2", second.text)
        detail = await self.tap(second, "rec", first=True)
        returned = await self.tap(detail, "notes")
        self.assertIn("страница 2/2", returned.text,
                      "Record detail hardcoded its back button to page zero")

    async def test_record_edit_save_and_delete_cancel_retain_list_page(self):
        for index in range(6):
            await self.seed_note(f"Note {index}", age=index + 1)
        for action in ("edt", "del"):
            with self.subTest(action=action):
                second = await self.show("notes", extra="1")
                detail = await self.tap(second, "rec", first=True)
                form = await self.tap(detail, action)
                if action == "edt":
                    form = await self.type_text("Corrected on page two")
                    returned = await self.tap(form, "save")
                else:
                    returned = await self.tap(form, "cancel")
                self.assertIn("страница 2/2", returned.text,
                              "Completing a record form reset the source list page")

    async def test_profile_round_trip_keeps_activity_period(self):
        stats = await self.show("stats", period="7d")
        profile = await self.tap(stats, "profile")
        returned = await self.tap(profile, "stats")
        self.assertIn("последние 7 дней", returned.text,
                      "Profile's activity button discarded the selected period")
        self.assertTrue(any(b.text == "7 дней" and b.style == "primary" for b in self.buttons(returned)))


if __name__ == "__main__":
    unittest.main()
