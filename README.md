# XMPLUS_info

Телеграм-бот управления подписками и уведомлений об истечении сроков (XMPLUS).

## Требования

- Ubuntu 20.04–24.04 или Debian 10–13
- Docker и Docker Compose (установщик поставит автоматически)

## Быстрая установка

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh)"
```

Установщик предложит два варианта:

1. **Новая установка** — спросит BOT_TOKEN, OWNER_CHAT_ID и создаст чистую базу.
2. **Восстановление из бэкапа** — восстановит базу данных, .env и настройки из ZIP-архива.

## Восстановление из бэкапа при переустановке

Если вы переустанавливаете сервер и у вас есть бэкап (ZIP-архив из бота), выполните следующие шаги.

### Шаг 1: Загрузите бэкап на сервер

Из PowerShell (Windows):
```powershell
scp C:\Users\Berdi\Documents\GitHub\XMPLUS_info\backup\xmplus_backup_XXXXXXXX_XXXXXX.zip root@YOUR_SERVER_IP:/opt/xmplus/backup/
```

Если папка ещё не существует (первый запуск), создайте её:
```bash
ssh root@YOUR_SERVER_IP "mkdir -p /opt/xmplus/backup"
```

А затем загрузите:
```powershell
scp C:\path\to\xmplus_backup_XXXXXXXX_XXXXXX.zip root@YOUR_SERVER_IP:/opt/xmplus/backup/
```

Из Linux/Mac:
```bash
scp xmplus_backup_XXXXXXXX_XXXXXX.zip root@YOUR_SERVER_IP:/opt/xmplus/backup/
```

### Шаг 2: Запустите установку

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh)"
```

При вопросе выберите `2` (Восстановление из бэкапа). Установщик покажет список найденных архивов, вы выберете нужный, и он восстановит базу данных и настройки.

## Создание бэкапа

В боте: нажмите **💾 Бэкап** → **📦 Создать бэкап**. Бот создаст ZIP-архив и отправит вам файлом. Архив содержит:
- Базу данных (клиенты, дилеры, балансы, платежи, настройки)
- Конфигурацию .env (токен бота, ID администратора)
- Настройки часового пояса

## Команды бота

### Администратор
- `/start` — запуск бота
- `/help` — справка по командам
- `/add` — добавить клиента (мастер: USERID → USERNAME → дата/время)
- `/renew` — продлить по USERID
- `/delete` — удалить по USERID (с подтверждением)
- `/list` — список клиентов (отсортировано по дате)
- `/disabled` — список отключённых (просроченных)
- `/next` — ближайшие истечения (3 дня)
- `/dealers` — раздел дилеров
- `/balance` — балансы и долги дилеров
- `/pay` — методы оплаты
- `/backup` — бэкап базы данных (создать / восстановить / список)
- `/timezone` — показать/сменить часовой пояс
- `/status` — статус бота

### Дилер
- `/list` — список своих клиентов
- `/disabled` — просроченные (свои)
- `/next` — ближайшие истечения (свои)
- `/renew` — запрос на продление
- `/order` — заказать новые ключи
- `/balance` — свой баланс (долг)
- `/pay` — оплата и реквизиты

## Настройки (.env)

```
BOT_TOKEN=...
OWNER_CHAT_ID=...
DEALER_NAME=main
TIMEZONE=Asia/Ashgabat
CHECK_INTERVAL_MINUTES=1
NOTIFY_EVERY_MINUTES=180
MAX_NOTIFICATIONS=9
DATABASE_URL=sqlite+aiosqlite:///./data/data.db
```

## Управление

```bash
# Логи
cd /opt/xmplus && docker compose logs -f xmplus

# Перезапуск
cd /opt/xmplus && docker compose restart

# Остановка
cd /opt/xmplus && docker compose down

# Обновление (git pull + пересборка)
cd /opt/xmplus && git pull && docker compose up -d --build
```

## Структура проекта

```
XMPLUS_info/
├── app/
│   ├── bot.py         # Логика бота (команды, FSM, клавиатуры)
│   ├── config.py      # Настройки из переменных окружения
│   ├── db.py          # Модели БД (SQLAlchemy) и миграции
│   ├── jobs.py        # Уведомления о просрочках
│   ├── main.py        # Точка входа
│   ├── scheduler.py   # APScheduler
│   └── utils.py       # Часовые пояса, форматирование дат
├── backup/            # Папка для бэкапов (при переустановке)
├── data/              # БД и рабочие данные (создаётся автоматически)
├── deploy/            # Systemd-сервис (альтернатива Docker)
├── docker-compose.yml
├── Dockerfile
├── install.sh         # Автоустановщик
└── requirements.txt
```
