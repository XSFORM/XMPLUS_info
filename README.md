# XMPLUS_info

Telegram expiry-notifier bot (XMPLUS) — репозиторий для развёртывания бота, который уведомляет об истекающих сроках.

## Автоустановка

Рекомендуемый запуск (stdin — ваш терминал, вопросы будут заданы корректно):
```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh)"
```

Альтернатива (скачать файл и запустить):
```bash
curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh -o install.sh
bash install.sh
```

Полная автоустановка на чистой Ubuntu/Debian (поставит curl, git, Docker и запустит скрипт):
```bash
bash -c 'set -e; apt-get update; apt-get install -y curl git; curl -fsSL https://get.docker.com | sh; curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh -o /tmp/install.sh; bash /tmp/install.sh'
```

Подсказка: можно передать параметры без вопросов (неинтерактивно):
```bash
BOT_TOKEN=XXX OWNER_CHAT_ID=123456 bash -c "$(curl -fsSL https://raw.githubusercontent.com/XSFORM/XMPLUS_info/main/install.sh)"
```

Логи после установки:
```bash
cd /opt/xmplus && docker compose logs -f xmplus
```
(если используется классический docker-compose: `docker-compose logs -f xmplus`)