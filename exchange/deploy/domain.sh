#!/usr/bin/env bash
# 専用ホストを既存の Caddy に追加。店と xbot のサービスは再起動しない。
set -euo pipefail
read -r domain
[[ "$domain" =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.duckdns\.org$ ]] || { echo 'DuckDNS のホスト名を指定してください'; exit 1; }
cd /opt/catsdaiko
sites=/opt/catsdaiko/caddy-sites
exec 9>/opt/paykyash/domain.lock
flock 9
target="$sites/exchange.caddy"
work=$(mktemp -d "$sites/.exchange.XXXXXXXX")
trap 'rm -rf "$work"' EXIT
if [[ -f "$target" ]]; then cp -p "$target" "$work/previous"; fi
cat >"$work/new" <<CADDY
$domain {
    encode gzip
    reverse_proxy host.docker.internal:8091
}
CADDY
chmod 644 "$work/new"
mv -f "$work/new" "$target"
caddy=(docker compose exec -T caddy caddy)
if ! "${caddy[@]}" validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null ||
   ! "${caddy[@]}" reload --config /etc/caddy/Caddyfile --adapter caddyfile --address 127.0.0.1:2019; then
  if [[ -e "$work/previous" ]]; then mv "$work/previous" "$target"; else rm -f "$target"; fi
  exit 1
fi
