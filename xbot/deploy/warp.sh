#!/usr/bin/env bash
# Cloudflare WARP（無料）を「プロキシモード」で入れる（root）。
# プロキシモードはサーバー全体の通信経路を変えない。127.0.0.1:40000 を使ったアプリ（xbot のブラウザ）だけが WARP を通る。
set -uo pipefail
if ! command -v warp-cli >/dev/null 2>&1; then
  curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | gpg --yes --dearmor -o /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg || exit 1
  echo "deb [signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(. /etc/os-release; echo $VERSION_CODENAME) main" > /etc/apt/sources.list.d/cloudflare-client.list
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq cloudflare-warp >/dev/null || exit 1
fi
systemctl enable --now warp-svc >/dev/null 2>&1; sleep 3
W="warp-cli --accept-tos"
$W registration show >/dev/null 2>&1 || $W registration new >/dev/null
# 必ず「接続する前に」プロキシモードにする（全体を WARP に通すモードで接続しない）
$W mode proxy >/dev/null
$W proxy port 40000 >/dev/null
$W connect >/dev/null
sleep 5
echo "  WARP の状態: $($W status 2>&1 | head -2 | tr '\n' ' ')"
echo "  WARP 経由: $(curl -s -m 15 --socks5-hostname 127.0.0.1:40000 https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(warp|loc)=' | tr '\n' ' ')"
echo "  直接（お店と同じ）: $(curl -s -m 15 https://www.cloudflare.com/cdn-cgi/trace | grep -E '^(warp|loc)=' | tr '\n' ' ')"
