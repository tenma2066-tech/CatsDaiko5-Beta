#!/usr/bin/env bash
# サーバー上で root 権限で実行される（GitHub Actions から）。何度実行しても安全。
# 引数: $1 = SITE_HOST, $2 = 新しいソースを展開したディレクトリ, $3 = 管理者パスワードを書いたファイル（読んだら削除）
set -euo pipefail
SITE_HOST="$1"
NEW_SRC="$2"
ADMIN_FILE="${3:-}"
APP_DIR=/opt/catsdaiko
ADMIN_PASSWORD=""
if [ -n "$ADMIN_FILE" ] && [ -f "$ADMIN_FILE" ]; then ADMIN_PASSWORD="$(cat "$ADMIN_FILE")"; shred -u "$ADMIN_FILE" 2>/dev/null || rm -f "$ADMIN_FILE"; fi

export DEBIAN_FRONTEND=noninteractive
# 1) Docker（未導入なら）
if ! command -v docker >/dev/null 2>&1; then
  apt-get update -y >/dev/null 2>&1 || true
  apt-get install -y curl ca-certificates openssl >/dev/null 2>&1 || true
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true
docker compose version >/dev/null 2>&1 || { apt-get install -y docker-compose-plugin >/dev/null 2>&1 || true; }

# 2) OS 側のファイアウォールで 22/80/443 を開ける（有効な場合のみ）
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi "^Status: active"; then
  ufw allow 22/tcp >/dev/null; ufw allow 80/tcp >/dev/null; ufw allow 443/tcp >/dev/null
fi
if command -v iptables >/dev/null 2>&1; then
  for p in 22 80 443; do
    iptables -C INPUT -p tcp --dport "$p" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport "$p" -j ACCEPT 2>/dev/null || true
  done
fi

# 3) 配置
mkdir -p "$APP_DIR/data"
chmod 700 "$APP_DIR" "$APP_DIR/data"
rm -rf "$APP_DIR/src"
mv "$NEW_SRC" "$APP_DIR/src"
cp "$APP_DIR/src/deploy/vps/docker-compose.yml" "$APP_DIR/docker-compose.yml"
cp "$APP_DIR/src/deploy/vps/Caddyfile" "$APP_DIR/Caddyfile"
mkdir -p "$APP_DIR/caddy-sites"   # 別サイト（xbot など）の追加設定。お店の更新では中身を消さない
[ -e "$APP_DIR/caddy-sites/00-placeholder.caddy" ] || echo "# 追加のサイト設定（xbot など）を置く場所" > "$APP_DIR/caddy-sites/00-placeholder.caddy"

# 4) 秘密情報（初回だけ生成。以後は上書きしない＝保存済みPayPay情報を読めなくしないため）
cd "$APP_DIR"
touch .env && chmod 600 .env
grep -q '^SECRET_KEY=' .env || echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
grep -q '^PAYPAY_ENCRYPTION_KEY=' .env || echo "PAYPAY_ENCRYPTION_KEY=$(openssl rand 32 | base64 | tr '+/' '-_')" >> .env
sed -i '/^SITE_HOST=/d' .env
echo "SITE_HOST=$SITE_HOST" >> .env
if [ -n "$ADMIN_PASSWORD" ]; then
  (umask 077; printf '%s' "$ADMIN_PASSWORD" > admin_password)
fi
[ -f admin_password ] || (umask 077; : > admin_password)
chmod 600 admin_password

# 4.5) バックアップ（毎日自動＋入れ替えの前）。注文・台帳・設定・暗号鍵を /opt/catsdaiko/backups に残す（root だけが読める）
cat > "$APP_DIR/backup.sh" <<'SH'
#!/bin/sh
# 使い方: backup.sh [daily|predeploy]   daily は14個、predeploy は10個まで残す
set -eu
D=/opt/catsdaiko; B=$D/backups; KIND=${1:-daily}
umask 077; mkdir -p "$B"; chmod 700 "$B"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/data"
cp -a "$D/data/." "$T/data/" 2>/dev/null || true
# 台帳（SQLite）は使用中でも壊れない写し方で取り直す
for f in $(find "$D/data" -name '*.sqlite3' 2>/dev/null); do
  rel=${f#"$D/data/"}
  python3 -c 'import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()' "$f" "$T/data/$rel" 2>/dev/null || true
done
for f in .env admin_password; do [ -f "$D/$f" ] && cp -a "$D/$f" "$T/"; done
OUT="$B/$(date +%Y%m%d-%H%M%S)-$KIND.tar.gz"
tar czf "$OUT" -C "$T" . && chmod 600 "$OUT"
KEEP=14; [ "$KIND" = predeploy ] && KEEP=10
ls -1t "$B"/*-"$KIND".tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
echo "backup: $OUT"
SH
chmod 700 "$APP_DIR/backup.sh"
cat > /etc/systemd/system/catsdaiko-backup.service <<'UNIT'
[Unit]
Description=にゃんこ代行のバックアップ（注文・台帳・設定）
[Service]
Type=oneshot
ExecStart=/opt/catsdaiko/backup.sh daily
UNIT
cat > /etc/systemd/system/catsdaiko-backup.timer <<'UNIT'
[Unit]
Description=にゃんこ代行のバックアップを毎日行う
[Timer]
OnCalendar=*-*-* 04:20:00
Persistent=true
RandomizedDelaySec=600
[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now catsdaiko-backup.timer >/dev/null 2>&1 || true
[ -d "$APP_DIR/data" ] && "$APP_DIR/backup.sh" predeploy || true

# 5) 先にビルド（今のサイトは動いたまま）
docker compose build

# 6) 実行中・決済確認中の処理がなくなるまで待つ（最大12分）。新しい実行の開始は一時的に待たせる。
APP_PY='import json,sys,urllib.request as u
t=open("/run/admin/admin_password").read()
def get(p): return json.load(u.urlopen("http://127.0.0.1:8080"+p,timeout=5))
mode=sys.argv[1]
if mode=="drain":
    import urllib.parse as q
    u.urlopen(u.Request("http://127.0.0.1:8080/api/drain",data=q.urlencode({"token":t,"off":sys.argv[2]} if sys.argv[2] else {"token":t}).encode()),timeout=5)
else:
    d=get("/api/state"); print(int(d.get("running_jobs",0))+int(d.get("queued_jobs",0))+int(d.get("paying_orders",0)))'
if docker compose ps --status running --services 2>/dev/null | grep -qx app; then
  docker compose exec -T app python3 -c "$APP_PY" drain "" >/dev/null 2>&1 || true
  N=1
  ALLOW="${ALLOW_RUNNING:-0}"   # 止まったまま終わらない実行がある場合だけ、その件数を許して入れ替える（手動実行時のみ）
  for i in $(seq 1 48); do
    N=$(docker compose exec -T app python3 -c "$APP_PY" state 2>/dev/null) || { N=0; break; }
    [ "$N" -le "$ALLOW" ] 2>/dev/null && { N=0; break; }
    echo "busy ($N) - waiting... ($i/48)"; sleep 15
  done
  if [ "$N" != "0" ] && [ "${FORCE_DEPLOY:-}" != "1" ]; then
    # 処理中のまま入れ替えると、利用者の実行が途中で止まる。入れ替えずに中止して、受付を再開する。
    docker compose exec -T app python3 -c "$APP_PY" drain 1 >/dev/null 2>&1 || true
    echo "::error::12分待っても処理中（$N 件）のため、入れ替えを中止しました（利用者の実行を守るため）。あとでもう一度デプロイしてください。"
    exit 3
  fi
fi

# 7) 入れ替え
# ネットワークの設定（MTU）が変わった場合は、作り直しが必要（コンテナを一度止める。処理中がないことは上で確認済み）
CUR_MTU=$(docker network inspect catsdaiko_default --format '{{index .Options "com.docker.network.driver.mtu"}}' 2>/dev/null || true)
if [ -n "$(docker network ls -q -f name=^catsdaiko_default$)" ] && [ "$CUR_MTU" != "1400" ]; then
  echo "network MTU '$CUR_MTU' -> 1400 (recreate)"
  docker compose down --remove-orphans
fi
docker compose up -d --remove-orphans
# 新しいコンテナ（または変更なしで動き続けたコンテナ）で、実行の開始を再び受け付ける
for i in $(seq 1 30); do
  if docker compose exec -T app python3 -c "$APP_PY" drain 1 >/dev/null 2>&1; then break; fi
  sleep 2
done
docker image prune -f >/dev/null 2>&1 || true
docker compose ps
