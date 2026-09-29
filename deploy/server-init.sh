#!/usr/bin/env bash
# Первичная настройка сервера (один раз): создаёт /opt/opt_catalog/.env со
# случайными паролями. Запуск: ssh root@host 'bash -s' < deploy/server-init.sh <домен> [telegram id админов]
set -euo pipefail
DOMAIN="${1:?домен, например opt.bozdyrevdev.ru}"
ADMINS="${2:-}"
REMOTE=/opt/opt_catalog
mkdir -p "$REMOTE"
if [ -f "$REMOTE/.env" ]; then
  echo "$REMOTE/.env уже есть — не трогаю"
  exit 0
fi
rand() { head -c 32 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "$1"; }
umask 077
cat > "$REMOTE/.env" <<ENV
DOMAIN=$DOMAIN
POSTGRES_USER=opt_catalog
POSTGRES_PASSWORD=$(rand 32)
POSTGRES_DB=opt_catalog
LOG_LEVEL=INFO
DEV_AUTH=false
INIT_DATA_MAX_AGE_SEC=86400
TELEGRAM_API_BASE_URL=https://api.telegram.org
WEBAPP_BASE_URL=https://$DOMAIN
SERVICE_API_KEY=$(rand 40)
PLATFORM_ADMIN_IDS=$ADMINS
PLATFORM_BOT_TOKEN=
ENV
echo "Создан $REMOTE/.env"
