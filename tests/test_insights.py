"""Read-only insight contracts, using synthetic fixtures and SQLite in memory."""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_tracking import bot as app, CHAT_ID, NOW
from tracker_insights import activity_comparison, member_directory, monthly_history


class InsightsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(app.Base.metadata.create_all)
        self.patches = [
            patch.object(app, "TRACK_CHAT_ID", CHAT_ID),
            patch.object(app, "BOT_TIMEZONE", "Europe/Moscow"),
            patch.object(app, "LOCAL_TZ", ZoneInfo("Europe/Moscow")),
            patch.object(app, "utc_now", return_value=NOW),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()

    def user(self, session, uid, username=None, name=None, **values):
        session.add(app.User(
            user_id=uid, username=username, first_name=name,
            first_seen=NOW - timedelta(days=100), last_seen=NOW, **values,
        ))

    def log(self, session, uid=10, stamp=None, category="message", **values):
        values.setdefault("chat_id", CHAT_ID)
        values.setdefault("username", "artist")
        values.setdefault("first_name", "Artist")
        session.add(app.MsgLog(
            user_id=uid, created_at=stamp if stamp is not None else NOW - timedelta(hours=1),
            category=category, **values,
        ))

    def archive(self, session, period, uid=10, **values):
        session.add(app.MonthlySnapshot(user_id=uid, period=period, **values))

    @contextmanager
    def statements(self):
        statements = []

        def capture(connection, cursor, statement, parameters, context, many):
            statements.append(statement)

        event.listen(self.engine.sync_engine, "before_cursor_execute", capture)
        try:
            yield statements
        finally:
            event.remove(self.engine.sync_engine, "before_cursor_execute", capture)

    async def test_directory_includes_all_known_sources_and_excludes_other_chat_only(self):
        async with self.sessions() as session:
            self.user(session, 10, "command_only", "Command")
            self.log(session, uid=20, username="group_only")
            session.add(app.Placement(user_id=30, username="@placed", created_at=NOW))
            session.add(app.Note(user_id=40, username="noted", text="Note", created_at=NOW))
            self.log(session, uid=50, chat_id=-100999)
            self.log(session, uid=60, chat_id=60)
            self.user(session, 0, name="Invalid")
            await session.commit()
            result = await member_directory(app, session)
            items = {item["uid"]: item for item in result["items"]}
            self.assertEqual(set(items), {10, 20, 30, 40})
            self.assertEqual(items[30]["name"], "@placed")
            self.assertEqual(items[40]["name"], "@noted")
            self.assertEqual((items[10]["total"], items[10]["last_activity"]), (0, None))
            self.assertEqual(result["total"], 4)

    async def test_recent_uses_group_activity_not_command_seen_or_manual_dates(self):
        async with self.sessions() as session:
            self.user(session, 10, "Zeta", "Zeta")
            self.user(session, 20, "Alpha", "Alpha")
            self.user(session, 30, "Beta", "Beta")
            self.log(session, uid=10, stamp=NOW - timedelta(days=2))
            self.log(session, uid=20, stamp=NOW - timedelta(days=1))
            self.log(session, uid=10, stamp=NOW - timedelta(minutes=1), chat_id=-100999)
            self.log(session, uid=10, stamp=NOW + timedelta(days=1))
            session.add(app.Note(user_id=30, username="Beta", text="New", created_at=NOW))
            await session.commit()
            items = (await member_directory(app, session))["items"]
            self.assertEqual([item["uid"] for item in items], [20, 10, 30])
            self.assertEqual(items[1]["last_activity"], NOW - timedelta(days=2))

    async def test_recent_ties_and_name_sort_use_unicode_casefold_then_id(self):
        async with self.sessions() as session:
            for uid, name in [(40, "Борис"), (20, "Straße"), (10, "STRASSE"), (30, "Анна")]:
                self.user(session, uid, name=name)
                self.log(session, uid=uid)
            await session.commit()
            for sort in ("recent", "name"):
                result = await member_directory(app, session, sort=sort)
                self.assertEqual([item["uid"] for item in result["items"]], [10, 20, 30, 40])

    async def test_active_sort_counts_current_local_month_only(self):
        async with self.sessions() as session:
            self.user(session, 10, name="Alpha")
            self.user(session, 20, name="Beta")
            self.log(session, uid=10, stamp=datetime(2026, 9, 30, 20, 59, 59))
            self.log(session, uid=20, stamp=datetime(2026, 9, 30, 21))
            self.log(session, uid=20)
            self.log(session, uid=10, chat_id=-100999)
            self.log(session, uid=10, stamp=NOW + timedelta(seconds=1))
            await session.commit()
            items = (await member_directory(app, session, sort="active"))["items"]
            self.assertEqual([(item["uid"], item["total"]) for item in items], [(20, 2), (10, 0)])

    async def test_current_profile_overrides_old_or_other_chat_identity(self):
        async with self.sessions() as session:
            self.user(session, 10, name="Новое", last_name="Имя")
            self.log(session, username="old_handle")
            self.log(session, stamp=NOW - timedelta(minutes=1), username="other_chat", chat_id=-100999)
            await session.commit()
            self.assertEqual((await member_directory(app, session))["items"][0]["name"], "Новое Имя")
            self.assertEqual((await member_directory(app, session, query="@old_handle"))["total"], 0)
            self.assertEqual((await member_directory(app, session, query="имя"))["total"], 1)

    async def test_legacy_identity_uses_latest_group_log_then_latest_manual_name(self):
        async with self.sessions() as session:
            self.log(session, username="old", stamp=NOW - timedelta(days=1))
            self.log(session, username="new", stamp=NOW - timedelta(hours=1))
            self.log(session, username="foreign", stamp=NOW - timedelta(minutes=1), chat_id=-100999)
            session.add(app.Placement(user_id=20, username="old_manual", created_at=NOW - timedelta(days=1)))
            session.add(app.Note(user_id=20, username="@new_manual", text="Note", created_at=NOW))
            session.add(app.Note(user_id=30, text="Unnamed", created_at=NOW))
            await session.commit()
            names = {item["uid"]: item["name"] for item in (await member_directory(app, session))["items"]}
            self.assertEqual(names, {10: "@new", 20: "@new_manual", 30: "ID 30"})

    async def test_directory_returns_all_ambiguous_matches(self):
        async with self.sessions() as session:
            self.user(session, 10, "producer_one", "Producer")
            self.user(session, 20, "producer_two", "Producer")
            self.user(session, 30, "someone", "Other")
            await session.commit()
            result = await member_directory(app, session, query="  PRODUCER  ", page_size=1)
            self.assertEqual((result["query"], result["total"], result["pages"]), ("PRODUCER", 2, 2))
            second = await member_directory(app, session, query="producer", page=1, page_size=1)
            self.assertEqual({result["items"][0]["uid"], second["items"][0]["uid"]}, {10, 20})

    async def test_username_is_exact_and_duplicate_handles_return_choices(self):
        async with self.sessions() as session:
            self.user(session, 10, "artist", "One")
            self.user(session, 20, "ARTIST", "Two")
            self.user(session, 30, "artist_extra", "Three")
            await session.commit()
            result = await member_directory(app, session, query="@ArTiSt")
            self.assertEqual([item["uid"] for item in result["items"]], [10, 20])
            self.assertEqual((await member_directory(app, session, query="@art"))["total"], 0)

    async def test_numeric_lookup_requires_exact_existing_positive_id(self):
        async with self.sessions() as session:
            self.user(session, 10, name="Artist")
            self.user(session, 20, name="999")
            await session.commit()
            for query in ("10", "0010", "+10"):
                self.assertEqual((await member_directory(app, session, query=query))["items"][0]["uid"], 10)
            for query in ("999", "0", "-10", "9" * 5000):
                self.assertEqual((await member_directory(app, session, query=query))["total"], 0)

    async def test_unicode_substring_and_literal_sql_wildcards(self):
        async with self.sessions() as session:
            self.user(session, 10, "wild_name", "Straße")
            self.user(session, 20, "wildXname", "Анна")
            self.user(session, 30, name="100% Real")
            await session.commit()
            for query, ids in [("STRASSE", [10]), ("АННА", [20]), ("wild_", [10]), ("%", [30]), ("' OR 1=1 --", [])]:
                result = await member_directory(app, session, query=query)
                self.assertEqual([item["uid"] for item in result["items"]], ids)

    async def test_directory_pagination_clamps_after_search(self):
        async with self.sessions() as session:
            for uid in range(1, 11):
                self.user(session, uid, name=f"Artist {uid:02}")
            await session.commit()
            result = await member_directory(app, session, page=99, page_size=3, sort="name")
            self.assertEqual((result["page"], result["pages"], result["total"]), (3, 4, 10))
            self.assertEqual([item["uid"] for item in result["items"]], [10])
            self.assertEqual((await member_directory(app, session, page=-2))["page"], 0)
            empty = await member_directory(app, session, page=99, query="absent")
            self.assertEqual((empty["items"], empty["page"], empty["pages"]), ([], 0, 1))
            with self.assertRaises(ValueError):
                await member_directory(app, session, page_size=0)
            with self.assertRaises(ValueError):
                await member_directory(app, session, sort="invalid")

    async def test_directory_batches_queries_instead_of_n_plus_one(self):
        async with self.sessions() as session:
            for uid in range(1, 41):
                self.user(session, uid, name=f"Artist {uid}")
                self.log(session, uid=uid)
                session.add(app.Note(user_id=uid, username=f"manual{uid}", text="Note", created_at=NOW))
            await session.commit()
            with self.statements() as small:
                await member_directory(app, session, page_size=1)
            with self.statements() as large:
                result = await member_directory(app, session, page_size=100)
            self.assertEqual(result["total"], 40)
            self.assertEqual(len(small), len(large))
            self.assertLessEqual(len(large), 6)

    async def test_monthly_history_respects_local_month_day_boundaries_and_group(self):
        async with self.sessions() as session:
            self.log(session, stamp=datetime(2026, 9, 30, 20, 59, 59))
            self.log(session, stamp=datetime(2026, 9, 30, 21), category="loop", opportunity=True)
            self.log(session, stamp=datetime(2026, 10, 1, 1), category="zip")
            self.log(session, stamp=datetime(2026, 10, 1, 21), category="legacy")
            self.log(session, uid=20)
            self.log(session, chat_id=-100999)
            self.log(session, stamp=NOW + timedelta(seconds=1))
            self.log(session, stamp=NOW)
            await session.commit()
            october, september = await monthly_history(app, session, 10)
            self.assertEqual(october, {
                "period": "2026-10", "messages": 0, "loops": 1, "zips": 1,
                "files": 1, "media": 0, "opportunities": 1, "total": 3, "active_days": 2,
            })
            self.assertEqual((september["period"], september["messages"], september["active_days"]), ("2026-09", 1, 1))

    async def test_history_uses_bot_timezone_including_negative_offset(self):
        with patch.object(app, "BOT_TIMEZONE", "America/Los_Angeles"):
            async with self.sessions() as session:
                self.log(session, stamp=datetime(2026, 10, 1, 6, 59, 59))
                self.log(session, stamp=datetime(2026, 10, 1, 7), category="media")
                await session.commit()
                history = await monthly_history(app, session, 10)
                self.assertEqual([(row["period"], row["total"]) for row in history], [("2026-10", 1), ("2026-09", 1)])

    async def test_raw_month_overrides_archive_and_archive_survives_retention(self):
        async with self.sessions() as session:
            self.log(session, stamp=datetime(2026, 9, 15), category="files")
            self.archive(session, "2026-09", messages=99, total=99, active_days=30)
            self.archive(session, "2026-08", messages=4, loops=2, total=6, active_days=3, opportunities=1)
            self.archive(session, "2025-12", media=2, total=2, active_days=1)
            self.archive(session, "2026-07", uid=20, total=100)
            await session.commit()
            history = await monthly_history(app, session, 10)
            self.assertEqual([row["period"] for row in history], ["2026-10", "2026-09", "2026-08", "2025-12"])
            self.assertEqual((history[1]["total"], history[1]["files"], history[1]["messages"]), (1, 1, 0))
            self.assertEqual((history[2]["total"], history[2]["active_days"], history[2]["opportunities"]), (6, 3, 1))

    async def test_foreign_raw_logs_do_not_override_the_users_archive(self):
        async with self.sessions() as session:
            self.log(session, stamp=datetime(2026, 8, 10), chat_id=-100999)
            self.log(session, uid=20, stamp=datetime(2026, 8, 10))
            self.archive(session, "2026-08", total=7)
            await session.commit()
            self.assertEqual((await monthly_history(app, session, 10))[1]["total"], 7)

    async def test_history_empty_current_month_and_invalid_or_future_archives(self):
        async with self.sessions() as session:
            for period in ("2026-11", "2026-10", "2026-13", "0000-01", "bad", "2026-8"):
                self.archive(session, period, total=100)
            self.archive(session, "2026-08", total=3)
            self.archive(session, "2026-08", total=5)
            await session.commit()
            history = await monthly_history(app, session, 10)
            self.assertEqual([(row["period"], row["total"]) for row in history], [("2026-10", 0), ("2026-08", 5)])
            self.assertEqual((await monthly_history(app, session, 999))[0]["total"], 0)

    async def test_all_raw_categories_and_overlapping_opportunities(self):
        async with self.sessions() as session:
            for category in ("message", "loop", "zip", "files", "media", "unknown"):
                self.log(session, category=category, opportunity=True)
            await session.commit()
            month = (await monthly_history(app, session, 10))[0]
            self.assertEqual([month[key] for key in ("messages", "loops", "zips", "files", "media")], [1, 1, 1, 2, 1])
            self.assertEqual((month["total"], month["opportunities"], month["active_days"]), (6, 6, 1))

    async def test_comparison_rolling_windows_are_adjacent_and_half_open(self):
        for period, days in (("7d", 7), ("30d", 30)):
            async with self.sessions() as session:
                uid = days
                start = NOW - timedelta(days=days)
                previous = start - timedelta(days=days)
                for stamp in (previous, previous + timedelta(seconds=1), start, start + timedelta(seconds=1), NOW - timedelta(seconds=1)):
                    self.log(session, uid=uid, stamp=stamp)
                for stamp in (previous - timedelta(microseconds=1), NOW, NOW + timedelta(seconds=1)):
                    self.log(session, uid=uid, stamp=stamp)
                self.log(session, uid=uid, stamp=start, chat_id=-100999)
                self.log(session, uid=999, stamp=start)
                await session.commit()
                result = await activity_comparison(app, session, uid, period)
                self.assertEqual((result["current"], result["previous"], result["delta"], result["percent"]), (3, 2, 1, 50.0))
                self.assertTrue(result["label"])

    async def test_comparison_partial_month_uses_same_elapsed_previous_duration(self):
        # Oct 10 15:00 Moscow compares Sept 1 00:00 through Sept 10 15:00.
        async with self.sessions() as session:
            for stamp in (datetime(2026, 8, 31, 21), datetime(2026, 9, 10, 11, 59, 59), datetime(2026, 9, 30, 21)):
                self.log(session, stamp=stamp)
            for stamp in (datetime(2026, 9, 10, 12), datetime(2026, 9, 29), NOW):
                self.log(session, stamp=stamp)
            self.log(session, stamp=datetime(2026, 9, 10, 1), chat_id=-100999)
            await session.commit()
            result = await activity_comparison(app, session, 10, "month")
            self.assertEqual((result["current"], result["previous"], result["delta"], result["percent"]), (1, 2, -1, -50.0))

    async def test_comparison_clamps_previous_shorter_month(self):
        now = datetime(2026, 3, 31, 9)
        with patch.object(app, "utc_now", return_value=now):
            async with self.sessions() as session:
                self.log(session, stamp=datetime(2026, 2, 28, 20, 59, 59))
                self.log(session, stamp=datetime(2026, 2, 28, 21))
                self.log(session, stamp=datetime(2026, 3, 1))
                await session.commit()
                result = await activity_comparison(app, session, 10, "month")
                self.assertEqual((result["current"], result["previous"]), (2, 1))

    async def test_comparison_month_boundary_year_rollover_and_zero_elapsed(self):
        now = datetime(2026, 12, 31, 21)  # Jan 1 00:00 Moscow.
        with patch.object(app, "utc_now", return_value=now):
            async with self.sessions() as session:
                self.log(session, stamp=now - timedelta(seconds=1))
                await session.commit()
                result = await activity_comparison(app, session, 10, "month")
                self.assertEqual((result["current"], result["previous"], result["percent"]), (0, 0, None))
                history = await monthly_history(app, session, 10)
                self.assertEqual([row["period"] for row in history], ["2027-01", "2026-12"])

    async def test_comparison_zero_baseline_never_means_infinite_percent(self):
        async with self.sessions() as session:
            self.log(session)
            await session.commit()
            result = await activity_comparison(app, session, 10, "7d")
            self.assertEqual((result["current"], result["previous"], result["delta"], result["percent"]), (1, 0, 1, None))
            empty = await activity_comparison(app, session, 999, "7d")
            self.assertEqual((empty["delta"], empty["percent"]), (0, None))

    async def test_comparison_all_skips_queries_and_unknown_period_fails(self):
        async with self.sessions() as session:
            with self.statements() as statements:
                self.assertIsNone(await activity_comparison(app, session, 10, "all"))
            self.assertEqual(statements, [])
            with self.assertRaises(ValueError):
                await activity_comparison(app, session, 10, "invalid")

    async def test_comparison_calls_injected_group_scoped_pivot_for_target(self):
        async with self.sessions() as session:
            with patch.object(app, "user_pivot", new=AsyncMock(wraps=app.user_pivot)) as pivot:
                await activity_comparison(app, session, 10, "7d")
            self.assertEqual(pivot.await_count, 2)
            self.assertEqual(pivot.await_args_list[0].args, (session, NOW - timedelta(days=7), NOW))
            self.assertEqual(pivot.await_args_list[1].args, (session, NOW - timedelta(days=14), NOW - timedelta(days=7)))
            self.assertTrue(all(call.kwargs == {"user_id": 10} for call in pivot.await_args_list))

    async def test_all_apis_only_select_and_do_not_flush_pending_changes(self):
        async with self.sessions() as session:
            self.user(session, 10, "artist", "Artist")
            self.log(session)
            await session.commit()
            profile = await session.get(app.User, 10)
            profile.first_name = "Pending edit"
            pending = app.Note(user_id=999, text="Pending insert", created_at=NOW)
            session.add(pending)
            with self.statements() as statements, patch.object(
                session, "commit", side_effect=AssertionError("unexpected commit"),
            ), patch.object(session, "flush", side_effect=AssertionError("unexpected flush")):
                directory = await member_directory(app, session)
                await monthly_history(app, session, 10)
                await activity_comparison(app, session, 10, "month")
            self.assertEqual([item["uid"] for item in directory["items"]], [10])
            self.assertTrue(all(sql.lstrip().upper().startswith("SELECT") for sql in statements))
            self.assertIsNone(pending.id)
            self.assertIn(pending, session.new)
            self.assertIn(profile, session.dirty)
            with session.no_autoflush:
                self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 0)


if __name__ == "__main__":
    unittest.main()
