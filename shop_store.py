"""Public checkout order storage for the isolated beta storefront."""
from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

DB_PATH = Path(os.environ.get("SHOP_DB_PATH", "instance/shop.sqlite3"))
ORDER_ID_RE = re.compile(r"^[A-Z0-9]{10}$")

class ShopError(Exception):
    pass

def _cipher():
    key = os.environ.get("PAYPAY_ENCRYPTION_KEY", "").strip()
    if not key:
        raise ShopError("ショップの暗号化キーが未設定です。管理者にお問い合わせください。")
    try:
        return Fernet(key.encode("ascii"))
    except Exception:
        raise ShopError("ショップの暗号化キー設定が正しくありません。") from None

@contextmanager
def _db():
    DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA busy_timeout=20000")
        con.execute("""CREATE TABLE IF NOT EXISTS orders (
            id TEXT PRIMARY KEY, access_hash TEXT NOT NULL, sku TEXT NOT NULL,
            title TEXT NOT NULL, amount INTEGER NOT NULL, credential_enc BLOB NOT NULL,
            status TEXT NOT NULL, payment_digest TEXT UNIQUE, payment_note TEXT,
            created REAL NOT NULL, updated REAL NOT NULL
        )""")
        columns = {row[1] for row in con.execute("PRAGMA table_info(orders)")}
        if "credentials_submitted" not in columns:
            con.execute("ALTER TABLE orders ADD COLUMN credentials_submitted INTEGER NOT NULL DEFAULT 0")
        os.chmod(DB_PATH, 0o600)
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()

def catalog():
    raw = os.environ.get("SHOP_CATALOG_JSON", "[]")
    try:
        rows = json.loads(raw)
    except ValueError:
        raise ShopError("商品カタログ設定を読み取れません。") from None
    if not isinstance(rows, list) or len(rows) > 100:
        raise ShopError("商品カタログ設定の形式が正しくありません。")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sku, title, price = row.get("id"), row.get("title"), row.get("price_yen")
        if (not isinstance(sku, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", sku)
                or sku in seen or not isinstance(title, str) or not 1 <= len(title.strip()) <= 80
                or type(price) is not int or not 1 <= price <= 1_000_000):
            continue
        seen.add(sku)
        result.append({"id": sku, "title": title.strip(), "price_yen": price,
                       "description": str(row.get("description") or "")[:300]})
    return result

def create_order(sku: str):
    items = {x["id"]: x for x in catalog()}
    item = items.get(sku)
    if not item:
        raise ShopError("商品が見つからないか、現在販売していません。")
    order_id = secrets.token_hex(5).upper()
    access_token = secrets.token_urlsafe(32)
    credentials = _cipher().encrypt(json.dumps(
        {"transfer_code": "", "pin": ""}, ensure_ascii=False
    ).encode("utf-8"))
    now = time.time()
    with _db() as db:
        db.execute("""INSERT INTO orders
            (id,access_hash,sku,title,amount,credential_enc,credentials_submitted,status,created,updated)
            VALUES(?,?,?,?,?,?,0,'awaiting_payment',?,?)""",
            (order_id, hashlib.sha256(access_token.encode()).hexdigest(), sku,
             item["title"], item["price_yen"], credentials, now, now))
    return {"id": order_id, "access_token": access_token, "title": item["title"],
            "amount": item["price_yen"], "status": "awaiting_payment"}

def save_credentials(order_id: str, access_token: str, transfer_code: str, pin: str):
    transfer_code, pin = (transfer_code or "").strip(), (pin or "").strip()
    if not transfer_code or not pin or len(transfer_code) > 128 or len(pin) > 32:
        raise ShopError("引継ぎコードと暗証番号を正しく入力してください。")
    if any(ord(ch) < 32 for ch in transfer_code + pin):
        raise ShopError("入力内容に使用できない文字が含まれています。")
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        raise ShopError("注文が見つかりません。")
    if row["status"] != "paid":
        raise ShopError("決済確認後に引継ぎ情報を入力できます。")
    encrypted = _cipher().encrypt(json.dumps(
        {"transfer_code": transfer_code, "pin": pin}, ensure_ascii=False
    ).encode("utf-8"))
    with _db() as db:
        updated = db.execute("""UPDATE orders SET credential_enc=?,credentials_submitted=1,updated=?
                                WHERE id=? AND status='paid'""",
                             (encrypted, time.time(), order_id))
        if updated.rowcount != 1:
            raise ShopError("注文状態が変わりました。注文画面を再読み込みしてください。")

def _row(order_id):
    if not ORDER_ID_RE.fullmatch(order_id or ""):
        return None
    with _db() as db:
        row = db.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    return dict(row) if row else None

def public_order(order_id: str, access_token: str):
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        return None
    return {"id": row["id"], "title": row["title"], "amount": row["amount"],
            "status": row["status"], "credentials_submitted": bool(row["credentials_submitted"]),
            "created": int(row["created"])}

def set_status(order_id: str, status: str, *, payment_digest=None, note=None):
    allowed = {"paid", "review", "fulfilling", "completed", "cancelled"}
    if status not in allowed:
        raise ShopError("不正な注文状態です。")
    with _db() as db:
        row = db.execute("SELECT status,credentials_submitted FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            raise ShopError("注文が見つかりません。")
        current = row["status"]
        transitions = {
            "awaiting_payment": {"paid", "review", "cancelled"},
            "paid": {"fulfilling", "cancelled"},
            "review": {"paid", "cancelled"},
            "fulfilling": {"completed"},
            "completed": set(), "cancelled": set()
        }
        if status not in transitions.get(current, set()):
            raise ShopError("この注文状態からは変更できません。")
        if status == "fulfilling" and not row["credentials_submitted"]:
            raise ShopError("購入者の引継ぎ情報が未入力です。")
        db.execute("""UPDATE orders SET status=?,payment_digest=COALESCE(?,payment_digest),
                      payment_note=?,updated=? WHERE id=?""",
                   (status, payment_digest, (note or "")[:300], time.time(), order_id))

def admin_orders(limit=100):
    with _db() as db:
        rows = db.execute("SELECT * FROM orders ORDER BY created DESC LIMIT ?", (limit,)).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        try:
            item["credentials"] = json.loads(_cipher().decrypt(item.pop("credential_enc")))
        except (InvalidToken, ValueError, TypeError):
            item["credentials"] = {"error": "復号できません"}
            item.pop("credential_enc", None)
        item.pop("access_hash", None)
        result.append(item)
    return result

def get_order_for_admin(order_id):
    row = _row(order_id)
    if not row:
        return None
    try:
        credentials = json.loads(_cipher().decrypt(row["credential_enc"]))
    except Exception:
        credentials = {"error": "復号できません"}
    row.pop("credential_enc", None)
    row.pop("access_hash", None)
    row["credentials"] = credentials
    return row
