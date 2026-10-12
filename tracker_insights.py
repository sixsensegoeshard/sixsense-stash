"""Read-only directory and activity queries for the tracker's button UI.

The application is injected to avoid importing configuration or opening its DB.
Activity windows are half-open [start, end), with end never later than utc_now.
Returned names are plain text; callers should escape them when rendering HTML.
"""

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo

from sqlalchemy import case, func, literal, select, union, union_all


__all__ = ["member_directory", "monthly_history", "activity_comparison"]

_MONTH_FIELDS = (
    "messages", "loops", "zips", "files", "media", "opportunities", "total",
    "active_days",
)
_CATEGORIES = {
    "message": "messages", "loop": "loops", "zip": "zips",
    "files": "files", "media": "media",
}


def _utc(value):
    """The tracker stores naive UTC datetimes, including in SQLite."""
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _month_start(app, now):
    local = now.replace(tzinfo=timezone.utc).astimezone(ZoneInfo(app.BOT_TIMEZONE))
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _group(app):
    # Match the core pivot's explicit compatibility mode when no chat is set.
    if app.TRACK_CHAT_ID is None:
        return app.MsgLog.chat_id < 0
    return app.MsgLog.chat_id == app.TRACK_CHAT_ID


def _username(value):
    return (value or "").strip().lstrip("@")


async def member_directory(app, session, page=0, query="", sort="recent", page_size=8):
    """Return known positive IDs, names, current-month totals and group recency.

    Profiles are authoritative (a removed username is not resurrected). Legacy
    names fall back to the latest group log, then the latest named manual entry.
    ``last_activity`` is naive UTC or None; private commands never affect it.
    Sorts are recent, name and active (current local month); ties use name + ID.
    Pages are zero-based and clamped, with one empty page for an empty result.
    The number of SQL queries is fixed, independent of the number of members.
    """
    if sort not in {"recent", "name", "active"}:
        raise ValueError("Unknown directory sort")
    page_size = int(page_size)
    if page_size < 1:
        raise ValueError("page_size must be positive")
    query = (query or "").strip()
    now = _utc(app.utc_now())
    month_start = _utc(_month_start(app, now))
    log = app.MsgLog

    with session.no_autoflush:
        known = union(
            select(app.User.user_id),
            select(log.user_id).where(_group(app)),
            select(app.Placement.user_id),
            select(app.Note.user_id),
        ).subquery()
        ids = (await session.scalars(
            select(known.c.user_id).where(known.c.user_id > 0)
        )).all()
        profiles = {
            row.user_id: row for row in (await session.execute(select(
                app.User.user_id, app.User.username,
                app.User.first_name, app.User.last_name,
            ))).all()
        }
        activity = {
            row.user_id: row for row in (await session.execute(select(
                log.user_id,
                func.max(log.created_at).label("last_activity"),
                func.sum(case((log.created_at >= month_start, 1), else_=0)).label("total"),
            ).where(_group(app), log.created_at < now).group_by(log.user_id))).all()
        }
        ranked = select(
            log.user_id, log.username, log.first_name,
            func.row_number().over(
                partition_by=log.user_id,
                order_by=(log.created_at.desc(), log.id.desc()),
            ).label("position"),
        ).where(_group(app), log.created_at < now).subquery()
        legacy = {
            row.user_id: row for row in (await session.execute(
                select(ranked).where(ranked.c.position == 1)
            )).all()
        }
        manual = union_all(*[
            select(
                model.user_id, model.username, model.created_at, model.id,
                literal(priority).label("source"),
            ).where(model.username.is_not(None))
            for priority, model in enumerate((app.Placement, app.Note))
        ]).subquery()
        manual_names = {}
        for row in (await session.execute(select(manual).order_by(
            manual.c.created_at.desc(), manual.c.id.desc(), manual.c.source,
        ))).all():
            if _username(row.username):
                manual_names.setdefault(row.user_id, row.username)

    numeric = query.isdecimal() or (
        query.startswith(("+", "-")) and query[1:].isdecimal()
    )
    try:
        target_id = int(query) if numeric else None
    except ValueError:
        target_id = 0
    needle = query.casefold()
    items = []
    for uid in ids:
        profile = profiles.get(uid)
        historical = legacy.get(uid)
        if profile is not None:
            username = _username(profile.username)
            full_name = " ".join(filter(None, (profile.first_name, profile.last_name)))
        elif historical is not None:
            username = _username(historical.username)
            full_name = historical.first_name or ""
        else:
            username = _username(manual_names.get(uid))
            full_name = ""
        name = app.display_name(username or None, full_name) if username or full_name else f"ID {uid}"
        if query:
            if numeric:
                matches = target_id == uid
            elif query.startswith("@"):
                matches = bool(username) and username.casefold() == needle[1:]
            else:
                matches = needle in full_name.casefold() or needle in username.casefold()
            if not matches:
                continue
        row = activity.get(uid)
        items.append({
            "uid": uid, "name": name, "total": int(row.total) if row else 0,
            "last_activity": row.last_activity if row else None,
        })

    items.sort(key=lambda item: (item["name"].casefold(), item["uid"]))
    if sort == "recent":
        items.sort(key=lambda item: item["last_activity"] or datetime.min, reverse=True)
    elif sort == "active":
        items.sort(key=lambda item: item["total"], reverse=True)
    total = len(items)
    pages = max(1, (total + page_size - 1) // page_size)
    page = max(0, min(int(page), pages - 1))
    return {
        "items": items[page * page_size:(page + 1) * page_size],
        "total": total, "page": page, "pages": pages, "query": query, "sort": sort,
    }


def _empty_month(period):
    return {"period": period, **dict.fromkeys(_MONTH_FIELDS, 0)}


async def monthly_history(app, session, uid):
    """Return local monthly counts newest first, always including this month.

    Any raw group logs for a user/month override that entire archived month.
    Snapshots have no chat ID in this schema and must be the tracked group's
    archives. Only completed, valid archive months are used. Unknown categories
    go to files, matching user_pivot; opportunities are an overlapping counter.
    """
    now = _utc(app.utc_now())
    tz = ZoneInfo(app.BOT_TIMEZONE)
    current = now.replace(tzinfo=timezone.utc).astimezone(tz).strftime("%Y-%m")
    log = app.MsgLog
    with session.no_autoflush:
        rows = (await session.execute(select(
            log.created_at, log.category, log.opportunity,
        ).where(log.user_id == uid, _group(app), log.created_at < now))).all()
        snapshots = (await session.scalars(select(app.MonthlySnapshot).where(
            app.MonthlySnapshot.user_id == uid,
        ).order_by(app.MonthlySnapshot.id.desc()))).all()

    months = {current: _empty_month(current)}
    days = {}
    for stamp, category, opportunity in rows:
        local = _utc(stamp).replace(tzinfo=timezone.utc).astimezone(tz)
        period = local.strftime("%Y-%m")
        month = months.setdefault(period, _empty_month(period))
        month[_CATEGORIES.get(category, "files")] += 1
        month["total"] += 1
        month["opportunities"] += int(bool(opportunity))
        days.setdefault(period, set()).add(local.date())
    for period, dates in days.items():
        months[period]["active_days"] = len(dates)
    for snapshot in snapshots:
        period = snapshot.period or ""
        if period in months or period >= current or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", period):
            continue
        try:
            datetime.strptime(period, "%Y-%m")
        except ValueError:
            continue
        months[period] = {
            "period": period,
            **{field: int(getattr(snapshot, field) or 0) for field in _MONTH_FIELDS},
        }
    return [months[period] for period in sorted(months, reverse=True)]


async def activity_comparison(app, session, uid, period):
    """Compare total activity in matching half-open windows, using user_pivot.

    Month compares elapsed UTC duration since local month start with the same
    duration from previous local month start, capped at that month's end.
    With a zero baseline percent is None (including when both counts are zero).
    """
    if period == "all":
        return None
    if period not in {"7d", "30d", "month"}:
        raise ValueError("Unknown comparison period")
    now = _utc(app.utc_now())
    if period == "month":
        local_start = _month_start(app, now)
        current_start = _utc(local_start)
        previous_start = _utc((local_start - timedelta(days=1)).replace(day=1))
        previous_end = min(previous_start + (now - current_start), current_start)
        label = "к сопоставимой части прошлого месяца"
    else:
        duration = timedelta(days=7 if period == "7d" else 30)
        current_start = now - duration
        previous_start = current_start - duration
        previous_end = current_start
        label = "к предыдущим 7 дням" if period == "7d" else "к предыдущим 30 дням"
    with session.no_autoflush:
        current = await app.user_pivot(session, current_start, now, user_id=uid)
        previous = await app.user_pivot(session, previous_start, previous_end, user_id=uid)
    current_total = int(current.get(uid, {}).get("total", 0))
    previous_total = int(previous.get(uid, {}).get("total", 0))
    delta = current_total - previous_total
    return {
        "current": current_total, "previous": previous_total, "delta": delta,
        "percent": delta * 100 / previous_total if previous_total else None,
        "label": label,
    }
