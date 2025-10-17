#!/usr/bin/env bash
# XMPLUS installer: авто-установка Docker + Compose, клонирование репо, настройка .env и запуск
set -Eeuo pipefail

# ---------------------------
# Конфиг по умолчанию
# ---------------------------
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
NON_INTERACTIVE=0
SKIP_DOCKER_INSTALL=0

# sudo-обертка (если скрипт не под root)
SUDO=""
if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  SUDO="sudo"
fi

usage() {
  cat <<EOF
Usage:
  bash install.sh [--token <BOT_TOKEN>] [--chat-id <CHAT_ID>] [--dealer-name <NAME>]
                  [--timezone <IANA_TZ>] [--repo owner/repo] [--branch main]
                  [--dir /opt/xmplus] [--csv-url <URL>] [--csv-path <PATH>]
                  [--no-build] [--non-interactive] [--skip-docker-install]

Examples:
  bash install.sh --token 123:ABC --timezone Europe/Moscow
  bash install.sh --token 123:ABC --chat-id 123456789 --csv-url https://example.com/clients.csv

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
    --non-interactive) NON_INTERACTIVE=1; shift;;
    --skip-docker-install) SKIP_DOCKER_INSTALL=1; shift;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 1;;
  esac
done

detect_platform() {
  if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS_ID="${ID:-}"
    OS_VER="${VERSION_CODENAME:-}"
    OS_LIKE="${ID_LIKE:-}"
  else
    OS_ID="$(uname -s || true)"
    OS_VER=""
    OS_LIKE=""
  fi
}

ensure_cmd() {
  local cmd="$1"
  local pkgs="${2:-$1}"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Installing dependency: $pkgs"
    $SUDO apt-get update -y
    $SUDO apt-get install -y $pkgs
  fi
}

install_docker_debian() {
  echo "[Docker] Installing Docker Engine + compose-plugin from Docker official repo..."
  $SUDO apt-get update -y
  ensure_cmd curl curl
  ensure_cmd gpg gnupg
  ensure_cmd ca-certificates ca-certificates

  $SUDO install -m 0755 -d /etc/apt/keyrings
  $SUDO curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  $SUDO chmod a+r /etc/apt/keyrings/docker.asc

  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${OS_VER} stable" | $SUDO tee /etc/apt/sources.list.d/docker.list >/dev/null

  $SUDO apt-get update -y
  $SUDO apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
}

fallback_docker_debian() {
  echo "[Docker] Fallback install via Ubuntu repos (docker.io)."
  $SUDO apt-get update -y
  $SUDO apt-get install -y docker.io
}

ensure_docker_running() {
  # В контейнерах/WSL systemd может быть недоступен — проверяем осторожно
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl enable --now docker || true
  fi
}

add_user_to_docker_group() {
  local target_user="${SUDO_USER:-$USER}"
  # Если мы root без SUDO_USER — смысла добавлять нет, но сделаем на всякий случай
  if getent group docker >/dev/null 2>&1; then
    echo "[Docker] Adding user '$target_user' to docker group..."
    $SUDO usermod -aG docker "$target_user" || true
  fi
}

ensure_docker() {
  if [[ "$SKIP_DOCKER_INSTALL" -eq 1 ]]; then
    echo "[Docker] Skipping Docker installation as requested."
    return
  fi

  detect_platform
  case "$OS_ID" in
    ubuntu|debian)
      if ! command -v docker >/dev/null 2>&1; then
        install_docker_debian || {
          echo "[Docker] Official install failed, trying fallback..."
          fallback_docker_debian
        }
      fi
      ensure_docker_running
      ;;
    *)
      echo "[Docker] Unsupported OS ($OS_ID). Please install Docker manually and re-run with --skip-docker-install."
      ;;
  esac

  if ! command -v docker >/dev/null 2>&1; then
    echo "[Docker] docker not found after installation. Abort."
    exit 1
  fi

  # Проверка compose
  if ! docker compose version >/dev/null 2>&1; then
    echo "[Docker] docker compose plugin not found. Trying to install plugin..."
    install_docker_debian || true
  fi

  # Версии
  docker --version || true
  docker compose version || true

  add_user_to_docker_group
}

main() {
  echo "[1/8] Ensure base tools (git, curl)..."
  ensure_cmd git git
  ensure_cmd curl curl

  echo "[2/8] Ensure Docker Engine + compose..."
  ensure_docker

  echo "[3/8] Prepare install dir: $INSTALL_DIR"
  $SUDO mkdir -p "$INSTALL_DIR"
  $SUDO chown -R "${SUDO_USER:-$USER}":"${SUDO_USER:-$USER}" "$INSTALL_DIR"

  if [[ ! -d "$INSTALL_DIR/.git" ]]; then
    echo "[4/8] Clone repository $REPO ($BRANCH) ..."
    git clone --branch "$BRANCH" "https://github.com/$REPO.git" "$INSTALL_DIR"
  else
    echo "[4/8] Repository exists, pulling updates..."
    git -C "$INSTALL_DIR" pull --ff-only
  fi

  cd "$INSTALL_DIR"

  # .env
  if [[ ! -f ".env" ]]; then
    cp .env.example .env
  fi

  if [[ -z "${BOT_TOKEN:-}" && "$NON_INTERACTIVE" -eq 0 ]]; then
    read -r -p "Введите BOT_TOKEN: " BOT_TOKEN
  fi
  if [[ -z "${BOT_TOKEN:-}" ]]; then
    echo "BOT_TOKEN обязателен. Укажите --token или задайте переменную окружения BOT_TOKEN."
    exit 1
  fi

  echo "[5/8] Configure .env ..."
  sed -i -E "s|^BOT_TOKEN=.*|BOT_TOKEN=${BOT_TOKEN}|" .env

  if [[ -n "${OWNER_CHAT_ID:-}" ]]; then
    sed -i -E "s|^OWNER_CHAT_ID=.*|OWNER_CHAT_ID=${OWNER_CHAT_ID}|" .env
  fi
  sed -i -E "s|^DEALER_NAME=.*|DEALER_NAME=${DEALER_NAME}|" .env
  sed -i -E "s|^TIMEZONE=.*|TIMEZONE=${TIMEZONE}|" .env

  # Убедимся в нужных дефолтах
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
  if grep -q '^DATABASE_URL=' .env; then
    sed -i -E "s|^DATABASE_URL=.*|DATABASE_URL=sqlite+aiosqlite:///./data/data.db|" .env
  else
    echo "DATABASE_URL=sqlite+aiosqlite:///./data/data.db" >> .env
  fi

  mkdir -p data

  echo "[6/8] Docker Compose up ..."
  if [[ "$NO_BUILD" -eq 1 ]]; then
    docker compose up -d
  else
    docker compose up -d --build
  fi

  echo "[7/8] Wait for container to be running..."
  for i in {1..30}; do
    if docker compose ps | awk '{print $1$3$4$5}' | grep -E "^xmplus.*running" >/dev/null 2>&1; then
      break
    fi
    sleep 2
  done
  docker compose ps || true

  echo "[8/8] Optional CSV import ..."
  if [[ -n "$CSV_URL" ]]; then
    echo "Downloading CSV from: $CSV_URL"
    curl -fsSL "$CSV_URL" -o "data/clients.csv"
    docker compose exec -T xmplus python scripts/import_csv.py data/clients.csv || true
  elif [[ -n "$CSV_PATH" && -f "$CSV_PATH" ]]; then
    echo "Importing CSV from: $CSV_PATH"
    cp "$CSV_PATH" data/
    BASENAME="$(basename "$CSV_PATH")"
    docker compose exec -T xmplus python scripts/import_csv.py "data/$BASENAME" || true
  fi

  cat <<'NEXT'

Готово!

Дальше:
1) Откройте Telegram и отправьте вашему боту /start — чат привяжется для уведомлений.
2) (Опционально) Импорт CSV позже:
   docker compose exec -T xmplus python scripts/import_csv.py data/clients.csv
3) Логи бота:
   docker compose logs -f

Примечание:
- Если вы запускали скрипт НЕ от root и без sudo, возможно понадобится перелогиниться,
  чтобы заработали права docker-группы.
- Ради безопасности удалите строку с токеном из истории оболочки (если вводили прямо в командной строке):
    history
    history -d <Номер_строки_с_токеном>
    history -w
NEXT
}

main "$@"