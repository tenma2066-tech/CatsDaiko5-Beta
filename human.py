"""購入のときの「人間であることの確認」（Cloudflare Turnstile）。

自動のプログラムによる大量の注文・送金リンクの総当たりを止める。ふつうのお客さんは、ほぼ自動で通る。
- 使うには、環境変数 TURNSTILE_SITE_KEY（画面に出す鍵）と TURNSTILE_SECRET_KEY（サーバーだけが持つ鍵）の両方が要る。
  どちらかが無ければ、この確認は行わない（今までどおり動く）。
- 画面のチェックを飛ばしても、サーバーが Cloudflare に問い合わせて確かめる（verify）。
- Cloudflare に問い合わせられないとき（障害・通信の不調）は、確認なしで通す＝売り上げを止めない。
  「ちがう」「期限切れ」「使用済み」とはっきり返ってきたものは断る。
"""
from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
WIDGET_ORIGIN = "https://challenges.cloudflare.com"
TIMEOUT = 6
_log = logging.getLogger("human")


def site_key() -> str:
    return os.environ.get("TURNSTILE_SITE_KEY", "").strip() if enabled() else ""


def enabled() -> bool:
    return bool(os.environ.get("TURNSTILE_SITE_KEY", "").strip() and os.environ.get("TURNSTILE_SECRET_KEY", "").strip())


def verify(token: str, ip: str = ""):
    """True＝人間と確認できた / False＝確認できなかった（未入力・期限切れ・使用済み・偽物）/ None＝Cloudflare に聞けなかった。"""
    token = str(token or "").strip()
    if not token or len(token) > 4096:
        return False
    body = {"secret": os.environ.get("TURNSTILE_SECRET_KEY", "").strip(), "response": token}
    if ip:
        body["remoteip"] = ip
    try:
        req = urllib.request.Request(VERIFY_URL, data=urllib.parse.urlencode(body).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        _log.warning("turnstile unreachable: %s", type(exc).__name__)
        return None
    if data.get("success") is True:
        return True
    codes = [str(c) for c in data.get("error-codes") or []]
    if any(c in ("internal-error",) for c in codes):                 # Cloudflare 側の不調
        return None
    if any(c in ("missing-input-secret", "invalid-input-secret") for c in codes):
        _log.error("turnstile secret key is wrong or missing: %s", codes)   # 設定の間違い → お客さんを止めない
        return None
    return False
