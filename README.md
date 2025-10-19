# XMPLUS_info

Телеграм-бот уведомлений об истечении сроков (XMPLUS).

## Автоустановка

Рекомендуемый запуск (stdin — ваш терминал, вопросы будут заданы корректно):
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh)"
```

Альтернатива:
```bash
curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh -o install.sh
bash install.sh
```

Полная автоустановка на чистой Ubuntu/Debian:
```bash
bash -c 'set -e; apt-get update; apt-get install -y curl git; curl -fsSL https://get.docker.com | sh; curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh -o /tmp/install.sh; bash /tmp/install.sh'
```

Неинтерактивно (через переменные окружения):
```bash
BOT_TOKEN=XXX OWNER_CHAT_ID=123456 bash -c "$(curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh)"
```

Логи после установки:
```bash
cd /opt/xmplus && docker compose logs -f xmplus
```

## Команды бота (в меню Telegram)
Бот регистрирует команды (кнопка-меню — иконка с точками):
- `/start` — запуск
- `/help` — справка
- `/add` — добавить запись: `/add Название; 2025-12-31`
- `/list` — список записей
- `/remove <id>` — удалить запись
- `/next` — ближайшие истечения
- `/status` — статус бота

## CSV импорт
Скрипт: `scripts/import_csv.py`

Формат CSV (UTF-8):
```csv
title,expires_at,chat_id
Домен example.com,2025-12-31,
Сертификат api.example.com,31.01.2026,123456789
```
Поддерживаемые форматы дат: `YYYY-MM-DD`, `DD.MM.YYYY`, и т.п.

Запуск:
```bash
docker compose exec xmplus python scripts/import_csv.py /app/data/clients.csv
```
(файл положить в `./data/clients.csv` на хосте)

## Настройки
Переменные `.env`:
```
BOT_TOKEN=...
OWNER_CHAT_ID=...
DEALER_NAME=main
TIMEZONE=Europe/Moscow
CHECK_INTERVAL_MINUTES=1
NOTIFY_EVERY_MINUTES=180
MAX_NOTIFICATIONS=9
DATABASE_URL=sqlite+aiosqlite:///./data/data.db
```

## Заметки
- По умолчанию — SQLite в `./data/data.db`. Для Postgres: `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname`.
- Предупреждение про `version:` в `docker-compose.yml` можно игнорировать или удалить строку.