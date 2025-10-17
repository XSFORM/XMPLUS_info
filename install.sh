#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/XSFORM/XMPLUS.git"
INSTALL_DIR="/opt/xmplus"

need() { command -v "$1" >/dev/null 2>&1; }

detect_compose() {
  if need docker && docker compose version >/dev/null 2>&1; then
    echo "docker compose"
  elif need docker-compose; then
    echo "docker-compose"
  else
    echo ""
  fi
}

ensure_root() {
  if [ "${EUID:-$(id -u)}" -ne 0 ]; then
    echo "Please run as root (sudo)." >&2
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

prompt_env() {
  echo
  echo "=== XMPLUS configuration ==="
  read -rp "Enter BOT_TOKEN: " BOT_TOKEN
  read -rp "Enter OWNER_CHAT_ID (numeric, optional, just ENTER to skip): " OWNER_CHAT_ID || true
  read -rp "Dealer name [main]: " DEALER_NAME || true
  read -rp "Timezone [Europe/Moscow]: " TIMEZONE || true

  DEALER_NAME=${DEALER_NAME:-main}
  TIMEZONE=${TIMEZONE:-Europe/Moscow}

  # Значения по умолчанию; можно потом изменить в .env
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

run_compose() {
  local compose_bin
  compose_bin=$(detect_compose)
  if [ -z "$compose_bin" ]; then
    echo "Docker Compose not found. Please install Docker and Compose plugin (or docker-compose)." >&2
    exit 1
  fi

  mkdir -p "$INSTALL_DIR/data"

  echo "[*] Building and starting services..."
  if [ "$compose_bin" = "docker compose" ]; then
    (cd "$INSTALL_DIR" && docker compose up --build -d)
  else
    (cd "$INSTALL_DIR" && docker-compose up --build -d)
  fi

  echo
  echo "=== Done ==="
  echo "View logs:  cd $INSTALL_DIR && $compose_bin logs -f xmplus"
}

main() {
  ensure_root
  need git || { echo "git is required"; exit 1; }
  need docker || { echo "docker is required"; exit 1; }

  clone_or_update_repo
  prompt_env
  run_compose
}

main "$@"