"""Read-only monthly reports; importing this module never loads bot settings.

Callers enforce permissions: ``uid=None`` is the administrator's group export,
and a personal export must receive the authenticated participant's ID. CSVs
contain all recorded months; the leaderboard contains only the local calendar
month to date. Archives have no chat ID and follow monthly_history's tracked
group assumption. Manual records and awards likewise belong to that group.
"""

import csv
from collections import Counter
from datetime import datetime, timezone
import io
import json
import re
from zoneinfo import ZoneInfo

from aiogram.types import BufferedInputFile
from sqlalchemy import select, union

from tracker_insights import monthly_history


__all__ = ["activity_csv", "placements_csv", "placement_roles_csv", "placement_month_rows", "ensure_month_archives"]

_COUNTERS = ("messages", "loops", "zips", "files", "media", "opportunities", "total", "active_days")
_ACTIVITY_FIELDS = (
    "period", "user_id", "name", "row_type", *_COUNTERS,
    "award_id", "role", "role_kind", "role_date", "role_period", "role_stats_json",
)
_PLACEMENT_FIELDS = (
    "period", "placement_id", "user_id", "winner", "artist", "track", "tier",
    "collaborators", "created_at", "comment", "proof",
)
_ROLE_FIELDS = (
    "period", "award_id", "user_id", "recipient_name", "kind", "created_at",
    "recipient_role_count", "recipient_rank",
    "small_count", "large_count", "evidence_status", "placement_id", "winner",
    "primary_artists", "collaborators", "track", "tier", "placement_date",
)


def _utc(value):
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


def _local(app, value):
    return _utc(value).replace(tzinfo=timezone.utc).astimezone(ZoneInfo(app.BOT_TIMEZONE))


def _period(app, value):
    return _local(app, value).strftime("%Y-%m")


def _date(app, value):
    return _local(app, value).isoformat(timespec="seconds")


def _valid_period(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        return False
    try:
        datetime.strptime(value, "%Y-%m")
        return True
    except ValueError:
        return False


def _scope(uid):
    if uid is not None and (isinstance(uid, bool) or not isinstance(uid, int) or not 0 < uid < 2**63):
        raise ValueError("uid must be a positive Telegram user ID or None for a group export")


def _group(app):
    return (app.MsgLog.chat_id == app.TRACK_CHAT_ID if app.TRACK_CHAT_ID is not None
            else app.MsgLog.chat_id < 0)


def _handle(value):
    """Normalize stored handles without treating display names as usernames."""
    value = str(value or "").strip()
    if value.startswith("@") and re.fullmatch(r"@+[A-Za-z0-9_]+", value):
        return "@" + value.lstrip("@")
    return value


def _csv(app, fields, rows, kind, uid, now):
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer)
    writer.writerow(fields)
    for row in rows:
        # Apply the shared guard to EVERY string, including handles, comments,
        # evidence fields and role names, not only the participant's name.
        writer.writerow([
            app.safe_csv_cell(value) if isinstance(value, str) else value
            for value in (row.get(field, "") for field in fields)
        ])
    scope = "group" if uid is None else str(uid)
    return BufferedInputFile(buffer.getvalue().encode("utf-8-sig"),
                             filename=f"301tracker_{kind}_{scope}_{_period(app, now)}.csv")


async def _records(session, model, now, uid=None):
    query = select(model).where(model.user_id > 0, model.created_at < now)
    if uid is not None:
        query = query.where(model.user_id == uid)
    return (await session.scalars(query.order_by(model.created_at.desc(), model.id.desc()))).all()


async def _names(app, session, ids, now):
    """Profiles win, then tracked-group logs, then archived identities."""
    if not ids:
        return {}
    names = {}
    profiles = (await session.scalars(select(app.User).where(app.User.user_id.in_(ids)))).all()
    for profile in profiles:
        username = (profile.username or "").strip().lstrip("@")
        full_name = " ".join(filter(None, (profile.first_name, profile.last_name)))
        names[profile.user_id] = (app.display_name(username or None, full_name)
                                  if username or full_name else f"ID {profile.user_id}")
    log = app.MsgLog
    logs = (await session.execute(select(log.user_id, log.username, log.first_name).where(
        log.user_id.in_(ids), _group(app), log.created_at < now,
    ).order_by(log.created_at.desc(), log.id.desc()))).all()
    for uid, username, first_name in logs:
        names.setdefault(uid, app.display_name((username or "").lstrip("@") or None, first_name)
                         if username or first_name else f"ID {uid}")
    snapshot = app.MonthlySnapshot
    archives = (await session.execute(select(snapshot.user_id, snapshot.username, snapshot.period).where(
        snapshot.user_id.in_(ids), snapshot.period < _period(app, now),
    ).order_by(snapshot.period.desc(), snapshot.id.desc()))).all()
    for uid, username, period in archives:
        if _valid_period(period) and username:
            names.setdefault(uid, _handle("@" + username.lstrip("@")))
    for model in (app.Placement, app.Note):
        manual = (await session.execute(select(model.user_id, model.username).where(
            model.user_id.in_(ids), model.created_at < now, model.username.is_not(None),
        ).order_by(model.created_at.desc(), model.id.desc()))).all()
        for user_id, username in manual:
            if username:
                names.setdefault(user_id, _handle(username))
    return names


async def activity_csv(app, session, uid=None) -> BufferedInputFile:
    """All months, one counter row per user/month plus separate role events.

    ``row_type=activity`` carries counters. ``placement_role`` and
    ``activity_role`` carry no counters, so summing the CSV never counts award
    evidence as additional activity. Placement roles use the local award date;
    activity roles use their explicitly awarded month when recorded, with the
    local marking date as a fallback for legacy awards without a month.
    The activity-role model is optional until the application's migration lands.
    """
    _scope(uid)
    now = _utc(app.utc_now())
    current = _period(app, now)
    with session.no_autoflush:
        placement_awards = await _records(session, app.RoleAward, now, uid)
        activity_model = getattr(app, "ActivityRoleAward", None)
        activity_awards = await _records(session, activity_model, now, uid) if activity_model is not None else []
        if uid is not None:
            ids = {uid}
        else:
            known = union(
                select(app.User.user_id),
                select(app.MsgLog.user_id).where(_group(app), app.MsgLog.created_at < now),
                select(app.Placement.user_id).where(app.Placement.created_at < now),
                select(app.Note.user_id).where(app.Note.created_at < now),
            ).subquery()
            ids = set((await session.scalars(select(known.c.user_id).where(known.c.user_id > 0))).all())
            archives = (await session.execute(select(app.MonthlySnapshot.user_id, app.MonthlySnapshot.period)
                        .where(app.MonthlySnapshot.period < current))).all()
            ids.update(user_id for user_id, period in archives if user_id > 0 and _valid_period(period))
            ids.update(award.user_id for award in [*placement_awards, *activity_awards])
        names = await _names(app, session, ids, now)
        rows = []
        for user_id in sorted(ids):
            for month in await monthly_history(app, session, user_id):
                rows.append({**month, "user_id": user_id, "name": names.get(user_id, f"ID {user_id}"),
                             "row_type": "activity"})
        for award in placement_awards:
            rows.append({
                "period": _period(app, award.created_at), "user_id": award.user_id,
                "name": _handle(award.username) or names.get(award.user_id, f"ID {award.user_id}"),
                "row_type": "placement_role", "award_id": award.id,
                "role": award.kind, "role_kind": "placement", "role_date": _date(app, award.created_at),
            })
        for award in activity_awards:
            rows.append({
                "period": award.period if _valid_period(award.period) else _period(app, award.created_at), "user_id": award.user_id,
                "name": names.get(award.user_id, f"ID {award.user_id}"),
                "row_type": "activity_role", "award_id": award.id,
                "role": award.role, "role_kind": "activity", "role_date": _date(app, award.created_at),
                "role_period": award.period, "role_stats_json": award.stats_json,
            })
    rows.sort(key=lambda row: (row["period"], row["user_id"], row["row_type"], row.get("award_id", 0)), reverse=True)
    return _csv(app, _ACTIVITY_FIELDS, rows, "activity", uid, now)


async def placements_csv(app, session, uid=None) -> BufferedInputFile:
    """All eligible placements by local month; collaborators are metadata only."""
    _scope(uid)
    now = _utc(app.utc_now())
    with session.no_autoflush:
        items = await _records(session, app.Placement, now, uid)
        names = await _names(app, session, {item.user_id for item in items}, now)
        rows = [{
            "period": _period(app, item.created_at), "placement_id": item.id,
            "user_id": item.user_id, "winner": _handle(item.username) or names.get(item.user_id, f"ID {item.user_id}"),
            "artist": item.artist, "track": item.track, "tier": item.tier,
            "collaborators": getattr(item, "collaborators", None), "created_at": _date(app, item.created_at),
            "comment": item.comment, "proof": item.proof,
        } for item in items]
    return _csv(app, _PLACEMENT_FIELDS, rows, "placements", uid, now)


def _evidence(award):
    raw = getattr(award, "evidence_json", None)
    if raw is None or raw == "":
        return [(None, "legacy_not_recorded")]
    try:
        values = json.loads(raw)
    except (TypeError, ValueError):
        return [(None, "invalid_evidence")]
    if not isinstance(values, list):
        return [(None, "invalid_evidence")]
    return [(value, "recorded") if isinstance(value, dict) else (None, "invalid_evidence")
            for value in values] or [(None, "recorded_empty")]


def _evidence_text(value):
    return value if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


def _evidence_date(app, value):
    if not isinstance(value, str):
        return ""
    try:
        return _date(app, datetime.fromisoformat(value))
    except ValueError:
        return ""


async def placement_roles_csv(app, session, uid=None) -> BufferedInputFile:
    """All placement awards; one row per frozen placement evidence item.

    An award without evidence still has one row, blank credit fields and
    ``legacy_not_recorded``. Never reconstruct evidence from today's records.
    Repeated award IDs identify multiple evidence items, not extra awards.
    Recipient counts/ranks cover the exported scope, sorted by cumulative award
    count descending and then recipient ID. A personal export has rank 1.
    """
    _scope(uid)
    now = _utc(app.utc_now())
    with session.no_autoflush:
        awards = await _records(session, app.RoleAward, now, uid)
        counts = Counter(award.user_id for award in awards)
        ranks = {user_id: rank for rank, user_id in enumerate(
            sorted(counts, key=lambda user_id: (-counts[user_id], user_id)), start=1)}
        awards.sort(key=lambda award: ranks[award.user_id])
        names = await _names(app, session, {award.user_id for award in awards}, now)
        rows = []
        for award in awards:
            base = {
                "period": _period(app, award.created_at), "award_id": award.id, "user_id": award.user_id,
                "recipient_name": _handle(award.username) or names.get(award.user_id, f"ID {award.user_id}"),
                "kind": award.kind, "created_at": _date(app, award.created_at),
                "recipient_role_count": counts[award.user_id], "recipient_rank": ranks[award.user_id],
                "small_count": award.small_count, "large_count": award.large_count,
            }
            for evidence, status in _evidence(award):
                row = {**base, "evidence_status": status}
                if evidence is not None:
                    row.update({
                        "placement_id": _evidence_text(evidence.get("id")),
                        "winner": _handle(_evidence_text(evidence.get("winner", evidence.get("username")))),
                        "primary_artists": _evidence_text(evidence.get("artist")),
                        "collaborators": _evidence_text(evidence.get("collaborators")),
                        "track": _evidence_text(evidence.get("track")),
                        "tier": _evidence_text(evidence.get("tier")),
                        "placement_date": _evidence_date(app, evidence.get("created_at")),
                    })
                rows.append(row)
    return _csv(app, _ROLE_FIELDS, rows, "placement_roles", uid, now)


async def placement_month_rows(app, session):
    """Return all current-month winners sorted by count, then name and ID.

    Result: ``[{uid, name, count, items: [Placement, ...]}]``. Names and model
    values are plain text; the UI owns truncation, HTML escaping and paging.
    Each row counts only Placement.user_id; artist/collaborator credits never
    create winners or extra counts. Used placements remain eligible.
    """
    now = _utc(app.utc_now())
    start = _utc(_local(app, now).replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    with session.no_autoflush:
        model = app.Placement
        items = (await session.scalars(select(model).where(
            model.user_id > 0, model.created_at >= start, model.created_at < now,
        ).order_by(model.created_at.desc(), model.id.desc()))).all()
        names = await _names(app, session, {item.user_id for item in items}, now)
        winners = {}
        for item in items:
            entry = winners.setdefault(item.user_id, {
                "uid": item.user_id, "name": names.get(item.user_id) or _handle(item.username) or f"ID {item.user_id}",
                "count": 0, "items": [],
            })
            entry["count"] += 1
            entry["items"].append(item)
    return sorted(winners.values(), key=lambda row: (-row["count"], row["name"].casefold(), row["uid"]))


async def ensure_month_archives(app, session) -> int:
    """Stage missing/extended archives for ALL completed local raw-log months.

    Returns the number of user/month snapshots inserted or updated. Does not
    acquire a lock, commit, roll back, or delete logs. The caller must serialize
    snapshot/retention writers with its own lock and commit these changes before
    deleting raw logs. Normal session autoflush makes repeated calls idempotent
    even before commit. Existing larger archives survive partial log retention;
    current/future months and foreign/private chat logs are never archived.
    """
    now = _utc(app.utc_now())
    start = _utc(_local(app, now).replace(day=1, hour=0, minute=0, second=0, microsecond=0))
    log = app.MsgLog
    logs = (await session.execute(select(log.user_id, log.username, log.created_at).where(
        log.user_id > 0, _group(app), log.created_at < start,
    ).order_by(log.created_at.desc(), log.id.desc()))).all()
    periods = {}
    usernames = {}
    for user_id, username, stamp in logs:
        period = _period(app, stamp)
        periods.setdefault(user_id, set()).add(period)
        usernames.setdefault((user_id, period), username)
    if not periods:
        return 0
    model = app.MonthlySnapshot
    existing = {}
    for archive in (await session.scalars(select(model).where(
        model.user_id.in_(periods), model.period < _period(app, now),
    ).order_by(model.id.desc()))).all():
        existing.setdefault((archive.user_id, archive.period), archive)
    saved = 0
    for user_id, raw_periods in periods.items():
        for month in await monthly_history(app, session, user_id):
            period = month["period"]
            if period not in raw_periods:
                continue
            archive = existing.get((user_id, period))
            if archive is None:
                archive = model(user_id=user_id, username=usernames[(user_id, period)], **month)
                session.add(archive)
                existing[(user_id, period)] = archive
                saved += 1
            elif any(int(getattr(archive, field) or 0) != month[field] for field in _COUNTERS):
                for field in _COUNTERS:
                    setattr(archive, field, month[field])
                saved += 1
    return saved
