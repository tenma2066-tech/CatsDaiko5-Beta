#!/usr/bin/env bash
set -euo pipefail
src=$(cd "$(dirname "$0")/.." && pwd)
app=/opt/paykyash
umask 022
id paykyash >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/paykyash --shell /usr/sbin/nologin paykyash
install -d -m 0755 "$app/releases"
exec 9>"$app/install.lock"
flock -n 9 || { echo "別のインストールが進行中です"; exit 1; }
install -d -m 0700 -o paykyash -g paykyash /var/lib/paykyash
install -d -m 0750 -o root -g paykyash /etc/paykyash
if [[ ! -e /etc/paykyash/secrets.json ]]; then
  install -m 0640 -o root -g paykyash "$src/secrets.example.json" /etc/paykyash/secrets.json
fi
if ! dpkg -s python3-venv >/dev/null 2>&1; then
  apt-get -o DPkg::Lock::Timeout=300 update -qq
  apt-get -o DPkg::Lock::Timeout=300 install -y python3-venv
fi
release=$(mktemp -d "$app/releases/release.XXXXXXXX")
trap 'rm -f "$app/current.next"; if [[ $(readlink "$app/current" || true) != "$release" ]]; then rm -rf "$release"; fi' EXIT
chmod 0755 "$release"
install -d "$release/system/tests"
install -m 0644 "$src/system/"*.py "$release/system/"
install -m 0644 "$src/system/tests/"test_*.py "$release/system/tests/"
python3 -m venv "$release/venv"
"$release/venv/bin/pip" -q install -r "$src/requirements.txt"
for suite in test_flows.py test_recovery.py; do
  runuser -u paykyash -- env EXCHANGE_DATA_DIR="$release" EXCHANGE_SECRETS_FILE=/dev/null \
    PYTHONDONTWRITEBYTECODE=1 "$release/venv/bin/python" "$release/system/tests/$suite"
done
if systemctl is-active --quiet paykyash; then
  echo "管理画面で受付を停止し、進行中の取引を確認してからサービスを停止してください。"
  exit 1
fi
install -m 0644 "$src/deploy/paykyash.service" /etc/systemd/system/
systemctl daemon-reload
ln -s "$release" "$app/current.next"
mv -Tf "$app/current.next" "$app/current"
echo "設置完了。資格情報・移行データを確認後にサービスを起動してください。"
