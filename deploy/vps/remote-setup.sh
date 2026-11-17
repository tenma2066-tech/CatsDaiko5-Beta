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
  for i in $(seq 1 48); do
    N=$(docker compose exec -T app python3 -c "$APP_PY" state 2>/dev/null) || { N=0; break; }
    [ "$N" = "0" ] && break
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
docker compose up -d --remove-orphans
# 新しいコンテナ（または変更なしで動き続けたコンテナ）で、実行の開始を再び受け付ける
for i in $(seq 1 30); do
  if docker compose exec -T app python3 -c "$APP_PY" drain 1 >/dev/null 2>&1; then break; fi
  sleep 2
done
docker image prune -f >/dev/null 2>&1 || true
docker compose ps
