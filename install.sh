#!/usr/bin/env bash
set -euo pipefail

REPO_URL="https://github.com/XSFORM/XMPLUS_info.git"
INSTALL_DIR="/opt/xmplus"

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
    need curl || pkg_install "$mgr" curl
    need git  || pkg_install "$mgr" git
  else
    # Без пакетного менеджера просто проверим наличие
    need curl || { echo "curl is required"; exit 1; }
    need git  || { echo "git is required"; exit 1; }
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
  # Нужен либо docker compose (плагин), либо docker-compose (классический)
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
    # Попытка поставить плагин
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

prompt_env() {
  echo
  echo "=== XMPLUS configuration ==="
  read -rp "Enter BOT_TOKEN: " BOT_TOKEN
  read -rp "Enter OWNER_CHAT_ID (numeric, optional, just ENTER to skip): " OWNER_CHAT_ID || true
  read -rp "Dealer name [main]: " DEALER_NAME || true
  read -rp "Timezone [Europe/Moscow]: " TIMEZONE || true

  DEALER_NAME=${DEALER_NAME:-main}
  TIMEZONE=${TIMEZONE:-Europe/Moscow}

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
  mkdir -p "$INSTALL_DIR/data"

  if docker compose version >/dev/null 2>&1; then
    echo "[*] Building and starting services with docker compose..."
    (cd "$INSTALL_DIR" && docker compose up --build -d)
  else
    echo "[*] Building and starting services with docker-compose..."
    (cd "$INSTALL_DIR" && docker-compose up --build -d)
  fi

  echo
  echo "=== Done ==="
  if docker compose version >/dev/null 2>&1; then
    echo "View logs:  cd $INSTALL_DIR && docker compose logs -f xmplus"
  else
    echo "View logs:  cd $INSTALL_DIR && docker-compose logs -f xmplus"
  fi
}

main() {
  ensure_root
  ensure_basics
  ensure_docker
  ensure_compose
  clone_or_update_repo
  prompt_env
  run_compose
}

main "$@"