#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/XSFORM/XMPLUS_info.git"
INSTALL_DIR="/opt/xmplus"
BACKUP_DIR="$INSTALL_DIR/backup"

need() { command -v "$1" >/dev/null 2>&1; }

ensure_root() {
  if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "Please run as root (sudo)." >&2
    exit 1
  fi
}

detect_pkg_mgr() {
  if command -v apt-get >/dev/null 2>&1; then
    echo "apt"
  elif command -v dnf >/dev/null 2>&1; then
    echo "dnf"
  elif command -v yum >/dev/null 2>&1; then
    echo "yum"
  else
    echo ""
  fi
}

pkg_install() {
  local mgr="$1"; shift
  case "$mgr" in
    apt) DEBIAN_FRONTEND=noninteractive apt-get update -y && apt-get install -y "$@" ;;
    dnf) dnf install -y "$@" ;;
    yum) yum install -y "$@" ;;
    *) return 1 ;;
  esac
}

ensure_basics() {
  local mgr
  mgr=$(detect_pkg_mgr)
  if [ -n "$mgr" ]; then
    need curl  || pkg_install "$mgr" curl
    need git   || pkg_install "$mgr" git
    need unzip || pkg_install "$mgr" unzip
  else
    need curl  || { echo "curl is required"; exit 1; }
    need git   || { echo "git is required"; exit 1; }
    need unzip || { echo "unzip is required"; exit 1; }
  fi
}

ensure_docker() {
  if need docker; then
    return
  fi
  echo "[*] Docker not found. Installing via get.docker.com ..."
  curl -fsSL https://get.docker.com | sh
  if command -v systemctl >/dev/null 2>&1; then
    systemctl enable --now docker || true
  fi
  if ! need docker; then
    echo "Docker installation failed. Install Docker manually and re-run." >&2
    exit 1
  fi
}

ensure_compose() {
  if docker compose version >/dev/null 2>&1; then
    return
  fi
  if need docker-compose; then
    return
  fi

  local mgr
  mgr=$(detect_pkg_mgr)
  if [ -n "$mgr" ]; then
    echo "[*] Installing docker compose plugin ..."
    pkg_install "$mgr" docker-compose-plugin || true
    if docker compose version >/dev/null 2>&1; then
      return
    fi
    echo "[*] Installing legacy docker-compose ..."
    pkg_install "$mgr" docker-compose || true
  fi

  if ! docker compose version >/dev/null 2>&1 && ! need docker-compose; then
    echo "Docker Compose not found. Please install docker compose plugin or docker-compose." >&2
    exit 1
  fi
}

clone_or_update_repo() {
  if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[*] Updating repo in $INSTALL_DIR ..."
    git -C "$INSTALL_DIR" fetch --all --prune
    git -C "$INSTALL_DIR" checkout main
    git -C "$INSTALL_DIR" pull --ff-only origin main
  else
    echo "[*] Cloning repo to $INSTALL_DIR ..."
    mkdir -p "$INSTALL_DIR"
    git clone --branch main --depth 1 "$REPO_URL" "$INSTALL_DIR"
  fi
}

# Универсальный вопрос: читает из /dev/tty, чтобы работать даже при "curl ... | bash"
ask() {
  local prompt="$1"; shift
  local __var="$1"; shift
  local def="${1:-}"

  local input=""
  if [ -t 0 ]; then
    read -rp "$prompt" input
  else
    read -rp "$prompt" input </dev/tty
  fi

  if [ -z "$input" ] && [ -n "$def" ]; then
    printf -v "$__var" "%s" "$def"
  else
    printf -v "$__var" "%s" "$input"
  fi
}

# =============================================
#  Режим установки: Новая или Восстановление
# =============================================

choose_install_mode() {
  echo
  echo "============================================"
  echo "  XMPLUS — Установка"
  echo "============================================"
  echo
  echo "  1) Новая установка"
  echo "  2) Восстановление из бэкапа"
  echo
  ask "Выберите [1/2]: " INSTALL_MODE "1"

  case "$INSTALL_MODE" in
    2) RESTORE_MODE=true ;;
    *) RESTORE_MODE=false ;;
  esac
}

# =============================================
#  Новая установка — спрашивает токен и т.д.
# =============================================

prompt_env_fresh() {
  echo
  echo "=== Новая установка — настройка ==="

  BOT_TOKEN=${BOT_TOKEN:-}
  OWNER_CHAT_ID=${OWNER_CHAT_ID:-}
  DEALER_NAME=${DEALER_NAME:-}
  TIMEZONE=${TIMEZONE:-}

  if [ -z "$BOT_TOKEN" ]; then
    ask "Enter BOT_TOKEN: " BOT_TOKEN
  fi
  if [ -z "${OWNER_CHAT_ID:-}" ]; then
    ask "Enter OWNER_CHAT_ID (numeric, optional, ENTER to skip): " OWNER_CHAT_ID
  fi
  ask "Dealer name [main]: " DEALER_NAME "main"
  ask "Timezone [Asia/Ashgabat]: " TIMEZONE "Asia/Ashgabat"

  CHECK_INTERVAL_MINUTES=${CHECK_INTERVAL_MINUTES:-1}
  NOTIFY_EVERY_MINUTES=${NOTIFY_EVERY_MINUTES:-180}
  MAX_NOTIFICATIONS=${MAX_NOTIFICATIONS:-9}
  DATABASE_URL=${DATABASE_URL:-sqlite+aiosqlite:///./data/data.db}

  cat > "$INSTALL_DIR/.env" <<EOF
BOT_TOKEN=$BOT_TOKEN
OWNER_CHAT_ID=${OWNER_CHAT_ID:-}
DEALER_NAME=$DEALER_NAME
TIMEZONE=$TIMEZONE

CHECK_INTERVAL_MINUTES=$CHECK_INTERVAL_MINUTES
NOTIFY_EVERY_MINUTES=$NOTIFY_EVERY_MINUTES
MAX_NOTIFICATIONS=$MAX_NOTIFICATIONS

DATABASE_URL=$DATABASE_URL
EOF
  echo "[*] .env written to $INSTALL_DIR/.env"
}

# =============================================
#  Восстановление из бэкапа
# =============================================

restore_from_backup() {
  echo
  echo "=== Восстановление из бэкапа ==="
  echo

  mkdir -p "$BACKUP_DIR"
  mkdir -p "$INSTALL_DIR/data"

  # Ищем ZIP-файлы в backup/
  local zips=()
  while IFS= read -r -d $'\0' f; do
    zips+=("$f")
  done < <(find "$BACKUP_DIR" -maxdepth 1 -name "*.zip" -print0 2>/dev/null | sort -z -r)

  if [ ${#zips[@]} -eq 0 ]; then
    echo "В папке $BACKUP_DIR нет ZIP-архивов."
    echo
    echo "Сначала загрузите бэкап на сервер, например:"
    echo "  scp xmplus_backup_XXXXXXXX_XXXXXX.zip root@YOUR_SERVER:$BACKUP_DIR/"
    echo
    echo "Затем запустите установку повторно."
    exit 1
  fi

  echo "Найденные бэкапы:"
  local i=1
  for f in "${zips[@]}"; do
    local fname
    fname=$(basename "$f")
    local fsize
    fsize=$(du -h "$f" | cut -f1)
    echo "  $i) $fname ($fsize)"
    i=$((i + 1))
  done
  echo

  local choice
  ask "Выберите номер бэкапа [1]: " choice "1"
  local idx=$((choice - 1))

  if [ "$idx" -lt 0 ] || [ "$idx" -ge "${#zips[@]}" ]; then
    echo "Неверный выбор." >&2
    exit 1
  fi

  local selected="${zips[$idx]}"
  local selected_name
  selected_name=$(basename "$selected")
  echo
  echo "[*] Распаковка: $selected_name ..."

  # Показываем содержимое архива
  echo "[*] Содержимое архива:"
  unzip -l "$selected"
  echo

  # Проверяем наличие базы данных (ищем data.db в любом пути)
  if ! unzip -l "$selected" | grep -q "data\.db"; then
    echo "ОШИБКА: архив не содержит data.db — это не бэкап XMPLUS." >&2
    exit 1
  fi

  # Распаковываем всё во временную папку, потом раскладываем
  local tmp_restore="$INSTALL_DIR/_restore_tmp"
  rm -rf "$tmp_restore"
  mkdir -p "$tmp_restore"
  unzip -o "$selected" -d "$tmp_restore"

  # Ищем data.db в распакованном архиве (на любой глубине)
  local found_db=""
  found_db=$(find "$tmp_restore" -name "data.db" -type f | head -1)
  if [ -z "$found_db" ]; then
    echo "ОШИБКА: data.db не найден в архиве." >&2
    rm -rf "$tmp_restore"
    exit 1
  fi
  cp -f "$found_db" "$INSTALL_DIR/data/data.db"
  echo "[*] База данных восстановлена: $(du -h "$INSTALL_DIR/data/data.db" | cut -f1)"

  # Восстанавливаем .tz_override если есть
  local found_tz=""
  found_tz=$(find "$tmp_restore" -name ".tz_override" -type f | head -1)
  if [ -n "$found_tz" ]; then
    cp -f "$found_tz" "$INSTALL_DIR/.tz_override"
    echo "[*] Часовой пояс восстановлен."
  fi

  # Восстанавливаем .env если есть
  local found_env=""
  found_env=$(find "$tmp_restore" -name ".env" -type f | head -1)
  if [ -n "$found_env" ]; then
    cp -f "$found_env" "$INSTALL_DIR/.env"
    echo "[*] .env восстановлен из бэкапа."
    echo
    echo "--- Текущие настройки (.env) ---"
    cat "$INSTALL_DIR/.env"
    echo "--------------------------------"
    echo
    ask "Хотите изменить настройки? (y/n) [n]: " EDIT_ENV "n"
    if [ "$EDIT_ENV" = "y" ] || [ "$EDIT_ENV" = "Y" ]; then
      prompt_env_fresh
    fi
  else
    echo "[!] Архив не содержит .env — потребуется настроить вручную."
    prompt_env_fresh
  fi

  rm -rf "$tmp_restore"
  echo "[*] Восстановление завершено."
}

# =============================================
#  Запуск docker compose
# =============================================

run_compose() {
  mkdir -p "$INSTALL_DIR/data"
  mkdir -p "$BACKUP_DIR"

  local compose_cmd="docker compose"
  if ! docker compose version >/dev/null 2>&1; then
    compose_cmd="docker-compose"
  fi

  echo "[*] Building and starting containers ..."
  cd "$INSTALL_DIR"
  $compose_cmd up -d --build

  echo
  echo "============================================"
  echo "  XMPLUS установлен и запущен!"
  echo "============================================"
  echo
  echo "  Логи:       cd $INSTALL_DIR && $compose_cmd logs -f xmplus"
  echo "  Перезапуск: cd $INSTALL_DIR && $compose_cmd restart"
  echo "  Остановка:  cd $INSTALL_DIR && $compose_cmd down"
  echo
}

# =============================================
#  Точка входа
# =============================================

main() {
  ensure_root
  ensure_basics
  ensure_docker
  ensure_compose
  clone_or_update_repo
  choose_install_mode

  if [ "$RESTORE_MODE" = true ]; then
    restore_from_backup
  else
    prompt_env_fresh
  fi

  run_compose
}

main "$@"
