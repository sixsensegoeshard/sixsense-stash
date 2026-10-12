"""Record correction service tests using synthetic config and memory DB only."""

import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import event, func, inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from test_tracking import bot as app, NOW
from tracker_records import (
    load_record, modify_record, parse_placement, record_version, restore_record,
)


class RecordTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as connection:
            await connection.run_sync(app.Base.metadata.create_all)
        self.patches = [
            patch.object(app, "ADMIN_IDS", {900, 901}),
            patch.object(app, "utc_now", return_value=NOW),
        ]
        for item in self.patches:
            item.start()

    async def asyncTearDown(self):
        for item in reversed(self.patches):
            item.stop()
        await self.engine.dispose()

    async def seed(self, session, kind="note", **changes):
        values = dict(user_id=10, username="@artist", actor_id=800,
                      created_at=NOW - timedelta(hours=1, microseconds=123))
        if kind == "note":
            values["text"] = "Исходная заметка"
            model = app.Note
        else:
            values.update(tier="small", artist="Артист", track="Трек",
                          comment=None, proof="https://example.invalid/proof",
                          used_by_award_id=None)
            model = app.Placement
        values.update(changes)
        record = model(**values)
        session.add(record)
        await session.commit()
        return record

    async def changes(self, session):
        return (await session.scalars(select(app.RecordChange).order_by(app.RecordChange.id))).all()

    async def delete_record(self, session, record, kind="note"):
        await modify_record(app, session, 900, record.user_id, kind, record.id,
                            record_version(record), "delete")
        return (await self.changes(session))[-1]

    def test_placement_parser_matches_entry_validation(self):
        for separator in ("|", " — ", " - "):
            with self.subTest(separator=separator):
                self.assertEqual(parse_placement(f"  Артист{separator}Трек  "),
                                 {"artist": "Артист", "track": "Трек", "comment": None})
        self.assertEqual(parse_placement("  Описание  "),
                         {"artist": None, "track": None, "comment": "Описание"})
        self.assertEqual(parse_placement("Артист | Трек | ремикс")["track"], "Трек | ремикс")
        self.assertEqual(len(parse_placement("я" * 500)["comment"]), 500)
        self.assertEqual(len(parse_placement("а" * 200 + " | " + "т" * 200)["artist"]), 200)
        for invalid in (None, "", "   ", "я" * 501, "Артист | ", " | Трек",
                        "а" * 201 + " | Трек", "Артист | " + "т" * 201):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                parse_placement(invalid)

    async def test_load_validates_kind_participant_and_presence_without_commit(self):
        async with self.sessions() as session:
            record = await self.seed(session)
            with patch.object(session, "commit", side_effect=AssertionError("read-only")):
                self.assertIs(await load_record(app, session, 10, "note", record.id), record)
                for uid, kind, record_id, message in (
                    (20, "note", record.id, "другому участнику"),
                    (10, "note", 999, "не найдена"),
                    (10, "role", record.id, "тип записи"),
                ):
                    with self.subTest(kind=kind, uid=uid), self.assertRaisesRegex(ValueError, message):
                        await load_record(app, session, uid, kind, record_id)

    async def test_load_does_not_autoflush_pending_writes(self):
        async with self.sessions() as session:
            record = await self.seed(session)
            pending = app.Note(user_id=20, text="Ещё не подтверждено", created_at=NOW)
            session.add(pending)
            with patch.object(session.sync_session, "flush", side_effect=AssertionError("read-only")):
                self.assertIs(await load_record(app, session, 10, "note", record.id), record)
            self.assertIn(pending, session.new)
            self.assertIsNone(pending.id)

    async def test_version_is_stable_and_covers_all_stored_content(self):
        async with self.sessions() as session, self.sessions() as fresh:
            for kind in ("note", "place"):
                record = await self.seed(session, kind)
                version = record_version(record)
                self.assertRegex(version, r"^[0-9a-f]{12}$")
                other = await load_record(app, fresh, 10, kind, record.id)
                self.assertEqual(record_version(other), version)
                changes = dict(id=record.id + 10, user_id=20, username="Другой",
                               actor_id=901, created_at=NOW)
                if kind == "note":
                    changes["text"] = "Изменено"
                else:
                    changes.update(tier="large", artist="Другой артист", track="Другой трек",
                                   comment="Описание", proof="Новая ссылка", used_by_award_id=7)
                for key, value in changes.items():
                    with self.subTest(kind=kind, field=key):
                        original = getattr(record, key)
                        setattr(record, key, value)
                        self.assertNotEqual(record_version(record), version)
                        setattr(record, key, original)
                self.assertEqual(record_version(record), version)

    async def test_note_edit_preserves_creator_and_date_and_appends_audit(self):
        async with self.sessions() as session:
            record = await self.seed(session)
            old_version = record_version(record)
            created_at = record.created_at
            result = await modify_record(app, session, 901, 10, "note", record.id,
                                         old_version, "edit", text="  Исправлено <&>  ")
            self.assertEqual(result, "Заметка исправлена.")
            self.assertEqual(record.text, "Исправлено <&>")
            self.assertEqual((record.actor_id, record.created_at, record.username),
                             (800, created_at, "@artist"))
            change, = await self.changes(session)
            self.assertEqual((change.user_id, change.record_kind, change.record_id,
                              change.action, change.actor_id, change.created_at),
                             (10, "note", record.id, "edit", 901, NOW))
            self.assertEqual(json.loads(change.before_json)["text"], "Исходная заметка")
            self.assertEqual(json.loads(change.after_json)["text"], record.text)
            self.assertNotEqual(record_version(record), old_version)
            await modify_record(app, session, 900, 10, "note", record.id,
                                record_version(record), "edit", text="Ещё одно исправление")
            self.assertEqual(len(await self.changes(session)), 2)
            await session.refresh(change)
            self.assertEqual(json.loads(change.before_json)["text"], "Исходная заметка")

    async def test_placement_edit_switches_between_structured_and_free_text(self):
        async with self.sessions() as session:
            record = await self.seed(session, "place")
            created_at, proof = record.created_at, record.proof
            result = await modify_record(app, session, 900, 10, "place", record.id,
                                         record_version(record), "edit", text="Описание", tier="large")
            self.assertEqual(result, "Плейсмент исправлен.")
            self.assertEqual((record.artist, record.track, record.comment, record.tier),
                             (None, None, "Описание", "large"))
            await modify_record(app, session, 900, 10, "place", record.id,
                                record_version(record), "edit", text="Новый — Релиз", tier="small")
            self.assertEqual((record.artist, record.track, record.comment, record.tier),
                             ("Новый", "Релиз", None, "small"))
            self.assertEqual((record.created_at, record.proof, record.actor_id), (created_at, proof, 800))
            first, second = await self.changes(session)
            self.assertEqual(json.loads(first.before_json)["artist"], "Артист")
            self.assertEqual(json.loads(second.before_json)["comment"], "Описание")

    async def test_invalid_edits_and_operation_do_not_change_records_or_audit(self):
        async with self.sessions() as session:
            note = await self.seed(session)
            placement = await self.seed(session, "place")
            cases = [(note, "note", "edit", text, None) for text in (None, "", "  ", "x" * 501)]
            cases += [(placement, "place", "edit", text, tier) for text, tier in
                      (("a | ", "small"), ("x" * 501, "large"), ("ok", "big"), ("ok", None))]
            cases += [(note, "note", "restore", None, None)]
            for record, kind, operation, text, tier in cases:
                with self.subTest(kind=kind, text=text, tier=tier), self.assertRaises(ValueError):
                    await modify_record(app, session, 900, 10, kind, record.id,
                                        record_version(record), operation, text=text, tier=tier)
            self.assertEqual(note.text, "Исходная заметка")
            self.assertEqual(placement.artist, "Артист")
            self.assertEqual(await self.changes(session), [])
            await modify_record(app, session, 900, 10, "note", note.id,
                                record_version(note), "edit", text="я" * 500)
            self.assertEqual(len(note.text), 500)

    async def test_modify_requires_admin_even_for_original_creator(self):
        async with self.sessions() as session:
            record = await self.seed(session)
            for operation in ("edit", "delete"):
                with self.subTest(operation=operation), self.assertRaisesRegex(PermissionError, "администратора"):
                    await modify_record(app, session, 800, 10, "note", record.id,
                                        record_version(record), operation, text="Изменено")
            self.assertEqual(await self.changes(session), [])

    async def test_wrong_uid_missing_and_stale_modify_are_rejected(self):
        async with self.sessions() as session:
            record = await self.seed(session)
            record_id, version = record.id, record_version(record)
            for uid, target, expected, message in (
                (20, record_id, version, "другому участнику"),
                (10, 999, version, "не найдена"),
                (10, record_id, "stale", "уже изменена"),
            ):
                with self.subTest(uid=uid, target=target), self.assertRaisesRegex(ValueError, message):
                    await modify_record(app, session, 900, uid, "note", target, expected,
                                        "delete")
            self.assertEqual((await load_record(app, session, 10, "note", record_id)).text,
                             "Исходная заметка")
            self.assertEqual(await self.changes(session), [])

    async def test_preloaded_stale_record_is_refreshed_after_commit(self):
        async with self.sessions() as left, self.sessions() as right:
            record = await self.seed(left)
            record_id, version = record.id, record_version(record)
            other = await load_record(app, right, 10, "note", record_id)
            await modify_record(app, right, 901, 10, "note", record_id,
                                record_version(other), "edit", text="Другой администратор")
            self.assertEqual(record.text, "Исходная заметка")
            with self.assertRaisesRegex(ValueError, "уже изменена"):
                await modify_record(app, left, 900, 10, "note", record_id, version, "delete")
            self.assertEqual((await load_record(app, left, 10, "note", record_id)).text,
                             "Другой администратор")
            self.assertEqual(len(await self.changes(left)), 1)

    async def test_preloaded_deleted_or_reassigned_record_is_not_modified(self):
        for remove in (True, False):
            with self.subTest(remove=remove):
                async with self.sessions() as left, self.sessions() as right:
                    record = await self.seed(left)
                    record_id, version = record.id, record_version(record)
                    other = await load_record(app, right, 10, "note", record_id)
                    if remove:
                        await right.delete(other)
                    else:
                        other.user_id = 20
                    await right.commit()
                    with self.assertRaisesRegex(ValueError, "не найдена|другому участнику"):
                        await modify_record(app, left, 900, 10, "note", record_id, version,
                                            "edit", text="Исправлено")
                    self.assertEqual(await self.changes(left), [])

    async def test_role_protects_edits_and_deletes_by_award_marker_or_cycle(self):
        for tier, protection in (("small", "award"), ("small", "cycle"), ("large", "cycle")):
            with self.subTest(tier=tier, protection=protection):
                async with self.sessions() as session:
                    record = await self.seed(session, "place", tier=tier)
                    if protection == "award":
                        record.used_by_award_id = 7
                    else:
                        session.add(app.PlacementProgress(
                            user_id=10, small_reset_at=NOW if tier == "small" else None,
                            big_reset_at=NOW if tier == "large" else None, updated_at=NOW,
                        ))
                    await session.commit()
                    record_id, version = record.id, record_version(record)
                    for operation in ("edit", "delete"):
                        with self.subTest(operation=operation), self.assertRaisesRegex(ValueError, "подтверждена роль"):
                            await modify_record(app, session, 900, 10, "place", record_id,
                                                version, operation, text="Исправлено", tier=tier)
                    self.assertEqual(await self.changes(session), [])
                    await session.rollback()
                    if protection == "cycle":
                        progress = await session.get(app.PlacementProgress, 10)
                        await session.delete(progress)
                        await session.commit()

    async def test_tier_change_cannot_move_old_record_into_consumed_cycle(self):
        async with self.sessions() as session:
            record = await self.seed(session, "place")
            record_id, version = record.id, record_version(record)
            session.add(app.PlacementProgress(user_id=10, small_reset_at=None,
                                              big_reset_at=NOW, updated_at=NOW))
            await session.commit()
            with self.assertRaisesRegex(ValueError, "Изменить уровень нельзя"):
                await modify_record(app, session, 900, 10, "place", record_id,
                                    version, "edit", text="Новый | Трек", tier="large")
            unchanged = await load_record(app, session, 10, "place", record_id)
            self.assertEqual((unchanged.tier, unchanged.artist), ("small", "Артист"))
            self.assertEqual(await self.changes(session), [])

    async def test_delete_audit_and_restore_preserve_all_original_data_with_new_id(self):
        for kind, model in (("note", app.Note), ("place", app.Placement)):
            with self.subTest(kind=kind):
                async with self.sessions() as session:
                    record = await self.seed(session, kind)
                    old_id, created_at = record.id, record.created_at
                    deletion = await self.delete_record(session, record, kind)
                    deletion_id, before_json = deletion.id, deletion.before_json
                    original = json.loads(before_json)
                    self.assertIsNone(await session.get(model, old_id))
                    self.assertEqual(deletion.action, "delete")
                    self.assertIsNone(deletion.after_json)
                    self.assertIn("восстановлена" if kind == "note" else "восстановлен",
                                  await restore_record(app, session, 901, deletion_id))
                    restored = await session.scalar(select(model).where(model.user_id == 10))
                    self.assertNotEqual(restored.id, old_id)
                    self.assertEqual(restored.created_at, created_at)
                    for key, value in original.items():
                        if key not in {"id", "created_at"}:
                            self.assertEqual(getattr(restored, key), value)
                    audit = (await self.changes(session))[-1]
                    self.assertEqual((audit.action, audit.record_id, audit.actor_id),
                                     ("restore", restored.id, 901))
                    self.assertEqual(audit.before_json, before_json)
                    after = json.loads(audit.after_json)
                    self.assertEqual((after["restored_from"], after["id"]), (deletion_id, restored.id))
                    await session.refresh(deletion)
                    self.assertEqual((deletion.before_json, deletion.after_json), (before_json, None))
                    with self.assertRaisesRegex(ValueError, "уже восстановлено"):
                        await restore_record(app, session, 900, deletion_id)

    async def test_restore_requires_current_admin_and_deleted_change(self):
        async with self.sessions() as session:
            record = await self.seed(session)
            record_id = record.id
            await modify_record(app, session, 900, 10, "note", record.id,
                                record_version(record), "edit", text="Исправлено")
            edit = (await self.changes(session))[0]
            with self.assertRaisesRegex(ValueError, "только удалённую"):
                await restore_record(app, session, 900, edit.id)
            with self.assertRaisesRegex(ValueError, "не найдено"):
                await restore_record(app, session, 900, 999)
            record = await load_record(app, session, 10, "note", record_id)
            deletion = await self.delete_record(session, record)
            deletion_id = deletion.id
            with patch.object(app, "ADMIN_IDS", {901}):
                with self.assertRaisesRegex(PermissionError, "администратора"):
                    await restore_record(app, session, 900, deletion_id)
                await restore_record(app, session, 901, deletion_id)

    async def test_restore_rejects_cycle_consumed_after_deletion_at_original_date(self):
        for tier in ("small", "large"):
            with self.subTest(tier=tier):
                async with self.sessions() as session:
                    record = await self.seed(session, "place", tier=tier)
                    created_at = record.created_at
                    deletion = await self.delete_record(session, record, "place")
                    deletion_id = deletion.id
                    # The equality boundary is consumed too (the helper uses >).
                    session.add(app.PlacementProgress(
                        user_id=10, small_reset_at=created_at if tier == "small" else None,
                        big_reset_at=created_at if tier == "large" else None, updated_at=NOW,
                    ))
                    await session.commit()
                    with self.assertRaisesRegex(ValueError, "подтверждена роль"):
                        await restore_record(app, session, 900, deletion_id)
                    self.assertEqual(await session.scalar(select(func.count(app.Placement.id))), 0)
                    self.assertEqual(len(await self.changes(session)), 1 if tier == "small" else 2)
                    progress = await session.get(app.PlacementProgress, 10)
                    await session.delete(progress)
                    await session.commit()

    async def test_restore_rejects_persisted_consumption_marker(self):
        async with self.sessions() as session:
            record = await self.seed(session, "place", used_by_award_id=7)
            # Simulate an older audited deletion; current deletes forbid this.
            before = {field.key: getattr(record, field.key) for field in inspect(record).mapper.column_attrs}
            before["created_at"] = record.created_at.isoformat(timespec="microseconds")
            session.add(app.RecordChange(user_id=10, record_kind="place", record_id=record.id,
                                         action="delete", actor_id=900, before_json=json.dumps(before),
                                         after_json=None, created_at=NOW))
            await session.delete(record)
            await session.commit()
            deletion = (await self.changes(session))[0]
            with self.assertRaisesRegex(ValueError, "подтверждена роль"):
                await restore_record(app, session, 900, deletion.id)
            self.assertEqual(await session.scalar(select(func.count(app.Placement.id))), 0)

    async def test_concurrent_restores_from_preloaded_sessions_restore_only_once(self):
        async with self.sessions() as seed_session:
            deletion = await self.delete_record(seed_session, await self.seed(seed_session))
            deletion_id, old_id = deletion.id, deletion.record_id
        async with self.sessions() as left, self.sessions() as right:
            await left.get(app.RecordChange, deletion_id)
            await right.get(app.RecordChange, deletion_id)
            await self.changes(left)
            await self.changes(right)
            results = await asyncio.gather(
                restore_record(app, left, 900, deletion_id),
                restore_record(app, right, 901, deletion_id), return_exceptions=True,
            )
            self.assertEqual(sum(isinstance(result, str) for result in results), 1)
            errors = [result for result in results if isinstance(result, Exception)]
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], ValueError)
            self.assertIn("уже восстановлено", str(errors[0]))
        async with self.sessions() as session:
            self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 1)
            restored = await session.scalar(select(app.Note))
            self.assertNotEqual(restored.id, old_id)
            self.assertEqual([change.action for change in await self.changes(session)], ["delete", "restore"])

    async def test_concurrent_edits_apply_one_preview_only_once(self):
        async with self.sessions() as session:
            record = await self.seed(session)
            record_id, version = record.id, record_version(record)
        async with self.sessions() as left, self.sessions() as right:
            results = await asyncio.gather(
                modify_record(app, left, 900, 10, "note", record_id, version, "edit", text="Первое"),
                modify_record(app, right, 901, 10, "note", record_id, version, "edit", text="Второе"),
                return_exceptions=True,
            )
            self.assertEqual(sum(isinstance(result, str) for result in results), 1)
            self.assertEqual(sum(isinstance(result, ValueError) for result in results), 1)
        async with self.sessions() as session:
            self.assertEqual(len(await self.changes(session)), 1)

    async def test_original_deletion_cannot_be_restored_again_after_restored_row_deleted(self):
        async with self.sessions() as session:
            deletion = await self.delete_record(session, await self.seed(session))
            deletion_id = deletion.id
            await restore_record(app, session, 900, deletion_id)
            restored = await session.scalar(select(app.Note))
            second_deletion = await self.delete_record(session, restored)
            second_id = second_deletion.id
            with self.assertRaisesRegex(ValueError, "уже восстановлено"):
                await restore_record(app, session, 900, deletion_id)
            await restore_record(app, session, 900, second_id)
            self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 1)

    async def test_restore_handles_corrupt_snapshot_without_creating_record(self):
        async with self.sessions() as session:
            deletion = await self.delete_record(session, await self.seed(session))
            deletion_id, original = deletion.id, json.loads(deletion.before_json)
            for invalid in ("bad json", "[]", json.dumps({**original, "user_id": 20}),
                            json.dumps({**original, "id": 999}),
                            json.dumps({**original, "created_at": "bad date"}),
                            json.dumps({**original, "unexpected": "not a column"})):
                deletion = await session.get(app.RecordChange, deletion_id)
                deletion.before_json = invalid
                await session.commit()
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "повреждены"):
                    await restore_record(app, session, 900, deletion_id)
                self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 0)
                self.assertEqual(len(await self.changes(session)), 1)

    async def test_audit_failure_rolls_back_edit_delete_and_restore(self):
        def fail_audit(*args):
            raise RuntimeError("synthetic audit failure")

        for operation in ("edit", "delete", "restore"):
            with self.subTest(operation=operation):
                async with self.sessions() as session:
                    record = await self.seed(session)
                    record_id, version = record.id, record_version(record)
                    if operation == "restore":
                        deletion = await self.delete_record(session, record)
                        deletion_id = deletion.id
                    previous_audit = len(await self.changes(session))
                    previous_records = await session.scalar(select(func.count(app.Note.id)))
                    event.listen(app.RecordChange, "before_insert", fail_audit)
                    try:
                        with self.assertRaisesRegex(RuntimeError, "audit failure"):
                            if operation == "restore":
                                await restore_record(app, session, 900, deletion_id)
                            else:
                                await modify_record(app, session, 900, 10, "note", record_id,
                                                    version, operation, text="Исправлено")
                    finally:
                        event.remove(app.RecordChange, "before_insert", fail_audit)
                    await session.rollback()
                    self.assertEqual(len(await self.changes(session)), previous_audit)
                    self.assertEqual(await session.scalar(select(func.count(app.Note.id))), previous_records)
                    current = await session.get(app.Note, record_id)
                    if operation == "restore":
                        self.assertIsNone(current)
                    else:
                        self.assertEqual(current.text, "Исходная заметка")

    async def test_cancelled_restore_rolls_back_flushed_record(self):
        async with self.sessions() as session:
            deletion = await self.delete_record(session, await self.seed(session))
            deletion_id = deletion.id
            actual_flush = session.flush

            async def cancelled_flush(*args, **kwargs):
                await actual_flush(*args, **kwargs)
                raise asyncio.CancelledError()

            with patch.object(session, "flush", side_effect=cancelled_flush):
                with self.assertRaises(asyncio.CancelledError):
                    await restore_record(app, session, 900, deletion_id)
            # A subsequent commit must not publish an unaudited restored row.
            await session.commit()
            self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 0)
            self.assertEqual([change.action for change in await self.changes(session)], ["delete"])
            await restore_record(app, session, 900, deletion_id)
            self.assertEqual(await session.scalar(select(func.count(app.Note.id))), 1)


if __name__ == "__main__":
    unittest.main()
