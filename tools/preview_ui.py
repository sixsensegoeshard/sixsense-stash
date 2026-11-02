"""Render actual bot cards with synthetic data; never opens the live database."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta
from html import escape
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with patch.dict(os.environ, {
    "BOT_TOKEN": "123456:preview-only", "ADMIN_IDS": "900", "TRACK_CHAT_ID": "-100123",
    "BOT_TIMEZONE": "Europe/Moscow", "DB_URL": "sqlite+aiosqlite:///:memory:",
}, clear=True), patch("dotenv.load_dotenv", return_value=False):
    import trackerbot as app

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from tracker_ui import METRICS, PERIODS, Screen, build_screen, confirmation_text, draft_buttons


CSS = """*{box-sizing:border-box}body{margin:0;padding:40px;background:#f5f5f7;color:#1d1d1f;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;-webkit-font-smoothing:antialiased}
h1{font-size:30px;letter-spacing:-1px;margin:0 0 8px}header p{color:#6e6e73;max-width:800px;line-height:1.5}main{display:flex;flex-wrap:wrap;align-items:flex-start;gap:28px;margin-top:28px}
.sample{width:320px;flex-shrink:0}.sample>h2{font-size:13px;color:#6e6e73;font-weight:500;margin:0 0 10px}.phone{background:#e9edf2;border:1px solid #d5d8dc;border-radius:22px;padding:14px 10px;box-shadow:0 5px 20px #00000007}
.chat{padding:0 4px 12px;display:flex;gap:8px;align-items:center;font-size:14px;font-weight:600}.avatar{background:#202125;color:white;display:grid;place-items:center;width:32px;height:32px;border-radius:50%;font-size:11px}.chat small{display:block;color:#7b8289;font-weight:400;font-size:11px}
.message{background:white;border-radius:15px 15px 15px 4px;padding:12px;font-size:15px;line-height:1.35;white-space:pre-wrap;overflow-wrap:anywhere}.message b{font-weight:600}.message i{font-style:italic;color:inherit}.message code{font-size:13px}
.keys{display:grid;gap:5px;margin-top:6px}.row{display:flex;gap:5px}.row button{font-family:inherit;flex:1;min-width:0;border:0;border-radius:9px;color:#2577be;background:#ffffffbd;min-height:44px;font-weight:600;font-size:13px;padding:9px 5px;line-height:18px;cursor:default}.label{display:block;white-space:nowrap}.dark{background:#17212b;border-color:#25323e}.dark .chat{color:#f1f4f6}.dark .message{background:#182633;color:#f1f4f6}.dark .row button{background:#202f3e;color:#f1f4f6}
.message blockquote{display:inline-block;vertical-align:top;width:100%;margin:0;padding:6px 9px;border-left:3px solid #4c96cc;border-radius:5px;background:#4c96cc15;white-space:pre-wrap}.message blockquote[expandable]{max-height:100px;overflow:hidden}.row button[data-style=primary]{background:#337cba;color:white}.row button[data-style=success]{background:#3e9162;color:white}.row button[data-style=danger]{background:#b94e54;color:white}
.bubble{width:max-content;max-width:100%;min-width:min(220px,100%)}.controls{display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin:18px 0}.controls select,.controls input{font:inherit;padding:7px;border:1px solid #bbb;border-radius:6px}.audit-result{font-size:14px;color:#666}
"""


def render(label: str, screen: Screen, dark: bool = False) -> str:
    rows = "".join('<div class="row">' + "".join(f'<button data-style="{b.style or "default"}"><span class="label">' + escape(b.text) + '</span></button>' for b in row) + '</div>' for row in screen.markup.inline_keyboard)
    return (f'<section class="sample"><h2>{escape(label)}</h2><div class="phone {"dark" if dark else ""}">'
            '<div class="chat"><div class="avatar">301</div><div>301secrets tracker<small>бот</small></div></div>'
            f'<div class="bubble"><div class="message">{screen.text}</div><div class="keys">{rows}</div></div></div></section>')


async def generate(target: Path) -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.run_sync(app.Base.metadata.create_all)
    cards = []
    async with sessions() as session:
        await app.upsert_user(session, 900, "sixsense", "Admin")
        await app.upsert_user(session, 10, "artist", "Artist")
        await app.upsert_user(session, 11, None, "Очень длинное имя 👨‍👩‍👧‍👦 " * 8)
        await app.upsert_user(session, 13, "empty", "Empty")
        await app.upsert_user(session, 14, None, "W" * 120)
        for index in range(90):
            session.add(app.MsgLog(user_id=10, username="artist", chat_id=-100123,
                category=("loop", "message", "zip", "media", "files")[index % 5],
                created_at=app.utc_now() - timedelta(hours=index * 2 + 1), text_preview="Новый луп · session 01"))
        for index in range(7):
            session.add(app.Placement(user_id=10, tier="small" if index % 3 else "large",
                artist="@singer", track="Late Night", collaborators="@producer1 @producer2", actor_id=900,
                created_at=app.utc_now() - timedelta(days=index), comment="Artist | Late Night"))
        session.add(app.Note(user_id=10, actor_id=900, text="Связаться по поводу следующего релиза.", created_at=app.utc_now()))
        for index in range(6):
            session.add(app.Note(user_id=14, actor_id=900, text="<&>" * 166, created_at=app.utc_now() - timedelta(minutes=index)))
        for uid in range(20, 32):
            await app.upsert_user(session, uid, f"producer_{uid}", "Producer")
            for index in range(uid - 19):
                session.add(app.MsgLog(user_id=uid, username=f"producer_{uid}", chat_id=-100123,
                    category="loop", created_at=app.utc_now() - timedelta(minutes=index + 1)))
        change = app.RecordChange(user_id=10, record_kind="note", record_id=999, action="edit", actor_id=900,
            before_json=json.dumps({"text": "Старый текст " * 35}), after_json=json.dumps({"text": "Новый текст " * 35}), created_at=app.utc_now())
        session.add(change)
        await app.upsert_user(session, 12, "raybanglassesonmyface", "Artist")
        for stamp in (datetime(2026, 10, 31, 12), datetime(2026, 11, 1, 1)):
            session.add(app.MsgLog(user_id=12, chat_id=-100123, category="media", created_at=stamp))
        await session.commit()
        screenshot_time = datetime(2026, 11, 1, 1, 14)
        with patch.object(app, "utc_now", return_value=screenshot_time), patch.object(app, "local_now", return_value=app.local_from_utc_naive(screenshot_time)):
            screenshot = await build_screen(app, session, 900, "stats", 12)
            cards.append(render("Сценарий со скриншота / тёмная · 320px", screenshot, True))
            cards.append(render("Сценарий со скриншота / светлая · 320px", screenshot))
        views = [("home", "0"), ("profile", "0"), ("stats", "0"), ("roles", "0"), ("places", "0"),
                 ("more", "0"), ("members", "0"), ("placetop", "0"), ("top", "contribution"), ("metrics", "total"),
                 ("scoring", "contribution"), ("admin", "0"), ("board", "ready.0"), ("service", "0"),
                 ("health", "0"), ("reports", "0"), ("reportsetup", "0"), ("report", "0"),
                 ("help", "0"), ("rules", "0"), ("history", "0"), ("notes", "0"), ("last", "0"), ("changes", "0")]
        for mode, uid in (("Обычные данные", 10), ("Длинное имя · пусто", 11), ("Широкие буквы · длинный текст", 14)):
            for view, extra in views:
                screen = await build_screen(app, session, 900, view, uid, extra=extra)
                cards.append(render(f"{mode} / {view} · 320px", screen))
                cards.append(render(f"{mode} / {view} · тёмная", screen, True))
        for metric in METRICS:
            for period in PERIODS:
                screen = await build_screen(app, session, 900, "top", 10, period=period, extra=metric)
                cards.append(render(f"Рейтинг / {metric} / {period}", screen, True))
        for view, extra in (("rec", "p.1"), ("change", str(change.id))):
            cards.append(render(f"Детали / {view}", await build_screen(app, session, 900, view, 10, extra=extra), True))
        for view in ("home", "profile", "stats", "roles", "help", "rules"):
            cards.append(render(f"Участник / {view}", await build_screen(app, session, 11, view)))
        draft = {"kind": "place", "uid": 10, "name": "@artist", "tier": "small", "text": "Artist | Late Night", "nonce": "preview"}
        cards.append(render("Подтверждение", Screen(confirmation_text(app, draft), draft_buttons(900, draft))))
        for kind in ("note", "delete", "edit", "small", "restore", "undo", "snapshot", "reportoff"):
            data = {**draft, "kind": kind, "text": "Текст записи " * 38, "original": "Исходный текст " * 30,
                    "record_kind": "place", "tier_only": False}
            if kind in {"undo", "snapshot", "restore", "reportoff"}:
                data["text"] = "<b>Подтвердить действие?</b>\nПроверка сценария без сохранения."
            cards.append(render(f"Подтверждение / {kind}", Screen(confirmation_text(app, data), draft_buttons(900, data)), True))
    await engine.dispose()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                      f'<title>301 · UI review</title><style>{CSS}</style><header><h1>301 / TRACKER</h1>'
                      '<p>Проверка реальных текстов и кнопок на синтетических данных. Макет шириной 320px, светлая и тёмная темы. '
                      'Ширина сообщения подстраивается под содержимое, как в Telegram. Это проверочный макет, а не сам клиент.</p>'
                      '<div class="controls"><label>Ширина <select id="width"><option>260</option><option selected>320</option><option>375</option><option>420</option></select></label>'
                      '<label>Шрифт <select id="font"><option selected>15</option><option>17</option><option>19</option></select></label>'
                      '<label>Найти экран <input id="filter" placeholder="top, notes, confirm…"></label><span class="audit-result" id="result"></span></div></header><main>'
                      + ''.join(cards) + '</main><script>function update(){const w=document.querySelector("#width").value;const f=document.querySelector("#font").value;'
                      'const q=document.querySelector("#filter").value.toLowerCase();document.querySelectorAll(".sample").forEach(e=>{e.style.width=w+"px";e.style.display=e.querySelector("h2").innerText.toLowerCase().includes(q)?"block":"none"});'
                      'document.querySelectorAll(".message").forEach(e=>e.style.fontSize=f+"px");document.querySelectorAll(".row button").forEach(e=>e.style.fontSize=(f-2)+"px");'
                      'const bad=[...document.querySelectorAll(".label,.message")].filter(e=>e.offsetWidth&&e.scrollWidth>e.clientWidth+1);document.querySelector("#result").textContent="Выходов за границы: "+bad.length;}'
                      'document.querySelectorAll(".controls input,.controls select").forEach(e=>e.addEventListener("input",update));update();</script></html>')
    print(f"{target} · {len(cards)} cards")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(generate(parser.parse_args().output))
