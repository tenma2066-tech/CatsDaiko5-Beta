#!/usr/bin/env python3
"""
store.py -- 設定・注文・本人確認の紐付け・リンク使用記録の永続化 (JSON, アトミック書込)

  config.json         運用パラメータ (手数料/上限/チャンネル/本人確認の条件 など)
  orders.json         換金注文 (着金 → 払い出しの状態機械)
  aml_accounts.json   Discord垢 ↔ PayPay垢 / Kyash垢 の紐付け (= 個人情報そのもの)
  used_links.json     受け取ったリンクの記録 (二重受取・二重払いの防止)
  pending_holds.json  PayPayの一時保留で監視中の受取
  detections.json     拒否・保留・返金などの検知ログ

注文の状態:
  PAID       着金済・払い出し待ち
  HOLD       着金済・管理者の判断待ち (名義の保留 / 送信者不明 / 上限超過 など)
  SENDING    払い出しの実行中 (この状態の注文は誰も触れない)
  SENT       払い出し済
  UNCERTAIN  払い出しを投げたが成否が分からない (二重払いを避けるため要手動確認)
  FAILED     払い出し失敗 (要手動対応)
  RETURNING  送信者へ返金中 (Kyash→PayPay で名義が違った時)
  RETURNED   送信者へ返金済
  REFUNDED   管理者が手動で返金して閉じた
  CANCELED   管理者が取り消した

遷移は claim_order() の compare-and-set 経由のみ。update_order で status を
直接書き換えると /再送 の連打や自動払い出しとの競合で二重払いになる。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("EXCHANGE_DATA_DIR", "/var/lib/paykyash")
CONFIG_FILE = os.path.join(BASE, "config.json")
ORDERS_FILE = os.path.join(BASE, "orders.json")
ACCOUNTS_FILE = os.path.join(BASE, "aml_accounts.json")
USED_FILE = os.path.join(BASE, "used_links.json")
HOLDS_FILE = os.path.join(BASE, "pending_holds.json")
DETECT_FILE = os.path.join(BASE, "detections.json")
JST = timezone(timedelta(hours=9))

_lock = threading.RLock()

DIRECTIONS = ("p2k", "k2p")
DIRECTION_LABEL = {"p2k": "PayPay → Kyash", "k2p": "Kyash → PayPay"}
SERVICES = ("paypay", "kyash")
SERVICE_LABEL = {"paypay": "PayPay", "kyash": "Kyash"}

DEFAULT_CONFIG = {
    "allowed_guild_ids": [],      # BOTを使わせるサーバー。空なら初回起動時に現在のサーバーで固定
    "log_channel_id": 0,          # 取引ログ (換金成立)
    "error_channel_id": 0,        # 内部ログ (払い出し失敗・受取失敗・保留・例外)。0なら取引ログへ
    "detect_channel_id": 0,       # 本人確認・名義不一致・マネラ検知。0なら内部ログへ
    "log_receive_fail": True,     # 受取失敗をログに流すか
    "enabled": True,              # メンテナンス時 False で受付停止
    "auto_send": True,            # False なら全件 HOLD (管理者が /再送 で払い出す)
    "maintenance_note": "",
    # --- 方向ごとの条件 ---
    # fee_percent: 手数料% / fee_fixed_jpy: 1回ごとの固定手数料 (円)
    "directions": {
        "p2k": {"enabled": True, "fee_percent": 5.0, "fee_fixed_jpy": 0,
                "min_jpy": 500, "max_jpy": 50000},
        "k2p": {"enabled": True, "fee_percent": 5.0, "fee_fixed_jpy": 0,
                "min_jpy": 500, "max_jpy": 50000},
    },
    "daily_limit_jpy": 100000,    # 1ユーザー1日あたり (両方向の合計・受取額ベース)
    # --- 払い出し ---
    # paypay_payout: link=送金リンクを作って本人に渡す / direct=登録済みPayPay垢へ直接送る
    # kyash_payout : transfer=登録済みKyash垢へ直接送る / link=送金リンクを作って本人に渡す
    "paypay_payout": "link",
    "kyash_payout": "transfer",
    # 受け取る前に「払い出し側の残高で払えるか」を見る。足りなければ受け取らずに断る
    "check_balance_before_receive": True,
    "payout_reserve_jpy": 0,      # 払い出し側に常に残しておく額
    "balance_cache_sec": 60,
    # --- PayPay の残高種別 (PayPay→Kyash の時だけ見る) ---
    # mode: allow=通す / deny=受け取らずに拒否 / hold=受け取って管理者承認待ち
    "wallet_policy": {
        "money_only": {"mode": "allow", "extra_fee_percent": 0.0},
        "mixed":      {"mode": "allow", "extra_fee_percent": 3.0},
        "light_only": {"mode": "deny",  "extra_fee_percent": 10.0},
        "unknown":    {"mode": "hold",  "extra_fee_percent": 0.0},
    },
    "mixed_fee_mode": "flat",
    "mixed_max_light_ratio": 0.5,
    # --- 本人確認 (マネロン対策) ---
    "reg_amount": 1,              # 登録リンクの金額 (円)
    "reg_amount_max": 1,          # reg_amount より大きくすると、その範囲でランダムに指定する
    "reg_window_sec": 300,        # 金額を出してからリンクを貼るまでの制限時間
    "reg_max_attempts": 5,        # 1回の登録で貼り直せる回数
    "reg_allow_rebind": False,    # 登録済みの人が自分で別の垢に付け替えられるか
    "reg_receive_paypay": True,   # PayPayの登録リンクを受け取る (False=見るだけ。本人がキャンセル)
    # ※ 登録と違う垢から来た送金は、設定に関わらず受け取らずに拒否する (aml.py)
    # --- PayPay の一時保留 ---
    "hold_watch_hours": 24,
    "hold_history_pages": 5,
    "hold_taken_grace_min": 30,
    "remind_stuck": True,
    "stuck_remind_hours": 6.0,
}


def _now() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def _atomic_dump(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    directory = os.open(os.path.dirname(path) or ".", os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("JSON object expected")
        for key, value in default.items():
            if key in data and type(data[key]) is not type(value):
                raise ValueError(f"invalid field: {key}")
        return data
    except FileNotFoundError:
        if glob.glob(glob.escape(path) + ".broken_*"):
            raise RuntimeError(f"{os.path.basename(path)} のバックアップを確認してください")
        return json.loads(json.dumps(default))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"{os.path.basename(path)} を読み込めません。元ファイルを保持しました") from exc


def _deep_defaults(cfg: dict, default: dict) -> dict:
    """入れ子の既定値も埋める (古い config.json に新しい項目が無い時の保険)"""
    for k, v in default.items():
        if k not in cfg:
            cfg[k] = json.loads(json.dumps(v))
        elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
            _deep_defaults(cfg[k], v)
    return cfg


# ============================================================
# config
# ============================================================

_config_broken = False


def config_is_broken() -> bool:
    return _config_broken


def load_config() -> dict:
    """config.json が読めない時は**閉じる方向**へ倒す (受付と自動払い出しを止める)。

    既定値へ戻すと enabled / auto_send / 上限 / 本人確認の条件がまとめて巻き戻り、
    メンテ停止も承認モードも1バイトの破損で解除される。
    """
    global _config_broken
    with _lock:
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            _config_broken = False
            return _deep_defaults(cfg, DEFAULT_CONFIG)
        except FileNotFoundError:
            _config_broken = False
            return json.loads(json.dumps(DEFAULT_CONFIG))
        except Exception as e:
            _config_broken = True
            print(f"[store] ⚠️ config.json 読込失敗: {e} → 受付と自動払い出しを停止する")
            try:
                with open(CONFIG_FILE + ".bak", "r", encoding="utf-8") as f:
                    cfg = json.load(f)
                print("[store] バックアップ config.json.bak の値を使う")
            except Exception:
                cfg = json.loads(json.dumps(DEFAULT_CONFIG))
            cfg = _deep_defaults(cfg, DEFAULT_CONFIG)
            cfg["enabled"] = False
            cfg["auto_send"] = False
            cfg["maintenance_note"] = "設定ファイルが読めないため受付を停止中。管理者に連絡して。"
            return cfg


def save_config(cfg: dict) -> None:
    with _lock:
        if _config_broken:
            # 壊れたファイルの上に「停止中」の値を書くと、復元の手掛かりが消える
            raise RuntimeError("config.json が壊れているので保存しない。手で直してから再起動して")
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                    prev = json.load(f)
                _atomic_dump(CONFIG_FILE + ".bak", prev)
        except Exception:
            pass
        _atomic_dump(CONFIG_FILE, cfg)


def set_config(key: str, value) -> dict:
    with _lock:
        cfg = load_config()
        cfg[key] = value
        save_config(cfg)
        return cfg


def update_config(**fields) -> dict:
    with _lock:
        cfg = load_config()
        cfg.update(fields)
        save_config(cfg)
        return cfg


def direction_cfg(cfg: dict, direction: str) -> dict:
    d = dict(DEFAULT_CONFIG["directions"][direction])
    d.update((cfg.get("directions") or {}).get(direction) or {})
    return d


def set_direction(direction: str, **fields) -> dict:
    with _lock:
        cfg = load_config()
        dirs = cfg.setdefault("directions", {})
        cur = dirs.setdefault(direction, dict(DEFAULT_CONFIG["directions"][direction]))
        cur.update({k: v for k, v in fields.items() if v is not None})
        save_config(cfg)
        return cfg


# ============================================================
# 注文
# ============================================================

ORDER_PREFIX = {"p2k": "PK", "k2p": "KP"}
OPEN_STATES = ("RECEIVING", "PAID", "HOLD", "SENDING", "UNCERTAIN", "FAILED", "RETURNING")
# 日次上限の集計から外す状態 (お金が戻った・取り消した)
NOT_COUNTED = ("RETURNED", "REFUNDED", "CANCELED")


def load_orders() -> dict:
    with _lock:
        marker = ORDERS_FILE + ".initialized"
        if not os.path.exists(ORDERS_FILE) and os.path.exists(marker):
            raise RuntimeError("orders.json がありません。バックアップを確認してください")
        data = _load(ORDERS_FILE, {"seq": 0, "orders": {}})
        orders, seq = data.get("orders"), data.get("seq")
        if type(seq) is not int or seq < 0 or not isinstance(orders, dict):
            raise RuntimeError("orders.json の形式が不正です")
        for oid, o in orders.items():
            if (not isinstance(o, dict) or o.get("id") != oid
                    or o.get("direction") not in ORDER_PREFIX or oid[:2] != ORDER_PREFIX[o["direction"]]
                    or not oid[2:].isdigit() or int(oid[2:]) > seq
                    or not isinstance(o.get("status"), str)
                    or not all(type(o.get(k)) is int for k in ("user_id", "in_amount", "out_amount"))):
                raise RuntimeError("orders.json に不正な注文があります")
        if not os.path.exists(marker):
            if not os.path.exists(ORDERS_FILE):
                used = load_used()["links"]
                if any(str(row.get("ref", ""))[:2] in ORDER_PREFIX.values() for row in used.values()):
                    raise RuntimeError("取引履歴に対応する orders.json を復元してください")
                _atomic_dump(ORDERS_FILE, data)
            _atomic_dump(marker, True)
        return data


def save_orders(data: dict) -> None:
    with _lock:
        _atomic_dump(ORDERS_FILE, data)


def receipt_order(orders: dict, direction: str, key: str, provider_id: str) -> dict | None:
    """旧形式の注文には receipt_key がないため、決済側の元取引番号も照合する。"""
    service, field = ("paypay_in", "order_id") if direction == "p2k" else ("kyash_in", "uuid")
    for order in orders.values():
        if order.get("receipt_rejected") and order["status"] == "CANCELED":
            continue
        if order.get("receipt_key") == key or (provider_id and (order.get(service) or {}).get(field) == provider_id):
            return order
    return None


def get_or_create_order(direction: str, user_id: int, user_name: str, in_amount: int,
                        out_amount: int, fee_jpy: int, fee_percent: float, *,
                        receipt_key: str, status: str = "PAID", **fields) -> tuple[dict, bool]:
    if not receipt_key:
        raise ValueError("受取キーがありません")
    with _lock:
        data = load_orders()
        orders = data["orders"]
        provider_id = ((fields.get("paypay_in") or {}).get("order_id") if direction == "p2k"
                       else (fields.get("kyash_in") or {}).get("uuid"))
        previous = receipt_order(orders, direction, receipt_key, provider_id)
        if previous is None and direction == "p2k":
            previous = orders.get((link_used(receipt_key) or {}).get("ref"))
        if previous:
            if (previous["user_id"] != user_id or previous["direction"] != direction
                    or previous["in_amount"] != in_amount):
                raise RuntimeError("入金の注文情報が一致しません")
            return dict(previous), False
        if direction == "p2k" and not provider_id:
            for record in orders.values():
                if (record["direction"] == "p2k" and record["user_id"] == user_id
                        and record["in_amount"] == in_amount and not record.get("receipt_key")
                        and not (record.get("paypay_in") or {}).get("order_id")):
                    raise RuntimeError("旧注文と入金を識別できません。台帳を照合してください")
        fields["receipt_key"] = receipt_key
        data["seq"] = int(data.get("seq", 0)) + 1
        oid = f"{ORDER_PREFIX[direction]}{data['seq']:05d}"
        order = {
            "id": oid,
            "direction": direction,
            "user_id": int(user_id),
            "user_name": user_name,
            "in_amount": int(in_amount),
            "out_amount": int(out_amount),
            "fee_jpy": int(fee_jpy),
            "fee_percent": float(fee_percent),
            "status": status,
            "error": "",
            "hold_reason": "",
            "payout": {},
            "created_at": _now(),
            "created_ts": time.time(),
            "updated_at": _now(),
            "history": [{"at": _now(), "status": status, "note": "作成"}],
        }
        order.update(fields)
        data["orders"][oid] = order
        save_orders(data)
        return dict(order), True


def update_order(oid: str, **fields) -> dict | None:
    """status 以外の項目の更新用。status を変える時は claim_order を使う。"""
    if "status" in fields:
        raise ValueError("status は claim_order() で変える")
    with _lock:
        data = load_orders()
        o = data["orders"].get(oid)
        if not o:
            return None
        o.update(fields)
        o["updated_at"] = _now()
        save_orders(data)
        return dict(o)


def claim_order(oid: str, expect: tuple[str, ...], new_status: str,
                note: str = "", **fields) -> dict | None:
    """compare-and-set。status が expect のいずれかの時だけ new_status に遷移する。

    「読んで → 判定して → 書く」を分けて書くと、/再送 の連打や自動払い出しと
    /強制状態変更 の同時実行で二重に払い出せてしまう。遷移を1つのロックに
    閉じ込めて、奪えなかった側 (None) は処理を降りる。
    """
    with _lock:
        data = load_orders()
        o = data["orders"].get(oid)
        if not o or o.get("status") not in expect:
            return None
        o["status"] = new_status
        o.update(fields)
        o["updated_at"] = _now()
        hist = o.setdefault("history", [])
        hist.append({"at": _now(), "status": new_status, "note": str(note)[:200]})
        if len(hist) > 30:
            o["history"] = hist[-30:]
        save_orders(data)
        return dict(o)


def get_order(oid: str) -> dict | None:
    o = load_orders()["orders"].get(str(oid or "").strip().upper())
    return dict(o) if o else None


def user_orders(user_id: int, limit: int = 10) -> list[dict]:
    orders = [o for o in load_orders()["orders"].values() if o["user_id"] == int(user_id)]
    return sorted(orders, key=lambda o: o.get("created_ts", 0), reverse=True)[:limit]


def recent_orders(limit: int = 20, status: str | None = None) -> list[dict]:
    orders = list(load_orders()["orders"].values())
    if status:
        orders = [o for o in orders if o.get("status") == status]
    return sorted(orders, key=lambda o: o.get("created_ts", 0), reverse=True)[:limit]


def open_orders() -> list[dict]:
    return [o for o in load_orders()["orders"].values() if o.get("status") in OPEN_STATES]


def today_total_jpy(user_id: int) -> int:
    d = today()
    total = 0
    for o in load_orders()["orders"].values():
        if o["user_id"] != int(user_id) or o.get("status") in NOT_COUNTED:
            continue
        if str(o.get("created_at", "")).startswith(d):
            total += int(o.get("in_amount", 0))
    return total


def unpaid_out(side: str) -> int:
    """払い出し側 (paypay / kyash) で、受け取り済みなのにまだ払っていない額。

    受け取った分の払い出し原資は残高に含まれているので、別の換金の
    与信に使ってはいけない。
    """
    want = "p2k" if side == "kyash" else "k2p"
    total = 0
    for o in load_orders()["orders"].values():
        if o.get("direction") == want and o.get("status") in ("RECEIVING", "PAID", "HOLD", "FAILED",
                                                               "SENDING", "UNCERTAIN"):
            total += int(o.get("out_amount", 0))
    return total


def used_kyash_tx() -> set[str]:
    """注文に紐付け済みのKyash入金 (timeline の txUUIDAPI)。送信者の特定で使い回さない。"""
    out = set()
    for o in load_orders()["orders"].values():
        tx = str((o.get("kyash_in") or {}).get("tx") or "")
        if tx:
            out.add(tx)
    for rec in (load_used().get("links") or {}).values():
        tx = str(rec.get("kyash_tx") or "")
        if tx:
            out.add(tx)
    return out


def stats() -> dict:
    orders = list(load_orders()["orders"].values())
    out = {"total": len(orders)}
    for st in ("SENT", "PAID", "HOLD", "SENDING", "UNCERTAIN", "FAILED", "RETURNING",
               "RETURNED", "REFUNDED", "CANCELED"):
        out[st] = len([o for o in orders if o.get("status") == st])
    for d in DIRECTIONS:
        sent = [o for o in orders if o.get("direction") == d and o.get("status") == "SENT"]
        out[f"{d}_count"] = len(sent)
        out[f"{d}_in"] = sum(int(o.get("in_amount", 0)) for o in sent)
        out[f"{d}_out"] = sum(int(o.get("out_amount", 0)) for o in sent)
        out[f"{d}_fee"] = sum(int(o.get("fee_jpy", 0)) for o in sent)
    return out


# ============================================================
# 本人確認の紐付け (aml_accounts.json)
#
# {"accounts": {"<discord_id>": {
#     "user_id", "user_name", "state": "ACTIVE"|"BLOCKED", "created_at",
#     "paypay": {"state": "VERIFIED"|"REVOKED", "id", "name", "icon", "verified_at", "ref"},
#     "kyash":  {"state": ..., "id", "user_name", "name", "verified_at", "ref"},
#     "count", "volume_jpy", "events": [...]}},
#  "blocked": {"paypay:<id>": {...}, "kyash:<id>": {...}}}
#
# 記録は消さない。「いつ誰がどの垢で通っていたか」はマネロン対策として
# 一番残すべき記録なので、解除も状態を変えて履歴に追記するだけにする。
# ============================================================

def load_accounts() -> dict:
    with _lock:
        data = _load(ACCOUNTS_FILE, {"accounts": {}, "blocked": {}})
        data.setdefault("accounts", {})
        data.setdefault("blocked", {})
        return data


def save_accounts(data: dict) -> None:
    with _lock:
        _atomic_dump(ACCOUNTS_FILE, data)


def get_account(user_id: int) -> dict | None:
    rec = load_accounts()["accounts"].get(str(int(user_id)))
    return json.loads(json.dumps(rec)) if rec else None


def binding(user_id: int, service: str) -> dict | None:
    """有効な紐付け (VERIFIED) だけ返す"""
    acc = get_account(user_id) or {}
    b = acc.get(service) or {}
    return b if b.get("state") == "VERIFIED" and b.get("id") else None


def _event(rec: dict, event: str, note: str = "", by: int | None = None) -> None:
    ev = rec.setdefault("events", [])
    ev.append({"at": _now(), "event": event, "note": str(note)[:300],
               **({"by": int(by)} if by else {})})
    if len(ev) > 60:
        rec["events"] = ev[-60:]


def _ensure(data: dict, user_id: int, user_name: str = "") -> dict:
    rec = data["accounts"].setdefault(str(int(user_id)), {
        "user_id": int(user_id), "user_name": user_name, "state": "ACTIVE",
        "created_at": _now(), "count": 0, "volume_jpy": 0, "events": []})
    if user_name:
        rec["user_name"] = user_name
    return rec


def bind(user_id: int, user_name: str, service: str, identity: dict,
         ref: str = "", amount: int = 0) -> dict:
    """紐付けを作る (または同じ垢で付け直す)。identity は aml.*_identity の戻り値。"""
    with _lock:
        data = load_accounts()
        rec = _ensure(data, user_id, user_name)
        prev = rec.get(service) or {}
        new = {"state": "VERIFIED", "verified_at": _now(), "ref": str(ref)[:80],
               "amount": int(amount)}
        new.update({k: v for k, v in identity.items() if k in
                    ("id", "name", "user_name", "icon")})
        new["id"] = str(identity.get("id") or "")
        if prev.get("id") and prev.get("id") != new["id"]:
            new["previous"] = {"id": prev.get("id"), "name": prev.get("name"),
                               "user_name": prev.get("user_name"),
                               "verified_at": prev.get("verified_at"),
                               "state": prev.get("state")}
        rec[service] = new
        _event(rec, f"VERIFIED_{service.upper()}",
               f"{identity.get('name') or identity.get('user_name') or '?'} / ID {new['id']}"
               + (f" (前: {prev.get('id')})" if prev.get("id") and prev.get("id") != new["id"]
                  else ""))
        save_accounts(data)
        return json.loads(json.dumps(rec))


def update_binding(user_id: int, service: str, event: str = "", note: str = "",
                   **fields) -> dict | None:
    with _lock:
        data = load_accounts()
        rec = data["accounts"].get(str(int(user_id)))
        if not rec or not isinstance(rec.get(service), dict):
            return None
        rec[service].update(fields)
        if event:
            _event(rec, event, note)
        save_accounts(data)
        return json.loads(json.dumps(rec))


def revoke(user_id: int, service: str, by: int, note: str = "") -> dict | None:
    """紐付けを解除する (記録は残す)"""
    with _lock:
        data = load_accounts()
        rec = data["accounts"].get(str(int(user_id)))
        if not rec or not isinstance(rec.get(service), dict):
            return None
        rec[service]["state"] = "REVOKED"
        rec[service]["revoked_at"] = _now()
        _event(rec, f"REVOKED_{service.upper()}", note, by)
        save_accounts(data)
        return json.loads(json.dumps(rec))


def set_user_state(user_id: int, state: str, by: int, note: str = "",
                   user_name: str = "") -> dict:
    """ACTIVE / BLOCKED。BLOCKED にすると紐付いている垢も封じる (他の誰も登録できない)。"""
    with _lock:
        data = load_accounts()
        rec = _ensure(data, user_id, user_name)
        rec["state"] = state
        _event(rec, state, note, by)
        for svc in SERVICES:
            b = rec.get(svc) or {}
            if not b.get("id"):
                continue
            key = f"{svc}:{b['id']}"
            if state == "BLOCKED":
                data["blocked"][key] = {"at": _now(), "user_id": int(user_id),
                                        "name": b.get("name") or "",
                                        "user_name": b.get("user_name") or "",
                                        "note": str(note)[:200]}
            else:
                data["blocked"].pop(key, None)
        save_accounts(data)
        return json.loads(json.dumps(rec))


def is_blocked_identity(service: str, ident_id: str) -> dict | None:
    if not ident_id:
        return None
    return load_accounts()["blocked"].get(f"{service}:{ident_id}")


def find_owner(service: str, ident_id: str, exclude_user_id: int | None = None) -> dict | None:
    """その垢 (ID) が既に別の誰かに紐付いていないか。REVOKED は対象外。"""
    ident_id = str(ident_id or "")
    if not ident_id:
        return None
    for uid, rec in load_accounts()["accounts"].items():
        if exclude_user_id is not None and str(uid) == str(int(exclude_user_id)):
            continue
        b = rec.get(service) or {}
        if b.get("state") == "VERIFIED" and str(b.get("id") or "") == ident_id:
            return json.loads(json.dumps(rec))
    return None


def _kid(v) -> str:
    return str(v or "").strip().lstrip("@").casefold()


def find_kyash_owner_by_user_name(user_name: str,
                                  exclude_user_id: int | None = None) -> dict | None:
    """Kyash ID (ユーザー名) から、それを登録している別の利用者を探す (受取前の照合用)"""
    want = _kid(user_name)
    if not want:
        return None
    for uid, rec in load_accounts()["accounts"].items():
        if exclude_user_id is not None and str(uid) == str(int(exclude_user_id)):
            continue
        b = rec.get("kyash") or {}
        if b.get("state") == "VERIFIED" and _kid(b.get("user_name")) == want:
            return json.loads(json.dumps(rec))
    return None


def is_blocked_kyash_user_name(user_name: str) -> dict | None:
    want = _kid(user_name)
    if not want:
        return None
    for key, rec in load_accounts()["blocked"].items():
        if key.startswith("kyash:") and _kid(rec.get("user_name")) == want:
            return rec
    return None


def find_by_ref(service: str, ref: str, exclude_user_id: int | None = None) -> dict | None:
    """同じ登録リンクで2人目を登録させないための照合"""
    ref = str(ref or "")
    if not ref:
        return None
    for uid, rec in load_accounts()["accounts"].items():
        if exclude_user_id is not None and str(uid) == str(int(exclude_user_id)):
            continue
        if str((rec.get(service) or {}).get("ref") or "") == ref:
            return json.loads(json.dumps(rec))
    return None


def record_usage(user_id: int, amount: int, order_id: str) -> None:
    with _lock:
        data = load_accounts()
        rec = data["accounts"].get(str(int(user_id)))
        if not rec:
            return
        counted = rec.setdefault("counted_orders", [])
        if order_id in counted:
            return
        counted.append(order_id)
        rec["count"] = int(rec.get("count", 0)) + 1
        rec["volume_jpy"] = int(rec.get("volume_jpy", 0)) + int(amount)
        rec["last_used_at"] = _now()
        save_accounts(data)


def list_accounts(limit: int = 25) -> list[dict]:
    items = list(load_accounts()["accounts"].values())
    items.sort(key=lambda a: a.get("created_at", ""), reverse=True)
    return items[:limit]


def search_accounts(query: str, limit: int = 10) -> list[dict]:
    """PayPay名 / Kyashユーザー名 / ID の部分一致"""
    import unicodedata
    q = unicodedata.normalize("NFKC", str(query or "")).casefold().replace(" ", "")
    if not q:
        return []
    hits = []
    for rec in load_accounts()["accounts"].values():
        blob = " ".join(str(v) for svc in SERVICES for v in (
            (rec.get(svc) or {}).get("name"), (rec.get(svc) or {}).get("user_name"),
            (rec.get(svc) or {}).get("id")) if v)
        blob = unicodedata.normalize("NFKC", blob).casefold().replace(" ", "")
        if q in blob:
            hits.append(rec)
    return hits[:limit]


# ============================================================
# リンクの使用記録 (二重受取・二重払いの防止)
# ============================================================

def mask_code(code: str) -> str:
    """リンクコードは平文で残さない (受け取っていないリンクは相手の手元で生きている)"""
    code = str(code or "")
    if not code:
        return ""
    return f"{code[:4]}…({hashlib.sha256(code.encode('utf-8')).hexdigest()[:12]})"


def paypay_link_key(info: dict | None, code: str = "") -> str:
    """PayPay側の orderId を最優先 (表記ゆれで同じリンクを何度も通させない)"""
    info = info or {}
    oid = str(info.get("order_id") or "").strip()
    if oid:
        return f"PP_OID_{oid}"
    code = str(info.get("code") or code or "").strip()
    return f"PP_LINK_{code}" if code else ""


def kyash_link_key(uuid: str) -> str:
    uuid = str(uuid or "").strip().lower()
    return f"KY_{uuid}" if uuid else ""


def load_used() -> dict:
    with _lock:
        return _load(USED_FILE, {"links": {}})


def reserve_link(key: str, user_id: int, note: str = "", *, order_id: str = "") -> bool:
    """未使用なら予約して True。既に誰かが押さえていれば False (atomic)。

    受け取りを投げる前に必ずこれを取る。ユーザー単位ロックだけだと、別の
    Discordアカウントから同じリンクを同時に投げられた時に両方が受取へ進む。
    """
    if not key:
        return False
    with _lock:
        data = load_used()
        links = data.setdefault("links", {})
        if key in links:
            return False
        links[key] = {"at": _now(), "user_id": int(user_id), "note": note, "state": "RESERVED"}
        if order_id:
            links[key]["order_id"] = order_id
        if len(links) > 20000:
            done = sorted((k for k in links if links[k].get("state") == "USED"),
                          key=lambda k: links[k].get("at", ""))[:2000]
            for k in done:
                links.pop(k, None)
        _atomic_dump(USED_FILE, data)
        return True


def release_link(key: str) -> None:
    """受け取りに失敗した時だけ解放する (受け取った後は解放しない)"""
    if not key:
        return
    with _lock:
        data = load_used()
        rec = data.get("links", {}).get(key)
        if rec and rec.get("state") != "USED":
            data["links"].pop(key, None)
            _atomic_dump(USED_FILE, data)


def confirm_link(key: str, ref: str, amount: int, **fields) -> None:
    """受け取り成立を記録。以後この鍵は絶対に再利用させない。"""
    if not key:
        return
    with _lock:
        data = load_used()
        rec = data.setdefault("links", {}).setdefault(key, {"at": _now()})
        rec.update({"state": "USED", "ref": ref, "amount": int(amount), "used_at": _now()})
        rec.update(fields)
        _atomic_dump(USED_FILE, data)


def link_used(key: str) -> dict | None:
    return load_used().get("links", {}).get(key)


# ============================================================
# PayPay の一時保留の監視待ち
# ============================================================

HOLD_SETTLED = ("RECEIVED", "DONE")
HOLD_FINISHED = ("RECEIVED", "DONE", "FAILED", "CLOSED", "TAKEN", "EXPIRED")


def load_holds() -> dict:
    with _lock:
        return _load(HOLDS_FILE, {"holds": {}})


def add_hold(key: str, **fields) -> dict:
    with _lock:
        data = load_holds()
        rec = {"key": key, "created_at": _now(), "created_ts": time.time(),
               "updated_at": _now(), "checks": 0, "state": "RECEIVING",
               "last_status": "", "note": ""}
        rec.update(fields)
        data.setdefault("holds", {})[key] = rec
        _atomic_dump(HOLDS_FILE, data)
        return dict(rec)


def update_hold(key: str, **fields) -> dict | None:
    with _lock:
        data = load_holds()
        rec = data.get("holds", {}).get(key)
        if not rec:
            return None
        rec.update(fields)
        rec["updated_at"] = _now()
        _atomic_dump(HOLDS_FILE, data)
        return dict(rec)


def finish_hold(key: str, state: str, **fields) -> dict | None:
    """監視を終える。パスコードとリンクコードは決着後に残す理由が無いので消す。"""
    with _lock:
        data = load_holds()
        rec = data.get("holds", {}).get(key)
        if not rec:
            return None
        rec.update(fields)
        rec["state"] = state
        rec["finished_at"] = _now()
        rec["passcode"] = ""
        if rec.get("code"):
            rec["code"] = mask_code(rec["code"])
        rec["updated_at"] = _now()
        _atomic_dump(HOLDS_FILE, data)
        return dict(rec)


def get_hold(key: str) -> dict | None:
    rec = load_holds().get("holds", {}).get(key)
    return dict(rec) if rec else None


def holds_in(*states: str) -> list[dict]:
    return [dict(h) for h in load_holds().get("holds", {}).values()
            if h.get("state") in states]


def all_holds(limit: int = 25) -> list[dict]:
    items = list(load_holds().get("holds", {}).values())
    return sorted(items, key=lambda h: h.get("created_ts", 0), reverse=True)[:limit]


# ============================================================
# 検知ログ (拒否・保留・返金・登録)
# ============================================================

def log_detection(user_id: int, user_name: str, kind: str, action: str,
                  amount: int = 0, note: str = "", **fields) -> dict:
    """kind: AML / WALLET / REG など。action: DENY / HOLD / RETURN / PASS / BIND"""
    with _lock:
        data = _load(DETECT_FILE, {"items": []})
        rec = {"at": _now(), "user_id": int(user_id), "user_name": user_name,
               "kind": kind, "action": action, "amount": int(amount),
               "note": str(note)[:300]}
        rec.update(fields)
        items = data.setdefault("items", [])
        items.append(rec)
        if len(items) > 3000:
            data["items"] = items[-3000:]
        _atomic_dump(DETECT_FILE, data)
        return rec


def recent_detections(limit: int = 15, user_id: int | None = None,
                      action: str | None = None) -> list[dict]:
    items = _load(DETECT_FILE, {"items": []}).get("items", [])
    if user_id is not None:
        items = [i for i in items if i.get("user_id") == int(user_id)]
    if action:
        items = [i for i in items if i.get("action") == action]
    return list(reversed(items))[:limit]
