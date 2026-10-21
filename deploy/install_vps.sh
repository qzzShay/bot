#!/usr/bin/env bash
# Installiert den Bot auf einem Ubuntu/Debian-VPS als systemd-Dienst.
# Aufruf (als root, im geklonten Repo):  sudo bash deploy/install_vps.sh
set -euo pipefail

APP_DIR=/opt/cryptobot
APP_USER=cryptobot
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then echo "Bitte mit sudo/root ausführen"; exit 1; fi

echo ">> Pakete installieren"
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip rsync chrony >/dev/null
# Exakte Uhrzeit ist für signierte Börsen-Requests wichtig
systemctl enable --now chrony >/dev/null 2>&1 || true

echo ">> Benutzer & Verzeichnis"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
mkdir -p "$APP_DIR"
rsync -a --delete --exclude .venv --exclude data --exclude .env --exclude config/config.yaml \
      --exclude .git "$SRC_DIR"/ "$APP_DIR"/
mkdir -p "$APP_DIR/data"

echo ">> Python-Umgebung"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

[[ -f "$APP_DIR/config/config.yaml" ]] || cp "$APP_DIR/config/config.example.yaml" "$APP_DIR/config/config.yaml"
[[ -f "$APP_DIR/.env" ]] || cp "$APP_DIR/.env.example" "$APP_DIR/.env"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo ">> systemd-Dienst"
cp "$APP_DIR/deploy/cryptobot.service" /etc/systemd/system/cryptobot.service
systemctl daemon-reload
systemctl enable cryptobot >/dev/null

cat <<MSG

Fertig. Nächste Schritte:
  1) sudo nano $APP_DIR/.env                 # API-Keys, Telegram
  2) sudo nano $APP_DIR/config/config.yaml   # Modus, Symbole, Risiko
  3) cd $APP_DIR && sudo -u $APP_USER .venv/bin/python -m bot check
  4) sudo systemctl start cryptobot
  5) journalctl -u cryptobot -f              # Logs live ansehen

Status:  cd $APP_DIR && sudo -u $APP_USER .venv/bin/python -m bot status
MSG
