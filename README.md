# 301secrets Tracker

Telegram-бот для учёта активности в группе: статистика, роли, отчёты, профили, места и резервные копии базы данных.

## Требования

- Python 3.11 или новее
- Telegram-бот, созданный через [@BotFather](https://t.me/BotFather)
- ID администраторов и отслеживаемого чата

## Установка

```bash
git clone https://github.com/sixsensegoeshard/sixsense-stash.git
cd sixsense-stash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

На Windows активация окружения выглядит так:

```powershell
.venv\Scripts\Activate.ps1
```

## Настройка `.env`

Открой `.env` и заполни:

| Переменная | Назначение |
| --- | --- |
| `BOT_TOKEN` | Токен Telegram-бота от BotFather |
| `ADMIN_IDS` | ID администраторов через запятую |
| `TRACK_CHAT_ID` | ID группы или супергруппы для отслеживания |
| `DB_URL` | URL базы данных; по умолчанию SQLite-файл `tracker_v3.db` |
| `BOT_TIMEZONE` | Часовой пояс для отчётов, например `Europe/Moscow` |
| `REQUIRE_TRACK_CHAT_ID` | `1` — требовать чат, `0` — разрешить запуск без него |

Дополнительные пороги статистики находятся в `.env.example`.

## Настройка Telegram

1. Создай бота через BotFather и скопируй токен в `BOT_TOKEN`.
2. Добавь бота в нужную группу.
3. Выдай ему права администратора, если боту нужно видеть обычные сообщения.
4. Узнай числовой ID группы и укажи его в `TRACK_CHAT_ID`.
5. Узнай свой Telegram ID и укажи его в `ADMIN_IDS`.

Не публикуй `.env`: в нём находится секретный токен бота. Этот файл уже добавлен в `.gitignore`.

## Запуск

```bash
source .venv/bin/activate
python trackerbot.py
```

Остановка — `Ctrl+C`.

## Обновление проекта

После изменения кода:

```bash
git add .
git commit -m "Описание изменений"
git push
```

Чтобы получить изменения с GitHub:

```bash
git pull
```

## Структура

- `trackerbot.py` — исходный код бота.
- `.env.example` — пример настроек без секретов.
- `requirements.txt` — зависимости Python.
- `tracker_v3.db` — локальная база данных, не загружается на GitHub.
