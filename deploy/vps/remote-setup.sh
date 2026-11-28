#!/usr/bin/env bash
# サーバー上で root 権限で実行される（GitHub Actions から）。何度実行しても安全。
# 引数: $1 = SITE_HOST, $2 = 新しいソースを展開したディレクトリ, $3 = 管理者パスワードを書いたファイル（読んだら削除）,
#       $4 = ANTHROPIC_API_KEY を書いたファイル（任意・読んだら削除。空なら .env は変更しない）
#       $5 = TURNSTILE_SITE_KEY / TURNSTILE_SECRET_KEY を書いたファイル（任意・読んだら削除。空なら .env は変更しない）
#       $6 = ミニゲーム（ダイナマイト野球）の暗証番号を書いたファイル（任意・読んだら削除。空なら .env は変更しない）
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

# 2.5) SSH の総当たり対策（fail2ban：同じ相手が5回まちがえたら、しばらく接続を断る）。入っていなければ入れる
if ! command -v fail2ban-client >/dev/null 2>&1; then
  apt-get -o DPkg::Lock::Timeout=120 install -y fail2ban python3-systemd >/dev/null 2>&1 || true
fi
if command -v fail2ban-client >/dev/null 2>&1; then
  mkdir -p /etc/fail2ban/jail.d
  [ -f /etc/fail2ban/jail.d/catsdaiko-sshd.local ] || printf '[sshd]\nenabled = true\nbackend = systemd\nmaxretry = 5\nfindtime = 10m\nbantime = 1h\n' > /etc/fail2ban/jail.d/catsdaiko-sshd.local
  systemctl enable --now fail2ban >/dev/null 2>&1 || true
  fail2ban-client reload >/dev/null 2>&1 || true
  echo "fail2ban: $(systemctl is-active fail2ban 2>/dev/null) / $(fail2ban-client status sshd 2>/dev/null | grep -E 'Total banned' | tr -s ' \t' ' ' | sed 's/^[ |`-]*//')"
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
# 手で書き足した最後の行に改行が無いと、下の追記がその行につながって両方とも壊れる → 必ず改行で終わるようにしておく
if [ -s .env ] && [ -n "$(tail -c1 .env)" ]; then echo >> .env; fi
grep -q '^SECRET_KEY=' .env || echo "SECRET_KEY=$(openssl rand -hex 32)" >> .env
grep -q '^PAYPAY_ENCRYPTION_KEY=' .env || echo "PAYPAY_ENCRYPTION_KEY=$(openssl rand 32 | base64 | tr '+/' '-_')" >> .env
sed -i '/^SITE_HOST=/d' .env
echo "SITE_HOST=$SITE_HOST" >> .env
# Q&AチャットのAI回答キー。第4引数にキーを書いた一時ファイルのパスがあれば .env に反映する。
# 無い・空のときは何もしない＝手動で .env に書いた値を消さない。
ANTHROPIC_KEY_FILE="${4:-}"
if [ -n "$ANTHROPIC_KEY_FILE" ] && [ -s "$ANTHROPIC_KEY_FILE" ]; then
  sed -i '/^ANTHROPIC_API_KEY=/d' .env
  printf 'ANTHROPIC_API_KEY=%s\n' "$(cat "$ANTHROPIC_KEY_FILE")" >> .env
  shred -u "$ANTHROPIC_KEY_FILE" 2>/dev/null || rm -f "$ANTHROPIC_KEY_FILE"
fi
# 購入時の人間確認（Cloudflare Turnstile）の鍵。第5引数のファイル（KEY=値 が2行）があれば .env に反映する。
# 無い・空のときは何もしない＝手動で .env に書いた値を消さない。
TURNSTILE_FILE="${5:-}"
if [ -n "$TURNSTILE_FILE" ] && [ -s "$TURNSTILE_FILE" ]; then
  # 先に形を確かめる（確かめる前に古い鍵を消すと、貼り付けの空白や改行コード1つで、人間確認が黙って無効になる）
  TS_LINES="$(tr -d '\r ' < "$TURNSTILE_FILE" | grep -E '^TURNSTILE_(SITE|SECRET)_KEY=[A-Za-z0-9_.-]+$' || true)"
  if [ "$(printf '%s\n' "$TS_LINES" | grep -c 'KEY=' || true)" = "2" ]; then
    sed -i '/^TURNSTILE_SITE_KEY=/d; /^TURNSTILE_SECRET_KEY=/d' .env
    printf '%s\n' "$TS_LINES" >> .env
  else
    echo "::warning::Turnstile の鍵の形が正しくないため、.env は変更しませんでした（これまでの設定のまま動きます）。"
  fi
  shred -u "$TURNSTILE_FILE" 2>/dev/null || rm -f "$TURNSTILE_FILE"
fi
# ミニゲームの暗証番号（数字4〜12桁）。第6引数のファイルがあれば .env の DYA_PIN に反映する。
DYA_PIN_FILE="${6:-}"
if [ -n "$DYA_PIN_FILE" ] && [ -s "$DYA_PIN_FILE" ]; then
  DYA_PIN_VAL="$(tr -d '\r\n ' < "$DYA_PIN_FILE")"
  if printf '%s' "$DYA_PIN_VAL" | grep -Eq '^[0-9]{4,12}$'; then
    sed -i '/^DYA_PIN=/d' .env
    printf 'DYA_PIN=%s\n' "$DYA_PIN_VAL" >> .env
  else
    echo "::warning::DYA_PIN は数字4〜12桁にしてください。.env は変更しませんでした。"
  fi
  shred -u "$DYA_PIN_FILE" 2>/dev/null || rm -f "$DYA_PIN_FILE"
fi
OLD_PW_SUM="$(sha256sum admin_password 2>/dev/null | cut -d' ' -f1 || true)"
if [ -n "$ADMIN_PASSWORD" ]; then
  (umask 077; printf '%s' "$ADMIN_PASSWORD" > admin_password)
fi
[ -f admin_password ] || (umask 077; : > admin_password)
chmod 600 admin_password
# 管理者パスワードは、アプリが起動のときに読む。変わったのにコンテナが作り直されないと、古いパスワードのまま動き続ける（下の 7 で作り直す）
PW_CHANGED=0
if [ "$OLD_PW_SUM" != "$(sha256sum admin_password | cut -d' ' -f1)" ]; then PW_CHANGED=1; fi

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
# ミニゲームの戦績（osero の stats.json）も残す
[ -d "$D/osero-data" ] && cp -a "$D/osero-data" "$T/" 2>/dev/null || true
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
# 管理画面の「サーバーの点検」に、最後に保存した時刻を出す（アプリからは /data/last_backup として見える）
date +%s > "$D/data/last_backup" 2>/dev/null || true
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

# 4.7) Tor（LINEログインのIPローテーション用）。
#      ビルド中のコンテナからは Debian のミラー(deb.debian.org)へ繋がらず apt が失敗するため、
#      apt が通るホスト(Ubuntu)側に入れる。app コンテナからは host.docker.internal 経由で使う。
if ! command -v tor >/dev/null 2>&1; then
  apt-get update -y >/dev/null 2>&1 || true
  apt-get -o DPkg::Lock::Timeout=120 install -y tor >/dev/null 2>&1 || true
fi
if command -v tor >/dev/null 2>&1; then
  # SOCKS は localhost と docker のホストゲートウェイ（app コンテナが host.docker.internal で
  # 解決する先。この環境では docker0=192.168.0.1）だけで待ち受け、公開インターフェースには出さない。
  # ゲートウェイIPは環境ごとに違うので自動検出する。検出できないときだけ 0.0.0.0 に退避
  # （特定IPが無くて tor が起動失敗し、回線の回転が黙って消えるのを避けるため）。
  # さらに SocksPolicy で private レンジのみ許可（二重の防御）。IsolateSOCKSAuth により、
  # SOCKS の user:pass が違うと別の出口回線(別IP)になる（tsum_login.fresh_socks_identity が利用）。
  DOCKER_GW="$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
  if [ -n "$DOCKER_GW" ]; then
    SOCKS_LINES=$(printf '%s\n' \
      'SocksPort 127.0.0.1:9050 IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth' \
      "SocksPort ${DOCKER_GW}:9050 IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth")
  else
    SOCKS_LINES='SocksPort 0.0.0.0:9050 IsolateSOCKSAuth KeepAliveIsolateSOCKSAuth'
  fi
  NEW_TORRC=$(printf '%s\n%s\n%s\n' \
    '# managed by catsdaiko (LINEログインのIPローテーション)。編集しても次のデプロイで上書きされる。' \
    "$SOCKS_LINES" \
    "$(printf '%s\n' \
      'SocksPolicy accept 127.0.0.0/8' \
      'SocksPolicy accept 10.0.0.0/8' \
      'SocksPolicy accept 172.16.0.0/12' \
      'SocksPolicy accept 192.168.0.0/16' \
      'SocksPolicy reject *')")
  TOR_CHANGED=0
  if [ "$(cat /etc/tor/catsdaiko.conf 2>/dev/null || true)" != "$NEW_TORRC" ]; then
    printf '%s\n' "$NEW_TORRC" > /etc/tor/catsdaiko.conf
    TOR_CHANGED=1
  fi
  grep -qxF '%include /etc/tor/catsdaiko.conf' /etc/tor/torrc 2>/dev/null \
    || { echo '%include /etc/tor/catsdaiko.conf' >> /etc/tor/torrc; TOR_CHANGED=1; }
  # tor は docker の後に起動させる（systemd のテンプレ単位 tor@default と、まとめ単位 tor の両方に効かせる）
  for unit in tor@default tor; do
    mkdir -p "/etc/systemd/system/${unit}.service.d"
    DROPIN=$(printf '%s\n' '[Unit]' 'After=docker.service' 'Wants=docker.service' '[Service]' 'Restart=on-failure' 'RestartSec=5')
    if [ "$(cat "/etc/systemd/system/${unit}.service.d/catsdaiko.conf" 2>/dev/null || true)" != "$DROPIN" ]; then
      printf '%s\n' "$DROPIN" > "/etc/systemd/system/${unit}.service.d/catsdaiko.conf"
      TOR_CHANGED=1
    fi
  done
  systemctl daemon-reload >/dev/null 2>&1 || true
  systemctl enable tor@default >/dev/null 2>&1 || systemctl enable tor >/dev/null 2>&1 || true
  # 設定を反映させる（tor@default を優先。無ければ tor）。確実に効かせるため restart する。
  systemctl restart tor@default >/dev/null 2>&1 || systemctl restart tor >/dev/null 2>&1 || true
  sleep 2
  TOR_ACT="$(systemctl is-active tor@default 2>/dev/null || true)"
  [ "$TOR_ACT" = active ] || TOR_ACT="$(systemctl is-active tor 2>/dev/null || echo inactive)"
  echo "tor: ${TOR_ACT}  listen=[$(ss -ltn 2>/dev/null | grep ':9050' | awk '{print $4}' | tr '\n' ' ')]"
else
  echo "::warning::tor をホストに入れられませんでした。TSUM_SOCKS_URL は無効になり、LINEログインは直接接続のまま動きます。"
fi

# 4.9) ミニゲーム。ソースがそろったものだけ起動する（COMPOSE_PROFILES に足す）。
#      dya は暗証番号（.env の DYA_PIN）も必要。osero は外部通信なしのため暗証番号なしで公開してよい。
sed -i '/^COMPOSE_PROFILES=/d; /^DYA_ENABLED=/d; /^OSERO_ENABLED=/d' .env
PROFILES=""
# --- ダイナマイト野球（dya）---
if [ -f "$APP_DIR/src/dya/proxy.js" ] && [ -f "$APP_DIR/src/dya/Dockerfile" ] && grep -Eq '^DYA_PIN=[0-9]{4,12}$' .env; then
  PROFILES="${PROFILES:+$PROFILES,}dya"
  echo "DYA_ENABLED=1" >> .env
  echo "dya: enabled (/dya/)"
else
  echo "DYA_ENABLED=0" >> .env
  docker compose --profile dya rm -sf dya >/dev/null 2>&1 || true
  if [ -f "$APP_DIR/src/dya/proxy.js" ]; then
    echo "::warning::ミニゲーム（dya）のソースはありますが、暗証番号（シークレット DYA_PIN）が未設定のため動かしません。"
  else
    echo "dya: disabled (no source)"
  fi
fi
# --- UltraOSERO（osero）---
if [ -f "$APP_DIR/src/osero/server.js" ] && [ -f "$APP_DIR/src/osero/Dockerfile" ]; then
  PROFILES="${PROFILES:+$PROFILES,}osero"
  echo "OSERO_ENABLED=1" >> .env
  echo "osero: enabled (/osero/)"
else
  echo "OSERO_ENABLED=0" >> .env
  docker compose --profile osero rm -sf osero >/dev/null 2>&1 || true
  echo "osero: disabled (no source)"
fi
[ -n "$PROFILES" ] && echo "COMPOSE_PROFILES=$PROFILES" >> .env

# 4.95) ミニゲームが書き込む場所。コンテナは node ユーザ（uid 1000）で動くので、持ち主を合わせておく
#       （root のままだと書き込めず、戦績や採取ファイルが保存されない）
mkdir -p "$APP_DIR/osero-data" "$APP_DIR/dya-data"
chown 1000:1000 "$APP_DIR/osero-data" "$APP_DIR/dya-data" 2>/dev/null || true

# 5) 先にビルド（今のサイトは動いたまま）
docker compose build

# 5.5) 入れ替える前に、新しい版がアプリとして読み込めて、ページを返せるかを確かめる。
#      読み込めない版に入れ替えると、直した版がもう一度届くまでサイトが落ちたままになる。
#      確かめるのは使い捨てのコンテナで、データの置き場所は中の一時フォルダーに向ける（本物の注文・台帳には触れない）。
SMOKE='import app
c = app.app.test_client()
assert c.get("/health").status_code == 200
assert c.get("/").status_code == 200
print("smoke ok")'
if ! docker compose run --rm --no-deps -T \
     -e PAYPAY_DATA_DIR=/tmp/smoke -e PAYPAY_LEDGER_PATH=/tmp/smoke/l.sqlite3 -e PUNI_DATA_DIR=/tmp/smoke/puni -e TSUM_DATA_DIR=/tmp/smoke/tsum \
     app sh -c 'mkdir -p /tmp/smoke/puni /tmp/smoke/tsum && exec python3 -c "$0"' "$SMOKE" > /tmp/catsdaiko-smoke.log 2>&1; then
  echo "::error::新しい版が起動できませんでした。入れ替えを中止しました（サイトは今の版のまま動いています）。"
  tail -25 /tmp/catsdaiko-smoke.log | cut -c1-300 || true
  exit 4
fi
echo "smoke test: OK"

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
    # 1回聞けなかっただけで「何も動いていない」と決めない（4つの受付がたまたま全部ふさがっていると、5秒で返事が来ない）。
    # 3回続けて聞けなければ、アプリ自体が応答していないとみなして入れ替える（壊れた版を直す入れ替えは止めない）。
    N=""
    for t in 1 2 3; do
      if N=$(docker compose exec -T app python3 -c "$APP_PY" state 2>/dev/null); then break; fi
      N=""; sleep 4
    done
    [ -n "$N" ] || { N=0; break; }
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
APP_BEFORE="$(docker compose ps -q app 2>/dev/null || true)"
docker compose up -d --remove-orphans
if [ "$PW_CHANGED" = "1" ] && [ -n "$APP_BEFORE" ] && [ "$APP_BEFORE" = "$(docker compose ps -q app 2>/dev/null || true)" ]; then
  # 管理者パスワードだけが変わった（プログラムは同じ）→ コンテナが作り直されていないので、読み直させるために作り直す
  echo "admin password changed -> recreate app"
  docker compose up -d --no-deps --force-recreate app
fi
# Caddy はバインドマウントの Caddyfile を自動で読み直さない（compose up -d ではファイル変更だけでは作り直さない）。
# ルーティングを変えるデプロイ（/dya/ の追加など）が無反応にならないよう、毎回 reload で反映する。
# reload は無停止。失敗した場合だけ restart（こちらは数秒の瞬断）でフォールバック。
if [ -n "$(docker compose ps -q caddy 2>/dev/null || true)" ]; then
  if docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
    echo "caddy: reloaded Caddyfile"
  else
    docker compose restart caddy >/dev/null 2>&1 || true
    echo "caddy: restarted (reload unavailable)"
  fi
fi
# 新しいコンテナ（または変更なしで動き続けたコンテナ）で、実行の開始を再び受け付ける
for i in $(seq 1 30); do
  if docker compose exec -T app python3 -c "$APP_PY" drain 1 >/dev/null 2>&1; then break; fi
  sleep 2
done
# app コンテナからホストの Tor(SOCKS 9050)へ届くか確認し、届かなければ原因切り分け用の情報を出す
# （届かなくてもアプリは直接接続で動く＝サービスは止まらず、回線の回転だけ無効）
echo "--- tor 到達診断 ---"
echo "host docker0: $(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}')"
docker compose exec -T app python3 - <<'PY' 2>&1 | sed 's/^/app: /' || true
import socket, subprocess
try:
    gw = subprocess.run(["getent","hosts","host.docker.internal"],capture_output=True,text=True,timeout=5).stdout.strip()
    print("host.docker.internal ->", gw or "(未解決)")
except Exception as e:
    print("getent 失敗", type(e).__name__)
for host in ("host.docker.internal", "172.17.0.1"):
    try:
        socket.create_connection((host, 9050), timeout=4)
        print(host, "9050 OK")
    except Exception as e:
        print(host, "9050 FAIL", type(e).__name__)
PY
if docker compose exec -T app python3 -c "import socket; socket.create_connection(('host.docker.internal',9050),timeout=4)" >/dev/null 2>&1; then
  echo "tor reachable from app: OK"
else
  echo "::warning::app コンテナからホストの Tor(9050)へ届きません。LINEログインは直接接続のまま動きます（回線の回転は無効）。上の診断を参照。"
fi

docker image prune -f >/dev/null 2>&1 || true
docker compose ps
