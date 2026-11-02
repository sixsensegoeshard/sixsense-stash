"""Telegram screens and guided admin actions. No network or DB work at import time."""

from __future__ import annotations

import math
import json
import secrets
import unicodedata
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
PAGE_SIZE = 4
FORM_TTL = 15 * 60


def entry_body(item, kind: str) -> str:
    if kind == "note":
        return item.text or ""
    body = item.comment or f"{item.artist or '—'} | {item.track or '—'}"
    return body + (f" | {item.collaborators}" if getattr(item, "collaborators", None) else "")


def activity_role_input(value: str, current: str) -> tuple[str, str]:
    import re
    role, separator, period = value.rpartition("|")
    if not separator:
        role, period = value, current
    role, period = role.strip(), period.strip()
    if not role or len(role) > 100 or "\n" in role:
        raise ValueError("Название роли — от 1 до 100 символов в одной строке.")
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period) or period > current:
        raise ValueError("Укажи существующий месяц в формате ГГГГ-ММ, не позднее текущего.")
    return role, period


def record_key(kind: str, record_id: int, version: str = "") -> str:
    return f"{'p' if kind == 'place' else 'n'}.{record_id}" + (f".{version}" if version else "")


def parse_record_key(value: str) -> tuple[str, int, str]:
    parts = value.split("~", 1)[0].split(".")
    if len(parts) not in {2, 3} or parts[0] not in {"p", "n"} or not parts[1].isdigit():
        raise ValueError("Эта запись недоступна.")
    return ("place" if parts[0] == "p" else "note", int(parts[1]), parts[2] if len(parts) == 3 else "")


def source_page(value: str) -> str:
    page = value.split("~", 1)[1] if "~" in value else "0"
    if not page.isdigit():
        raise ValueError("Недоступная страница записи.")
    return page


def draft_buttons(owner: int, data: dict) -> InlineKeyboardMarkup:
    nonce = data["nonce"]
    label = "✓ Сохранить" if data["kind"] in {"place", "note", "edit"} else "✓ Подтвердить"
    save_style = "danger" if data["kind"] in {"delete", "undo", "reportoff"} else "success"
    rows = [[button(owner, label, "save", extra=nonce, style=save_style)]]
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


def directory_context(data: dict) -> dict:
    return {key: data[key] for key in ("directory_query", "directory_nonce", "directory_extra") if key in data}


def short(value: Any, limit: int = 160) -> str:
    text = " ".join(str(value or "").split())
    if text_units(text) <= limit:
        return text
    clusters = []
    for char in text:
        joined = clusters and (unicodedata.category(char).startswith("M")
            or char == "\u200d" or clusters[-1].endswith("\u200d")
            or 0x1F3FB <= ord(char) <= 0x1F3FF
            or (0x1F1E6 <= ord(char) <= 0x1F1FF and len(clusters[-1]) == 1
                and 0x1F1E6 <= ord(clusters[-1]) <= 0x1F1FF))
        if joined:
            clusters[-1] += char
        else:
            clusters.append(char)
    result, used = [], 0
    for cluster in clusters:
        size = text_units(cluster)
        if used + size > max(0, limit - 1):
            break
        result.append(cluster)
        used += size
    return "".join(result) + "…"


def text_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def number(value: int | float) -> str:
    return f"{value:,}".replace(",", "\u202f")


def plural(value: int, one: str, few: str, many: str) -> str:
    n = abs(value)
    return many if 11 <= n % 100 <= 14 else one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many


def summary(total: int, days: int) -> str:
    return (f"<b>{number(total)}</b> {plural(total, 'действие', 'действия', 'действий')} · "
            f"<b>{number(days)}</b> {plural(days, 'активный день', 'активных дня', 'активных дней')}")


def metric_panel(content: str) -> str:
    return f"<blockquote>{content}</blockquote>"


def card_title(text: str, subtitle: str = "") -> str:
    """Telegram-native accents; names stay upright and easy to read."""
    icons = {"Статистика активности": "📊", "Профиль": "👤", "Прогресс к роли": "🎖",
             "Плейсменты": "🎯", "Плейсмент": "🎯", "Заметки": "📝", "Заметка": "📝",
             "Действия с профилем": "⚙️", "По месяцам": "🗓", "Последние сообщения": "🕓",
             "История изменений": "🧾", "Участники": "👥", "Управление": "⚙️",
             "Сервис": "🛠", "Отчёты": "📋", "Помощь": "💡"}
    heading = f"{icons[text]} {text}" if text in icons else text
    return f"<b>{escape(heading)}</b>" + (f"\n{escape(short(subtitle, 48))}" if subtitle else "")


def page_label(count: int, page: int, pages: int, noun: str = "записей") -> str:
    suffix = f" · страница {page + 1}/{pages}" if pages > 1 else ""
    if noun == "записей":
        noun = plural(count, "запись", "записи", "записей")
    elif noun == "участников":
        noun = plural(count, "участник", "участника", "участников")
    return f"<i>{number(count)} {noun}{suffix}</i>"


def month_label(period: str) -> str:
    months = ("Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
              "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь")
    try:
        year, month = map(int, period.split("-"))
        return f"{months[month - 1]} {year}" if 1 <= month <= 12 else period
    except ValueError:
        return period


def button(owner: int, label: str, action: str, uid: int = 0, period: str = "month", extra: str = "0", *, style: str | None = None) -> InlineKeyboardButton:
    data = f"ui:{owner}:{action}:{uid}:{period}:{extra}"
    if len(data.encode()) > 64:
        raise ValueError("Callback exceeds Telegram's 64-byte limit")
    accents = {"addplace": "primary", "search": "primary", "del": "danger", "reportoff": "danger",
               "awardsmall": "success", "awardbig": "success"}
    return InlineKeyboardButton(text=label, callback_data=data, style=style or accents.get(action))


def keyboard(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    compact_rows = []
    for row in rows:
        # Static actions keep their full meaning. Long paired actions get their
        # own row instead of being truncated in a narrow native Telegram bubble.
        if len(row) > 1 and any(label_width(item.text) > 13 for item in row):
            compact_rows.extend([[item] for item in row])
        else:
            compact_rows.append(row)
    for row in compact_rows:
        for item in row:
            limit = min(text_units(item.text), 36 if len(row) == 1 else 22)
            while label_width(short(item.text, limit)) > (24 if len(row) == 1 else 13) and limit > 1:
                limit -= 1
            item.text = short(item.text, limit)
    return InlineKeyboardMarkup(inline_keyboard=compact_rows)


def label_width(text: str) -> float:
    """Conservative relative glyph widths, not just UTF-16 character counts."""
    result = 0.0
    for char in text:
        if unicodedata.category(char).startswith("M") or char == "\u200d":
            continue
        if char.isspace() or char in ".,:;|!il'`":
            result += 0.45
        elif char in "WMmwЖШЩЮЫжшщюы":
            result += 1.5
        elif unicodedata.east_asian_width(char) in {"W", "F"}:
            result += 2
        elif char.isupper():
            result += 1.15
        else:
            result += 1
    return result


def period_buttons(owner: int, view: str, uid: int, selected: str, extra: str = "0") -> list[list[InlineKeyboardButton]]:
    buttons = [button(owner, title, view, uid, key, extra,
                      style="primary" if key == selected else None) for key, title in PERIODS.items()]
    return [buttons[:2], buttons[2:]]


def progress_bar(value: int, target: int) -> str:
    filled = min(8, max(0, value * 8 // max(1, target)))
    return "▰" * filled + "▱" * (8 - filled)


def stats_card(name: str, s: dict) -> str:
    lines = [card_title("Статистика активности", name), escape(s['period_label']), ""]
    if s["total"]:
        lines += [metric_panel(summary(s["total"], s["active_days"]))]
        values = [("🎵 Лупы", "loops"), ("📦 Архивы", "zips"), ("📎 Файлы", "files"),
                  ("🖼 Медиа", "media"), ("💬 Сообщения", "messages")]
        lines += [f"{label} · <b>{number(s[key])}</b>" for label, key in values if s[key]]
    else:
        lines += ["Пока нет активности за этот период.", "Новые сообщения в группе появятся здесь автоматически."]
    if s["opportunities"]:
        lines += [f"🎯 Возможности · <b>{number(s['opportunities'])}</b>"]
    if s["streak"]:
        lines += [f"🔥 Серия · <b>{number(s['streak'])}</b> {plural(s['streak'], 'день', 'дня', 'дней')} подряд"]
    return "\n".join(lines)


def roles_card(name: str, p: dict) -> str:
    status = "Порог достигнут" if p["ready"] else "Пока порог не достигнут"
    need_s = max(0, p["small_target"] - p["small_raw"])
    need_b = max(0, p["large_target"] - p["large_raw"])
    return (
        card_title("Прогресс к роли", name) + "\n\n"
        f"Small — <b>{p['small_since']} / {p['small_target']}</b>\n{progress_bar(p['small_raw'], p['small_target'])}\n"
        + (f"<i>{p['small_raw']} в текущем цикле</i>\n" if p['small_raw'] > p['small_target'] else "")
        + f"\nBig — <b>{p['large_since']} / {p['large_target']}</b>\n{progress_bar(p['large_raw'], p['large_target'])}\n"
        + (f"<i>{p['large_raw']} в текущем цикле</i>\n" if p['large_raw'] > p['large_target'] else "")
        + "\n"
        + f"{'✅' if p['ready'] else '◷'} <b>{status}</b>\n"
        + ("" if p["ready"] else f"До порога: {need_s} Small или {need_b} Big.\n")
        + "\nДостаточно одной шкалы. Роль выдаёт администратор."
    )


def help_card(admin: bool) -> str:
    text = (
        "💡 <b>Помощь</b>\n\n"
        "<b>Активность</b>\nСообщения и вложения за текущий календарный месяц.\nПервого числа счёт начинается заново; история остаётся в CSV.\n\n"
        "<b>Профиль</b>\nПлейсменты и прогресс к следующей роли.\n\n"
        "«Месяц» — статистика в боте. «Всё время» — CSV по месяцам.\n\n"
        "Бот учитывает новые сообщения в подключённой группе. "
        "Старая переписка автоматически не загружается.\n\n"
        "<code>/menu</code> — меню · <code>/cancel</code> — отмена ввода."
    )
    if admin:
        text += (
            "\n\n<b>Администратору</b>\n"
            "Найди человека в «Участниках». Добавление плейсмента — в профиле, "
            "заметки и история правок — в «Ещё». Запись сохраняется после подтверждения."
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


async def build_screen(app, session, owner: int, view: str = "home", uid: int = 0, period: str = "month", extra: str = "0", query: str = "", directory_extra: str = "recent.0.0") -> Screen:
    """Render bounded screens; permission checks also cover direct callers."""
    admin = owner in app.ADMIN_IDS
    uid = uid or owner
    if view in {"stats", "top"}:
        period = "month"
    if period not in PERIODS:
        raise ValueError("Unknown period")
    personal = {"stats", "profile", "places", "roles", "history", "rec"}
    if view in personal and uid != owner and not admin:
        raise PermissionError("Этот профиль доступен только администратору.")
    if view not in personal | {"home", "help", "rules"} and not admin:
        raise PermissionError("Этот раздел доступен только администратору.")
    name = await app.resolve_name(session, uid) or f"ID {uid}"
    rows = []
    if view == "home":
        text = card_title("301 / TRACKER", app.current_month_label())
        if admin:
            start, end, _ = app.period_bounds("month")
            people = await app.user_pivot(session, start, end)
            total = sum(item['total'] for item in people.values())
            totals = (f"{number(total)} {plural(total, 'действие', 'действия', 'действий')}"
                      f" · {number(len(people))} {plural(len(people), 'участник', 'участника', 'участников')}")
            text += "\n\n" + metric_panel(f"<b>Активность группы</b>\n{totals}")
            if not total:
                text += "\n\nНовые сообщения в группе появятся здесь автоматически."
            rows = [[button(owner, "🏆 Активность", "top", extra="total"), button(owner, "🎯 Плейсменты", "placetop")],
                    [button(owner, "📊 Статистика", "stats", owner), button(owner, "👥 Участники", "members")],
                    [button(owner, "⚙️ Управление", "admin"), button(owner, "💡 Помощь", "help")]]
        else:
            s = await app.get_user_stats(session, owner, "month")
            text += f"\n{escape(short(name, 36))}\n\n" + metric_panel(summary(s['total'], s['active_days']))
            if not s['total']:
                text += "\n\nОтправь сообщение или луп в группу — здесь появится твоя активность."
            rows = [[button(owner, "📊 Активность", "stats", owner, style="primary"), button(owner, "👤 Профиль", "profile", owner)],
                    [button(owner, "💡 Помощь", "help")]]
    elif view == "stats":
        s = await app.get_user_stats(session, uid, period)
        s["streak"] = min(s["streak"], s["active_days"])
        text = stats_card(name, s)
        rows = [[button(owner, "Месяц", "stats", uid, style="primary"),
                 button(owner, "Всё время", "archive", uid, extra="activity")]]
    elif view == "profile":
        month = await app.get_user_stats(session, uid, "month")
        p = await app.placement_progress(session, uid)
        activity_where = (app.MsgLog.user_id == uid) & (app.MsgLog.created_at < app.utc_now())
        if app.TRACK_CHAT_ID is not None:
            activity_where &= app.MsgLog.chat_id == app.TRACK_CHAT_ID
        has_messages = bool(await session.scalar(select(app.MsgLog.id).where(activity_where).limit(1)))
        text = card_title("Профиль", name) + "\n\n<b>Плейсменты и прогресс</b>\n"
        if p['total']:
            text += (f"<b>{number(p['total'])}</b> за всё время · Small {number(p['small'])} · Big {number(p['large'])}\n\n"
                     f"{'✅ <b>Порог для роли достигнут</b>' if p['ready'] else '🎖 До следующей роли'}\n"
                     f"Small <b>{p['small_since']} / {p['small_target']}</b>\n"
                     f"Big <b>{p['large_since']} / {p['large_target']}</b>")
        else:
            text += "Пока нет плейсментов."
        text += f"\n\n<b>Активность за месяц</b>\n{summary(month['total'], month['active_days'])}"
        role_actions = [button(owner, "🎖 Роли", "roles", uid)]
        if p["total"]:
            role_actions.insert(0, button(owner, "🎯 Плейсменты", "places", uid))
        activity_actions = [button(owner, "📊 Активность", "stats", uid, period)]
        has_archive = bool(await session.scalar(select(app.MonthlySnapshot.id).where(app.MonthlySnapshot.user_id == uid).limit(1)))
        if has_messages or has_archive:
            activity_actions.append(button(owner, "Всё время · CSV", "archive", uid, extra="activity"))
        rows = [role_actions, activity_actions]
        if admin:
            rows += [[button(owner, "＋ Плейсмент", "addplace", uid), button(owner, "Ещё", "more", uid)]]
    elif view == "more":
        text = card_title("Действия с профилем", name)
        rows = [[button(owner, "＋ Заметка", "addnote", uid)],
                [button(owner, "Роль за активность", "activityrole", uid)]]
        if await session.scalar(select(app.Note.id).where(app.Note.user_id == uid).limit(1)):
            rows += [[button(owner, "Заметки", "notes", uid)]]
        activity_where = (app.MsgLog.user_id == uid) & (app.MsgLog.created_at < app.utc_now())
        if app.TRACK_CHAT_ID is not None:
            activity_where &= app.MsgLog.chat_id == app.TRACK_CHAT_ID
        if await session.scalar(select(app.MsgLog.id).where(activity_where).limit(1)):
            rows += [[button(owner, "Последние сообщения", "last", uid)]]
        if await session.scalar(select(app.RecordChange.id).where(app.RecordChange.user_id == uid).limit(1)):
            rows += [[button(owner, "История изменений", "changes", uid)]]
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
        text = card_title("По месяцам", name) + "\n"
        if not history:
            text += "\nПока нет активности. Здесь появятся месяцы с сообщениями в группе."
        for item in history[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]:
            text += f"\n<b>{escape(month_label(item['period']))}</b>\n{summary(item['total'], item['active_days'])}\n"
            counts = [f"{label} {number(item[key])}" for key, label in (("loops", "Лупы"), ("zips", "Архивы"),
                      ("files", "Файлы"), ("media", "Медиа"), ("messages", "Сообщения")) if item[key]]
            text += " · ".join(counts) + "\n"
        if nav := pager(owner, view, uid, page, pages):
            rows += [nav]
    elif view in {"places", "notes", "last", "changes"}:
        page = int(extra)
        model = {"places": app.Placement, "notes": app.Note, "last": app.MsgLog, "changes": app.RecordChange}[view]
        where = model.user_id == uid
        if view == "last":
            where &= model.created_at < app.utc_now()
        if view == "last" and app.TRACK_CHAT_ID is not None:
            where = where & (model.chat_id == app.TRACK_CHAT_ID)
        items, count, page, pages = await page_rows(session, model, where, [model.created_at.desc(), model.id.desc()], page)
        title = {"places": "Плейсменты", "notes": "Заметки", "last": "Последние сообщения", "changes": "История изменений"}[view]
        text = card_title(title, name) + "\n"
        if count:
            text += page_label(count, page, pages) + "\n"
        if not items:
            text += "\nПока нет записей."
        for idx, item in enumerate(items, start=page * PAGE_SIZE + 1):
            if view in {"places", "notes"}:
                kind = "place" if view == "places" else "note"
                label = f"{idx}. {app.short_date(item.created_at)}" + (f" · {'Big' if item.tier == 'large' else 'Small'}" if kind == "place" else "")
                text += f"\n<b>{label}</b>\n{escape(short(entry_body(item, kind), 96))}\n"
                rows += [[button(owner, f"{idx}. Открыть", "rec", uid, extra=record_key(kind, item.id) + f"~{page}")]]
            elif view == "changes":
                labels = {"edit": "Исправлено", "delete": "Удалено", "restore": "Восстановлено"}
                actor_name = await app.resolve_name(session, item.actor_id) or f"ID {item.actor_id}"
                text += f"\n<b>{labels.get(item.action, item.action)}</b> · {app.short_date(item.created_at)}\n"
                text += f"{'Плейсмент' if item.record_kind == 'place' else 'Заметка'} · {escape(short(actor_name, 36))}\n"
                rows += [[button(owner, f"Изменение #{item.id}", "change", uid, extra=f"{item.id}~{page}")]]
            else:
                category = CATEGORIES.get(item.category, "Файл")
                text += f"\n<b>{category}</b> · {app.format_local(item.created_at)}\n{escape(short(item.text_preview, 96))}\n"
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
            title = "Плейсмент" if kind == "place" else "Заметка"
            text = card_title(title, name) + f"\n<i>{app.format_local(item.created_at)}</i>\n\n"
            if kind == "place":
                text += f"<b>{'Big' if item.tier == 'large' else 'Small'}</b>\n"
            text += escape(entry_body(item, kind))
            editable = kind == "note" or await app.placement_can_be_undone(session, item)
            if admin and editable:
                key = record_key(kind, item.id, record_version(item)) + "~" + source_page(extra)
                rows += [[button(owner, "✎ Текст", "edt", uid, extra=key),
                          button(owner, "Удалить", "del", uid, extra=key)]]
                if kind == "place":
                    rows += [[button(owner, "Изменить Small / Big", "tier", uid, extra=key)]]
            elif admin:
                text += "\n\n🔒 По этой записи уже подтверждена роль. Изменение недоступно."
    elif view == "change":
        item = await session.get(app.RecordChange, int(extra.split("~", 1)[0]))
        if not item or item.user_id != uid:
            raise ValueError("Изменение не найдено.")
        old, new = json.loads(item.before_json), json.loads(item.after_json) if item.after_json else None
        def body(values):
            return values.get("text") or values.get("comment") or f"{values.get('artist') or '—'} | {values.get('track') or '—'}"
        def excerpt(values):
            content = body(values)
            shown = escape(short(content, 600))
            return f"<blockquote expandable>{shown}</blockquote>" if text_units(content) > 200 else shown
        text = card_title(f"Изменение #{item.id}", name) + f"\n<i>{app.format_local(item.created_at)}</i>\n\n"
        text += f"<b>До</b>\n{excerpt(old)}"
        if new:
            text += f"\n\n<b>После</b>\n{excerpt(new)}"
        if item.action == "delete":
            restored = (await session.scalars(select(app.RecordChange.after_json)
                        .where(app.RecordChange.user_id == uid, app.RecordChange.action == "restore"))).all()
            already_restored = any(json.loads(payload).get("restored_from") == item.id for payload in restored if payload)
            if already_restored:
                text += "\n\n✅ Запись уже восстановлена."
            else:
                text += "\n\nЗапись удалена. Восстановление вернёт исходные дату и содержание."
                rows += [[button(owner, "Восстановить запись", "restore", uid, extra=f"{item.id}~{source_page(extra)}")]]
    elif view == "members":
        parts = extra.split(".")
        if len(parts) > 2 and parts[2] == "0":
            query = ""
        sort, raw_page = (parts[0], parts[1]) if len(parts) > 1 else ("recent", extra)
        directory_nonce = parts[2] if len(parts) > 2 else ("0" if not query else secrets.token_hex(3))
        directory = await member_directory(app, session, page=int(raw_page), query=query, sort=sort)
        text = card_title("Участники") + "\n" + page_label(directory["total"], directory["page"], directory["pages"], "в базе") + "\n"
        text += f"\nПоиск: <b>{escape(short(query, 80))}</b>" if query else (
            "\nСначала — недавно активные в группе." if sort == "recent" else "\nУчастники по имени.")
        if not directory["items"]:
            text += "\nНикого не найдено. Попробуй часть имени или username."
        rows = [[button(owner, "⌕ Найти участника", "search")]]
        if query:
            rows += [[button(owner, "Сбросить поиск", "members")]]
        for member in directory["items"]:
            rows += [[button(owner, short(member["name"], 45), "profile", member["uid"])]]
        switches = [[button(owner, ("✓ " if sort == key else "") + label, "members", extra=f"{key}.0.{directory_nonce}", style="primary" if sort == key else None)
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
        rows = [[button(owner, "Месяц", "top", extra=extra, style="primary"),
                 button(owner, "Всё время", "archive", extra="activity")]]
    elif view == "placetop":
        from tracker_monthly import placement_month_rows
        entries = await placement_month_rows(app, session)
        pages = max(1, math.ceil(len(entries) / PAGE_SIZE))
        page = max(0, min(int(extra), pages - 1))
        text = card_title("Плейсменты · лидерборд", app.current_month_label()) + "\n"
        if not entries:
            text += "\nВ этом месяце пока нет плейсментов."
        for idx, member in enumerate(entries[page * PAGE_SIZE:(page + 1) * PAGE_SIZE], page * PAGE_SIZE + 1):
            text += f"\n<b>{idx}. {escape(short(member['name'], 36))}</b> · {member['count']}\n"
            for item in member["items"][:3]:
                text += f"{escape(short(item.artist or 'Артист не указан', 45))} · {escape(short(item.track or item.comment or 'Плейсмент', 60))}\n"
                if item.collaborators:
                    text += f"На проде: {escape(short(item.collaborators, 100))}\n"
            if member["count"] > 3:
                text += f"Ещё {member['count'] - 3} — в профиле.\n"
            rows += [[button(owner, short(member["name"], 32), "profile", member["uid"])]]
        rows.insert(0, [button(owner, "Месяц", "placetop", style="primary"),
                        button(owner, "Всё время", "archive", extra="placeroles")])
        if nav := pager(owner, view, 0, page, pages):
            rows += [nav]
    elif view == "scoring":
        text = ("<b>Баллы вклада</b>\n<i>Объём участия в группе</i>\n\n"
                "Луп — <b>3</b>\nАрхив — <b>2</b>\nФайл — <b>1,5</b>\n"
                "Медиа — <b>1</b>\nСообщение — <b>0,25</b>\n\n"
                "Дополнительно: <b>2</b> за тег возможности и <b>1</b> за активный день.\n\n"
                "Баллы отражают количество вклада, а не качество музыки.")
    elif view == "metrics":
        if extra not in METRICS:
            raise ValueError("Unknown metric")
        text = "<b>Категория рейтинга</b>\n\nВыбери, что сравнить."
        for key in METRICS:
            rows += [[button(owner, ("✓ " if key == extra else "") + METRICS[key], "top", period=period, extra=key,
                             style="primary" if key == extra else None)]]
    elif view == "admin":
        text = card_title("Управление") + "\nРоли, отчёты и данные группы."
        rows = [[button(owner, "🎖 К роли", "board", extra="ready.0"), button(owner, "📋 Отчёты", "reports")],
                [button(owner, "📤 CSV", "export"), button(owner, "🛠 Сервис", "service")]]
        undo_target = await last_undo_target(app, session, owner)
        if undo_target and (isinstance(undo_target, app.Note) or await app.placement_can_be_undone(session, undo_target)):
            rows.insert(-1, [button(owner, "↩ Отменить последнее", "undo")])
    elif view == "service":
        text = card_title("Сервис") + "\nСостояние и резервные копии."
        rows = [[button(owner, "Состояние", "health"), button(owner, "Копия базы", "backup")],
                [button(owner, "Пересчитать архив", "snapshot")]]
    elif view == "reports":
        destination = await session.get(app.Setting, "report_chat")
        text = card_title("Отчёты") + "\nПрошлая неделя · понедельник — воскресенье."
        if destination:
            text += f"\n\n<b>Автоотчёт включён</b>\nПонедельник · 12:00\n<i>{escape(app.BOT_TIMEZONE)}</i>"
        else:
            text += "\n\nАвтоотчёт выключен."
        rows = [[button(owner, "Прошлая неделя", "report"), button(owner, "Настроить", "reportsetup")]]
        if destination:
            rows += [[button(owner, "Выключить автоотчёт", "reportoff")]]
    elif view == "reportsetup":
        text = ("<b>Настройка автоотчёта</b>\n\n"
                "1. Открой подключённую группу и нужный топик.\n"
                "2. Ответь на любое сообщение командой <code>/setreport</code>.\n\n"
                f"Отчёт придёт по понедельникам в 12:00 ({escape(app.BOT_TIMEZONE)}).")
    elif view == "help":
        text = help_card(admin)
        rows = [[button(owner, "Как считается активность", "rules")]]
    elif view == "rules":
        text = ("<b>Учёт активности</b>\n<i>Одно сообщение — одно действие</i>\n\n"
                "<b>Лупы</b>\nАудио и музыкальные файлы: WAV, MP3, MIDI и другие.\n\n"
                "<b>Архивы</b>\nZIP, RAR, 7Z и другие архивы.\n\n"
                "<b>Медиа</b>\nФото, видео, GIF и видеокружки.\n\n"
                "<b>Сообщения и файлы</b>\nТекст и голосовые — сообщения. Остальные документы — файлы.\n\n"
                "Тег #opportunity — дополнительный признак. Команды, боты и служебные сообщения не учитываются.")
    elif view == "report":
        text = await app.build_weekly_report(session)
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
        text = f"<b>{'Готовы к роли' if mode == 'ready' else 'Прогресс ролей'}</b>\n"
        if entries:
            text += page_label(len(entries), page, pages, "участников") + "\n"
        if not entries:
            text += "\nСейчас нет участников с достигнутым порогом." if mode == "ready" else "\nПлейсментов пока нет. Добавь первый из карточки участника."
        rows += [[button(owner, ("✓ " if mode == key else "") + label, "board", extra=f"{key}.0")
                  for key, label in (("ready", "Готовы"), ("all", "Все"))]]
        for member_id, member_name, p in entries[page * 8:(page + 1) * 8]:
            text += f"\n<b>{escape(short(member_name, 32))}</b>\nSmall {p['small_since']} / {p['small_target']} · Big {p['large_since']} / {p['large_target']}\n"
            rows += [[button(owner, short(member_name, 45), "roles", member_id, extra=f"b.{mode}.{page}")]]
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
    parents = {
        **{key: ("‹ Профиль", "profile", uid, "0") for key in ("stats", "places", "notes", "last", "roles", "history", "changes")},
        "rec": ("‹ К списку", "places" if kind == "place" else "notes", uid, source_page(extra)) if view == "rec" else None,
        "change": ("‹ История", "changes", uid, source_page(extra)) if view == "change" else None,
        "report": ("‹ Отчёты", "reports", 0, "0"),
        "reportsetup": ("‹ Отчёты", "reports", 0, "0"),
        "board": ("‹ Назад", "admin", 0, "0"),
        "reports": ("‹ Управление", "admin", 0, "0"),
        "service": ("‹ Управление", "admin", 0, "0"),
        "health": ("‹ Сервис", "service", 0, "0"),
        "metrics": ("‹ Рейтинг", "top", 0, extra),
        "scoring": ("‹ Рейтинг", "top", 0, "contribution"),
        "more": ("‹ Профиль", "profile", uid, "0"),
        "rules": ("‹ Помощь", "help", 0, "0"),
    }
    for key in ("notes", "last", "changes"):
        parents[key] = ("‹ Ещё", "more", uid, "0")
    if view == "roles" and extra.startswith("b."):
        parents[view] = ("‹ Роли группы", "board", 0, extra[2:])
    if view == "profile" and admin and uid != owner:
        parents[view] = ("‹ Участники", "members", 0, directory_extra)
    if view not in {"home", "stats"}:
        parent = parents.get(view)
        navigation = [button(owner, parent[0], parent[1], parent[2], period, parent[3])] if parent else []
        navigation += [button(owner, "⌂ Меню", "home")]
        rows += [navigation]
    context = {"directory_query": query, "directory_nonce": directory_nonce,
               "directory_extra": f"{sort}.{directory['page']}.{directory_nonce}"} if view == "members" else {}
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
        await app.reset_placement_progress(session, uid, data["kind"], actor, name,
                                          expected_cycle=data.get("role_cycle"))
        return "Выдача роли подтверждена. Начат новый цикл."
    lock_id = -3 if data["kind"] == "reportoff" else -1 if data["kind"] == "snapshot" else data["uid"]
    async with app.user_write_lock(lock_id):
        await session.commit()
        return await _save_entry(app, session, actor, data)


async def _save_entry(app, session, actor: int, data: dict) -> str:
    if data["kind"] == "snapshot":
        return f"Архив за {await app.make_snapshot(session)} сохранён."
    if data["kind"] == "reportoff":
        current = dict((await session.execute(select(app.Setting.key, app.Setting.value)
                       .where(app.Setting.key.in_(("report_chat", "report_thread"))))).all())
        if "report_destination" in data and current != data["report_destination"]:
            raise ValueError("Настройка отчёта уже изменилась. Открой отчёты заново.")
        for key in ("report_chat", "report_thread"):
            row = await session.get(app.Setting, key)
            if row:
                await session.delete(row)
        await session.commit()
        return "Автоотчёт выключен."
    uid = data["uid"]
    if data["kind"] == "activityrole":
        role, period = activity_role_input(data["text"], app.local_now().strftime("%Y-%m"))
        existing = await session.scalar(select(app.ActivityRoleAward.id).where(
            app.ActivityRoleAward.user_id == uid, app.ActivityRoleAward.role == role,
            app.ActivityRoleAward.period == period))
        if existing:
            return "Эта роль за этот месяц уже отмечена."
        history = await monthly_history(app, session, uid)
        stats = next((row for row in history if row["period"] == period), None)
        session.add(app.ActivityRoleAward(user_id=uid, role=role, period=period,
                    stats_json=json.dumps(stats, ensure_ascii=False), actor_id=actor, created_at=app.utc_now()))
        await session.commit()
        return "Роль за активность отмечена в истории."
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
    name = escape(short(data["name"], 48))
    if data["kind"] == "activityrole":
        role, period = activity_role_input(data["text"], app.local_now().strftime("%Y-%m"))
        return (f"<b>Отметить выданную роль за активность?</b>\n{name}\n\n"
                f"Роль: {escape(role)}\nМесяц: {escape(period)}\n\nЗапись попадёт в CSV. Права в Telegram не меняются.")
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
    step = "Шаг 3 из 3 · Проверка" if data["kind"] == "place" else "Шаг 2 из 2 · Проверка"
    return f"<b>{label}</b>\n{name}\n{step}\n\n{escape(data['text'])}\n\nСохранить запись?"


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
        data = {**directory_context(await state.get_data()), **data, "nonce": nonce,
                "preview_id": query.message.message_id, "started": app.utc_now().timestamp()}
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
        personal = {"home", "help", "rules", "stats", "profile", "places", "roles", "history", "rec", "archive"}
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
        if action in {"archive", "history"}:
            from tracker_monthly import activity_csv, placement_roles_csv
            if extra not in {"activity", "placeroles", "0"}:
                return
            await state.clear()
            target = (uid or None) if owner in app.ADMIN_IDS else owner
            report = placement_roles_csv if extra == "placeroles" else activity_csv
            document = await report(app, session, target)
            await query.message.answer_document(document, caption="История за всё время · CSV по месяцам")
            return
        if action == "cancel":
            data = await state.get_data()
            if data.get("nonce") != extra:
                return
            await state.clear()
            await state.set_data(directory_context(data))
            cancel_view = data.get("cancel_view", "profile")
            cancel_extra = data.get("directory_extra", "0") if cancel_view == "members" else data.get("cancel_extra", data.get("result_extra", "0"))
            await edit_screen(query, await build_screen(app, session, owner, cancel_view, data.get("uid", owner),
                                                      data.get("result_period", period), cancel_extra,
                                                      query=data.get("directory_query", ""),
                                                      directory_extra=data.get("directory_extra", "recent.0.0")))
            return
        if action in {"small", "large"}:
            data = await state.get_data()
            same_form = await state.get_state() == Entry.tier.state and data.get("nonce") == extra and data.get("uid") == uid
            if not same_form or app.utc_now().timestamp() - data.get("started", 0) > FORM_TTL:
                if not same_form:
                    return
                if same_form:
                    await state.clear()
                await edit_screen(query, Screen("Этот выбор уже устарел. Открой карточку участника и повтори.", keyboard([[button(owner, "👤 Профиль", "profile", uid)]])))
                return
        if action in {"save", "revise"}:
            data = await state.get_data()
            same_form = await state.get_state() == Entry.confirm.state and data.get("nonce") == extra and data.get("preview_id") == query.message.message_id
            if not same_form or app.utc_now().timestamp() - data.get("started", 0) > FORM_TTL:
                if not same_form:
                    return
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
            await state.set_data(directory_context(data))
            screen = await build_screen(app, session, owner, data.get("result_view",
                                        "admin" if data["kind"] in {"undo", "snapshot"} else "profile"), data["uid"],
                                        data.get("result_period", period), data.get("result_extra", "0"),
                                        directory_extra=data.get("directory_extra", "recent.0.0"))
            screen.text = f"{escape(result)}\n\n" + screen.text
            if data["kind"] in {"place", "note"} and result in {"Плейсмент сохранён.", "Заметка сохранена."}:
                target = await last_undo_target(app, session, owner)
                if target and target.user_id == data["uid"]:
                    kind = "note" if isinstance(target, app.Note) else "place"
                    key = "r." + record_key(kind, target.id, record_version(target))
                    screen.markup.inline_keyboard.insert(0, [button(owner, "↩ Отменить запись", "undo", data["uid"], extra=key)])
            await edit_screen(query, screen)
            return
        previous = await state.get_data()
        await state.clear()
        await state.set_data(directory_context(previous))
        if action == "activityrole":
            nonce = secrets.token_hex(4)
            await state.set_state(Entry.details)
            await state.update_data(kind="activityrole", uid=uid,
                name=await app.resolve_name(session, uid) or f"ID {uid}", nonce=nonce,
                started=app.utc_now().timestamp(), cancel_view="more", result_view="more")
            await edit_screen(query, Screen("<b>Выданная роль за активность</b>\n\n"
                "Отправь название роли и месяц:\n<code>Contributor | " + app.local_now().strftime("%Y-%m") + "</code>\n\n"
                "Без месяца будет указан текущий. Сохраним после подтверждения.",
                keyboard([[button(owner, "Отмена", "cancel", uid, extra=nonce)]])))
            return
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
                    "result_extra": source_page(extra), "cancel_extra": source_page(extra),
                    "nonce": nonce, "started": app.utc_now().timestamp(), "tier_only": action == "tier"}
            if action == "del":
                await preview(query, state, data)
            elif action == "tier" and kind == "place":
                await state.set_state(Entry.tier)
                await state.set_data({**directory_context(previous), **data})
                await edit_screen(query, Screen("🎯 <b>Изменить тип плейсмента</b>\n\n" + escape(data["original"]),
                    keyboard([[button(owner, "Small", "small", uid, extra=nonce), button(owner, "Big", "large", uid, extra=nonce)],
                              [button(owner, "Отмена", "cancel", uid, extra=nonce)]])))
            else:
                await state.set_state(Entry.details)
                await state.set_data({**directory_context(previous), **data})
                await edit_screen(query, Screen("✎ <b>Исправить запись</b>\n\nСейчас:\n" + escape(data["original"])
                    + "\n\nОтправь новый текст (до 500 символов).",
                    keyboard([[button(owner, "Отмена", "cancel", uid, extra=nonce)]])))
            return
        if action in {"restore", "reportoff"}:
            if action == "restore":
                change = await session.get(app.RecordChange, int(extra.split("~", 1)[0]))
                if not change or change.user_id != uid or change.action != "delete":
                    await edit_screen(query, await build_screen(app, session, owner, "changes", uid))
                    return
                text = "🧾 <b>Восстановить удалённую запись?</b>\n\nВернутся исходные содержание, участник и дата."
                data = {"kind": "restore", "change_id": change.id, "uid": uid, "text": text, "result_view": "changes", "result_extra": source_page(extra)}
            else:
                destination = dict((await session.execute(select(app.Setting.key, app.Setting.value)
                                   .where(app.Setting.key.in_(("report_chat", "report_thread"))))).all())
                data = {"kind": "reportoff", "uid": owner, "text": "📋 <b>Выключить автоотчёт?</b>\n\nРучной просмотр останется доступным.",
                        "result_view": "reports", "report_destination": destination}
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
                await edit_screen(query, Screen(card_title("Добавить плейсмент", name) + "\n\n<i>Шаг 1 из 3 · Тип</i>\n\nВыбери Small или Big.", keyboard([[button(owner, "Small", "small", uid, extra=nonce), button(owner, "Big", "large", uid, extra=nonce)], [button(owner, "Отмена", "cancel", uid, extra=nonce)]])))
                return
            if action.startswith("award"):
                kind = action.removeprefix("award")
                p = await app.placement_progress(session, uid)
                if not p["ready_small" if kind == "small" else "ready_big"]:
                    screen = await build_screen(app, session, owner, "roles", uid)
                    screen.text += "\n\nПорог этой роли ещё не достигнут."
                    await edit_screen(query, screen)
                    return
                reset_at = p["small_reset_at" if kind == "small" else "large_reset_at"]
                await preview(query, state, {"kind": kind, "uid": uid, "name": name,
                                            "result_view": "roles", "cancel_view": "roles", "result_extra": extra,
                                            "role_cycle": reset_at.isoformat() if reset_at else "initial"})
                return
            if action in {"small", "large"} and previous.get("kind") == "edit":
                await preview(query, state, {**previous, "tier": action})
                return
            kind = "note" if action == "addnote" else "place"
            nonce = secrets.token_hex(4)
            await state.set_state(Entry.details)
            await state.update_data(kind=kind, tier=action, uid=uid, name=name, started=app.utc_now().timestamp(), nonce=nonce, cancel_view="profile")
            prompt = "Отправь текст заметки (до 500 символов)." if kind == "note" else "Отправь артиста, трек и участников продакшена:\n<code>@artist | Название трека | @producer1 @producer2</code>\n\nПоследнюю часть можно пропустить. Плейсмент засчитывается человеку из открытого профиля. До 500 символов."
            step = "Шаг 1 из 2 · Текст" if kind == "note" else "Шаг 2 из 3 · Описание"
            await edit_screen(query, Screen(card_title("Новая заметка" if kind == "note" else "Новый плейсмент", name)
                + f"\n\n<i>{step}</i>\n\n{prompt}", keyboard([[button(owner, "Отмена", "cancel", uid, extra=nonce)]])))
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
                if extra == "profile" or extra.startswith("r."):
                    data.update(result_view="profile", cancel_view="profile")
                target = await last_undo_target(app, session, owner)
                if not target:
                    view = data.get("cancel_view", "admin")
                    screen = await build_screen(app, session, owner, view, uid if view == "profile" else owner)
                    screen.text = "Нет доступных записей для отмены.\n\n" + screen.text
                    await edit_screen(query, screen)
                    return
                kind = "note" if isinstance(target, app.Note) else "place"
                if extra.startswith("r."):
                    expected_kind, expected_id, expected_version = parse_record_key(extra[2:])
                    if (kind, target.id, target.user_id, record_version(target)) != (expected_kind, expected_id, uid, expected_version):
                        screen = await build_screen(app, session, owner, "profile", uid)
                        screen.text = "Эта кнопка относится к прежней записи. Открой нужную запись из списка.\n\n" + screen.text
                        await edit_screen(query, screen)
                        return
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
            screen = await build_screen(app, session, owner, action, uid, period, extra, query=directory_query,
                                        directory_extra=previous.get("directory_extra", "recent.0.0"))
            if action == "members":
                await state.set_data(screen.context)
            elif previous.get("directory_extra") and action != "home":
                await state.set_data(directory_context(previous))
        except (ValueError, PermissionError):
            screen = await build_screen(app, session, owner, "home")
        if action == "home":
            await state.clear()
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
            if data["kind"] == "activityrole":
                role, month = activity_role_input(value, app.local_now().strftime("%Y-%m"))
                value = f"{role} | {month}"
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
    async def waiting_confirmation(message: Message, state: FSMContext):
        data = await state.get_data()
        if data.get("kind") in {"place", "note", "edit"}:
            hint = "Проверь запись выше. Нажми «Сохранить» или «Отмена»."
            if not data.get("tier_only"):
                hint += " Текст можно исправить кнопкой «Изменить текст»."
        else:
            hint = "Нажми «Подтвердить» или «Отмена» в сообщении выше."
        await message.answer(hint)

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
