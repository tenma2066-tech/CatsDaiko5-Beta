#!/usr/bin/env bash
# DM画面の作りを調べる（root）。常駐プロセスに依頼して、結果（部品の名前と数だけ）をログから出す。
sudo -u xbot bash -c 'set -a; . /etc/xbot/env; set +a; cd /opt/xbot && /opt/xbot/venv/bin/python -c "from xbot import store; store.put(\"request\", \"dm_probe\")"'
START=$(date "+%Y-%m-%d %H:%M:%S")
for i in $(seq 1 36); do
  sleep 5
  journalctl -u xbot-worker --since "$START" --no-pager | grep -q "DM画面を調べ" && break
done
journalctl -u xbot-worker --since "$START" --no-pager | grep -E "probe|DM画面" | cut -c1-3000
