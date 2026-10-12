"""Confirmed corrections to manual records, with an append-only audit trail.

The caller owns the confirmation/FSM flow and supplies its preview's version.
Pass the application module explicitly; importing this service never loads bot
configuration or opens the application's database.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import func, inspect, select, union_all
from sqlalchemy.exc import InvalidRequestError


def _model(app, kind: str):
    if kind == "place":
        return app.Placement
    if kind == "note":
        return app.Note
    raise ValueError("Неизвестный тип записи. Выбери плейсмент или заметку.")


def _require_admin(app, actor: int) -> None:
    if actor not in app.ADMIN_IDS:
        raise PermissionError("Нужны права администратора.")


def _snapshot(record) -> dict:
    values = {}
    for field in inspect(record).mapper.column_attrs:
        value = getattr(record, field.key)
        values[field.key] = (
            value.isoformat(timespec="microseconds")
            if isinstance(value, datetime) else value
        )
    return values


def _json(values: dict) -> str:
    return json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def record_version(record) -> str:
    """Stable preview token covering content, identity, date and role markers."""
    return hashlib.sha256(_json(_snapshot(record)).encode("utf-8")).hexdigest()[:12]


def _validated_text(text: str | None, label: str) -> str:
    if not isinstance(text, str) or not text.strip() or len(text.strip()) > 500:
        raise ValueError(f"Введи {label} от 1 до 500 символов.")
    return text.strip()


def parse_placement(text: str) -> dict:
    """Shared placement parser; matches the existing entry form's validation."""
    text = _validated_text(text, "описание")
    artist = track = None
    for separator in ("|", " — ", " - "):
        if separator in text:
            artist, track = [part.strip() for part in text.split(separator, 1)]
            if not artist or not track or len(artist) > 200 or len(track) > 200:
                raise ValueError(
                    "Укажи артиста и трек (до 200 символов каждый): Артист | Трек."
                )
            break
    return {"artist": artist, "track": track, "comment": None if artist else text}


async def load_record(app, session, uid: int, kind: str, record_id: int):
    """Read a fresh record for this participant, without writes or a commit."""
    model = _model(app, kind)
    with session.no_autoflush:
        record = await session.scalar(
            select(model).where(model.id == record_id)
            .execution_options(populate_existing=True)
        )
    if record is None:
        raise ValueError("Запись не найдена или уже удалена. Обнови список.")
    if record.user_id != uid:
        raise ValueError("Запись принадлежит другому участнику. Обнови список.")
    return record


async def _refresh(session, record) -> None:
    try:
        await session.refresh(record)
    except InvalidRequestError as exc:
        raise ValueError("Запись уже удалена. Обнови список.") from exc


async def modify_record(
    app, session, actor: int, uid: int, kind: str, record_id: int,
    expected_version: str, operation: str, text: str | None = None,
    tier: str | None = None,
) -> str:
    """Apply a confirmed edit/delete and its audit entry in one transaction.

    Existing creator, username, proof and creation date stay intact on edits.
    The session must not contain unrelated pending writes: like save_entry,
    this service commits any preceding read/profile transaction under the lock.
    """
    _require_admin(app, actor)
    _model(app, kind)
    if operation not in {"edit", "delete", "tier"}:
        raise ValueError("Выбери исправление или удаление записи.")
    values = {}
    if operation == "tier":
        if kind != "place" or tier not in {"small", "large"}:
            raise ValueError("Выбери уровень плейсмента: Small или Big.")
        values = {"tier": tier}
    elif operation == "edit":
        if kind == "note":
            values = {"text": _validated_text(text, "текст заметки")}
        else:
            if tier not in {"small", "large"}:
                raise ValueError("Выбери уровень плейсмента: Small или Big.")
            values = {**parse_placement(text), "tier": tier}

    async with app.user_write_lock(uid):
        await session.commit()
        try:
            record = await load_record(app, session, uid, kind, record_id)
            await _refresh(session, record)
            if record.user_id != uid:
                raise ValueError("Запись принадлежит другому участнику. Обнови список.")
            if record_version(record) != expected_version:
                raise ValueError("Запись уже изменена. Открой её заново и повтори действие.")
            if kind == "place":
                if not await app.placement_can_be_undone(session, record):
                    raise ValueError(
                        "По этому плейсменту уже подтверждена роль. "
                        "Исправление и удаление недоступны."
                    )
                if operation in {"edit", "tier"} and tier != record.tier:
                    # Moving an older entry into a consumed destination cycle
                    # must not silently change that cycle's historical totals.
                    candidate = app.Placement(
                        user_id=uid, tier=tier, created_at=record.created_at,
                        used_by_award_id=record.used_by_award_id,
                    )
                    if not await app.placement_can_be_undone(session, candidate):
                        raise ValueError(
                            "В выбранном уровне по этой дате уже подтверждена роль. "
                            "Изменить уровень нельзя."
                        )
            before_json = _json(_snapshot(record))
            if operation == "delete":
                await session.delete(record)
                after_json = None
            else:
                for key, value in values.items():
                    setattr(record, key, value)
                after_json = _json(_snapshot(record))
            session.add(app.RecordChange(
                user_id=uid, record_kind=kind, record_id=record_id,
                action="edit" if operation == "tier" else operation, actor_id=actor, before_json=before_json,
                after_json=after_json, created_at=app.utc_now(),
            ))
            await session.commit()
        except BaseException:
            # Cancellation must not leave a partial write for a later commit.
            await session.rollback()
            raise
    if operation in {"edit", "tier"}:
        return "Заметка исправлена." if kind == "note" else "Плейсмент исправлен."
    label = "Заметка удалена." if kind == "note" else "Плейсмент удалён."
    return f"{label} Запись сохранена в истории изменений и доступна для восстановления."


def _restore_values(app, change) -> dict:
    """Only a server-stored deletion snapshot may construct a restored row."""
    model = _model(app, change.record_kind)
    fields = {field.key for field in inspect(model).column_attrs}
    try:
        snapshot = json.loads(change.before_json)
        if (
            not isinstance(snapshot, dict) or set(snapshot) != fields
            or type(snapshot["id"]) is not int
            or snapshot["id"] != change.record_id
            or type(snapshot["user_id"]) is not int
            or snapshot["user_id"] != change.user_id
        ):
            raise ValueError("Invalid deletion snapshot")
        values = {key: value for key, value in snapshot.items() if key != "id"}
        values["created_at"] = datetime.fromisoformat(values["created_at"])
        if values["created_at"].tzinfo is not None:
            raise ValueError("Expected UTC-naive creation time")
        if change.record_kind == "place" and values["tier"] not in {"small", "large"}:
            raise ValueError("Invalid placement tier")
    except (TypeError, ValueError, KeyError) as exc:
        raise ValueError(
            "Сохранённые данные удаления повреждены. Восстановление недоступно."
        ) from exc
    return values


async def _already_restored(app, session, uid: int, change_id: int) -> bool:
    # Stream the target's restore entries instead of loading their entire audit.
    result = await session.stream_scalars(
        select(app.RecordChange.after_json).where(
            app.RecordChange.user_id == uid, app.RecordChange.action == "restore",
        ).execution_options(yield_per=100)
    )
    try:
        async for payload in result:
            try:
                values = json.loads(payload)
                if not isinstance(values, dict):
                    raise ValueError("Invalid restore snapshot")
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "История восстановления повреждена. Восстановление недоступно."
                ) from exc
            if values.get("restored_from") == change_id:
                return True
    finally:
        await result.close()
    return False


def _next_record_id(app, model, kind: str):
    # SQLite without AUTOINCREMENT can reuse a deleted row's ID. Allocate above
    # both live and audited IDs inside the INSERT, not via a racing Python read.
    known_ids = union_all(
        select(model.id.label("id")),
        select(app.RecordChange.record_id.label("id")).where(
            app.RecordChange.record_kind == kind,
        ),
    ).subquery()
    return select(func.coalesce(func.max(known_ids.c.id), 0) + 1).scalar_subquery()


async def restore_record(app, session, actor: int, change_id: int) -> str:
    """Restore one deletion once, as a new record, for any current admin."""
    _require_admin(app, actor)
    with session.no_autoflush:
        uid = await session.scalar(
            select(app.RecordChange.user_id).where(app.RecordChange.id == change_id)
        )
    if uid is None:
        raise ValueError("Удаление не найдено. Обнови историю изменений.")
    async with app.user_write_lock(uid):
        await session.commit()
        try:
            change = await session.scalar(
                select(app.RecordChange).where(app.RecordChange.id == change_id)
                .execution_options(populate_existing=True)
            )
            if change is None:
                raise ValueError("Удаление не найдено. Обнови историю изменений.")
            await _refresh(session, change)
            if change.user_id != uid:
                raise ValueError("Участник записи изменился. Обнови историю изменений.")
            if change.action != "delete":
                raise ValueError("Восстановить можно только удалённую запись.")
            if await _already_restored(app, session, uid, change_id):
                raise ValueError("Это удаление уже восстановлено. Обнови список.")
            model = _model(app, change.record_kind)
            record = model(**_restore_values(app, change))
            if (
                change.record_kind == "place"
                and not await app.placement_can_be_undone(session, record)
            ):
                raise ValueError(
                    "По исходной дате плейсмента уже подтверждена роль. "
                    "Восстановление недоступно."
                )
            record.id = _next_record_id(app, model, change.record_kind)
            session.add(record)
            await session.flush()
            session.add(app.RecordChange(
                user_id=uid, record_kind=change.record_kind, record_id=record.id,
                action="restore", actor_id=actor, before_json=change.before_json,
                after_json=_json({**_snapshot(record), "restored_from": change.id}),
                created_at=app.utc_now(),
            ))
            await session.commit()
            label = (
                "Заметка восстановлена." if change.record_kind == "note"
                else "Плейсмент восстановлен."
            )
        except BaseException:
            await session.rollback()
            raise
    return label
