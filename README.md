```md
# XMPLUS_info

Telegram expiry-notifier bot (XMPLUS) — репозиторий для развертывания бота, который уведомляет об истекающих сроках.

Краткое содержание:
- app/ — основной код бота (aiogram, APScheduler, SQLAlchemy)
- scripts/import_csv.py — импорт CSV в БД
- deploy/ — systemd unit пример
- data/ — CSV и БД (не храните секреты в репозитории)
- .env.example — пример переменных окружения

Quickstart:
1. Создать виртуальное окружение и установить зависимости:
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt

2. Создать .env на основе .env.example и заполнить BOT_TOKEN и OWNER_CHAT_ID (не коммитить .env).

3. Импорт CSV (если есть):
   python scripts/import_csv.py data/clients.csv

4. Запустить бота:
   python -m app.main

5. Рекомендуется запускать под systemd или в Docker (см. Dockerfile / deploy/)
```