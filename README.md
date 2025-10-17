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

Если Docker и Git уже установлены:
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh)"
```

Полная автоустановка на чистой Ubuntu/Debian (поставит curl, git, Docker и запустит скрипт):
```bash
bash -c 'set -e; apt-get update; apt-get install -y curl git; curl -fsSL https://get.docker.com | sh; curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh | bash'
```

Скрипт:
- установит/обновит код в `/opt/xmplus`,
- спросит `BOT_TOKEN` и `OWNER_CHAT_ID`,
- создаст `.env`,
- соберёт и запустит контейнеры через Docker Compose.

Логи:
```bash
cd /opt/xmplus && docker compose logs -f xmplus
```
(если у вас классический docker-compose — замените на `docker-compose logs -f xmplus`)

## Команды бота (в меню Telegram)
Бот регистрирует команды через Bot API — они появляются в «кнопке меню» (иконка с точками в Telegram):
- `/start` — запуск бота
- `/help` — справка по командам
- `/add`, `/list`, `/remove`, `/import`, `/export`, `/next`, `/status`, `/settings` — заготовки под функционал

## Примечания
- По умолчанию используется SQLite `./data/data.db`. Можно заменить на Postgres, указав `DATABASE_URL=postgresql+psycopg://user:pass@host:5432/dbname`.
- Предупреждение про устаревший ключ `version` в `docker-compose.yml` можно игнорировать или удалить строку `version:` из файла.