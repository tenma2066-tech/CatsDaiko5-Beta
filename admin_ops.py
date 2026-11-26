"""管理画面の運用機能（設定の保存と、台帳からの集計）。

- 運用の設定（ops.json）：お知らせ・セール・受付の一時停止・お客さんの受付停止・実行枠の数
- 台帳（payments.sqlite3）の表：管理操作の記録・注文のメモと返金・実行の成否・Q&A の追加回答
- 集計：売上の推移（部門別・時間帯・曜日・メニュー別・お客さん数）・注文の検索・実行の成功率

引継ぎコード・ログイン情報・パスワードは、ここには入れない（台帳の history と同じ扱い）。
"""
from __future__ import annotations

import copy
import datetime
import ipaddress
import json
import os
import re
import threading
import time

import paypay_gateway

JST = datetime.timezone(datetime.timedelta(hours=9))
SHOPS = ("nyanko", "tsum", "puni", "ticket", "support", "source")
SHOP_LABELS = {"nyanko": "にゃんこ", "tsum": "ツムツム", "puni": "ぷにぷに", "ticket": "チケット", "support": "支援BOX",
               "source": "ソースコード"}
SERIES = ("nyanko", "tsum", "puni", "other")          # グラフの系列（チケット・支援・ソースは「その他」にまとめる）
SERIES_LABELS = {"nyanko": "にゃんこ", "tsum": "ツムツム", "puni": "ぷにぷに", "other": "チケット・支援など"}
SALE_SHOPS = ("nyanko", "tsum", "puni")              # セールの対象にできる部門（チケット・支援・ソースは対象外）
SLOT_LIMITS = {"normal": (1, 8), "long": (0, 6)}      # 管理画面から変えられる実行枠の数（メモリに合わせて上限を置く）
MAX_BLOCKS = 200
MAX_FAQ = 100
UNITS = ("hour", "day", "week", "month")
MAX_BUCKETS = 400

_DEFAULT = {"notice": {"text": "", "until": 0, "level": "info"},
            "sale": {"pct": 0, "until": 0, "shops": [], "label": ""},
            "paused": {"shops": [], "menus": [], "message": ""},
            "blocks": [],
            "slots": {"normal": None, "long": None}}
_cache = {"path": None, "mtime": None, "data": None}
_lock = threading.Lock()


def _path() -> str:
    return os.path.join(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay"), "ops.json")


def _clean_text(value, limit: int) -> str:
    text = "".join(ch for ch in str(value or "") if ch == "\n" or ch.isprintable())
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:limit]


def settings() -> dict:
    """いまの運用の設定（読めなければ既定値）。呼ぶ側で書き換えてもよい写しを返す。"""
    path = _path()
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return copy.deepcopy(_DEFAULT)
    with _lock:
        if _cache["path"] != path or _cache["mtime"] != mtime:
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
            except (OSError, ValueError):
                raw = {}
            data = copy.deepcopy(_DEFAULT)
            for key, value in (raw if isinstance(raw, dict) else {}).items():
                if key in data and isinstance(value, type(data[key])):
                    data[key] = value if not isinstance(value, dict) else dict(data[key], **value)
            _cache.update(path=path, mtime=mtime, data=data)
        return copy.deepcopy(_cache["data"])


def _save(data: dict) -> None:
    path = _path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)
    with _lock:
        _cache["mtime"] = None


def _until(hours) -> float:
    """「何時間後まで」を時刻に。0・空欄は期限なし（0）。"""
    raw = str(hours or "").strip()
    if not raw or raw == "0":
        return 0
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= 24 * 90:
        raise ValueError("期間は 1〜2160 時間（90日）で入れてください。空欄なら期限なしです。")
    return time.time() + int(raw) * 3600


# ---------------------------------------------------------------- お知らせ・セール・一時停止
def set_notice(text, hours, level="info") -> None:
    d = settings()
    d["notice"] = {"text": _clean_text(text, 200), "until": _until(hours) if str(text or "").strip() else 0,
                   "level": "warn" if level == "warn" else "info"}
    _save(d)


def notice() -> dict | None:
    """いま出すお知らせ（無ければ None）。"""
    n = settings()["notice"]
    if not n.get("text") or (n.get("until") and time.time() > n["until"]):
        return None
    return {"text": n["text"], "level": n.get("level", "info"), "until": n.get("until") or 0}


def set_sale(pct, hours, shops, label="") -> None:
    raw = str(pct or "").strip()
    d = settings()
    if not raw or raw == "0":
        d["sale"] = dict(_DEFAULT["sale"])
        _save(d)
        return
    if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= 50:
        raise ValueError("割引は 1〜50 % で入れてください（0 か空欄で終了）。")
    shops = [s for s in SALE_SHOPS if s in (shops or [])]
    if not shops:
        raise ValueError("セールの対象の部門を1つ以上選んでください。")
    until = _until(hours)
    if not until:
        raise ValueError("セールの期間（時間）を入れてください。終わりの無いセールは作れません。")
    d["sale"] = {"pct": int(raw), "until": until, "shops": shops, "label": _clean_text(label, 40)}
    _save(d)


def sale() -> dict | None:
    s = settings()["sale"]
    if not s.get("pct") or not s.get("until") or time.time() > s["until"]:
        return None
    return s


def sale_pct(shop: str) -> int:
    s = sale()
    return int(s["pct"]) if s and shop in s.get("shops", []) else 0


def set_paused(shops, menus, message="", known_menus=()) -> None:
    d = settings()
    d["paused"] = {"shops": [s for s in SHOPS if s in (shops or [])],
                   "menus": [m for m in known_menus if m in (menus or [])],
                   "message": _clean_text(message, 200)}
    _save(d)


def paused_shop(shop: str) -> bool:
    return shop in settings()["paused"].get("shops", [])


def paused_menus() -> list:
    return list(settings()["paused"].get("menus", []))


def paused_message() -> str:
    return settings()["paused"].get("message") or ""


# ---------------------------------------------------------------- お客さんの受付停止
def _block_value(kind: str, value: str) -> str:
    value = str(value or "").strip()
    if kind == "uid":
        value = value.lower()
        if not re.fullmatch(r"[0-9a-f]{8}", value):
            raise ValueError("USERID は 8文字（0-9・a-f）で入れてください。")
        return value
    if kind == "ip":
        try:
            return str(ipaddress.ip_address(value))
        except ValueError:
            raise ValueError("IPアドレスの形が正しくありません。") from None
    raise ValueError("種類が正しくありません。")


def add_block(kind: str, value: str, days, note: str = "") -> str:
    value = _block_value(kind, value)
    raw = str(days or "").strip()
    if raw and raw != "0" and not (raw.isascii() and raw.isdigit() and 1 <= int(raw) <= 365):
        raise ValueError("期間は 1〜365 日で入れてください（空欄か0で無期限）。")
    until = time.time() + int(raw) * 86400 if raw and raw != "0" else 0
    d = settings()
    now = time.time()
    blocks = [b for b in d["blocks"] if not (b.get("kind") == kind and b.get("value") == value)
              and not (b.get("until") and b["until"] < now)]
    if len(blocks) >= MAX_BLOCKS:
        raise ValueError(f"受付停止は {MAX_BLOCKS} 件までです。古いものを解除してください。")
    blocks.append({"kind": kind, "value": value, "until": until, "note": _clean_text(note, 60), "at": now})
    d["blocks"] = blocks
    _save(d)
    return value


def remove_block(kind: str, value: str) -> bool:
    d = settings()
    left = [b for b in d["blocks"] if not (b.get("kind") == kind and b.get("value") == str(value or "").strip().lower())]
    if len(left) == len(d["blocks"]):
        return False
    d["blocks"] = left
    _save(d)
    return True


def blocks() -> list:
    now = time.time()
    return [b for b in settings()["blocks"] if not (b.get("until") and b["until"] < now)]


def blocked(uid: str, ip: str) -> bool:
    for b in blocks():
        if b.get("kind") == "uid" and b.get("value") == uid:
            return True
        if b.get("kind") == "ip" and ip and b.get("value") == ip:
            return True
    return False


# ---------------------------------------------------------------- 実行枠の数
def set_slots(normal, long) -> dict:
    out = {}
    for key, raw in (("normal", normal), ("long", long)):
        raw = str(raw if raw is not None else "").strip()
        if not raw:
            out[key] = None
            continue
        lo, hi = SLOT_LIMITS[key]
        if not (raw.isascii() and raw.isdigit()) or not lo <= int(raw) <= hi:
            raise ValueError(f"{'ふつうの枠' if key == 'normal' else '長時間の枠'}は {lo}〜{hi} で入れてください（空欄で初期値）。")
        out[key] = int(raw)
    d = settings()
    d["slots"] = out
    _save(d)
    return out


def slots() -> dict:
    s = settings()["slots"]
    return {"normal": s.get("normal"), "long": s.get("long")}


# ---------------------------------------------------------------- 台帳の表
def _tables(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS admin_log (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, ip TEXT,
                  action TEXT NOT NULL, target TEXT, msg TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS order_notes (order_id TEXT PRIMARY KEY, note TEXT NOT NULL DEFAULT '',
                  refund INTEGER NOT NULL DEFAULT 0, refund_note TEXT NOT NULL DEFAULT '', refund_at REAL, updated REAL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS job_stats (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, shop TEXT,
                  menu TEXT, ok INTEGER NOT NULL, error TEXT, order_id TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS job_stats_at ON job_stats(at)")
    db.execute("""CREATE TABLE IF NOT EXISTS faq (id INTEGER PRIMARY KEY AUTOINCREMENT, created REAL NOT NULL, shop TEXT NOT NULL,
                  q TEXT NOT NULL, a TEXT NOT NULL)""")


def _db():
    return paypay_gateway._ledger()


def log_action(ip: str, action: str, target: str = "", msg: str = "") -> None:
    with _db() as db:
        _tables(db)
        db.execute("INSERT INTO admin_log(at, ip, action, target, msg) VALUES(?,?,?,?,?)",
                   (time.time(), str(ip or "")[:64], str(action)[:40], str(target or "")[:80], _clean_text(msg, 300)))
        db.execute("DELETE FROM admin_log WHERE id <= (SELECT MAX(id) FROM admin_log) - 5000")   # 古いものから捨てる


def action_log(limit: int = 200, target: str = "") -> list:
    with _db() as db:
        _tables(db)
        if target:
            rows = db.execute("SELECT * FROM admin_log WHERE target = ? ORDER BY id DESC LIMIT ?", (target, int(limit)))
        else:
            rows = db.execute("SELECT * FROM admin_log ORDER BY id DESC LIMIT ?", (int(limit),))
        return [dict(r) for r in rows.fetchall()]


def save_note(order_id: str, note: str, refund, refund_note: str = "") -> dict:
    raw = str(refund or "0").strip().replace(",", "")
    if not (raw.isascii() and raw.isdigit()) or int(raw) > 1_000_000:
        raise ValueError("返金額は 0〜1,000,000 円の数字で入れてください。")
    refund = int(raw)
    now = time.time()
    with _db() as db:
        _tables(db)
        old = db.execute("SELECT refund, refund_at FROM order_notes WHERE order_id = ?", (order_id,)).fetchone()
        refund_at = None
        if refund:
            refund_at = old["refund_at"] if old and old["refund"] == refund and old["refund_at"] else now
        db.execute("""INSERT INTO order_notes(order_id, note, refund, refund_note, refund_at, updated) VALUES(?,?,?,?,?,?)
                      ON CONFLICT(order_id) DO UPDATE SET note=excluded.note, refund=excluded.refund,
                      refund_note=excluded.refund_note, refund_at=excluded.refund_at, updated=excluded.updated""",
                   (order_id, _clean_text(note, 500), refund, _clean_text(refund_note, 100), refund_at, now))
    return {"refund": refund, "refund_at": refund_at}


def note_of(order_id: str) -> dict | None:
    with _db() as db:
        _tables(db)
        row = db.execute("SELECT * FROM order_notes WHERE order_id = ?", (order_id,)).fetchone()
        return dict(row) if row else None


def record_jobs(rows) -> None:
    """実行の結末（成功・失敗）を残す。rows：(shop, menu, ok, error, order_id)。"""
    if not rows:
        return
    now = time.time()
    with _db() as db:
        _tables(db)
        db.executemany("INSERT INTO job_stats(at, shop, menu, ok, error, order_id) VALUES(?,?,?,?,?,?)",
                       [(now, s, str(m)[:60], 1 if ok else 0, _clean_text(e, 120) if not ok else None, oid) for s, m, ok, e, oid in rows])
        db.execute("DELETE FROM job_stats WHERE at < ?", (now - 120 * 86400,))   # 120日より前は捨てる


def job_summary(now: float | None = None) -> list:
    """メニューごとの成否（直近24時間・7日）と、多い失敗の理由。失敗の多いものを先に。"""
    now = now or time.time()
    with _db() as db:
        _tables(db)
        rows = [dict(r) for r in db.execute("SELECT shop, menu, ok, error, at FROM job_stats WHERE at >= ?", (now - 7 * 86400,))]
    out = {}
    for r in rows:
        k = (r["shop"] or "", r["menu"] or "")
        e = out.setdefault(k, {"shop": k[0], "shop_label": SHOP_LABELS.get(k[0], k[0]), "menu": k[1],
                               "day": [0, 0], "week": [0, 0], "errors": {}})
        e["week"][0] += 1
        e["week"][1] += r["ok"]
        if r["at"] >= now - 86400:
            e["day"][0] += 1
            e["day"][1] += r["ok"]
        if not r["ok"]:
            reason = r["error"] or "（理由の記録なし）"
            e["errors"][reason] = e["errors"].get(reason, 0) + 1
    result = []
    for e in out.values():
        e["errors"] = sorted(e["errors"].items(), key=lambda kv: -kv[1])[:3]
        e["day_rate"] = round(100 * e["day"][1] / e["day"][0]) if e["day"][0] else None
        e["week_rate"] = round(100 * e["week"][1] / e["week"][0]) if e["week"][0] else None
        result.append(e)
    result.sort(key=lambda e: (-(e["week"][0] - e["week"][1]), -e["week"][0]))
    return result


def faq_list(shop: str | None = None) -> list:
    with _db() as db:
        _tables(db)
        if shop:
            rows = db.execute("SELECT * FROM faq WHERE shop IN (?, 'all') ORDER BY id", (shop,))
        else:
            rows = db.execute("SELECT * FROM faq ORDER BY id")
        return [dict(r) for r in rows.fetchall()]


def faq_add(shop: str, q: str, a: str) -> int:
    if shop not in ("all", "nyanko", "tsum", "puni"):
        raise ValueError("部門を選んでください。")
    q, a = _clean_text(q, 100), _clean_text(a, 400)
    if len(q) < 3 or len(a) < 3:
        raise ValueError("質問と答えを入れてください。")
    with _db() as db:
        _tables(db)
        if db.execute("SELECT COUNT(*) FROM faq").fetchone()[0] >= MAX_FAQ:
            raise ValueError(f"追加の回答は {MAX_FAQ} 件までです。古いものを消してください。")
        return int(db.execute("INSERT INTO faq(created, shop, q, a) VALUES(?,?,?,?)", (time.time(), shop, q, a)).lastrowid)


def faq_delete(fid: int) -> bool:
    with _db() as db:
        _tables(db)
        return db.execute("DELETE FROM faq WHERE id = ?", (int(fid),)).rowcount == 1


# ---------------------------------------------------------------- 売上の集計
def series_of(shop: str | None) -> str:
    return shop if shop in ("nyanko", "tsum", "puni") else "other"


def shop_from_summary(summary: str, product_labels=()) -> str:
    """部門の記録が無い古い行：要約の書き出しから部門を決める（product_labels＝[(部門, 商品名), …]）。"""
    s = re.sub(r"^【[^】]{0,12}】", "", str(summary or ""))   # 【無料クーポン】【期間チケット】の印は飛ばす
    if s.startswith("ツムツム代行"):
        return "tsum"
    if s.startswith("ぷにぷに代行"):
        return "puni"
    for key, label in product_labels:
        if s.startswith(label):
            return key
    return "nyanko"


def parse_day(text: str, end: bool = False) -> float | None:
    """'YYYY-MM-DD'（日本時間）をその日の始まり（end なら終わり）の時刻に。"""
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", str(text or "").strip())
    if not m:
        return None
    try:
        day = datetime.datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=JST)
    except ValueError:
        return None
    return (day + datetime.timedelta(days=1)).timestamp() - 0.001 if end else day.timestamp()


def _bucket_start(ts: float, unit: str) -> datetime.datetime:
    d = datetime.datetime.fromtimestamp(ts, JST)
    if unit == "hour":
        return d.replace(minute=0, second=0, microsecond=0)
    d = d.replace(hour=0, minute=0, second=0, microsecond=0)
    if unit == "week":
        return d - datetime.timedelta(days=d.weekday())
    if unit == "month":
        return d.replace(day=1)
    return d


def _next_bucket(d: datetime.datetime, unit: str) -> datetime.datetime:
    if unit == "hour":
        return d + datetime.timedelta(hours=1)
    if unit == "week":
        return d + datetime.timedelta(days=7)
    if unit == "month":
        return (d.replace(day=28) + datetime.timedelta(days=4)).replace(day=1)
    return d + datetime.timedelta(days=1)


def _bucket_label(d: datetime.datetime, unit: str) -> str:
    if unit == "hour":
        return d.strftime("%m/%d %H時")
    if unit == "month":
        return d.strftime("%Y/%m")
    if unit == "week":
        return d.strftime("%m/%d") + "〜"
    return d.strftime("%m/%d")


def pick_unit(start: float, end: float, unit: str | None) -> str:
    """区切りを決める（指定が無ければ期間の長さから）。区切りが多すぎるときは、粗くする。"""
    span = end - start
    if unit not in UNITS:
        unit = "hour" if span <= 2 * 86400 else "day" if span <= 92 * 86400 else "week" if span <= 731 * 86400 else "month"
    limits = {"hour": 3600, "day": 86400, "week": 7 * 86400, "month": 28 * 86400}
    order = list(UNITS)
    while span / limits[unit] > MAX_BUCKETS and unit != "month":
        unit = order[order.index(unit) + 1]
    return unit


def paid_rows(start: float | None = None, end: float | None = None, product_labels=()) -> list:
    """入金を確認できた注文（PayPay の受け取りが済んだもの）。時刻は入金の時刻（送金リンクを受け付けた時刻）。"""
    sql = """SELECT h.order_id, h.created, h.user_id, COALESCE(l.amount, h.amount) AS amount, h.amount AS order_amount, h.state,
                    h.summary, h.shop, h.base, h.lines, h.coupon_kind, l.created AS paid_at, l.sender_id, l.sender_name
             FROM history h JOIN links l ON l.order_id = h.order_id AND l.state = 'paid'"""
    args = []
    if start is not None:
        sql += " WHERE l.created >= ? AND l.created <= ?"
        args += [start, end if end is not None else time.time() + 86400]
    with _db() as db:
        rows = [dict(r) for r in db.execute(sql + " ORDER BY l.created", args).fetchall()]
    for r in rows:
        r["shop"] = r["shop"] or shop_from_summary(r["summary"], product_labels)
        r["series"] = series_of(r["shop"])
        r["who"] = r["sender_id"] or r["user_id"] or r["order_id"]
        r["amount"] = int(r["amount"] or 0)
    return rows


def first_paid_at() -> float | None:
    with _db() as db:
        row = db.execute("SELECT MIN(created) FROM links WHERE state = 'paid'").fetchone()
        return float(row[0]) if row and row[0] else None


def refunds(start: float, end: float) -> list:
    with _db() as db:
        _tables(db)
        return [dict(r) for r in db.execute("SELECT order_id, refund, refund_at, refund_note FROM order_notes "
                                            "WHERE refund > 0 AND refund_at >= ? AND refund_at <= ?", (start, end)).fetchall()]


def _menu_shares(row) -> list:
    """1件の注文の金額を、内訳の行（メニュー）に、定価の割合で分ける。内訳の記録が無いものは1行にまとめる。"""
    try:
        lines = json.loads(row["lines"]) if row.get("lines") else []
    except ValueError:
        lines = []
    parts = []
    for line in lines if isinstance(lines, list) else []:
        if not isinstance(line, dict) or line.get("coupon"):
            continue
        try:
            value = int(line.get("unit") or 0) * int(line.get("count") or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            parts.append((str(line.get("label") or "")[:60], value, int(line.get("count") or 0)))
    if not parts:
        return [((row.get("summary") or "（内訳の記録なし）").split("／")[0][:40], row["amount"], 1)]
    whole = sum(p[1] for p in parts)
    return [(label, row["amount"] * value / whole, count) for label, value, count in parts]


def _totals(rows, refund_rows, first_paid, start: float) -> dict:
    gross = sum(r["amount"] for r in rows)
    refund = sum(int(r["refund"]) for r in refund_rows)
    discount = sum(max(0, int(r["base"]) - int(r["order_amount"] or 0)) for r in rows if r.get("base"))
    people = {r["who"] for r in rows}
    new = {w for w in people if first_paid.get(w, 0) >= start}
    return {"gross": gross, "count": len(rows), "avg": round(gross / len(rows)) if rows else 0, "refund": refund,
            "net": gross - refund, "discount": discount, "customers": len(people), "new_customers": len(new),
            "repeat_customers": len(people) - len(new)}


def report(start: float, end: float, unit: str | None = None, series: str = "all", product_labels=()) -> dict:
    """売上の推移と内訳。series＝all / nyanko / tsum / puni / other。"""
    unit = pick_unit(start, end, unit)
    every = paid_rows(product_labels=product_labels)
    first_paid = {}
    for r in every:   # お客さんごとの初めての入金（新しいお客さん・リピーターの区別に使う）
        first_paid.setdefault(r["who"], r["paid_at"])
    pick = (lambda r: True) if series == "all" else (lambda r: r["series"] == series)
    rows = [r for r in every if start <= r["paid_at"] <= end and pick(r)]
    span = end - start
    prev_rows = [r for r in every if start - span <= r["paid_at"] < start and pick(r)]
    refund_rows = refunds(start, end)
    prev_refunds = refunds(start - span, start)
    if series != "all":
        ids = {r["order_id"]: r["series"] for r in every}
        refund_rows = [x for x in refund_rows if ids.get(x["order_id"]) == series]
        prev_refunds = [x for x in prev_refunds if ids.get(x["order_id"]) == series]

    buckets, index = [], {}
    d = _bucket_start(start, unit)
    while d.timestamp() <= end and len(buckets) < MAX_BUCKETS + 2:
        index[d.timestamp()] = len(buckets)
        buckets.append({"start": d.timestamp(), "label": _bucket_label(d, unit), "yen": {s: 0 for s in SERIES},
                        "count": {s: 0 for s in SERIES}, "refund": 0})
        d = _next_bucket(d, unit)
    for r in rows:
        at = index.get(_bucket_start(r["paid_at"], unit).timestamp())
        if at is None:
            continue
        b = buckets[at]
        b["yen"][r["series"]] += r["amount"]
        b["count"][r["series"]] += 1
    for x in refund_rows:
        key = _bucket_start(x["refund_at"], unit).timestamp()
        if key in index:
            buckets[index[key]]["refund"] += int(x["refund"])
    running = 0
    for b in buckets:
        b["total"] = sum(b["yen"].values())
        running += b["total"] - b["refund"]
        b["cumulative"] = running

    by_series = [{"key": s, "label": SERIES_LABELS[s], "yen": sum(r["amount"] for r in rows if r["series"] == s),
                  "count": sum(1 for r in rows if r["series"] == s)} for s in SERIES]
    by_shop = [{"key": s, "label": SHOP_LABELS[s], "yen": sum(r["amount"] for r in rows if r["shop"] == s),
                "count": sum(1 for r in rows if r["shop"] == s)} for s in SHOPS]
    menus = {}
    for r in rows:
        for label, yen, count in _menu_shares(r):
            m = menus.setdefault((r["series"], label), {"label": label, "series": r["series"], "yen": 0.0, "orders": 0})
            m["yen"] += yen
            m["orders"] += 1
    top = sorted(menus.values(), key=lambda m: -m["yen"])[:15]
    for m in top:
        m["yen"] = round(m["yen"])
    hours, weekdays = [0] * 24, [0] * 7
    hour_n, week_n = [0] * 24, [0] * 7
    for r in rows:
        t = datetime.datetime.fromtimestamp(r["paid_at"], JST)
        hours[t.hour] += r["amount"]
        hour_n[t.hour] += 1
        weekdays[t.weekday()] += r["amount"]
        week_n[t.weekday()] += 1
    days = max(1, round(span / 86400))
    best = max(buckets, key=lambda b: b["total"], default=None)
    totals = _totals(rows, refund_rows, first_paid, start)
    totals["per_day"] = round(totals["net"] / days)
    return {"from": start, "to": end, "unit": unit, "series": list(SERIES), "series_labels": SERIES_LABELS,
            "buckets": buckets, "totals": totals, "prev": _totals(prev_rows, prev_refunds, first_paid, start - span),
            "by_series": by_series, "by_shop": by_shop, "menus": top,
            "hours": [{"h": h, "yen": hours[h], "count": hour_n[h]} for h in range(24)],
            "weekdays": [{"d": i, "label": "月火水木金土日"[i], "yen": weekdays[i], "count": week_n[i]} for i in range(7)],
            "best": {"label": best["label"], "yen": best["total"]} if best and best["total"] else None,
            "all_time": {"gross": sum(r["amount"] for r in every), "count": len(every), "customers": len(first_paid),
                         "since": every[0]["paid_at"] if every else None}}


def csv_rows(start: float, end: float, product_labels=()) -> list:
    """帳簿用：期間内の入金1件ごとの行（返金も入れる）。"""
    rows = paid_rows(start, end, product_labels)
    notes = {}
    with _db() as db:
        _tables(db)
        for r in db.execute("SELECT order_id, refund, refund_note, note FROM order_notes").fetchall():
            notes[r["order_id"]] = dict(r)
    out = [["入金日時", "注文番号", "部門", "内容", "割引前", "受け取った金額", "返金", "返金のメモ", "送金者", "USERID"]]
    for r in rows:
        n = notes.get(r["order_id"]) or {}
        out.append([datetime.datetime.fromtimestamp(r["paid_at"], JST).strftime("%Y-%m-%d %H:%M"), r["order_id"],
                    SHOP_LABELS.get(r["shop"], r["shop"]), r["summary"] or "", r["base"] if r.get("base") is not None else "",
                    r["amount"], n.get("refund") or 0, n.get("refund_note") or "", r["sender_name"] or "", r["user_id"] or ""])
    return out


# ---------------------------------------------------------------- 注文の検索と時系列
def search(q: str = "", state: str = "", shop: str = "", start: float | None = None, end: float | None = None,
           limit: int = 100, product_labels=()) -> list:
    sql = """SELECT h.order_id, h.created, h.user_id, h.amount, h.state, h.summary, h.diag, h.shop,
                    l.state AS pay_state, l.sender_name, l.sender_id, n.note, n.refund
             FROM history h LEFT JOIN links l ON l.order_id = h.order_id LEFT JOIN order_notes n ON n.order_id = h.order_id
             WHERE 1=1"""
    args: list = []
    q = str(q or "").strip()[:80]
    if q:
        like = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        sql += """ AND (h.order_id LIKE ? ESCAPE '\\' OR h.user_id LIKE ? ESCAPE '\\' OR h.summary LIKE ? ESCAPE '\\'
                   OR h.diag LIKE ? ESCAPE '\\' OR l.sender_name LIKE ? ESCAPE '\\' OR l.sender_id LIKE ? ESCAPE '\\'
                   OR n.note LIKE ? ESCAPE '\\' OR CAST(h.amount AS TEXT) = ?)"""
        args += [like] * 7 + [q]
    if state == "paid_money":
        sql += " AND l.state = 'paid'"
    elif state:
        sql += " AND h.state = ?"
        args.append(state)
    if start is not None:
        sql += " AND h.created >= ?"
        args.append(start)
    if end is not None:
        sql += " AND h.created <= ?"
        args.append(end)
    sql += " ORDER BY h.created DESC LIMIT ?"
    args.append(max(1, min(int(limit), 300)) * (3 if shop else 1))
    with paypay_gateway._ledger() as db:
        _tables(db)
        rows = [dict(r) for r in db.execute(sql, args).fetchall()]
    for r in rows:
        r["shop"] = r["shop"] or shop_from_summary(r["summary"], product_labels)
    if shop:
        rows = [r for r in rows if r["shop"] == shop or (shop == "other" and series_of(r["shop"]) == "other")]
    return rows[:max(1, min(int(limit), 300))]


def order_record(order_id: str) -> dict | None:
    with _db() as db:
        _tables(db)
        h = db.execute("SELECT * FROM history WHERE order_id = ?", (order_id,)).fetchone()
        if not h:
            return None
        link = db.execute("SELECT state, created, amount, sender_id, sender_name, last_status, used FROM links WHERE order_id = ?",
                          (order_id,)).fetchone()
        events = [dict(r) for r in db.execute("SELECT at, state, text FROM order_events WHERE order_id = ? ORDER BY id",
                                              (order_id,)).fetchall()]
        review = None
        try:
            row = db.execute("SELECT stars, body, created, hidden FROM reviews WHERE order_id = ?", (order_id,)).fetchone()
            review = dict(row) if row else None
        except Exception:  # noqa: BLE001  実績の表がまだ無い
            review = None
    return {"history": dict(h), "link": dict(link) if link else None, "events": events, "review": review}
