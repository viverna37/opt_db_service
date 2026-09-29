#!/usr/bin/env bash
# Выкатка на прод с машины разработчика:
#   DEPLOY_HOST=root@201.34.158.94 FRONT_DIR=~/WebstormProjects/opt_catalog_app deploy/deploy.sh
# 1) собирает фронт (npm run build, VITE_API_URL из .env.production фронта),
# 2) rsync кода бэкенда в /opt/opt_catalog/api и сборки фронта в /opt/opt_catalog/web,
# 3) docker compose up -d --build.
# .env на сервере создаётся один раз (deploy/server-init.sh) и здесь не трогается.
set -euo pipefail

HOST="${DEPLOY_HOST:?укажите DEPLOY_HOST, например root@1.2.3.4}"
FRONT_DIR="${FRONT_DIR:-$HOME/WebstormProjects/opt_catalog_app}"
REMOTE=/opt/opt_catalog
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> сборка фронта ($FRONT_DIR)"
(cd "$FRONT_DIR" && npm run build)

echo "==> заливка на $HOST"
ssh "$HOST" "mkdir -p $REMOTE/api $REMOTE/web"
rsync -az --delete \
  --exclude .git --exclude .venv --exclude .idea --exclude samples --exclude uploads \
  --exclude __pycache__ --exclude .pytest_cache --exclude app/.env \
  "$ROOT/" "$HOST:$REMOTE/api/"
rsync -az --delete "$FRONT_DIR/dist/" "$HOST:$REMOTE/web/"
rsync -az "$ROOT/deploy/docker-compose.yml" "$ROOT/deploy/Caddyfile" "$HOST:$REMOTE/"

echo "==> перезапуск"
ssh "$HOST" "cd $REMOTE && test -f .env || { echo 'Нет $REMOTE/.env — запустите deploy/server-init.sh'; exit 1; }; docker compose up -d --build && docker compose ps"
