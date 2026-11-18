#!/usr/bin/env bash
# 管理サイトの URL（https://XBOT_DOMAIN/）を、にゃんこ代行の Caddy に「追加」する（root で実行）。
# Caddy は再起動せず、設定の読み直し（reload）だけ行う。お店の設定は書き換えない。
set -euo pipefail
: "${XBOT_DOMAIN:?XBOT_DOMAIN がありません}"
SITES=/opt/catsdaiko/caddy-sites
[ -d "$SITES" ] || { echo "Caddy に追加用のフォルダがありません（お店側の準備がまだです）"; exit 1; }
cat > "$SITES/xbot.caddy.new" <<CADDY
$XBOT_DOMAIN {
	encode gzip
	handle_path /novnc/* {
		forward_auth host.docker.internal:8090 {
			uri /auth
		}
		reverse_proxy host.docker.internal:6080
	}
	handle {
		reverse_proxy host.docker.internal:8090
	}
}
CADDY
mv "$SITES/xbot.caddy.new" "$SITES/xbot.caddy"
cd /opt/catsdaiko
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null </dev/null
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile --address 127.0.0.1:2019 --force 2>&1  </dev/null| tail -3
sleep 3
N=$(docker compose exec -T caddy wget -qO- http://127.0.0.1:2019/config/ </dev/null | grep -o "$XBOT_DOMAIN" | wc -l)
echo "動いている設定に $XBOT_DOMAIN が入っているか: $N"
[ "$N" -gt 0 ] || { echo "反映できませんでした"; exit 1; }
docker compose logs --since 1m caddy 2>&1 | grep -iE "tnshop|error|obtain" | tail -10 | cut -c1-300
echo "Caddy に $XBOT_DOMAIN を追加しました（reload のみ・再起動なし）"
