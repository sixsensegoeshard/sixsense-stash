"""Monthly reports using test_tracking's synthetic app and fresh memory DBs."""

import codecs
import csv
from datetime import datetime, timedelta, timezone
import io
import json
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from aiogram.types import BufferedInputFile
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_tracking import bot as app, CHAT_ID, NOW
from tracker_monthly import (activity_csv, ensure_month_archives, placement_month_rows,
                             placement_roles_csv, placements_csv)


HAS_EVIDENCE = hasattr(app.RoleAward, "evidence_json")
HAS_COLLABORATORS = hasattr(app.Placement, "collaborators")
HAS_ACTIVITY_ROLES = hasattr(app, "ActivityRoleAward")


class MonthlyReportsTests(unittest.IsolatedAsyncioTestCase):
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

    @staticmethod
    def rows(document):
        return list(csv.DictReader(io.StringIO(document.data.decode("utf-8-sig"))))

    @staticmethod
    def log(session, uid=10, stamp=None, category="message", **values):
        values.setdefault("chat_id", CHAT_ID)
        values.setdefault("username", "artist")
        session.add(app.MsgLog(user_id=uid, created_at=stamp or NOW - timedelta(hours=1),
                               category=category, **values))

    @staticmethod
    def placement(session, uid=10, stamp=None, **values):
        values.setdefault("username", "winner")
        values.setdefault("tier", "small")
        values.setdefault("artist", "@primary")
        values.setdefault("track", "Трек 🎵")
        item = app.Placement(user_id=uid, created_at=stamp or NOW - timedelta(hours=1), **values)
        session.add(item)
        return item

    @staticmethod
    def award(session, uid=10, stamp=None, **values):
        values.setdefault("username", "@winner")
        values.setdefault("kind", "small")
        item = app.RoleAward(user_id=uid, created_at=stamp or NOW - timedelta(minutes=1), **values)
        session.add(item)
        return item

    @staticmethod
    def archive(session, uid=10, period="2026-09", **values):
        session.add(app.MonthlySnapshot(user_id=uid, period=period, **values))

    async def test_empty_csvs_have_bom_and_personal_current_zero_row(self):
        async with self.sessions() as session:
            for export in (activity_csv, placements_csv, placement_roles_csv):
                document = await export(app, session)
                self.assertIsInstance(document, BufferedInputFile)
                self.assertTrue(document.data.startswith(codecs.BOM_UTF8))
                self.assertEqual(self.rows(document), [])
            own = self.rows(await activity_csv(app, session, 10))
            self.assertEqual((own[0]["period"], own[0]["total"]), ("2026-10", "0"))
            self.assertEqual(await placement_month_rows(app, session), [])

    async def test_activity_preserves_archive_and_never_adds_partial_retained_logs(self):
        async with self.sessions() as session:
            self.log(session, stamp=datetime(2026, 9, 15), category="files")
            self.archive(session, messages=99, total=99, active_days=30)
            self.archive(session, period="2026-08", username="archived", loops=4, total=4, active_days=2)
            self.archive(session, uid=20, period="2025-12", media=7, total=7)
            await session.commit()
            own = self.rows(await activity_csv(app, session, 10))
            september = next(row for row in own if row["period"] == "2026-09")
            self.assertEqual((september["total"], september["messages"], september["files"]), ("99", "99", "0"))
            group = self.rows(await activity_csv(app, session))
            self.assertTrue(any(row["user_id"] == "20" and row["total"] == "7" for row in group))
            self.assertEqual({row["user_id"] for row in own}, {"10"})

    async def test_complete_raw_logs_extend_archive_without_doublecount(self):
        async with self.sessions() as session:
            self.archive(session, files=1, total=1, active_days=1)
            for day in (10, 11):
                self.log(session, stamp=datetime(2026, 9, day), category="files")
            await session.commit()
            row = next(row for row in self.rows(await activity_csv(app, session, 10)) if row["period"] == "2026-09")
            self.assertEqual((row["files"], row["total"], row["active_days"]), ("2", "2", "2"))

    async def test_activity_uses_local_boundary_excludes_future_and_foreign_chat(self):
        async with self.sessions() as session:
            self.log(session, stamp=datetime(2026, 9, 30, 20, 59, 59))
            self.log(session, stamp=datetime(2026, 9, 30, 21), category="loop", opportunity=True)
            self.log(session, stamp=NOW)
            self.log(session, stamp=NOW + timedelta(seconds=1))
            self.log(session, uid=20, chat_id=-100999)
            self.log(session, uid=30, chat_id=30)
            await session.commit()
            rows = self.rows(await activity_csv(app, session))
            self.assertEqual({row["user_id"] for row in rows}, {"10"})
            self.assertEqual([(row["period"], row["total"]) for row in rows], [("2026-10", "1"), ("2026-09", "1")])
            self.assertEqual((rows[0]["loops"], rows[0]["opportunities"]), ("1", "1"))

    async def test_activity_categories_and_overlapping_opportunities(self):
        async with self.sessions() as session:
            for category in ("message", "loop", "zip", "files", "media", "legacy"):
                self.log(session, category=category, opportunity=True)
            await session.commit()
            row = self.rows(await activity_csv(app, session, 10))[0]
            self.assertEqual([row[key] for key in ("messages", "loops", "zips", "files", "media", "total", "opportunities")],
                             ["1", "1", "1", "2", "1", "6", "6"])

    async def test_names_include_note_only_placement_only_and_snapshot_only_members(self):
        async with self.sessions() as session:
            session.add(app.Note(user_id=777, username="=formula", text="Synthetic", created_at=NOW - timedelta(days=1)))
            self.placement(session, uid=20, username="@placed")
            self.archive(session, uid=30, username="archived", messages=1, total=1)
            await session.commit()
            rows = self.rows(await activity_csv(app, session))
            names = {row["user_id"]: row["name"] for row in rows}
            self.assertEqual(names, {"777": "'=formula", "20": "'@placed", "30": "'@archived"})

    async def test_profile_removed_username_is_not_resurrected(self):
        async with self.sessions() as session:
            session.add(app.User(user_id=10, username=None, first_name="Имя 🎹", last_name="Фамилия", first_seen=NOW, last_seen=NOW))
            self.log(session, username="old")
            self.placement(session, username="old")
            await session.commit()
            self.assertEqual(self.rows(await activity_csv(app, session, 10))[0]["name"], "Имя 🎹 Фамилия")
            self.assertEqual((await placement_month_rows(app, session))[0]["name"], "Имя 🎹 Фамилия")

    async def test_self_exports_scope_every_source_to_recipient(self):
        async with self.sessions() as session:
            for uid in (10, 20):
                self.log(session, uid=uid)
                self.placement(session, uid=uid, track=f"private-{uid}")
                self.award(session, uid=uid)
                self.archive(session, uid=uid, total=uid)
            await session.commit()
            for export in (activity_csv, placements_csv, placement_roles_csv):
                rows = self.rows(await export(app, session, 10))
                self.assertTrue(rows)
                self.assertEqual({row["user_id"] for row in rows}, {"10"})
                self.assertNotIn("private-20", str(rows))
                self.assertEqual({row["user_id"] for row in self.rows(await export(app, session))}, {"10", "20"})

    async def test_formula_guard_applies_to_every_placement_text_and_preserves_unicode(self):
        async with self.sessions() as session:
            values = dict(username="@winner", artist="@primary", track=' =SUM(1,2)\nТрек 🎵',
                          comment="\t+formula", proof="-formula", tier="=tier")
            if HAS_COLLABORATORS:
                values["collaborators"] = "@one @два"
            self.placement(session, **values)
            await session.commit()
            document = await placements_csv(app, session, 10)
            self.assertTrue(document.data.startswith(codecs.BOM_UTF8))
            row = self.rows(document)[0]
            for field in ("winner", "artist", "track", "comment", "proof", "tier"):
                original = values["username" if field == "winner" else field]
                self.assertEqual(row[field], app.safe_csv_cell(original))
            if HAS_COLLABORATORS:
                self.assertEqual(row["collaborators"], "'@one @два")

    async def test_placements_export_all_months_and_exclude_now_future(self):
        async with self.sessions() as session:
            self.placement(session, stamp=datetime(2026, 9, 30, 20, 59, 59))
            self.placement(session, stamp=datetime(2026, 9, 30, 21))
            self.placement(session, stamp=NOW)
            self.placement(session, stamp=NOW + timedelta(days=1))
            await session.commit()
            rows = self.rows(await placements_csv(app, session))
            self.assertEqual([row["period"] for row in rows], ["2026-10", "2026-09"])
            self.assertTrue(rows[0]["created_at"].endswith("+03:00"))

    async def test_legacy_awards_never_invent_evidence_from_live_placements(self):
        async with self.sessions() as session:
            self.placement(session, artist="DO NOT INFER")
            self.award(session, stamp=datetime(2026, 9, 30, 21))
            self.award(session, stamp=NOW)
            await session.commit()
            rows = self.rows(await placement_roles_csv(app, session, 10))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["period"], "2026-10")
            self.assertEqual(rows[0]["evidence_status"], "legacy_not_recorded")
            for field in ("placement_id", "winner", "primary_artists", "collaborators", "track", "tier", "placement_date"):
                self.assertEqual(rows[0][field], "")

    @unittest.skipUnless(HAS_EVIDENCE, "main has not added evidence_json yet")
    async def test_award_evidence_uses_frozen_credits_after_live_edit_and_delete(self):
        evidence = [{"id": 42, "artist": "@primary", "track": "Песня 🎹", "winner": "@winner",
                     "collaborators": "@one @two", "created_at": "2026-08-01T00:00:00"},
                    {"id": 43, "artist": "@second", "track": "=danger", "winner": "@winner",
                     "collaborators": "@three", "created_at": "2026-09-01T00:00:00"}]
        async with self.sessions() as session:
            item = self.placement(session, artist="@edited", track="Changed")
            self.award(session, evidence_json=json.dumps(evidence, ensure_ascii=False))
            await session.commit()
            await session.delete(item)
            await session.commit()
            rows = self.rows(await placement_roles_csv(app, session, 10))
            self.assertEqual(len(rows), 2)
            self.assertEqual(len({row["award_id"] for row in rows}), 1)
            self.assertEqual([row["primary_artists"] for row in rows], ["'@primary", "'@second"])
            self.assertEqual([row["collaborators"] for row in rows], ["'@one @two", "'@three"])
            self.assertEqual(rows[0]["winner"], "'@winner")
            self.assertEqual(rows[0]["track"], "Песня 🎹")
            self.assertEqual(rows[1]["track"], "'=danger")

    @unittest.skipUnless(HAS_EVIDENCE, "main has not added evidence_json yet")
    async def test_invalid_or_empty_evidence_is_explicit_without_crashing(self):
        async with self.sessions() as session:
            for payload in ("bad", "null", "{}", "[7]", "[]", "[{\"winner\":42,\"artist\":[]}]", None):
                self.award(session, evidence_json=payload)
            await session.commit()
            rows = self.rows(await placement_roles_csv(app, session))
            self.assertEqual(len(rows), 7)
            self.assertEqual([row["evidence_status"] for row in rows].count("invalid_evidence"), 4)
            self.assertEqual([row["evidence_status"] for row in rows].count("legacy_not_recorded"), 1)
            self.assertEqual([row["evidence_status"] for row in rows].count("recorded_empty"), 1)

    @unittest.skipUnless(HAS_EVIDENCE, "main has not added evidence_json yet")
    async def test_award_leaderboard_counts_awards_not_evidence_and_breaks_ties_by_uid(self):
        evidence = json.dumps([{"artist": "@first"}, {"artist": "@second"}])
        async with self.sessions() as session:
            self.award(session, uid=20, stamp=datetime(2026, 8, 1), evidence_json=evidence)
            self.award(session, uid=20, stamp=datetime(2026, 9, 1), evidence_json=evidence)
            self.award(session, uid=10, evidence_json=evidence)
            self.award(session, uid=30)
            self.award(session, uid=30, stamp=NOW)
            await session.commit()
            rows = self.rows(await placement_roles_csv(app, session))
            self.assertEqual([row["user_id"] for row in rows], ["20"] * 4 + ["10"] * 2 + ["30"])
            self.assertEqual({(row["user_id"], row["recipient_role_count"], row["recipient_rank"]) for row in rows},
                             {("20", "2", "1"), ("10", "1", "2"), ("30", "1", "3")})
            self.assertEqual([row["period"] for row in rows[:4]], ["2026-09", "2026-09", "2026-08", "2026-08"])
            own = self.rows(await placement_roles_csv(app, session, 20))
            self.assertEqual({(row["recipient_role_count"], row["recipient_rank"]) for row in own}, {("2", "1")})

    @unittest.skipUnless(HAS_ACTIVITY_ROLES, "main has not added ActivityRoleAward yet")
    async def test_legacy_activity_role_missing_month_and_stats_remains_exportable(self):
        model = app.ActivityRoleAward
        missing_period = None if model.__table__.c.period.nullable else ""
        missing_stats = None if model.__table__.c.stats_json.nullable else "null"
        async with self.sessions() as session:
            session.add(model(user_id=10, role="Legacy", period=missing_period, stats_json=missing_stats,
                              actor_id=900, created_at=datetime(2026, 9, 30, 21)))
            await session.commit()
            row = next(row for row in self.rows(await activity_csv(app, session, 10))
                       if row["row_type"] == "activity_role")
            self.assertEqual((row["period"], row["role_period"], row["role_stats_json"]),
                             ("2026-10", "", missing_stats or ""))

    async def test_placement_role_events_have_no_activity_counters(self):
        async with self.sessions() as session:
            self.log(session)
            self.award(session)
            self.award(session, uid=20)
            await session.commit()
            rows = self.rows(await activity_csv(app, session, 10))
            event = next(row for row in rows if row["row_type"] == "placement_role")
            self.assertEqual((event["role_kind"], event["role"]), ("placement", "small"))
            self.assertEqual(event["total"], "")
            self.assertEqual(sum(int(row["total"] or 0) for row in rows), 1)

    @unittest.skipUnless(HAS_ACTIVITY_ROLES, "main has not added ActivityRoleAward yet")
    async def test_activity_roles_keep_manual_month_nullable_stats_and_self_privacy(self):
        async with self.sessions() as session:
            for uid, period, stats in ((10, "2026-08", "null"), (10, "2026-09", '{"total":7}'), (20, "2026-09", "null")):
                session.add(app.ActivityRoleAward(user_id=uid, role="Роль 🎖", period=period,
                    stats_json=stats, actor_id=900, created_at=NOW - timedelta(minutes=1)))
            session.add(app.ActivityRoleAward(user_id=10, role="future", period="2026-10", stats_json="null", actor_id=900, created_at=NOW))
            await session.commit()
            rows = self.rows(await activity_csv(app, session, 10))
            events = [row for row in rows if row["row_type"] == "activity_role"]
            self.assertEqual({row["user_id"] for row in rows}, {"10"})
            self.assertEqual([row["period"] for row in events], ["2026-09", "2026-08"])
            self.assertEqual([row["role_period"] for row in events], ["2026-09", "2026-08"])
            self.assertEqual([row["role_stats_json"] for row in events], ['{"total":7}', "null"])
            self.assertTrue(all(row["total"] == "" for row in events))

    async def test_missing_optional_activity_model_is_supported(self):
        class LegacyApp:
            def __getattr__(self, name):
                if name == "ActivityRoleAward":
                    raise AttributeError(name)
                return getattr(app, name)
        async with self.sessions() as session:
            self.log(session)
            await session.commit()
            rows = self.rows(await activity_csv(LegacyApp(), session, 10))
            self.assertEqual(rows[0]["total"], "1")

    async def test_leaderboard_counts_winners_only_with_stable_ties_and_complete_items(self):
        async with self.sessions() as session:
            for uid, count in ((10, 3), (20, 2), (30, 2)):
                for index in range(count):
                    values = {"username": {10: "zeta", 20: "beta", 30: "alpha"}[uid]}
                    if HAS_COLLABORATORS:
                        values["collaborators"] = "@beta @alpha @outsider"
                    self.placement(session, uid=uid, **values)
            self.placement(session, uid=40, stamp=datetime(2026, 9, 30, 20, 59, 59))
            self.placement(session, uid=50, stamp=NOW)
            self.placement(session, uid=60, stamp=NOW + timedelta(hours=1))
            await session.commit()
            rows = await placement_month_rows(app, session)
            self.assertEqual([(row["uid"], row["count"]) for row in rows], [(10, 3), (30, 2), (20, 2)])
            self.assertTrue(all(len(row["items"]) == row["count"] for row in rows))
            self.assertTrue(all(isinstance(item, app.Placement) for row in rows for item in row["items"]))
            self.assertTrue(all(set(row) == {"uid", "name", "count", "items"} for row in rows))

    async def test_negative_timezone_month_boundary_and_aware_clock(self):
        with patch.object(app, "BOT_TIMEZONE", "America/Los_Angeles"), patch.object(app, "utc_now", return_value=NOW.replace(tzinfo=timezone.utc)):
            async with self.sessions() as session:
                self.placement(session, uid=10, stamp=datetime(2026, 10, 1, 6, 59, 59))
                self.placement(session, uid=20, stamp=datetime(2026, 10, 1, 7))
                await session.commit()
                self.assertEqual([row["uid"] for row in await placement_month_rows(app, session)], [20])
                self.assertEqual([row["period"] for row in self.rows(await placements_csv(app, session))], ["2026-10", "2026-09"])

    async def test_read_helpers_never_flush_or_commit_pending_writes(self):
        async with self.sessions() as session:
            self.log(session)
            await session.commit()
            self.placement(session, track="Pending")
            with patch.object(session, "flush", side_effect=AssertionError("read flushed")), patch.object(session, "commit", side_effect=AssertionError("read committed")):
                for export in (activity_csv, placements_csv, placement_roles_csv):
                    await export(app, session, 10)
                self.assertEqual(await placement_month_rows(app, session), [])
            self.assertEqual(len(session.new), 1)

    async def test_invalid_uid_never_becomes_group_export(self):
        async with self.sessions() as session:
            for export in (activity_csv, placements_csv, placement_roles_csv):
                for uid in (0, -1, True, "10", 2**63):
                    with self.assertRaises(ValueError):
                        await export(app, session, uid)

    async def test_offline_january_to_march_backfills_all_months_before_retention(self):
        with patch.object(app, "utc_now", return_value=datetime(2026, 3, 10, 12)):
            async with self.sessions() as session:
                self.log(session, stamp=datetime(2026, 1, 15), category="loop")
                self.log(session, stamp=datetime(2026, 1, 31, 21), category="zip")
                self.log(session, stamp=datetime(2026, 2, 15), category="files")
                self.log(session, stamp=datetime(2026, 3, 1), category="media")
                self.log(session, uid=20, stamp=datetime(2026, 1, 15), chat_id=-100999)
                self.log(session, uid=30, stamp=datetime(2026, 1, 15), chat_id=30)
                await session.commit()
                self.assertEqual(await ensure_month_archives(app, session), 2)
                self.assertEqual(await ensure_month_archives(app, session), 0)
                await session.commit()
                archives = (await session.scalars(select(app.MonthlySnapshot).order_by(app.MonthlySnapshot.period))).all()
                self.assertEqual([(row.user_id, row.period, row.total) for row in archives], [(10, "2026-01", 1), (10, "2026-02", 2)])
                await session.execute(delete(app.MsgLog))
                await session.commit()
                rows = self.rows(await activity_csv(app, session, 10))
                self.assertEqual([(row["period"], row["total"]) for row in rows], [("2026-03", "0"), ("2026-02", "2"), ("2026-01", "1")])

    async def test_backfill_preserves_partial_archives_and_extends_complete_ones(self):
        async with self.sessions() as session:
            self.archive(session, messages=99, total=99, active_days=30)
            self.log(session, stamp=datetime(2026, 9, 15))
            self.archive(session, period="2026-08", loops=1, total=1, active_days=1)
            self.log(session, stamp=datetime(2026, 8, 10), category="loop")
            self.log(session, stamp=datetime(2026, 8, 11), category="loop")
            await session.commit()
            with patch.object(session, "commit", side_effect=AssertionError("helper committed")):
                self.assertEqual(await ensure_month_archives(app, session), 1)
            await session.commit()
            rows = (await session.scalars(select(app.MonthlySnapshot).order_by(app.MonthlySnapshot.period))).all()
            self.assertEqual([(row.period, row.total) for row in rows], [("2026-08", 2), ("2026-09", 99)])
            self.assertEqual(await ensure_month_archives(app, session), 0)
            self.assertEqual(await session.scalar(select(func.count(app.MonthlySnapshot.id))), 2)

    async def test_backfill_does_not_commit_and_caller_can_rollback(self):
        async with self.sessions() as session:
            self.log(session, stamp=datetime(2026, 9, 15))
            await session.commit()
            self.assertEqual(await ensure_month_archives(app, session), 1)
            await session.flush()
            await session.rollback()
            self.assertEqual(await session.scalar(select(func.count(app.MonthlySnapshot.id))), 0)


if __name__ == "__main__":
    unittest.main()
