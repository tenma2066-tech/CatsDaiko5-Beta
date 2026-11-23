#!/bin/sh
# アプリ起動の入口。IPローテーション用の Tor を使う構成なら、先に Tor を起動してから
# gunicorn を起動する。既定（TSUM_USE_TOR 未設定）は何もせず直接接続のまま起動する。
#
# なぜ Tor か：LINE はデータセンター/VPS の固定IPからのログインを怪しみ、画像認証を
# 毎回出す／reCAPTCHA へ切り替えることがある。送信元IPを毎回変えられれば弾かれにくい。
# tsum 側は TSUM_SOCKS_URL を見て、やり直しごとに別の Tor 回線（別IP）を割り当てる。
#
# 安全策：Tor が起動しない・遅い場合でも、アプリは SOCKS ポートに繋がらないと判断して
# 直接接続にフォールバックする（line_password_login._proxy_reachable）。ここで失敗しても
# サービスは止めない。
set -e

_use_tor=$(printf '%s' "${TSUM_USE_TOR:-}" | tr 'A-Z' 'a-z')
case "$_use_tor" in
  1|true|yes|on)
    if command -v tor >/dev/null 2>&1; then
      echo "[entrypoint] Tor を起動します（LINEログインのIPローテーション用）。"
      tor --SocksPort 9050 --quiet >/tmp/tor.log 2>&1 &
      # 明示設定があればそれを優先。無ければ同居の Tor を指す。
      if [ -z "${TSUM_SOCKS_URL:-}" ]; then
        TSUM_SOCKS_URL="socks5h://127.0.0.1:9050"
        export TSUM_SOCKS_URL
      fi
      # SOCKS ポートが開くまで最大15秒待つ（開かなくても直接接続で続行する）。
      python3 - <<'PY' || echo "[entrypoint] Tor の SOCKS ポートが時間内に開きませんでした。直接接続で続行します。" >&2
import socket, time
for _ in range(30):
    try:
        with socket.create_connection(("127.0.0.1", 9050), timeout=1):
            print("[entrypoint] Tor の SOCKS ポート(9050)が開きました。")
            raise SystemExit(0)
    except OSError:
        time.sleep(0.5)
raise SystemExit(1)
PY
    else
      echo "[entrypoint] TSUM_USE_TOR=$TSUM_USE_TOR ですが tor コマンドが見つかりません。直接接続で続行します。" >&2
    fi
    ;;
esac

exec gunicorn app:app --workers 1 --threads 4 --timeout 120 --graceful-timeout 110 --bind "0.0.0.0:${PORT:-8080}"
