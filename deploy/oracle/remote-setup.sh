#!/usr/bin/env bash
# サーバー上で root 権限で実行される（GitHub Actions から）。何度実行しても安全。
# 標準入力: 1行目 ADMIN_PASSWORD（値はファイルにだけ保存し、表示しない）
# 引数: $1 = SITE_HOST, $2 = 新しいソースを展開したディレクトリ
set -euo pipefail
SITE_HOST="$1"
NEW_SRC="$2"
APP_DIR=/opt/catsdaiko
ADMIN_PASSWORD="$(cat)"

# 1) Docker（未導入なら）
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
fi
systemctl enable --now docker >/dev/null 2>&1 || true

# 2) OS のファイアウォールで 80/443 を開ける（Oracle の Ubuntu イメージは既定で閉じている）
if command -v iptables >/dev/null 2>&1; then
  for p in 80 443; do
    iptables -C INPUT -p tcp --dport "$p" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport "$p" -j ACCEPT
  done
  if command -v netfilter-persistent >/dev/null 2>&1; then netfilter-persistent save >/dev/null 2>&1 || true; fi
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-service=http --add-service=https >/dev/null
  firewall-cmd --reload >/dev/null
fi

# 3) 配置
mkdir -p "$APP_DIR/data"
chmod 700 "$APP_DIR" "$APP_DIR/data"
rm -rf "$APP_DIR/src"
mv "$NEW_SRC" "$APP_DIR/src"
cp "$APP_DIR/src/deploy/oracle/docker-compose.yml" "$APP_DIR/docker-compose.yml"
cp "$APP_DIR/src/deploy/oracle/Caddyfile" "$APP_DIR/Caddyfile"

# 4) 秘密情報（初回だけ生成。以後は上書きしない＝保存済みPayPay情報を読めなくしないため）
cd "$APP_DIR"
touch .env && chmod 600 .env
grep -q '^SECRET_KEY=' .env || echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
grep -q '^PAYPAY_ENCRYPTION_KEY=' .env || echo "PAYPAY_ENCRYPTION_KEY=$(openssl rand 32 | base64 | tr '+/' '-_')" >> .env
sed -i '/^SITE_HOST=/d' .env
echo "SITE_HOST=$SITE_HOST" >> .env
if [ -n "$ADMIN_PASSWORD" ]; then
  umask 077
  printf '%s' "$ADMIN_PASSWORD" > admin_password
fi
touch admin_password && chmod 600 admin_password

# 5) 起動
docker compose up -d --build --remove-orphans
docker image prune -f >/dev/null 2>&1 || true
docker compose ps
