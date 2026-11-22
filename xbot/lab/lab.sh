#!/usr/bin/env bash
# DM の開き方の実験（root で実行）。本番の常駐はそのまま。プロフィールの写しで、読むだけの実験を1回行い、写しは消す。
# 使い方: bash lab.sh /tmp/dm_lab.py [件数] [方法]
set -u
LAB_PY=${1:?dm_lab.py の場所}; N=${2:-3}; M=${3:-M0,M1,M2,M3}
APP=/opt/xbot; ENVF=/etc/xbot/env; SRC=/var/lib/xbot/data
TMP=$(mktemp -d /tmp/xbot-lab.XXXXXX)
trap 'rm -rf "$TMP"' EXIT
# 写すのはログイン情報（プロフィール）と設定だけ。画像・画面の記録は写さない
for x in profile profile-iphone xbot.sqlite3; do
  [ -e "$SRC/$x" ] && cp -a "$SRC/$x" "$TMP/"
done
rm -f "$TMP"/profile*/Singleton* 2>/dev/null
chown -R xbot:xbot "$TMP"; chmod 700 "$TMP"
cp "$LAB_PY" "$TMP/dm_lab.py"; chown xbot:xbot "$TMP/dm_lab.py"
START=$(date "+%Y-%m-%d %H:%M:%S")
sudo -u xbot bash -c "set -a; . $ENVF; set +a; export XBOT_DATA_DIR=$TMP; cd $APP && PYTHONPATH=$APP timeout 1500 $APP/venv/bin/python $TMP/dm_lab.py $N $M" 2>&1 \
  | grep -E '^\[lab\]' | sed -E 's/[0-9]{8,}/#/g' | cut -c1-1500
# 実験中に、本番の常駐が同時に DM を見ていたか（件数だけ。名前は出さない）
echo "[lab] 実験中の本番の動き：DM確認 $(journalctl -u xbot-worker --since "$START" --no-pager 2>/dev/null | grep -c 'DMを確認しました') 回・投稿 $(journalctl -u xbot-worker --since "$START" --no-pager 2>/dev/null | grep -cE 'post: #')件"
