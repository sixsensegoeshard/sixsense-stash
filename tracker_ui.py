"""Telegram screens and guided admin actions. No network or DB work at import time."""

from __future__ import annotations

import math
import secrets
from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select, union

PERIODS = {"7d": "7 дней", "30d": "30 дней", "month": "Месяц", "all": "Всё время"}
METRICS = {
    "total": "Вся активность", "loops": "Лупы", "zips": "Архивы",
    "messages": "Сообщения", "files": "Файлы", "media": "Медиа",
    "days": "Активные дни", "opportunities": "Возможности",
}
CATEGORIES = {"message": "Сообщение", "loop": "Луп", "zip": "Архив", "files": "Файл", "media": "Медиа"}
PAGE_SIZE = 5


class Entry(StatesGroup):
    tier = State()
    search = State()
    details = State()
    confirm = State()


@dataclass
class Screen:
    text: str
    markup: InlineKeyboardMarkup


def short(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def button(owner: int, label: str, action: str, uid: int = 0, period: str = "month", extra: str = "0") -> InlineKeyboardButton:
    data = f"ui:{owner}:{action}:{uid}:{period}:{extra}"
    if len(data.encode()) > 64:
        raise ValueError("Callback exceeds Telegram's 64-byte limit")
    return InlineKeyboardButton(text=label, callback_data=data)


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def period_buttons(owner: int, view: str, uid: int, selected: str, extra: str = "0") -> list[InlineKeyboardButton]:
    return [button(owner, ("✓ " if key == selected else "") + title, view, uid, key, extra) for key, title in PERIODS.items()]


def progress_bar(value: int, target: int) -> str:
    filled = min(8, max(0, value * 8 // max(1, target)))
    return "▰" * filled + "▱" * (8 - filled)


def stats_card(name: str, s: dict) -> str:
    lines = [
        "📊 <b>Статистика</b>", escape(short(name, 80)),
        f"<i>{escape(s['period_label'])}</i>", "",
        f"<b>{s['total']:,}</b> действий · <b>{s['active_days']}</b> активных дней",
    ]
    values = [("Лупы", "loops"), ("Архивы", "zips"), ("Файлы", "files"), ("Медиа", "media"), ("Сообщения", "messages")]
    nonzero = [f"{label}  <b>{s[key]:,}</b>" for label, key in values if s[key]]
    if nonzero:
        lines += [""] + nonzero
    else:
        lines += ["", "Пока нет активности за этот период.", "Сообщения в подключённой группе появятся здесь автоматически."]
    if s["opportunities"]:
        lines += [f"Возможности  <b>{s['opportunities']}</b>"]
    lines += ["", f"🔥 Текущая серия: <b>{s['streak']}</b> дн."]
    return "\n".join(lines)


def roles_card(name: str, p: dict) -> str:
    status = "Можно подтвердить выдачу роли" if p["ready"] else "В процессе"
    need_s = max(0, p["small_target"] - p["small_raw"])
    need_b = max(0, p["large_target"] - p["large_raw"])
    return (
        f"🎖 <b>Прогресс к роли</b>\n{escape(short(name, 80))}\n\n"
        f"Small  <b>{p['small_raw']}/{p['small_target']}</b>\n{progress_bar(p['small_raw'], p['small_target'])}\n\n"
        f"Big  <b>{p['large_raw']}/{p['large_target']}</b>\n{progress_bar(p['large_raw'], p['large_target'])}\n\n"
        f"{'✅' if p['ready'] else '◷'} {status}\n"
        + ("" if p["ready"] else f"До порога: {need_s} Small или {need_b} Big.\n")
        + f"\nВсего плейсментов: <b>{p['total']}</b> · Small {p['small']} · Big {p['large']}\n"
        "<i>После ручной выдачи роли администратор подтверждает её здесь.</i>"
    )


def help_card(admin: bool) -> str:
    text = (
        "💡 <b>Как пользоваться трекером</b>\n\n"
        "<b>Статистика</b> — твоя активность за выбранный период.\n"
        "<b>Профиль</b> — общая сводка и прогресс плейсментов.\n\n"
        "Музыкальные файлы и аудио → лупы.\n"
        "ZIP / RAR / 7Z → архивы. Фото и видео → медиа.\n"
        "Голосовые → сообщения; видеокружки → медиа.\n\n"
        "Бот учитывает новые сообщения в подключённой группе. "
        "Старую переписку Telegram автоматически не передаёт. "
        "Команды и служебные сообщения в статистику не входят.\n\n"
        "/menu — главное меню · /cancel — отменить ввод."
    )
    if admin:
        text += (
            "\n\n<b>Администратору</b>\n"
            "В разделе «Участники» выбери человека или найди его по @username / ID. "
            "В карточке можно добавить плейсмент, заметку или подтвердить роль. "
            "Запись сохраняется только после подтверждения.\n\n"
            "Для автоотчёта ответь /setreport на сообщение в нужном топике. "
            "Отчёт приходит по понедельникам в 12:00 по часовому поясу бота."
        )
    return text


async def page_rows(session, model, where, order, page: int):
    total = int(await session.scalar(select(func.count()).select_from(model).where(where)) or 0)
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    rows = (await session.scalars(select(model).where(where).order_by(*order).offset(page * PAGE_SIZE).limit(PAGE_SIZE))).all()
    return rows, total, page, pages


def pager(owner: int, view: str, uid: int, page: int, pages: int) -> list[InlineKeyboardButton]:
    buttons = []
    if page:
        buttons.append(button(owner, "‹ Назад", view, uid, extra=str(page - 1)))
    if page + 1 < pages:
        buttons.append(button(owner, "Далее ›", view, uid, extra=str(page + 1)))
    return buttons


async def build_screen(app, session, owner: int, view: str = "home", uid: int = 0, period: str = "month", extra: str = "0") -> Screen:
    """Build bounded, escaped screens; enforce permissions independently of callbacks."""
    admin = owner in app.ADMIN_IDS
    uid = uid or owner
    if period not in PERIODS:
        raise ValueError("Unknown period")
    personal = {"stats", "profile", "places", "roles", "history"}
    if view in personal and uid != owner and not admin:
        raise PermissionError("Этот профиль доступен только администратору.")
    if view not in personal | {"home", "help"} and not admin:
        raise PermissionError("Этот раздел доступен только администратору.")
    name = await app.resolve_name(session, uid) or f"ID {uid}"
    rows = []
    if view == "home":
        s = await app.get_user_stats(session, owner, "month")
        text = (
            "<b>301 / TRACKER</b>\n<i>Активность · плейсменты · прогресс</i>\n\n"
            f"Привет, {escape(short(name, 60))}.\n"
            f"В этом месяце: <b>{s['total']:,}</b> действий · <b>{s['active_days']}</b> активных дней.\n\n"
            "Выбери раздел ниже."
        )
        rows = [[button(owner, "📊 Моя статистика", "stats", owner), button(owner, "👤 Мой профиль", "profile", owner)]]
        if admin:
            rows += [[button(owner, "👥 Участники", "members"), button(owner, "🏆 Рейтинг", "top", extra="total")], [button(owner, "⚙️ Управление", "admin"), button(owner, "💡 Как пользоваться", "help")]]
        else:
            rows += [[button(owner, "💡 Как пользоваться", "help")]]
    elif view == "stats":
        s = await app.get_user_stats(session, uid, period)
        text = stats_card(name, s)
        rows = [period_buttons(owner, view, uid, period)]
    elif view == "profile":
        month = await app.get_user_stats(session, uid, "month")
        all_time = await app.get_user_stats(session, uid, "all")
        p = await app.placement_progress(session, uid)
        text = (
            f"👤 <b>{escape(short(name, 80))}</b>\n<i>Карточка участника</i>\n\n"
            f"За месяц  <b>{month['total']:,}</b> действий · {month['active_days']} дн.\n"
            f"За всё время  <b>{all_time['total']:,}</b> действий · {all_time['active_days']} дн.\n"
            f"Текущая серия  <b>{month['streak']}</b> дн.\n\n"
            f"<b>Плейсменты</b>  {p['total']}\nSmall {p['small']} · Big {p['large']}\n"
            f"{'✅ Порог для роли достигнут' if p['ready'] else 'Прогресс к роли'}\n"
            f"Small {p['small_raw']}/{p['small_target']} · Big {p['large_raw']}/{p['large_target']}"
        )
        rows = [[button(owner, "📊 Статистика", "stats", uid), button(owner, "🎖 Прогресс", "roles", uid)], [button(owner, "🎯 Плейсменты", "places", uid), button(owner, "🗓 По месяцам", "history", uid)]]
        if admin:
            rows += [[button(owner, "＋ Плейсмент", "addplace", uid), button(owner, "＋ Заметка", "addnote", uid)], [button(owner, "📝 Заметки", "notes", uid), button(owner, "🕓 Активность", "last", uid)]]
    elif view == "roles":
        p = await app.placement_progress(session, uid)
        text = roles_card(name, p)
        if admin:
            rows = [[button(owner, "Подтвердить Small", "awardsmall", uid), button(owner, "Подтвердить Big", "awardbig", uid)]]
    elif view in {"places", "notes", "last", "history"}:
        page = int(extra)
        model = {"places": app.Placement, "notes": app.Note, "last": app.MsgLog, "history": app.MonthlySnapshot}[view]
        order = [model.period.desc(), model.id.desc()] if view == "history" else [model.created_at.desc(), model.id.desc()]
        items, count, page, pages = await page_rows(session, model, model.user_id == uid, order, page)
        title = {"places": "🎯 Плейсменты", "notes": "📝 Заметки", "last": "🕓 Последняя активность", "history": "🗓 По месяцам"}[view]
        text = f"<b>{title}</b>\n{escape(short(name, 80))}\n<i>{count} записей · страница {page + 1}/{pages}</i>\n"
        if not items:
            text += "\nПока нет записей."
        for item in items:
            if view == "history":
                text += f"\n<b>{escape(item.period)}</b> · {item.total} действий · {item.active_days} дн.\nЛупы {item.loops} · архивы {item.zips} · файлы {item.files}\n"
            elif view == "places":
                body = item.comment or f"{item.artist or '—'} — {item.track or '—'}"
                text += f"\n<b>{app.short_date(item.created_at)} · {'Big' if item.tier == 'large' else 'Small'}</b>\n{escape(short(body, 320))}\n"
            elif view == "notes":
                text += f"\n<b>{app.short_date(item.created_at)}</b>\n{escape(short(item.text, 500))}\n"
            else:
                category = CATEGORIES.get(item.category, "Файл")
                text += f"\n<b>{app.format_local(item.created_at)} · {category}</b>\n{escape(short(item.text_preview, 240))}\n"
        if view == "places" and admin:
            rows += [[button(owner, "＋ Плейсмент", "addplace", uid)]]
        if view == "notes":
            rows += [[button(owner, "＋ Заметка", "addnote", uid)]]
        if nav := pager(owner, view, uid, page, pages):
            rows += [nav]
    elif view == "members":
        # Include legacy users, manual placements and command-only users.
        ids = union(select(app.User.user_id), select(app.MsgLog.user_id), select(app.Placement.user_id), select(app.Note.user_id)).subquery()
        count = int(await session.scalar(select(func.count()).select_from(ids)) or 0)
        pages = max(1, math.ceil(count / 8))
        page = max(0, min(int(extra), pages - 1))
        members = (await session.scalars(select(ids.c.user_id).order_by(ids.c.user_id).offset(page * 8).limit(8))).all()
        text = f"👥 <b>Участники</b>\n<i>{count} в базе · страница {page + 1}/{pages}</i>\n\nВыбери участника или нажми «Найти»."
        if not members:
            text += "\nПока нет участников. Они появятся после первого сообщения в группе."
        rows = [[button(owner, "⌕ Найти участника", "search")]]
        for member_id in members:
            member_name = await app.resolve_name(session, member_id) or f"ID {member_id}"
            rows += [[button(owner, short(member_name, 45), "profile", member_id)]]
        if nav := pager(owner, view, 0, page, pages):
            rows += [nav]
    elif view == "top":
        if extra not in METRICS:
            raise ValueError("Unknown metric")
        text = await app.build_top(session, extra, period)
        rows = [period_buttons(owner, view, 0, period, extra)]
        for keys in [("total", "loops", "zips"), ("messages", "files", "media"), ("days", "opportunities")]:
            rows += [[button(owner, ("✓ " if key == extra else "") + METRICS[key], view, period=period, extra=key) for key in keys]]
    elif view == "admin":
        text = "⚙️ <b>Управление</b>\n\nОтчёты, обслуживание и данные группы.\nПлейсменты и заметки добавляются из карточки участника."
        rows = [[button(owner, "📋 Отчёт за неделю", "report"), button(owner, "🎖 Прогресс ролей", "board")], [button(owner, "🌟 Активные участники", "candidates")], [button(owner, "📤 Экспорт CSV", "export"), button(owner, "💾 Копия базы", "backup")], [button(owner, "🩺 Состояние", "health"), button(owner, "🗓 Архив месяца", "snapshot")], [button(owner, "↩️ Отменить запись", "undo")]]
    elif view == "help":
        text = help_card(admin)
    elif view == "report":
        text = await app.build_weekly_report(session)
    elif view == "board":
        ids = select(app.Placement.user_id).distinct().subquery()
        count = int(await session.scalar(select(func.count()).select_from(ids)) or 0)
        pages = max(1, math.ceil(count / 8))
        page = max(0, min(int(extra), pages - 1))
        member_ids = (await session.scalars(select(ids.c.user_id).order_by(ids.c.user_id).offset(page * 8).limit(8))).all()
        text = f"🎖 <b>Прогресс ролей</b>\n<i>{count} участников · страница {page + 1}/{pages}</i>\n"
        if not member_ids:
            text += "\nПлейсментов пока нет. Добавь первый из карточки участника."
        for member_id in member_ids:
            p = await app.placement_progress(session, member_id)
            member_name = await app.resolve_name(session, member_id) or f"ID {member_id}"
            text += f"\n{'✅' if p['ready'] else '◷'} <b>{escape(short(member_name, 45))}</b>\nSmall {p['small_raw']}/{p['small_target']} · Big {p['large_raw']}/{p['large_target']}\n"
            rows += [[button(owner, short(member_name, 45), "roles", member_id)]]
        if nav := pager(owner, view, 0, page, pages):
            rows += [nav]
    elif view == "candidates":
        text = await app.build_activity_candidates(session)
    elif view == "health":
        text = await app.build_health_text(session)
    else:
        raise ValueError("Unknown screen")
    if view in personal | {"notes", "last"} and view != "profile":
        rows += [[button(owner, "‹ Профиль", "profile", uid)]]
    if view in {"report", "board", "candidates", "health"}:
        rows += [[button(owner, "‹ Управление", "admin")]]
    if view != "home":
        rows += [[button(owner, "⌂ Главное меню", "home")]]
    return Screen(text, keyboard(rows))


async def deliver(message: Message, screen: Screen, bot) -> None:
    """Keep personal/admin navigation in DMs instead of cluttering the group."""
    if message.chat.type == "private":
        await message.answer(screen.text, reply_markup=screen.markup, link_preview_options={"is_disabled": True})
        return
    me = await bot.get_me()
    link = keyboard([[InlineKeyboardButton(text="Открыть трекер", url=f"https://t.me/{me.username}?start=menu")]])
    try:
        await bot.send_message(message.from_user.id, screen.text, reply_markup=screen.markup, link_preview_options={"is_disabled": True})
        await message.reply("Карточка отправлена в личные сообщения.", reply_markup=link)
    except TelegramForbiddenError:
        await message.reply("Открой бота и нажми Start — после этого смогу отправлять карточки в личку.", reply_markup=link)


async def edit_screen(query: CallbackQuery, screen: Screen) -> None:
    try:
        await query.message.edit_text(screen.text, reply_markup=screen.markup, link_preview_options={"is_disabled": True})
    except TelegramBadRequest as exc:
        if "message is not modified" not in exc.message.lower():
            raise


def parse_payload(data: str) -> tuple[int, str, int, str, str]:
    prefix, owner, action, uid, period, extra = data.split(":")
    if prefix != "ui" or period not in PERIODS or not (0 < int(owner) < 2**63) or not (0 <= int(uid) < 2**63):
        raise ValueError("Invalid callback")
    return int(owner), action, int(uid), period, extra


def parse_placement(payload: str) -> dict:
    text = payload.strip()
    if not text or len(text) > 500:
        raise ValueError("Введи описание от 1 до 500 символов.")
    artist = track = None
    for separator in ("|", " — ", " - "):
        if separator in text:
            artist, track = [part.strip() for part in text.split(separator, 1)]
            if not artist or not track or len(artist) > 200 or len(track) > 200:
                raise ValueError("Укажи артиста и трек (до 200 символов каждый): Артист | Трек.")
            break
    return {"artist": artist, "track": track, "comment": None if artist else text}


async def save_entry(app, session, actor: int, data: dict) -> str:
    """Recheck identity, target and business constraints before a confirmed write."""
    if actor not in app.ADMIN_IDS:
        raise PermissionError("Нужны права администратора.")
    if data["kind"] in {"small", "big"}:
        uid = data["uid"]
        name = await app.resolve_name(session, uid) or f"ID {uid}"
        await app.reset_placement_progress(session, uid, data["kind"], actor, name)
        return "Выдача роли подтверждена. Начат новый цикл."
    async with app.user_write_lock(data["uid"] if data["kind"] != "snapshot" else -1):
        await session.commit()
        return await _save_entry(app, session, actor, data)


async def _save_entry(app, session, actor: int, data: dict) -> str:
    if data["kind"] == "snapshot":
        return f"Архив за {await app.make_snapshot(session)} сохранён."
    if data["kind"] == "undo":
        target = await last_undo_target(app, session, actor)
        key = f"{'note' if isinstance(target, app.Note) else 'place'}-{target.id}" if target else None
        if key != data["target"]:
            raise ValueError("Запись уже отменена или появилась новая. Повтори действие.")
        if isinstance(target, app.Placement) and not await app.placement_can_be_undone(session, target):
            raise ValueError("По этому плейсменту уже подтверждена роль. Отмена недоступна.")
        await session.delete(target)
        await session.commit()
        return "Запись отменена."
    uid = data["uid"]
    name = await app.resolve_name(session, uid) or f"ID {uid}"
    if data["kind"] == "note":
        model = app.Note
        conditions = [model.user_id == uid, model.actor_id == actor, model.text == data["text"]]
        values = {"text": data["text"]}
    elif data["kind"] == "place":
        model = app.Placement
        values = parse_placement(data["text"])
        conditions = [model.user_id == uid, model.tier == data["tier"]]
        conditions += [getattr(model, key) == value for key, value in values.items()]
        values["tier"] = data["tier"]
    else:
        raise ValueError("Unknown entry kind")
    from datetime import timedelta
    conditions.append(model.created_at >= app.utc_now() - timedelta(minutes=2))
    if await session.scalar(select(model.id).where(*conditions).limit(1)):
        return "Такая запись уже сохранена. Дубликат не добавлен."
    session.add(model(user_id=uid, username=name, actor_id=actor, created_at=app.utc_now(), **values))
    await session.commit()
    return "Заметка сохранена." if data["kind"] == "note" else "Плейсмент сохранён."


def confirmation_text(app, data: dict) -> str:
    if data["kind"] in {"undo", "snapshot"}:
        return data["text"]
    name = escape(short(data["name"], 80))
    if data["kind"] in {"small", "big"}:
        return f"🎖 <b>Подтвердить выдачу роли?</b>\n\n{name} · {data['kind'].title()}\n\nПрогресс этой шкалы начнётся заново. История плейсментов сохранится."
    label = "Плейсмент · " + ("Big" if data.get("tier") == "large" else "Small") if data["kind"] == "place" else "Заметка"
    return f"<b>{label}</b>\n{name}\n\n{escape(data['text'])}\n\nСохранить запись?"


async def last_undo_target(app, session, actor: int):
    from datetime import timedelta
    cutoff = app.utc_now() - timedelta(minutes=app.UNDO_WINDOW_MINUTES)
    candidates = []
    for model in (app.Note, app.Placement):
        row = await session.scalar(select(model).where(model.actor_id == actor, model.created_at >= cutoff).order_by(model.created_at.desc(), model.id.desc()).limit(1))
        if row:
            candidates.append(row)
    return max(candidates, key=lambda row: (row.created_at, row.id)) if candidates else None


def register_ui(parent: Router, app) -> None:
    ui = Router(name="tracker_ui")
    ui.callback_query.outer_middleware(app.DatabaseMiddleware())

    async def preview(query, state, data):
        nonce = secrets.token_hex(4)
        await state.set_state(Entry.confirm)
        await state.update_data({**data, "nonce": nonce, "preview_id": query.message.message_id, "started": app.utc_now().timestamp()})
        cancel_view = "admin" if data["kind"] in {"undo", "snapshot"} else "profile"
        await state.update_data(cancel_view=cancel_view)
        await edit_screen(query, Screen(confirmation_text(app, data), keyboard([[button(query.from_user.id, "✓ Подтвердить", "save", extra=nonce)], [button(query.from_user.id, "Отмена", "cancel", data["uid"], extra=nonce)]])))

    @ui.callback_query(F.data.startswith("ui:"))
    async def navigate(query: CallbackQuery, session, state: FSMContext, bot):
        try:
            owner, action, uid, period, extra = parse_payload(query.data)
        except (ValueError, TypeError):
            await query.answer("Эта кнопка устарела. Открой /menu.", show_alert=True)
            return
        if owner != query.from_user.id:
            await query.answer("Открой своё меню командой /menu.", show_alert=True)
            return
        if not isinstance(query.message, Message) or query.message.chat.type != "private":
            await query.answer("Открой /menu в личном чате с ботом.", show_alert=True)
            return
        personal = {"home", "help", "stats", "profile", "places", "roles", "history"}
        if owner not in app.ADMIN_IDS and (action not in personal or (uid and uid != owner)):
            await query.answer("Этот раздел доступен только администратору.", show_alert=True)
            return
        await query.answer()
        if action == "cancel":
            data = await state.get_data()
            if data.get("nonce") != extra:
                await edit_screen(query, Screen("Этот ввод уже завершён. Текущая форма не отменена.", keyboard([[button(owner, "⌂ Главное меню", "home")]])))
                return
            await state.clear()
            await edit_screen(query, await build_screen(app, session, owner, data.get("cancel_view", "profile"), data.get("uid", owner)))
            return
        if action in {"small", "large"}:
            data = await state.get_data()
            if await state.get_state() != Entry.tier.state or data.get("nonce") != extra or data.get("uid") != uid:
                await edit_screen(query, Screen("Этот выбор уже устарел. Открой карточку участника и повтори.", keyboard([[button(owner, "👤 Профиль", "profile", uid)]])))
                return
        if action == "save":
            data = await state.get_data()
            if await state.get_state() != Entry.confirm.state or data.get("nonce") != extra or data.get("preview_id") != query.message.message_id or app.utc_now().timestamp() - data.get("started", 0) > 900:
                await edit_screen(query, Screen("Этот ввод уже завершён или устарел. Открой карточку участника и повтори.", keyboard([[button(owner, "⌂ Главное меню", "home")]])))
                return
            try:
                result = await save_entry(app, session, owner, data)
            except (ValueError, PermissionError) as exc:
                result = str(exc)
            await state.clear()
            screen = await build_screen(app, session, owner, "admin" if data["kind"] in {"undo", "snapshot"} else "profile", data["uid"])
            screen.text = f"{escape(result)}\n\n" + screen.text
            await edit_screen(query, screen)
            return
        await state.clear()
        if action == "search":
            nonce = secrets.token_hex(4)
            await state.set_state(Entry.search)
            await state.update_data(started=app.utc_now().timestamp(), nonce=nonce, cancel_view="members", uid=0)
            await edit_screen(query, Screen("⌕ <b>Найти участника</b>\n\nОтправь @username или числовой Telegram ID. Можно переслать сообщение участника, если Telegram показывает автора.", keyboard([[button(owner, "Отмена", "cancel", extra=nonce)]])))
            return
        if action in {"addplace", "addnote", "small", "large", "awardsmall", "awardbig"}:
            if uid <= 0:
                await edit_screen(query, await build_screen(app, session, owner, "members"))
                return
            name = await app.resolve_name(session, uid) or f"ID {uid}"
            if action == "addplace":
                nonce = secrets.token_hex(4)
                await state.set_state(Entry.tier)
                await state.update_data(nonce=nonce, uid=uid, cancel_view="profile")
                await edit_screen(query, Screen(f"🎯 <b>Добавить плейсмент</b>\n{escape(short(name, 80))}\n\nВыбери тип.", keyboard([[button(owner, "Small", "small", uid, extra=nonce), button(owner, "Big", "large", uid, extra=nonce)], [button(owner, "Отмена", "cancel", uid, extra=nonce)]])))
                return
            if action.startswith("award"):
                kind = action.removeprefix("award")
                p = await app.placement_progress(session, uid)
                if not p["ready_small" if kind == "small" else "ready_big"]:
                    screen = await build_screen(app, session, owner, "roles", uid)
                    screen.text += "\n\nПорог этой роли ещё не достигнут."
                    await edit_screen(query, screen)
                    return
                await preview(query, state, {"kind": kind, "uid": uid, "name": name})
                return
            kind = "note" if action == "addnote" else "place"
            nonce = secrets.token_hex(4)
            await state.set_state(Entry.details)
            await state.update_data(kind=kind, tier=action, uid=uid, name=name, started=app.utc_now().timestamp(), nonce=nonce, cancel_view="profile")
            prompt = "Отправь текст заметки (до 500 символов)." if kind == "note" else "Отправь артиста и трек:\n<code>Артист | Название трека</code>\n\nИли краткое описание (до 500 символов)."
            await edit_screen(query, Screen(f"<b>{'📝 Новая заметка' if kind == 'note' else '🎯 Новый плейсмент'}</b>\n{escape(short(name, 80))}\n\n{prompt}", keyboard([[button(owner, "Отмена", "cancel", uid, extra=nonce)]])))
            return
        if action == "export":
            await query.message.answer_document(await app.build_export(session), caption="📤 Статистика группы · CSV")
            await edit_screen(query, await build_screen(app, session, owner, "admin"))
            return
        if action == "backup":
            path = await app.backup_database(session)
            screen = await build_screen(app, session, owner, "admin")
            screen.text = (f"💾 Копия создана: <code>{escape(path.name)}</code>" if path else "Копия базы доступна для SQLite.") + "\n\n" + screen.text
            await edit_screen(query, screen)
            return
        if action in {"snapshot", "undo"}:
            text = "🗓 <b>Пересчитать архив прошлого месяца?</b>\n\nТекущие сообщения сохранятся."
            data = {"kind": action, "uid": owner, "name": ""}
            if action == "undo":
                target = await last_undo_target(app, session, owner)
                if not target:
                    await edit_screen(query, Screen("Нет записей для отмены за последние 15 минут.", keyboard([[button(owner, "‹ Управление", "admin")]])))
                    return
                kind = "note" if isinstance(target, app.Note) else "place"
                data["uid"] = target.user_id
                text = f"↩️ <b>Отменить последнюю запись?</b>\n\n{escape(short(target.username or target.user_id))}\n{escape(short(target.text if kind == 'note' else target.comment or target.artist or 'Плейсмент', 240))}"
                data["target"] = f"{kind}-{target.id}"
            data["text"] = text
            await preview(query, state, data)
            return
        try:
            screen = await build_screen(app, session, owner, action, uid, period, extra)
        except (ValueError, PermissionError):
            screen = await build_screen(app, session, owner, "home")
        await edit_screen(query, screen)

    @ui.message(Command("cancel"))
    async def cancel(message: Message, session, state: FSMContext, bot):
        await state.clear()
        await deliver(message, await build_screen(app, session, message.from_user.id), bot)

    @ui.message(Entry.search, F.chat.type == "private", ~F.text.startswith("/"))
    async def search(message: Message, session, state: FSMContext, bot):
        if not app.is_admin(message):
            await state.clear()
            return
        data = await state.get_data()
        if app.utc_now().timestamp() - data.get("started", 0) > 900:
            await state.clear()
            await message.answer("Время поиска истекло. Открой /menu.")
            return
        origin = message.forward_origin
        raw = str(origin.sender_user.id) if origin and origin.type == "user" else (message.text or "").strip()
        if not raw or len(raw) > 80:
            await message.answer("Отправь @username или ID. Скрытого автора пересылки Telegram не раскрывает.")
            return
        uid, name, error = await app.find_user(session, message, raw)
        if error:
            await message.answer(error)
            return
        await state.clear()
        await deliver(message, await build_screen(app, session, message.from_user.id, "profile", uid), bot)

    @ui.message(Entry.details, F.chat.type == "private", ~F.text.startswith("/"))
    async def details(message: Message, state: FSMContext):
        if not app.is_admin(message):
            await state.clear()
            return
        data = await state.get_data()
        if app.utc_now().timestamp() - data.get("started", 0) > 900:
            await state.clear()
            await message.answer("Время ввода истекло. Открой карточку участника и повтори.")
            return
        value = (message.text or "").strip()
        try:
            if not value or len(value) > 500:
                raise ValueError("Отправь текст от 1 до 500 символов.")
            if data["kind"] == "place":
                parse_placement(value)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        data["text"] = value
        nonce = secrets.token_hex(4)
        sent = await message.answer(confirmation_text(app, data), reply_markup=keyboard([[button(message.from_user.id, "✓ Сохранить", "save", extra=nonce)], [button(message.from_user.id, "Отмена", "cancel", data["uid"], extra=nonce)]]))
        await state.set_state(Entry.confirm)
        await state.update_data({**data, "nonce": nonce, "preview_id": sent.message_id})

    @ui.message(Entry.confirm, F.chat.type == "private", ~F.text.startswith("/"))
    async def waiting_confirmation(message: Message):
        await message.answer("Проверь запись выше и нажми «Сохранить» либо «Отмена». Отменить ввод можно также командой /cancel.")

    @ui.message(F.chat.type == "private")
    async def fallback(message: Message, session, state: FSMContext, bot):
        await state.clear()
        screen = await build_screen(app, session, message.from_user.id)
        screen.text = "Управление — через кнопки ниже.\n\n" + screen.text
        await deliver(message, screen, bot)

    parent.include_router(ui)
