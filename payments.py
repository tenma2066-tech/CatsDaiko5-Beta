"""決済方法の切り替えと振り分け（PayPay ／ Kyash）。

- 管理画面のスイッチで、それぞれ受け付ける／止めるを決める（pay_methods.json に保存。再起動しても続く）。
- お客さんに見せるのは「スイッチがオン」かつ「受取用アカウントが接続済み」の方法だけ（available）。
- 貼られたリンクの形（pay.paypay.ne.jp ／ kyash.me）で、どちらで受け取るかを自動で決める。
- 注文に残すリンクのコード（link_code）は、PayPay はそのまま、Kyash は "ky:<code>"。
  保留の再確認・管理者の再確認は、このコードの形で振り分ける（スイッチを切っても、受け付け済みの注文は確認できる）。
"""
from __future__ import annotations

import json
import os
import threading
import time

import kyash_gateway
import paypay_gateway
from paypay_gateway import PayPayError

METHODS = ("paypay", "kyash")
LABELS = {"paypay": "PayPay", "kyash": "Kyash"}
_FILE = os.path.join(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay"), "pay_methods.json")
_DEFAULT = {"paypay": True, "kyash": True}
_cache: dict = {"at": 0.0, "value": None}
_cache_lock = threading.Lock()
AVAILABLE_TTL = 30


def switches() -> dict:
    """管理画面のスイッチ（接続状態は見ない）。"""
    try:
        with open(_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    return {m: bool(data.get(m, _DEFAULT[m])) for m in METHODS}


def set_switches(on: dict) -> dict:
    value = {m: bool(on.get(m)) for m in METHODS}
    os.makedirs(os.path.dirname(_FILE) or ".", exist_ok=True)
    tmp = _FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f)
    os.replace(tmp, _FILE)
    forget()
    return value


def forget() -> None:
    """接続・スイッチが変わったとき、お客さん向けの表示をすぐ作り直す。"""
    with _cache_lock:
        _cache.update(at=0.0, value=None)


def _connected(method: str) -> bool:
    try:
        st = paypay_gateway.status() if method == "paypay" else kyash_gateway.status()
    except Exception:  # noqa: BLE001  読めないときは「使えない」と見せる（貼ってから断るより親切）
        return False
    return bool(st.get("connected")) and not st.get("needs_admin")


def available() -> dict:
    """お客さんが今使える方法 {"paypay": bool, "kyash": bool}。30秒だけ覚えておく（ページを開くたびに読まない）。"""
    with _cache_lock:
        if _cache["value"] is not None and time.time() - _cache["at"] < AVAILABLE_TTL:
            return dict(_cache["value"])
    sw = switches()
    value = {m: sw[m] and _connected(m) for m in METHODS}
    with _cache_lock:
        _cache.update(at=time.time(), value=value)
    return dict(value)


# PayPay が使えない（止めている・接続が切れている）あいだは、Kyash で払う注文を全品この割合だけ割り引く
PAYPAY_OFF_PCT = 20
PAYPAY_OFF_LABEL = "PayPay休止中の割引"


def paypay_off_pct() -> int:
    """いまの割引率。Kyash が使えて PayPay が使えないときだけ PAYPAY_OFF_PCT、それ以外は 0。"""
    try:
        av = available()
    except Exception:  # noqa: BLE001
        return 0
    return PAYPAY_OFF_PCT if av["kyash"] and not av["paypay"] else 0


def available_json() -> str:
    """テンプレート用（script の data 属性に入れる）。"""
    return json.dumps(available())


def provider_of(code) -> str:
    """注文に残したリンクのコード → "paypay" / "kyash"。"""
    return "kyash" if isinstance(code, str) and code.startswith(kyash_gateway.PREFIX) else "paypay"


def detect(raw) -> str | None:
    """貼られた文字 → どちらのリンクか（分からなければ None）。"""
    if not isinstance(raw, str):
        return None
    if kyash_gateway.looks_like(raw):
        return "kyash"
    if "paypay.ne.jp" in raw.lower():
        return "paypay"
    return None


def normalize(raw):
    """注文に残すコード。読めなければ None。"""
    return kyash_gateway.normalize_link(raw) if detect(raw) == "kyash" else paypay_gateway._normalize_link(raw)


def _closed_error(method: str) -> PayPayError:
    on = [LABELS[m] for m, v in available().items() if v]
    if not on:
        return PayPayError("pay_closed", "ただいまお支払いを受け付けていません。しばらくしてからお試しください（送金はしないでください）。")
    return PayPayError("method_off", f"ただいま{LABELS[method]}でのお支払いは止めています。{'・'.join(on)}の送金リンクを貼り付けてください"
                                     f"（{LABELS[method]}で送金リンクを作った場合は、そのリンクを取り消してください）。")


async def receive_payment(raw_link: str, amount: int, purchase_id: str):
    method = detect(raw_link)
    if method is None:
        if switches()["paypay"]:   # どちらの形でもない → PayPay 側のくわしい理由（空・複数・形式違い）で断る
            return await paypay_gateway.receive_payment(raw_link, amount, purchase_id)
        text = raw_link.strip() if isinstance(raw_link, str) else ""
        if not text:
            raise PayPayError("empty_link", "送金リンクが入力されていません。")
        raise PayPayError("not_paypay_link", "Kyash（https://kyash.me/payments/…）の送金リンクを貼り付けてください。")
    if not switches()[method]:
        raise _closed_error(method)
    if method == "kyash":
        return await kyash_gateway.receive_payment(raw_link, amount, purchase_id)
    return await paypay_gateway.receive_payment(raw_link, amount, purchase_id)


async def confirm_received(code, purchase_id: str, amount: int | None = None):
    """保留・結果不明の再確認。スイッチがオフでも行う（受け付け済みのお金を宙に浮かせない）。"""
    if provider_of(code) == "kyash":
        return await kyash_gateway.confirm_received(code, purchase_id, amount)
    return await paypay_gateway.confirm_received(code, purchase_id, amount)
