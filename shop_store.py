"""Public checkout order storage for the isolated beta storefront."""
from __future__ import annotations
import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

import github_runtime_store

DB_PATH = Path(os.environ.get("SHOP_DB_PATH", "instance/shop.sqlite3"))
ORDER_ID_RE = re.compile(r"^[A-Z0-9]{10}$")
DEFAULT_CATALOG = [
    {"id": "character-specific", "title": "キャラ特定選択", "unit_price_yen": 50, "description": "代行するキャラ1体につき50円"},
    {"id": "other-service", "title": "その他の代行", "unit_price_yen": 25, "description": "代行1体につき25円"},
]
MAX_ORDER_QUANTITY = 100

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
    try:
        with github_runtime_store.sqlite_database("shop.sqlite3", DB_PATH) as con:
            con.execute("""CREATE TABLE IF NOT EXISTS orders (
                id TEXT PRIMARY KEY, access_hash TEXT NOT NULL, sku TEXT NOT NULL,
                title TEXT NOT NULL, amount INTEGER NOT NULL, credential_enc BLOB NOT NULL,
                status TEXT NOT NULL, payment_digest TEXT UNIQUE, payment_note TEXT,
                created REAL NOT NULL, updated REAL NOT NULL
            )""")
            columns = {row[1] for row in con.execute("PRAGMA table_info(orders)")}
            if "credentials_submitted" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN credentials_submitted INTEGER NOT NULL DEFAULT 0")
            if "quantity" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1")
            if "unit_price_yen" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN unit_price_yen INTEGER NOT NULL DEFAULT 0")
                con.execute("UPDATE orders SET unit_price_yen=amount WHERE quantity=1")
            if "operation_enc" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN operation_enc BLOB")
            if "service_type" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN service_type TEXT NOT NULL DEFAULT 'other-service'")
            if "job_id" not in columns:
                con.execute("ALTER TABLE orders ADD COLUMN job_id TEXT")
            # Finish one-time schema migration before any caller starts an explicit transaction.
            con.commit()
            if not github_runtime_store.enabled():
                os.chmod(DB_PATH, 0o600)
            yield con
    except github_runtime_store.RuntimeStoreError as exc:
        raise ShopError(str(exc)) from exc

def catalog():
    raw = os.environ.get("SHOP_CATALOG_JSON", "").strip()
    if raw:
        try:
            rows = json.loads(raw)
        except ValueError:
            raise ShopError("商品カタログ設定を読み取れません。") from None
    else:
        rows = DEFAULT_CATALOG
    if not isinstance(rows, list) or len(rows) > 100:
        raise ShopError("商品カタログ設定の形式が正しくありません。")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        sku, title = row.get("id"), row.get("title")
        price = row.get("unit_price_yen", row.get("price_yen"))
        if (not isinstance(sku, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,40}", sku)
                or sku in seen or not isinstance(title, str) or not 1 <= len(title.strip()) <= 80
                or type(price) is not int or not 1 <= price <= 1000000):
            continue
        seen.add(sku)
        result.append({"id": sku, "title": title.strip(), "unit_price_yen": price,
                       "description": str(row.get("description") or "")[:300]})
    return result

def create_order(sku: str, quantity=1):
    if type(quantity) is not int or not 1 <= quantity <= MAX_ORDER_QUANTITY:
        raise ShopError("数量は1〜100体で指定してください。")
    items = {x["id"]: x for x in catalog()}
    item = items.get(sku)
    if not item:
        raise ShopError("商品が見つからないか、現在販売していません。")
    amount = item["unit_price_yen"] * quantity
    if amount > 1000000:
        raise ShopError("注文金額が上限を超えています。")
    order_id = secrets.token_hex(5).upper()
    access_token = secrets.token_urlsafe(32)
    credentials = _cipher().encrypt(json.dumps(
        {"transfer_code": "", "pin": ""}, ensure_ascii=False
    ).encode("utf-8"))
    now = time.time()
    with _db() as db:
        db.execute("""INSERT INTO orders
            (id,access_hash,sku,title,amount,unit_price_yen,quantity,credential_enc,
             credentials_submitted,status,created,updated)
            VALUES(?,?,?,?,?,?,?, ?,0,'awaiting_payment',?,?)""",
            (order_id, hashlib.sha256(access_token.encode()).hexdigest(), sku,
             item["title"], amount, item["unit_price_yen"], quantity, credentials, now, now))
    return {"id": order_id, "access_token": access_token, "title": item["title"],
            "unit_price_yen": item["unit_price_yen"], "quantity": quantity,
            "amount": amount, "status": "awaiting_payment"}

def _selected_character_count(raw: str) -> int:
    if not isinstance(raw, str) or len(raw) > 20_000:
        raise ShopError("キャラ番号の入力形式が正しくありません。")
    value = unicodedata.normalize("NFKC", raw)
    value = re.sub(r"[~〜～ー−―‐–—]", "-", value)
    numbers = set()
    for token in (part for part in re.split(r"[\s,、，;；・]+", value) if part):
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise ShopError(f"キャラ番号「{token}」を読み取れません。")
        first = int(match.group(1))
        last = int(match.group(2)) if match.group(2) else first
        if first > last:
            first, last = last, first
        if first < 1 or last > 9999:
            raise ShopError("キャラ番号は1〜9999の範囲で指定してください。")
        numbers.update(range(first, last + 1))
        if len(numbers) > 2000:
            raise ShopError("キャラ番号は一度に2000体まで指定できます。")
    return len(numbers)


def create_operation_order(operation: dict, quantity: int):
    """Create an encrypted order; derive character-specific billing from the saved operation.

    The browser cannot choose the cheaper service category or override the
    quantity for character-specific orders. The only customer-entered billing
    quantity is used when no character numbers were selected.
    """
    if not isinstance(operation, dict) or len(json.dumps(operation, ensure_ascii=False)) > 100_000:
        raise ShopError("注文内容が大きすぎるか、形式が正しくありません。")
    for field in ("safe", "dry", "clone", "integrity_only"):
        if field in operation and type(operation[field]) is not bool:
            raise ShopError(f"注文オプション「{field}」の形式が正しくありません。")
    if operation.get("clone") and operation.get("integrity_only"):
        raise ShopError("保全チェックとアカウント複製は同時に選択できません。")
    character_count = _selected_character_count(operation.get("chars", ""))
    if character_count:
        service_type = "character-specific"
        quantity = character_count
    else:
        service_type = "other-service"
        if type(quantity) is not int or not 1 <= quantity <= MAX_ORDER_QUANTITY:
            raise ShopError("代行体数は1〜100体で指定してください。")
    unit_price = 50 if service_type == "character-specific" else 25
    amount = unit_price * quantity
    order_id = secrets.token_hex(5).upper()
    access_token = secrets.token_urlsafe(32)
    credential_enc = _cipher().encrypt(json.dumps(
        {"transfer_code": "", "pin": ""}, ensure_ascii=False
    ).encode("utf-8"))
    operation_enc = _cipher().encrypt(json.dumps(
        operation, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8"))
    title = "キャラ特定選択" if service_type == "character-specific" else "その他の代行"
    now = time.time()
    with _db() as db:
        db.execute("""INSERT INTO orders
            (id,access_hash,sku,title,amount,unit_price_yen,quantity,credential_enc,
             operation_enc,service_type,job_id,credentials_submitted,status,created,updated)
            VALUES(?,?,?,?,?,?,?,?,?,?,NULL,0,'awaiting_payment',?,?)""",
            (order_id, hashlib.sha256(access_token.encode()).hexdigest(),
             "original-" + service_type, title, amount, unit_price, quantity,
             credential_enc, operation_enc, service_type, now, now))
    return {"id": order_id, "access_token": access_token, "title": title,
            "unit_price_yen": unit_price, "quantity": quantity,
            "amount": amount, "status": "awaiting_payment"}


def operation_payload(order_id: str, access_token: str):
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        return None
    if not row.get("operation_enc"):
        raise ShopError("この注文には実行内容が保存されていません。管理者へお問い合わせください。")
    try:
        operation = json.loads(_cipher().decrypt(row["operation_enc"]))
    except Exception:
        raise ShopError("注文内容を安全に読み取れません。管理者へお問い合わせください。") from None
    if not isinstance(operation, dict):
        raise ShopError("注文内容の形式が正しくありません。")
    return operation


def operation_for_execution(order_id: str, access_token: str):
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        return None
    if row["status"] != "paid" or not row.get("credentials_submitted"):
        raise ShopError("決済確認と引継ぎ情報の入力が必要です。")
    operation = operation_payload(order_id, access_token)
    try:
        credentials = json.loads(_cipher().decrypt(row["credential_enc"]))
    except Exception:
        raise ShopError("引継ぎ情報を安全に読み取れません。管理者へお問い合わせください。") from None
    if bool(credentials.get("transfer_code")) != bool(credentials.get("pin")):
        raise ShopError("引継ぎ情報が正しくありません。")
    if (operation.get("clone") or operation.get("integrity_only")) and not credentials.get("transfer_code"):
        raise ShopError("複製・保全チェックには既存セーブの引継ぎコードと暗証番号が必要です。")
    operation["transfer"] = credentials.get("transfer_code", "")
    operation["confirm"] = credentials.get("pin", "")
    return operation


def claim_execution(order_id: str, access_token: str):
    row = _row(order_id)
    if not row or not secrets.compare_digest(
        row["access_hash"], hashlib.sha256((access_token or "").encode()).hexdigest()
    ):
        raise ShopError("注文が見つかりません。")
    with _db() as db:
        changed = db.execute("""UPDATE orders SET job_id='starting',updated=?
            WHERE id=? AND status='paid' AND credentials_submitted=1 AND job_id IS NULL""",
            (time.time(), order_id))
        if changed.rowcount != 1:
            raise ShopError("この注文は既に処理開始済み、または状態が変わりました。")


def bind_job(order_id: str, job_id: str):
    with _db() as db:
        changed = db.execute("""UPDATE orders SET job_id=?,updated=?
            WHERE id=? AND status='paid' AND credentials_submitted=1 AND job_id='starting'""",
            (job_id, time.time(), order_id))
        if changed.rowcount != 1:
            raise ShopError("処理開始状態を確定できません。管理者による確認が必要です。")


def release_execution(order_id: str):
    with _db() as db:
        db.execute("UPDATE orders SET job_id=NULL,updated=? WHERE id=? AND job_id='starting'",
                   (time.time(), order_id))


def claim_payment(order_id: str):
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        changed = db.execute("""UPDATE orders SET status='payment_processing',updated=?
                                WHERE id=? AND status='awaiting_payment'""",
                             (time.time(), order_id))
        return changed.rowcount == 1

def release_payment(order_id: str, *, review=False, note=""):
    target = "review" if review else "awaiting_payment"
    with _db() as db:
        changed = db.execute("""UPDATE orders SET status=?,payment_note=?,updated=?
                                WHERE id=? AND status='payment_processing'""",
                             (target, (note or "")[:300], time.time(), order_id))
        return changed.rowcount == 1

def complete_payment(order_id: str, payment_digest: str):
    with _db() as db:
        changed = db.execute("""UPDATE orders SET status='paid',payment_digest=?,payment_note='',
                                updated=? WHERE id=? AND status='payment_processing'""",
                             (payment_digest, time.time(), order_id))
        if changed.rowcount != 1:
            raise ShopError("決済後の注文状態を確定できません。管理者による確認が必要です。")

def save_credentials(order_id: str, access_token: str, transfer_code: str, pin: str):
    transfer_code, pin = (transfer_code or "").strip(), (pin or "").strip()
    if bool(transfer_code) != bool(pin) or len(transfer_code) > 128 or len(pin) > 32:
        raise ShopError("引継ぎコードと暗証番号は、両方入力するか両方空欄にしてください。")
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
                                WHERE id=? AND status='paid' AND credentials_submitted=0 AND job_id IS NULL""",
                             (encrypted, time.time(), order_id))
        if updated.rowcount != 1:
            raise ShopError("引継ぎ情報は既に送信済み、または注文状態が変わりました。注文画面を再読み込みしてください。")

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
            "quantity": row.get("quantity", 1), "unit_price_yen": row.get("unit_price_yen", 0),
            "status": row["status"], "credentials_submitted": bool(row["credentials_submitted"]),
            "job_id": row.get("job_id"), "created": int(row["created"])}

def set_status(order_id: str, status: str, *, payment_digest=None, note=None):
    allowed = {"paid", "review", "fulfilling", "completed", "cancelled"}
    if status not in allowed:
        raise ShopError("不正な注文状態です。")
    with _db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status,credentials_submitted FROM orders WHERE id=?", (order_id,)).fetchone()
        if not row:
            raise ShopError("注文が見つかりません。")
        current = row["status"]
        transitions = {
            "awaiting_payment": {"review", "cancelled"},
            "payment_processing": {"review"},
            "paid": {"fulfilling", "cancelled"},
            "review": {"paid", "cancelled"},
            "fulfilling": {"completed", "review"},
            "completed": set(), "cancelled": set()
        }
        if status not in transitions.get(current, set()):
            raise ShopError("この注文状態からは変更できません。")
        if status == "fulfilling" and not row["credentials_submitted"]:
            raise ShopError("購入者の引継ぎ情報が未入力です。")
        changed = db.execute("""UPDATE orders SET status=?,payment_digest=COALESCE(?,payment_digest),
                                payment_note=?,updated=? WHERE id=? AND status=?""",
                             (status, payment_digest, (note or "")[:300], time.time(), order_id, current))
        if changed.rowcount != 1:
            raise ShopError("注文状態が同時に更新されました。再読み込みしてください。")

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
