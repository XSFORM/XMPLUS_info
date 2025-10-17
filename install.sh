#!/usr/bin/env bash
set -Eeuo pipefail

DEFAULT_REPO="XSFORM/XMPLUS_info"
DEFAULT_BRANCH="main"
DEFAULT_DIR="/opt/xmplus"
DEFAULT_TZ="Europe/Moscow"
DEFAULT_NOTIFY="180"
DEFAULT_MAX="8"

BOT_TOKEN="${BOT_TOKEN:-}"
OWNER_CHAT_ID="${OWNER_CHAT_ID:-}"
DEALER_NAME="${DEALER_NAME:-main}"
TIMEZONE="${TIMEZONE:-$DEFAULT_TZ}"
REPO="$DEFAULT_REPO"
BRANCH="$DEFAULT_BRANCH"
INSTALL_DIR="$DEFAULT_DIR"
CSV_URL=""
CSV_PATH=""
NO_BUILD=0

usage() {
  cat <<EOF
Usage:
  bash install.sh [--token <BOT_TOKEN>] [--chat-id <CHAT_ID>] [--dealer-name <NAME>]
                  [--timezone <IANA_TZ>] [--repo owner/repo] [--branch main]
                  [--dir /opt/xmplus] [--csv-url <URL>] [--csv-path <PATH>]
                  [--no-build]

Examples:
  bash install.sh --token 123:ABC --chat-id 123456789 --timezone Europe/Moscow
  bash install.sh --token 123:ABC --csv-url https://example.com/clients.csv
Notes:
  - OWNER_CHAT_ID можно не указывать: привяжется через /start в Telegram.
  - БД сохраняется в ./data/data.db (volume), контейнер — docker compose.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --token) BOT_TOKEN="$2"; shift 2;;
    --chat-id) OWNER_CHAT_ID="$2"; shift 2;;
    --dealer-name) DEALER_NAME="$2"; shift 2;;
    --timezone) TIMEZONE="$2"; shift 2;;
    --repo) REPO="$2"; shift 2;;
    --branch) BRANCH="$2"; shift 2;;
    --dir) INSTALL_DIR="$2"; shift 2;;
    --csv-url) CSV_URL="$2"; shift 2;;
    --csv-path) CSV_PATH="$2"; shift 2;;
    --no-build) NO_BUILD=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done

echo "[1/7] Проверка зависимостей (git, docker, compose)..."
if ! command -v git >/dev/null 2>&1; then
  sudo apt update && sudo apt -y install git
fi
if ! command -v docker >/dev/null 2>&1; then
  sudo apt update && sudo apt -y install docker.io docker-compose-plugin
  sudo systemctl enable --now docker
fi

echo "[2/7] Подготовка каталога: $INSTALL_DIR"
sudo mkdir -p "$INSTALL_DIR"
sudo chown -R "$USER":"$USER" "$INSTALL_DIR"

if [[ ! -d "$INSTALL_DIR/.git" ]]; then
  echo "[3/7] Клонируем репозиторий $REPO ($BRANCH) ..."
  git clone --branch "$BRANCH" "https://github.com/$REPO.git" "$INSTALL_DIR"
else
  echo "[3/7] Репозиторий уже есть, обновляем..."
  git -C "$INSTALL_DIR" pull --ff-only
fi

cd "$INSTALL_DIR"

# Создадим .env из примера при отсутствии
if [[ ! -f ".env" ]]; then
  cp .env.example .env
fi

# Если токен не передан — спросим интерактивно
if [[ -z "${BOT_TOKEN:-}" ]]; then
  read -r -p "Введите BOT_TOKEN: " BOT_TOKEN
fi

echo "[4/7] Настройка .env ..."
# Правим ключевые параметры в .env
sed -i -E "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|" .env

if [[ -n "${OWNER_CHAT_ID:-}" ]]; then
  sed -i -E "s|^OWNER_CHAT_ID=.*|OWNER_CHAT_ID=${OWNER_CHAT_ID}|" .env
fi

sed -i -E "s|^DEALER_NAME=.*|DEALER_NAME=${DEALER_NAME}|" .env
sed -i -E "s|^TIMEZONE=.*|TIMEZONE=${TIMEZONE}|" .env

# Убедимся, что уведомления по умолчанию 3ч и максимум 8 раз
if grep -q '^NOTIFY_EVERY_MINUTES=' .env; then
  sed -i -E "s|^NOTIFY_EVERY_MINUTES=.*|NOTIFY_EVERY_MINUTES=${DEFAULT_NOTIFY}|" .env
else
  echo "NOTIFY_EVERY_MINUTES=${DEFAULT_NOTIFY}" >> .env
fi
if grep -q '^MAX_NOTIFICATIONS=' .env; then
  sed -i -E "s|^MAX_NOTIFICATIONS=.*|MAX_NOTIFICATIONS=${DEFAULT_MAX}|" .env
else
  echo "MAX_NOTIFICATIONS=${DEFAULT_MAX}" >> .env
fi

# База в volume ./data (persist)
if grep -q '^DATABASE_URL=' .env; then
  sed -i -E "s|^DATABASE_URL=.*|DATABASE_URL=sqlite+aiosqlite:///./data/data.db|" .env
else
  echo "DATABASE_URL=sqlite+aiosqlite:///./data/data.db" >> .env
fi

mkdir -p data

echo "[5/7] Запуск Docker Compose ..."
if [[ "$NO_BUILD" -eq 1 ]]; then
  docker compose up -d
else
  docker compose up -d --build
fi

echo "[6/7] Ожидание запуска контейнера..."
for i in {1..30}; do
  if docker compose ps | grep -E "xmplus\s" | grep -q "running"; then
    break
  fi
  sleep 2
done
docker compose ps

# Опциональный импорт CSV
if [[ -n "$CSV_URL" ]]; then
  echo "[7/7] Скачиваю CSV: $CSV_URL"
  curl -fsSL "$CSV_URL" -o "data/clients.csv"
  docker compose exec -T xmplus python scripts/import_csv.py data/clients.csv || true
elif [[ -n "$CSV_PATH" && -f "$CSV_PATH" ]]; then
  echo "[7/7] Импортирую CSV из $CSV_PATH"
  cp "$CSV_PATH" data/
  BASENAME="$(basename "$CSV_PATH")"
  docker compose exec -T xmplus python scripts/import_csv.py "data/$BASENAME" || true
fi

cat <<'NEXT'

Готово!

Дальше:
1) Откройте Telegram, напишите вашему боту команду /start — чат привяжется для уведомлений.
2) (Опционально) Импорт CSV:
   - поместите файл в каталог data/ и выполните:
     docker compose exec -T xmplus python scripts/import_csv.py data/clients.csv
3) Логи бота:
   docker compose logs -f

Важно: не публикуйте BOT_TOKEN в скриншотах.
NEXT