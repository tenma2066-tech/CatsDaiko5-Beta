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
docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
echo "Caddy に $XBOT_DOMAIN を追加しました（reload のみ・再起動なし）"
