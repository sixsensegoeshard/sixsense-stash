"""Telegram screens and guided admin actions. No network or DB work at import time."""

from __future__ import annotations

import math
import json
import secrets
from dataclasses import dataclass, field
from html import escape
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import func, select

from tracker_insights import activity_comparison, member_directory, monthly_history
from tracker_records import load_record, modify_record, parse_placement, record_version, restore_record

PERIODS = {"7d": "7 дней", "30d": "30 дней", "month": "Месяц", "all": "Всё время"}
METRICS = {
    "total": "Вся активность", "loops": "Лупы", "zips": "Архивы",
    "messages": "Сообщения", "files": "Файлы", "media": "Медиа",
    "days": "Активные дни", "opportunities": "Возможности", "contribution": "По вкладу",
}
CATEGORIES = {"message": "Сообщение", "loop": "Луп", "zip": "Архив", "files": "Файл", "media": "Медиа"}
PAGE_SIZE = 5
FORM_TTL = 15 * 60


def entry_body(item, kind: str) -> str:
    if kind == "note":
        return item.text or ""
    return item.comment or f"{item.artist or '—'} | {item.track or '—'}"


def record_key(kind: str, record_id: int, version: str = "") -> str:
    return f"{'p' if kind == 'place' else 'n'}.{record_id}" + (f".{version}" if version else "")


def parse_record_key(value: str) -> tuple[str, int, str]:
    parts = value.split(".")
    if len(parts) not in {2, 3} or parts[0] not in {"p", "n"} or not parts[1].isdigit():
        raise ValueError("Эта запись недоступна.")
    return ("place" if parts[0] == "p" else "note", int(parts[1]), parts[2] if len(parts) == 3 else "")


def draft_buttons(owner: int, data: dict) -> InlineKeyboardMarkup:
    nonce = data["nonce"]
    label = "✓ Сохранить" if data["kind"] in {"place", "note", "edit"} else "✓ Подтвердить"
    rows = [[button(owner, label, "save", extra=nonce)]]
    if data["kind"] in {"place", "note", "edit"} and not data.get("tier_only"):
        rows += [[button(owner, "✎ Изменить текст", "revise", extra=nonce)]]
    rows += [[button(owner, "Отмена", "cancel", data.get("uid", owner), extra=nonce)]]
    return keyboard(rows)


class Entry(StatesGroup):
    tier = State()
    search = State()
    details = State()
    confirm = State()


@dataclass
class Screen:
    text: str
    markup: InlineKeyboardMarkup
    context: dict = field(default_factory=dict)


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
        "📊 <b>Статистика активности</b>", escape(short(name, 80)),
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
    lines += ["<i>Одно сообщение или вложение = одно действие. Плейсменты учитываются отдельно.</i>"]
    return "\n".join(lines)


def roles_card(name: str, p: dict) -> str:
    status = "Порог достигнут" if p["ready"] else "Пока порог не достигнут"
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
        "<b>Моя активность</b> — сообщения и вложения за выбранный период.\n"
        "<b>Мой профиль</b> — плейсменты, прогресс к роли и общая сводка.\n\n"
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
            "В списке записей нажми на нужную, чтобы исправить или удалить её. "
            "История изменений позволяет восстановить удалённое. "
            "Плейсменты, по которым уже подтверждена роль, защищены от изменений.\n"
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


async def build_screen(app, session, owner: int, view: str = "home", uid: int = 0, period: str = "month", extra: str = "0", query: str = "") -> Screen:
    """Render bounded screens; permission checks also cover direct callers."""
    admin = owner in app.ADMIN_IDS
    uid = uid or owner
    if period not in PERIODS:
        raise ValueError("Unknown period")
    personal = {"stats", "profile", "places", "roles", "history", "rec"}
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
        rows = [[button(owner, "📊 Моя активность", "stats", owner), button(owner, "👤 Мой профиль", "profile", owner)]]
        if admin:
            rows += [[button(owner, "👥 Участники", "members"), button(owner, "🏆 Рейтинг", "top", extra="total")],
                     [button(owner, "⚙️ Управление", "admin"), button(owner, "💡 Помощь", "help")]]
        else:
            rows += [[button(owner, "💡 Помощь", "help")]]
    elif view == "stats":
        s = await app.get_user_stats(session, uid, period)
        text = stats_card(name, s)
        comparison = await activity_comparison(app, session, uid, period)
        if comparison:
            current, previous = comparison["current"], comparison["previous"]
            delta = current - previous
            change = f"{delta:+,}" if previous else ("новая активность" if current else "без изменений")
            text += f"\n\n{escape(comparison['label'])}: <b>{previous:,} → {current:,}</b> · {change}"
        rows = [period_buttons(owner, view, uid, period)]
    elif view == "profile":
        month = await app.get_user_stats(session, uid, "month")
        p = await app.placement_progress(session, uid)
        text = (
            f"👤 <b>{escape(short(name, 80))}</b>\n<i>Плейсменты и прогресс к роли</i>\n\n"
            f"<b>{p['total']}</b> плейсментов за всё время\nSmall {p['small']} · Big {p['large']}\n\n"
            f"{'✅ Порог для роли достигнут' if p['ready'] else '🎖 Текущий прогресс'}\n"
            f"Small <b>{p['small_raw']}/{p['small_target']}</b> · Big <b>{p['large_raw']}/{p['large_target']}</b>\n"
            f"<i>Для порога достаточно {p['small_target']} Small или {p['large_target']} Big. "
            "После подтверждения роли начинается новый цикл соответствующей шкалы.</i>\n\n"
            f"Активность за месяц: {month['total']:,} действий · {month['active_days']} дн.\n"
            f"🔥 Текущая серия: {month['streak']} дн."
        )
        rows = [[button(owner, "🎯 Плейсменты", "places", uid), button(owner, "🎖 Прогресс роли", "roles", uid)],
                [button(owner, "📊 Активность", "stats", uid), button(owner, "🗓 По месяцам", "history", uid)]]
        if admin:
            rows += [[button(owner, "＋ Плейсмент", "addplace", uid), button(owner, "＋ Заметка", "addnote", uid)],
                     [button(owner, "📝 Заметки", "notes", uid), button(owner, "🕓 Последние сообщения", "last", uid)],
                     [button(owner, "🧾 История изменений", "changes", uid)],
                     [button(owner, "‹ Участники", "members", extra="recent.0")]]
    elif view == "roles":
        p = await app.placement_progress(session, uid)
        text = roles_card(name, p)
        awards = (await session.scalars(select(app.RoleAward).where(app.RoleAward.user_id == uid)
                  .order_by(app.RoleAward.created_at.desc(), app.RoleAward.id.desc()).limit(3))).all()
        if awards:
            text += "\n\n<b>Подтверждённые роли</b>"
            for award in awards:
                text += f"\n{app.short_date(award.created_at)} · {award.kind.title()}"
        if admin:
            eligible = []
            if p["ready_small"]:
                eligible.append(button(owner, "Подтвердить Small", "awardsmall", uid))
            if p["ready_big"]:
                eligible.append(button(owner, "Подтвердить Big", "awardbig", uid))
            if eligible:
                rows += [eligible]
    elif view == "history":
        history = await monthly_history(app, session, uid)
        pages = max(1, math.ceil(len(history) / PAGE_SIZE))
        page = max(0, min(int(extra), pages - 1))
        text = f"🗓 <b>Активность по месяцам</b>\n{escape(short(name, 80))}\n"
        if not history:
            text += "\nПока нет активности. Здесь появятся месяцы с сообщениями в группе."
        for item in history[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            text += f"\n<b>{item['period']}</b> · {item['total']:,} действий · {item['active_days']} дн.\n"
            counts = [f"{label} {item[key]}" for key, label in (("loops", "Лупы"), ("zips", "Архивы"),
                      ("files", "Файлы"), ("media", "Медиа"), ("messages", "Сообщения")) if item[key]]
            text += " · ".join(counts) + "\n"
        if nav := pager(owner, view, uid, page, pages):
            rows += [nav]
    elif view in {"places", "notes", "last", "changes"}:
        page = int(extra)
        model = {"places": app.Placement, "notes": app.Note, "last": app.MsgLog, "changes": app.RecordChange}[view]
        where = model.user_id == uid
        if view == "last" and app.TRACK_CHAT_ID is not None:
            where = where & (model.chat_id == app.TRACK_CHAT_ID)
        items, count, page, pages = await page_rows(session, model, where, [model.created_at.desc(), model.id.desc()], page)
        title = {"places": "🎯 Плейсменты", "notes": "📝 Заметки", "last": "🕓 Последние сообщения", "changes": "🧾 История изменений"}[view]
        text = f"<b>{title}</b>\n{escape(short(name, 80))}\n<i>{count} записей · страница {page + 1}/{pages}</i>\n"
        if not items:
            text += "\nПока нет записей."
        for idx, item in enumerate(items, start=page * PAGE_SIZE + 1):
            if view in {"places", "notes"}:
                kind = "place" if view == "places" else "note"
                label = f"{idx}. {app.short_date(item.created_at)}" + (f" · {'Big' if item.tier == 'large' else 'Small'}" if kind == "place" else "")
                text += f"\n<b>{label}</b>\n{escape(short(entry_body(item, kind), 240))}\n"
                rows += [[button(owner, label + " · Открыть", "rec", uid, extra=record_key(kind, item.id))]]
            elif view == "changes":
                labels = {"edit": "Исправлено", "delete": "Удалено", "restore": "Восстановлено"}
                actor_name = await app.resolve_name(session, item.actor_id) or f"ID {item.actor_id}"
                text += f"\n<b>{app.format_local(item.created_at)} · {labels.get(item.action, item.action)}</b>\n"
                text += f"{'Плейсмент' if item.record_kind == 'place' else 'Заметка'} · {escape(short(actor_name, 50))}\n"
                rows += [[button(owner, f"Изменение #{item.id}", "change", uid, extra=str(item.id))]]
            else:
                category = CATEGORIES.get(item.category, "Файл")
                text += f"\n<b>{app.format_local(item.created_at)} · {category}</b>\n{escape(short(item.text_preview, 240))}\n"
        if view == "places" and admin:
            rows += [[button(owner, "＋ Плейсмент", "addplace", uid)]]
        if view == "notes":
            rows += [[button(owner, "＋ Заметка", "addnote", uid)]]
        if nav := pager(owner, view, uid, page, pages):
            rows += [nav]
    elif view == "rec":
        kind, record_id, _ = parse_record_key(extra)
        if kind == "note" and not admin:
            raise PermissionError("Заметки доступны только администратору.")
        try:
            item = await load_record(app, session, uid, kind, record_id)
        except ValueError:
            item = None
        if item is None:
            text = "Эта запись уже удалена. Открой список заново."
        else:
            title = "🎯 Плейсмент" if kind == "place" else "📝 Заметка"
            text = f"<b>{title}</b>\n{escape(short(name, 80))}\n{app.format_local(item.created_at)}\n\n"
            if kind == "place":
                text += f"<b>{'Big' if item.tier == 'large' else 'Small'}</b>\n"
            text += escape(entry_body(item, kind))
            editable = kind == "note" or await app.placement_can_be_undone(session, item)
            if admin and editable:
                key = record_key(kind, item.id, record_version(item))
                rows += [[button(owner, "✎ Исправить текст", "edt", uid, extra=key),
                          button(owner, "Удалить", "del", uid, extra=key)]]
                if kind == "place":
                    rows += [[button(owner, "Изменить Small / Big", "tier", uid, extra=key)]]
            elif admin:
                text += "\n\n🔒 По этой записи уже подтверждена роль. Изменение недоступно."
        rows += [[button(owner, "‹ Плейсменты" if kind == "place" else "‹ Заметки", "places" if kind == "place" else "notes", uid)]]
    elif view == "change":
        item = await session.get(app.RecordChange, int(extra))
        if not item or item.user_id != uid:
            raise ValueError("Изменение не найдено.")
        old, new = json.loads(item.before_json), json.loads(item.after_json) if item.after_json else None
        def body(values):
            return values.get("text") or values.get("comment") or f"{values.get('artist') or '—'} | {values.get('track') or '—'}"
        text = f"🧾 <b>Изменение #{item.id}</b>\n{escape(short(name, 80))}\n{app.format_local(item.created_at)}\n\n"
        text += f"<b>До</b>\n{escape(short(body(old), 600))}"
        if new:
            text += f"\n\n<b>После</b>\n{escape(short(body(new), 600))}"
        if item.action == "delete":
            restored = (await session.scalars(select(app.RecordChange.after_json)
                        .where(app.RecordChange.user_id == uid, app.RecordChange.action == "restore"))).all()
            already_restored = any(json.loads(payload).get("restored_from") == item.id for payload in restored if payload)
            if already_restored:
                text += "\n\n✅ Запись уже восстановлена."
            else:
                text += "\n\nЗапись удалена. Восстановление вернёт исходные дату и содержание."
                rows += [[button(owner, "Восстановить запись", "restore", uid, extra=str(item.id))]]
        rows += [[button(owner, "‹ История изменений", "changes", uid)]]
    elif view == "members":
        parts = extra.split(".")
        if len(parts) > 2 and parts[2] == "0":
            query = ""
        sort, raw_page = (parts[0], parts[1]) if len(parts) > 1 else ("recent", extra)
        directory_nonce = parts[2] if len(parts) > 2 else ("0" if not query else secrets.token_hex(3))
        directory = await member_directory(app, session, page=int(raw_page), query=query, sort=sort)
        text = f"👥 <b>Участники</b>\n<i>{directory['total']} в базе · страница {directory['page'] + 1}/{directory['pages']}</i>\n"
        text += f"\nПоиск: <b>{escape(short(query, 80))}</b>" if query else (
            "\nСначала — недавно активные в группе." if sort == "recent" else "\nУчастники по имени.")
        if not directory["items"]:
            text += "\nНикого не найдено. Попробуй часть имени или username."
        rows = [[button(owner, "⌕ Найти участника", "search")]]
        if query:
            rows += [[button(owner, "Сбросить поиск", "members")]]
        for member in directory["items"]:
            rows += [[button(owner, short(member["name"], 45), "profile", member["uid"])]]
        switches = [[button(owner, ("✓ " if sort == key else "") + label, "members", extra=f"{key}.0.{directory_nonce}")
                     for key, label in (("recent", "Недавние"), ("name", "По имени"))]]
        rows += switches
        page, pages = directory["page"], directory["pages"]
        nav = []
        if page:
            nav += [button(owner, "‹ Назад", "members", extra=f"{sort}.{page - 1}.{directory_nonce}")]
        if page + 1 < pages:
            nav += [button(owner, "Далее ›", "members", extra=f"{sort}.{page + 1}.{directory_nonce}")]
        if nav:
            rows += [nav]
    elif view == "top":
        if extra not in METRICS:
            raise ValueError("Unknown metric")
        text = await app.build_top(session, extra, period)
        rows = [period_buttons(owner, view, 0, period, extra)]
        for keys in [("total", "contribution", "loops"), ("messages", "zips", "files"), ("media", "days", "opportunities")]:
            rows += [[button(owner, ("✓ " if key == extra else "") + METRICS[key], view, period=period, extra=key) for key in keys]]
    elif view == "admin":
        text = "⚙️ <b>Управление</b>\n\nУчастники, роли и отчёты.\nПлейсменты и заметки добавляются из карточки участника."
        rows = [[button(owner, "🎖 Кому доступна роль", "board", extra="ready.0"),
                 button(owner, "📋 Отчёты", "reports")],
                [button(owner, "👥 Найти участника", "search"), button(owner, "📤 Экспорт CSV", "export")],
                [button(owner, "↩️ Отменить мою последнюю запись", "undo")],
                [button(owner, "🛠 Сервис", "service")]]
    elif view == "service":
        text = "🛠 <b>Сервис</b>\n\nПроверка состояния, резервные копии и обслуживание данных."
        rows = [[button(owner, "🩺 Состояние", "health"), button(owner, "💾 Копия базы", "backup")],
                [button(owner, "Пересчитать прошлый месяц", "snapshot")]]
    elif view == "reports":
        destination = await session.get(app.Setting, "report_chat")
        thread = await session.get(app.Setting, "report_thread")
        text = "📋 <b>Отчёты</b>\n\nНедельный отчёт охватывает прошлую календарную неделю: понедельник — воскресенье."
        if destination:
            text += f"\n\n✅ Автоотчёт включён · понедельник, 12:00 ({escape(app.BOT_TIMEZONE)})."
            text += f"\nЧат <code>{escape(destination.value)}</code> · топик <code>{escape(thread.value if thread else '0')}</code>"
        else:
            text += "\n\nАвтоотчёт выключен."
        text += "\n\nЧтобы выбрать место отправки, ответь <code>/setreport</code> на сообщение в нужном топике подключённой группы."
        rows = [[button(owner, "Прошлая неделя", "report")]]
        if destination:
            rows += [[button(owner, "Выключить автоотчёт", "reportoff")]]
    elif view == "help":
        text = help_card(admin)
    elif view == "report":
        text = await app.build_weekly_report(session)
        rows += [[button(owner, "‹ Отчёты", "reports")]]
    elif view == "board":
        mode, raw_page = extra.split(".", 1) if "." in extra else ("ready", extra)
        mode = mode if mode in {"ready", "all"} else "ready"
        ids = (await session.scalars(select(app.Placement.user_id).distinct())).all()
        entries = []
        for member_id in ids:
            p = await app.placement_progress(session, member_id)
            if mode == "ready" and not p["ready"]:
                continue
            member_name = await app.resolve_name(session, member_id) or f"ID {member_id}"
            entries.append((member_id, member_name, p))
        entries.sort(key=lambda item: (not item[2]["ready"], item[1].casefold(), item[0]))
        pages = max(1, math.ceil(len(entries) / 8))
        page = max(0, min(int(raw_page), pages - 1))
        text = f"🎖 <b>{'Кому доступна роль' if mode == 'ready' else 'Прогресс ролей'}</b>\n<i>{len(entries)} участников · страница {page + 1}/{pages}</i>\n"
        if not entries:
            text += "\nСейчас нет участников с достигнутым порогом." if mode == "ready" else "\nПлейсментов пока нет. Добавь первый из карточки участника."
        rows += [[button(owner, ("✓ " if mode == key else "") + label, "board", extra=f"{key}.0")
                  for key, label in (("ready", "Готовы"), ("all", "Все"))]]
        for member_id, member_name, p in entries[page * 8:(page + 1) * 8]:
            text += f"\n{'✅' if p['ready'] else '◷'} <b>{escape(short(member_name, 45))}</b>\nSmall {p['small_raw']}/{p['small_target']} · Big {p['large_raw']}/{p['large_target']}\n"
            rows += [[button(owner, short(member_name, 45), "roles", member_id)]]
        nav = []
        if page:
            nav += [button(owner, "‹ Назад", "board", extra=f"{mode}.{page - 1}")]
        if page + 1 < pages:
            nav += [button(owner, "Далее ›", "board", extra=f"{mode}.{page + 1}")]
        if nav:
            rows += [nav]
    elif view == "candidates":
        return await build_screen(app, session, owner, "top", period="30d", extra="contribution")
    elif view == "health":
        text = await app.build_health_text(session)
    else:
        raise ValueError("Unknown screen")
    if view in personal | {"notes", "last", "changes"} and view != "profile":
        rows += [[button(owner, "‹ Профиль", "profile", uid)]]
    if view in {"board", "reports", "service", "health", "report"}:
        rows += [[button(owner, "‹ Управление", "admin")]]
    if view != "home":
        rows += [[button(owner, "⌂ Главное меню", "home")]]
    context = {"directory_query": query, "directory_nonce": directory_nonce} if view == "members" else {}
    return Screen(text, keyboard(rows), context)

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




async def save_entry(app, session, actor: int, data: dict) -> str:
    """Recheck identity, target and business constraints before a confirmed write."""
    if actor not in app.ADMIN_IDS:
        raise PermissionError("Нужны права администратора.")
    if data["kind"] in {"edit", "delete"}:
        return await modify_record(app, session, actor, data["uid"], data["record_kind"],
                                   data["record_id"], data["version"],
                                   ("tier" if data.get("tier_only") else "edit") if data["kind"] == "edit" else "delete",
                                   text=data.get("text"), tier=data.get("tier"))
    if data["kind"] == "restore":
        return await restore_record(app, session, actor, data["change_id"])
    if data["kind"] == "undo":
        target = await last_undo_target(app, session, actor)
        kind = "note" if isinstance(target, app.Note) else "place"
        if not target or f"{kind}-{target.id}" != data["target"]:
            raise ValueError("Запись уже отменена или появилась новая. Повтори действие.")
        return await modify_record(app, session, actor, target.user_id, kind, target.id,
                                   data["version"], "delete")
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
    if data["kind"] == "reportoff":
        for key in ("report_chat", "report_thread"):
            row = await session.get(app.Setting, key)
            if row:
                await session.delete(row)
        await session.commit()
        return "Автоотчёт выключен."
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
    if data["kind"] in {"undo", "snapshot", "restore", "reportoff"}:
        return data["text"]
    name = escape(short(data["name"], 80))
    if data["kind"] in {"edit", "delete"}:
        title = "Удалить запись?" if data["kind"] == "delete" else "Сохранить исправление?"
        text = f"<b>{title}</b>\n{name}\n\n<b>Сейчас</b>\n{escape(data['original'])}"
        if data["kind"] == "edit":
            text += f"\n\n<b>После изменения</b>\n{escape(data['text'])}"
            if data["record_kind"] == "place":
                text += f"\nТип: {'Big' if data['tier'] == 'large' else 'Small'}"
        else:
            text += "\n\nУдалённую запись можно восстановить из истории изменений."
        return text
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
        data = {**data, "nonce": nonce, "preview_id": query.message.message_id, "started": app.utc_now().timestamp()}
        cancel_view = data.get("result_view", "admin" if data["kind"] in {"undo", "snapshot", "reportoff"} else "profile")
        data["cancel_view"] = cancel_view
        await state.set_data(data)
        await edit_screen(query, Screen(confirmation_text(app, data), draft_buttons(query.from_user.id, data)))

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
        personal = {"home", "help", "stats", "profile", "places", "roles", "history", "rec"}
        if owner not in app.ADMIN_IDS and (action not in personal or (uid and uid != owner)):
            await query.answer("Этот раздел доступен только администратору.", show_alert=True)
            return
        current_data = await state.get_data()
        if action == "members" and len(extra.split(".")) > 2:
            directory_nonce = extra.split(".")[2]
            if directory_nonce != "0" and current_data.get("directory_nonce") != directory_nonce:
                await query.answer("Этот поиск уже завершён. Открой участников и повтори поиск.", show_alert=True)
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
            same_form = await state.get_state() == Entry.tier.state and data.get("nonce") == extra and data.get("uid") == uid
            if not same_form or app.utc_now().timestamp() - data.get("started", 0) > FORM_TTL:
                if same_form:
                    await state.clear()
                await edit_screen(query, Screen("Этот выбор уже устарел. Открой карточку участника и повтори.", keyboard([[button(owner, "👤 Профиль", "profile", uid)]])))
                return
        if action in {"save", "revise"}:
            data = await state.get_data()
            same_form = await state.get_state() == Entry.confirm.state and data.get("nonce") == extra and data.get("preview_id") == query.message.message_id
            if not same_form or app.utc_now().timestamp() - data.get("started", 0) > FORM_TTL:
                if same_form:
                    await state.clear()
                await edit_screen(query, Screen("Этот ввод уже завершён или устарел. Открой карточку участника и повтори.", keyboard([[button(owner, "⌂ Главное меню", "home")]])))
                return
            if action == "revise":
                if data["kind"] not in {"place", "note", "edit"}:
                    return
                nonce = secrets.token_hex(4)
                await state.set_state(Entry.details)
                await state.update_data(nonce=nonce, started=app.utc_now().timestamp())
                await edit_screen(query, Screen("✎ <b>Исправить текст</b>\n\nТекущий вариант:\n"
                    + escape(data["text"]) + "\n\nОтправь новый текст (до 500 символов).",
                    keyboard([[button(owner, "Отмена", "cancel", data["uid"], extra=nonce)]])))
                return
            try:
                result = await save_entry(app, session, owner, data)
            except (ValueError, PermissionError) as exc:
                result = str(exc)
            await state.clear()
            screen = await build_screen(app, session, owner, data.get("result_view",
                                        "admin" if data["kind"] in {"undo", "snapshot"} else "profile"), data["uid"])
            screen.text = f"{escape(result)}\n\n" + screen.text
            if data["kind"] in {"place", "note"}:
                screen.markup.inline_keyboard.insert(0, [button(owner, "↩️ Отменить последнее добавление", "undo")])
            await edit_screen(query, screen)
            return
        previous = await state.get_data()
        await state.clear()
        if action in {"edt", "del", "tier"}:
            try:
                kind, record_id, version = parse_record_key(extra)
                item = await load_record(app, session, uid, kind, record_id)
                if not item or record_version(item) != version:
                    raise ValueError("Запись уже изменилась. Открой её заново.")
                if kind == "place" and not await app.placement_can_be_undone(session, item):
                    raise ValueError("По этому плейсменту уже подтверждена роль.")
            except ValueError as exc:
                screen = await build_screen(app, session, owner, "profile", uid)
                screen.text = escape(str(exc)) + "\n\n" + screen.text
                await edit_screen(query, screen)
                return
            nonce = secrets.token_hex(4)
            data = {"kind": "delete" if action == "del" else "edit", "uid": uid,
                    "name": await app.resolve_name(session, uid) or f"ID {uid}",
                    "record_kind": kind, "record_id": record_id, "version": version,
                    "original": entry_body(item, kind), "text": entry_body(item, kind),
                    "tier": item.tier if kind == "place" else None,
                    "result_view": "places" if kind == "place" else "notes",
                    "cancel_view": "places" if kind == "place" else "notes",
                    "nonce": nonce, "started": app.utc_now().timestamp(), "tier_only": action == "tier"}
            if action == "del":
                await preview(query, state, data)
            elif action == "tier" and kind == "place":
                await state.set_state(Entry.tier)
                await state.set_data(data)
                await edit_screen(query, Screen("🎯 <b>Изменить тип плейсмента</b>\n\n" + escape(data["original"]),
                    keyboard([[button(owner, "Small", "small", uid, extra=nonce), button(owner, "Big", "large", uid, extra=nonce)],
                              [button(owner, "Отмена", "cancel", uid, extra=nonce)]])))
            else:
                await state.set_state(Entry.details)
                await state.set_data(data)
                await edit_screen(query, Screen("✎ <b>Исправить запись</b>\n\nСейчас:\n" + escape(data["original"])
                    + "\n\nОтправь новый текст (до 500 символов).",
                    keyboard([[button(owner, "Отмена", "cancel", uid, extra=nonce)]])))
            return
        if action in {"restore", "reportoff"}:
            if action == "restore":
                change = await session.get(app.RecordChange, int(extra))
                if not change or change.user_id != uid or change.action != "delete":
                    await edit_screen(query, await build_screen(app, session, owner, "changes", uid))
                    return
                text = "🧾 <b>Восстановить удалённую запись?</b>\n\nВернутся исходные содержание, участник и дата."
                data = {"kind": "restore", "change_id": change.id, "uid": uid, "text": text, "result_view": "changes"}
            else:
                data = {"kind": "reportoff", "uid": owner, "text": "📋 <b>Выключить автоотчёт?</b>\n\nРучной просмотр останется доступным.", "result_view": "reports"}
            await preview(query, state, data)
            return
        if action == "search":
            nonce = secrets.token_hex(4)
            await state.set_state(Entry.search)
            await state.update_data(started=app.utc_now().timestamp(), nonce=nonce, cancel_view="members", uid=0)
            await edit_screen(query, Screen("⌕ <b>Найти участника</b>\n\nОтправь имя, часть username, @username или Telegram ID. Если совпадений несколько, покажу их кнопками. Можно переслать сообщение участника с открытым автором.", keyboard([[button(owner, "Отмена", "cancel", extra=nonce)]])))
            return
        if action in {"addplace", "addnote", "small", "large", "awardsmall", "awardbig"}:
            if uid <= 0:
                await edit_screen(query, await build_screen(app, session, owner, "members"))
                return
            name = await app.resolve_name(session, uid) or f"ID {uid}"
            if action == "addplace":
                nonce = secrets.token_hex(4)
                await state.set_state(Entry.tier)
                await state.update_data(nonce=nonce, uid=uid, cancel_view="profile", started=app.utc_now().timestamp())
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
            if action in {"small", "large"} and previous.get("kind") == "edit":
                await preview(query, state, {**previous, "tier": action})
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
            screen = await build_screen(app, session, owner, "service")
            screen.text = (f"💾 Копия создана: <code>{escape(path.name)}</code>" if path else "Копия базы доступна для SQLite.") + "\n\n" + screen.text
            await edit_screen(query, screen)
            return
        if action in {"snapshot", "undo"}:
            text = "🗓 <b>Пересчитать архив прошлого месяца?</b>\n\nТекущие сообщения сохранятся."
            data = {"kind": action, "uid": owner, "name": "", "result_view": "service" if action == "snapshot" else "admin"}
            if action == "undo":
                target = await last_undo_target(app, session, owner)
                if not target:
                    await edit_screen(query, Screen("Нет записей для отмены за последние 15 минут.", keyboard([[button(owner, "‹ Управление", "admin")]])))
                    return
                kind = "note" if isinstance(target, app.Note) else "place"
                data["uid"] = target.user_id
                text = f"↩️ <b>Отменить последнюю запись?</b>\n\n{escape(short(target.username or target.user_id))}\n{escape(short(target.text if kind == 'note' else target.comment or target.artist or 'Плейсмент', 240))}"
                data["target"] = f"{kind}-{target.id}"
                data["version"] = record_version(target)
            data["text"] = text
            await preview(query, state, data)
            return
        try:
            directory_parts = extra.split(".")
            unfiltered = len(directory_parts) > 2 and directory_parts[2] == "0"
            directory_query = previous.get("directory_query", "") if action == "members" and extra != "0" and not unfiltered else ""
            if directory_query and len(extra.split(".")) == 2 and previous.get("directory_nonce"):
                extra += "." + previous["directory_nonce"]
            screen = await build_screen(app, session, owner, action, uid, period, extra, query=directory_query)
            if action == "members":
                await state.set_data(screen.context)
            elif previous.get("directory_query") and action != "home":
                await state.set_data({"directory_query": previous["directory_query"], "directory_nonce": previous.get("directory_nonce", "0")})
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
        if app.utc_now().timestamp() - data.get("started", 0) > FORM_TTL:
            await state.clear()
            await message.answer("Время поиска истекло. Открой /menu.")
            return
        origin = message.forward_origin
        raw = str(origin.sender_user.id) if origin and origin.type == "user" else (message.text or "").strip()
        if not raw or len(raw) > 80:
            await message.answer("Отправь @username или ID. Скрытого автора пересылки Telegram не раскрывает.")
            return
        await state.clear()
        screen = await build_screen(app, session, message.from_user.id, "members", query=raw)
        await state.set_data(screen.context)
        await deliver(message, screen, bot)

    @ui.message(Entry.details, F.chat.type == "private", ~F.text.startswith("/"))
    async def details(message: Message, state: FSMContext):
        if not app.is_admin(message):
            await state.clear()
            return
        data = await state.get_data()
        if app.utc_now().timestamp() - data.get("started", 0) > FORM_TTL:
            await state.clear()
            await message.answer("Время ввода истекло. Открой карточку участника и повтори.")
            return
        value = (message.text or "").strip()
        try:
            if not value or len(value) > 500:
                raise ValueError("Отправь текст от 1 до 500 символов.")
            if data["kind"] == "place" or (data["kind"] == "edit" and data["record_kind"] == "place"):
                parse_placement(value)
        except ValueError as exc:
            await message.answer(str(exc))
            return
        data["text"] = value
        nonce = secrets.token_hex(4)
        data["nonce"] = nonce
        sent = await message.answer(confirmation_text(app, data), reply_markup=draft_buttons(message.from_user.id, data))
        await state.set_state(Entry.confirm)
        await state.update_data({**data, "nonce": nonce, "preview_id": sent.message_id})

    @ui.message(Entry.confirm, F.chat.type == "private", ~F.text.startswith("/"))
    async def waiting_confirmation(message: Message):
        await message.answer("Проверь запись выше. Нажми «Сохранить», «Изменить текст» или «Отмена».")

    @ui.message(Entry.tier, F.chat.type == "private", ~F.text.startswith("/"))
    async def waiting_tier(message: Message):
        await message.answer("Выбери Small или Big кнопкой выше. Чтобы отменить ввод, отправь /cancel.")

    @ui.message(F.chat.type == "private")
    async def fallback(message: Message, session, state: FSMContext, bot):
        if app.is_command_message(message) and await state.get_state():
            await message.answer("Неизвестная команда. Твой ввод сохранён. Продолжи форму выше или отправь /cancel.")
            return
        await state.clear()
        screen = await build_screen(app, session, message.from_user.id)
        screen.text = "Управление — через кнопки ниже.\n\n" + screen.text
        await deliver(message, screen, bot)

    parent.include_router(ui)
