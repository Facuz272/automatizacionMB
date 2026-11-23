#!/usr/bin/env bash
# Instala el Lead Engine en una instancia Ubuntu 24.04 (AWS Lightsail / EC2) y programa el piloto automático.
# Idempotente: se puede volver a correr para actualizar el código.
#   sudo REPO_URL=https://github.com/Facuz272/automatizacionMB.git ./setup.sh
# Requisitos: acceso de lectura al repo (deploy key o token en REPO_URL) y el .env cargado después a mano.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Facuz272/automatizacionMB.git}"
BRANCH="${BRANCH:-main}"
APP=/opt/lead-engine
USER_NAME=leadengine
WITH_BROWSER="${WITH_BROWSER:-false}"      # true en instancias de 2 GB: Chromium para sitios con JS

[ "$(id -u)" -eq 0 ] || { echo "Correr con sudo"; exit 1; }

apt-get update -qq
apt-get install -y -qq git python3 python3-venv python3-pip sqlite3 tzdata ca-certificates >/dev/null
timedatectl set-timezone America/New_York || true

id "$USER_NAME" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$APP" --shell /usr/sbin/nologin "$USER_NAME"
mkdir -p "$APP" /var/log/lead-engine
chown -R "$USER_NAME":"$USER_NAME" "$APP" /var/log/lead-engine

if [ -d "$APP/repo/.git" ]; then
  sudo -u "$USER_NAME" git -C "$APP/repo" fetch -q origin "$BRANCH"
  sudo -u "$USER_NAME" git -C "$APP/repo" checkout -q "$BRANCH"
  sudo -u "$USER_NAME" git -C "$APP/repo" merge -q --ff-only "origin/$BRANCH"
else
  sudo -u "$USER_NAME" git clone -q --branch "$BRANCH" "$REPO_URL" "$APP/repo"
fi
ln -sfn "$APP/repo/v2" "$APP/v2"

[ -d "$APP/venv" ] || sudo -u "$USER_NAME" python3 -m venv "$APP/venv"
sudo -u "$USER_NAME" "$APP/venv/bin/pip" install -q --upgrade pip
sudo -u "$USER_NAME" "$APP/venv/bin/pip" install -q -r "$APP/v2/requirements.txt"
if [ "$WITH_BROWSER" = "true" ]; then
  sudo -u "$USER_NAME" "$APP/venv/bin/pip" install -q playwright
  "$APP/venv/bin/python" -m playwright install-deps chromium >/dev/null
  sudo -u "$USER_NAME" "$APP/venv/bin/python" -m playwright install chromium >/dev/null
fi

if [ ! -f "$APP/v2/.env" ]; then
  install -m 600 -o "$USER_NAME" -g "$USER_NAME" "$APP/v2/deploy/aws/env.aws.example" "$APP/v2/.env"
  echo ">> Completar las claves en $APP/v2/.env (queda en dry run: REPLENISH_COMMIT=false)"
fi
chmod 600 "$APP/v2/.env"; chown "$USER_NAME":"$USER_NAME" "$APP/v2/.env"

# Padrón DBPR local (condominios, licencias CAM/CAB, alojamiento): descarga pública, sin costo.
if [ ! -s "$APP/v2/data/dbpr.db" ]; then
  sudo -u "$USER_NAME" bash -c "cd $APP/v2 && $APP/venv/bin/python -m lead_engine dbpr-sync" || \
    echo ">> dbpr-sync falló: reintentar a mano o copiar data/dbpr.db"
fi

install -m 644 "$APP/v2/deploy/aws/"lead-engine-*.service "$APP/v2/deploy/aws/"lead-engine-*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now lead-engine-replenish.timer lead-engine-dbpr-sync.timer

cat > /etc/logrotate.d/lead-engine <<'ROT'
/var/log/lead-engine/*.log {
  weekly
  rotate 8
  compress
  missingok
  notifempty
  copytruncate
}
ROT

echo
echo "Listo. Próximas corridas:"
systemctl list-timers 'lead-engine-*' --no-pager
echo
echo "Plan sin costo (solo lee el Pool):  sudo -u $USER_NAME bash -c 'cd $APP/v2 && $APP/venv/bin/python -m lead_engine auto-replenish --plan'"
echo "Dry run a mano:                     sudo systemctl start lead-engine-replenish.service && tail -f /var/log/lead-engine/replenish.log"
