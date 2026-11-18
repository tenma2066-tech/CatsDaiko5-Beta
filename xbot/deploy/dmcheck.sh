#!/usr/bin/env bash
# 下書きモードのときだけ、DM の確認を1回動かして結果（件数だけ）を出す。本番モードなら何もしない。
R=$(sudo -u xbot bash -c 'set -a; . /etc/xbot/env; set +a; cd /opt/xbot && /opt/xbot/venv/bin/python -c "
from xbot import store
if store.get(\"dry_run\"):
    store.put(\"request\", \"check_dm\"); print(\"started\")
else:
    print(\"production\")
"')
echo "dmcheck: $R"
[ "$R" = "started" ] || exit 0
START=$(date "+%Y-%m-%d %H:%M:%S")
for i in $(seq 1 60); do
  sleep 5
  journalctl -u xbot-worker --since "$START" --no-pager | grep -qE "DMを確認しました|DMの確認に失敗|暗証番号が必要|ログインしていません" && break
done
# 公開のログに出るので、DM の相手の名前・ID は伏せて、件数の行だけ出す
journalctl -u xbot-worker --since "$START" --no-pager | grep -E "DMを確認しました|失敗|つまずき|暗証番号|ログインしていません" \
  | sed -E 's/dm: .*に(ルール)/dm: *** に\1/; s/(許可しました)：.*/\1：***/; s/@[A-Za-z0-9_]{1,15}/@***/g; s/[0-9]{6,}/#/g' | cut -c1-400
