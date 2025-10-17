# XMPLUS_info

Telegram expiry-notifier bot (XMPLUS) — репозиторий для развёртывания бота, который уведомляет об истекающих сроках.

Краткое содержание:
- `app/` — основной код бота (aiogram, APScheduler, SQLAlchemy)
- `scripts/import_csv.py` — импорт CSV в БД
- `deploy/` — systemd unit пример
- `data/` — CSV и БД (не храните секреты в репозитории)
- `.env.example` — пример переменных окружения
- `Dockerfile`, `docker-compose.yml` — сборка и запуск

## Автоустановка (одной командой)

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS/main/install.sh)"
```

Скрипт:
- установит/обновит код в `/opt/xmplus`,
- спросит `BOT_TOKEN` и `OWNER_CHAT_ID`,
- создаст `.env`,
- запустит контейнеры `docker compose up --build -d`.

Логи: 
```bash
cd /opt/xmplus && docker compose logs -f xmplus
```
(если у вас классический docker-compose — замените на `docker-compose logs -f xmplus`)

## Ручной запуск (если без скрипта)
1) Создать `.env` (см. `.env.example`), важные переменные:
```
BOT_TOKEN=...
OWNER_CHAT_ID=...
TIMEZONE=Europe/Moscow
DATABASE_URL=sqlite+aiosqlite:///./data/data.db
```

2) Собрать и запустить:
```bash
docker compose up --build -d
```

## Команды бота (в меню Telegram)
Бот регистрирует команды через Bot API, они появляются в «кнопке меню» (иконка с точками в Telegram):
- `/start` — запуск бота
- `/help` — справка по командам
- `/add` — добавить запись (заглушка)
- `/list` — список записей (заглушка)
- `/remove` — удалить запись (заглушка)
- `/import` — импорт CSV (заглушка)
- `/export` — экспорт CSV (заглушка)
- `/next` — ближайшие истечения (заглушка)
- `/status` — статус бота
- `/settings` — настройки (заглушка)

## Примечания
- По умолчанию используется SQLite в `./data/data.db`. Можно заменить на Postgres, указав `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname`.
- Если увидите предупреждение в `docker compose` про ключ `version` в `docker-compose.yml` — его можно удалить, это устаревший синтаксис и не влияет на работу.