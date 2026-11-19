#!/usr/bin/env bash
# xbot を VPS に入れる / 更新する（root で実行。何度実行しても同じ結果になる）。
# 使い方: XBOT_ADMIN_PASSWORD=... XBOT_DOMAIN=... bash setup.sh /tmp/xbot-src
# にゃんこ代行（/opt/catsdaiko・Docker）には触らない。Caddy への追加は caddy-site.sh で別に行う。
set -euo pipefail
SRC="${1:?ソースの場所}"
APP=/opt/xbot
DATA=/var/lib/xbot
ENVF=/etc/xbot/env
: "${XBOT_ADMIN_PASSWORD:?管理パスワード（XBOT_ADMIN_PASSWORD）がありません}"

pkill -f /tmp/xbot-check.py 2>/dev/null || true   # 前回の確認が残っていたら止める
echo "== 1. 必要なものを入れる（apt）"
export DEBIAN_FRONTEND=noninteractive
PKGS="python3-venv python3-pip xvfb x11vnc novnc python3-websockify fonts-noto-cjk"
if dpkg -s $PKGS >/dev/null 2>&1; then
  echo "  入っています（apt は使いません）"
else
  # 自動アップデートが apt を使っているときは、最大5分だけ待つ（ずっと止まらないように）
  apt-get -o DPkg::Lock::Timeout=300 update -qq
  apt-get -o DPkg::Lock::Timeout=300 install -y -qq $PKGS >/dev/null
fi

echo "== 2. 専用ユーザー xbot（ログインできない一般ユーザー）"
id xbot >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --create-home --shell /usr/sbin/nologin xbot
install -d -o xbot -g xbot -m 700 "$DATA" "$DATA/data"
install -d -o xbot -g xbot -m 700 "$DATA/data/profile"

echo "== 3. プログラムを /opt/xbot に置く"
install -d -m 755 "$APP"
rm -rf "$APP/xbot.new" && cp -r "$SRC/xbot" "$APP/xbot.new"
cp "$SRC/test_logic.py" "$SRC/requirements.txt" "$APP/"
rm -rf "$APP/xbot.old"; [ -d "$APP/xbot" ] && mv "$APP/xbot" "$APP/xbot.old"; mv "$APP/xbot.new" "$APP/xbot"
chown -R root:root "$APP/xbot" && chmod -R a+rX "$APP"

echo "== 4. Python の専用環境（venv）と Chromium"
[ -x "$APP/venv/bin/python" ] || python3 -m venv "$APP/venv"
"$APP/venv/bin/pip" install -q --upgrade pip
"$APP/venv/bin/pip" install -q -r "$APP/requirements.txt"
export PLAYWRIGHT_BROWSERS_PATH="$APP/browsers"
[ -d "$APP/browsers" ] && touch "$APP/.deps-ok"   # すでにブラウザが入っている＝部品も入っている
if [ ! -f "$APP/.deps-ok" ]; then   # ブラウザに必要な部品（apt を使う）は最初の1回だけ
  "$APP/venv/bin/python" -m playwright install-deps chromium >/dev/null
  "$APP/venv/bin/python" -m playwright install-deps webkit >/dev/null
  touch "$APP/.deps-ok"
fi
"$APP/venv/bin/python" -m playwright install chromium >/dev/null
"$APP/venv/bin/python" -m playwright install webkit >/dev/null
chmod -R a+rX "$APP/browsers"

echo "== 4b. Cloudflare WARP（プロキシモード・xbot のブラウザだけが使う）"
echo "  変更前の経路: $(ip route show default | head -1)"
bash "$SRC/deploy/warp.sh" || echo "  WARP は入れられませんでした（直接つなぎます）"
echo "  変更後の経路: $(ip route show default | head -1)"

echo "== 5. 設定ファイル /etc/xbot/env（root と xbot だけが読める）"
GW=$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
GW=${GW:-127.0.0.1}
install -d -m 750 -o root -g xbot /etc/xbot
umask 027
cat > "$ENVF" <<ENV
XBOT_ADMIN_PASSWORD=$XBOT_ADMIN_PASSWORD
XBOT_DATA_DIR=$DATA/data
XBOT_BIND=$GW
XBOT_VNC_BIND=$GW
PLAYWRIGHT_BROWSERS_PATH=$APP/browsers
XBOT_NOVNC_DIR=/usr/share/novnc
TZ=Asia/Tokyo
ENV
chown root:xbot "$ENVF"; chmod 640 "$ENVF"
umask 022

echo "== 6. テスト（Xには接続しない）"
(cd /tmp && XBOT_DATA_DIR=$(mktemp -d) PYTHONPATH="$APP" "$APP/venv/bin/python" "$APP/test_logic.py" 2>&1 | tail -3)

echo "== 7. 常駐サービス（systemd）"
cp "$SRC/deploy/xbot-web.service" "$SRC/deploy/xbot-worker.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now xbot-web.service xbot-worker.service
systemctl restart xbot-web.service xbot-worker.service
sleep 3
systemctl --no-pager --lines=0 status xbot-web.service xbot-worker.service | grep -E "●|Active:" || true
curl -fsS "http://$GW:8090/health" && echo "  管理サイト: 起動OK（$GW:8090）"

echo "== 8. ブラウザが起動するか（x.com を開くだけ・何も送らない）"
cat > /tmp/xbot-check.py <<'PY'
from xbot.browser import Browser
for eng in ("iphone", "pc"):
    try:
        with Browser(engine=eng) as b:
            print("  ブラウザ", eng, "起動OK /", b.exit_info(), "/ Xにログイン済み:", b.logged_in())
    except Exception as e:
        print("  ブラウザ", eng, "起動できませんでした:", str(e)[:300])
PY
chmod 644 /tmp/xbot-check.py
# 動いている常駐プロセスとブラウザのプロフィールを取り合わないよう、使い捨てのプロフィールで確かめる（2分で打ち切り）
CHK=$(mktemp -d); chown xbot:xbot "$CHK"
sudo -u xbot bash -c "set -a; . $ENVF; set +a; export XBOT_DATA_DIR=$CHK; cd $APP && PYTHONPATH=$APP timeout 120 $APP/venv/bin/python /tmp/xbot-check.py" 2>&1 | tail -4
rm -rf "$CHK"
rm -f /tmp/xbot-check.py
free -m | sed -n 2p
