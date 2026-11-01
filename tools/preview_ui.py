"""Render actual bot cards with synthetic data; never opens the live database."""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
from html import escape
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
from tracker_ui import Screen, build_screen, confirmation_text, draft_buttons


CSS = """*{box-sizing:border-box}body{margin:0;padding:40px;background:#f5f5f7;color:#1d1d1f;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',sans-serif;-webkit-font-smoothing:antialiased}
h1{font-size:30px;letter-spacing:-1px;margin:0 0 8px}header p{color:#6e6e73;max-width:800px;line-height:1.5}main{display:flex;flex-wrap:wrap;align-items:flex-start;gap:28px;margin-top:28px}
.sample{width:320px;flex-shrink:0}.sample>h2{font-size:13px;color:#6e6e73;font-weight:500;margin:0 0 10px}.phone{background:#e9edf2;border:1px solid #d5d8dc;border-radius:22px;padding:14px 10px;box-shadow:0 5px 20px #00000007}
.chat{padding:0 4px 12px;display:flex;gap:8px;align-items:center;font-size:14px;font-weight:600}.avatar{background:#202125;color:white;display:grid;place-items:center;width:32px;height:32px;border-radius:50%;font-size:11px}.chat small{display:block;color:#7b8289;font-weight:400;font-size:11px}
.message{background:white;border-radius:15px 15px 15px 4px;padding:14px;font-size:15px;line-height:1.45;white-space:pre-wrap;overflow-wrap:anywhere}.message b{font-weight:600}.message i{font-style:normal;color:#7a7d83}.message code{font-size:13px}
.keys{display:grid;gap:5px;margin-top:6px}.row{display:flex;gap:5px}.row button{font-family:inherit;flex:1;min-width:0;border:0;border-radius:9px;color:#2577be;background:#ffffffbd;min-height:44px;font-size:13px;padding:9px 5px;line-height:18px;cursor:default}.label{display:block;white-space:nowrap}.dark{background:#1b252f;border-color:#25323e}.dark .chat{color:#f1f4f6}.dark .message{background:#263340;color:#f1f4f6}.dark .message i{color:#a3b0bc}.dark .row button{background:#2e4153;color:#9acdf7}
"""


def render(label: str, screen: Screen, dark: bool = False) -> str:
    rows = "".join('<div class="row">' + "".join('<button><span class="label">' + escape(b.text) + '</span></button>' for b in row) + '</div>' for row in screen.markup.inline_keyboard)
    return (f'<section class="sample"><h2>{escape(label)}</h2><div class="phone {"dark" if dark else ""}">'
            '<div class="chat"><div class="avatar">301</div><div>301secrets tracker<small>бот</small></div></div>'
            f'<div class="message">{screen.text}</div><div class="keys">{rows}</div></div></section>')


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
        for index in range(90):
            session.add(app.MsgLog(user_id=10, username="artist", chat_id=-100123,
                category=("loop", "message", "zip", "media", "files")[index % 5],
                created_at=app.utc_now() - timedelta(hours=index * 2 + 1), text_preview="Новый луп · session 01"))
        for index in range(7):
            session.add(app.Placement(user_id=10, tier="small" if index % 3 else "large",
                artist="Artist", track="Late Night", actor_id=900,
                created_at=app.utc_now() - timedelta(days=index), comment="Artist | Late Night"))
        session.add(app.Note(user_id=10, actor_id=900, text="Связаться по поводу следующего релиза.", created_at=app.utc_now()))
        await session.commit()
        views = [("home", "0"), ("profile", "0"), ("stats", "0"), ("roles", "0"), ("places", "0"),
                 ("more", "0"), ("members", "0"), ("top", "contribution"), ("admin", "0"),
                 ("reports", "0"), ("help", "0"), ("rules", "0")]
        for mode, uid in (("Обычные данные", 10), ("Длинное имя · пусто", 11)):
            for view, extra in views:
                screen = await build_screen(app, session, 900, view, uid, extra=extra)
                cards.append(render(f"{mode} / {view} · 320px", screen))
                cards.append(render(f"{mode} / {view} · тёмная", screen, True))
        for view in ("home", "profile", "stats", "roles", "help", "rules"):
            cards.append(render(f"Участник / {view}", await build_screen(app, session, 11, view)))
        draft = {"kind": "place", "uid": 10, "name": "@artist", "tier": "small", "text": "Artist | Late Night", "nonce": "preview"}
        cards.append(render("Подтверждение", Screen(confirmation_text(app, draft), draft_buttons(900, draft))))
    await engine.dispose()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                      f'<title>301 · UI review</title><style>{CSS}</style><header><h1>301 / TRACKER</h1>'
                      '<p>Проверка реальных текстов и кнопок на синтетических данных. Макет шириной 320px, светлая и тёмная темы. '
                      'В Telegram шрифты, фон и кнопки зависят от темы приложения.</p></header><main>' + ''.join(cards) + '</main></html>')
    print(f"{target} · {len(cards)} cards")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    asyncio.run(generate(parser.parse_args().output))
