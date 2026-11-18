"""データの保存（SQLite）。管理サイトと常駐プロセスはこのファイルだけでやり取りする。"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

DATA_DIR = os.environ.get("XBOT_DATA_DIR", os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"))
DB_PATH = os.path.join(DATA_DIR, "xbot.sqlite3")
UPLOAD_DIR = os.path.join(DATA_DIR, "uploads")
SHOT_DIR = os.path.join(DATA_DIR, "shots")
PROFILE_DIR = os.path.join(DATA_DIR, "profile")

DEFAULTS = {
    "paused": False,            # 一時停止
    "dry_run": True,            # 下書きモード（最初はオン：投稿・送信ボタンは押さない）
    "dm_enabled": False,        # DM自動返信（最初はオフ）
    "max_posts_day": 5,         # 1日の投稿数の上限
    "max_replies_hour": 10,     # 1時間の返信数の上限
    "dm_interval_min": 5,       # DMを確認する間隔（分）
    "reply_cooldown_hours": 24, # 同じ相手に続けて返信しない時間
    "dm_scan_limit": 10,        # 1回に確認する会話の数
    "login_until": 0,           # ログイン画面を出しておく期限（UNIX時刻）
    "vnc_password": "",
    "status": {},               # 常駐プロセスの状態（ログイン中か・最後の確認など）
    "request": "",              # 管理サイトからの依頼（check_login / snapshot / stop_login）
}


def _init(db):
    db.executescript("""
    CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT NOT NULL, images TEXT NOT NULL DEFAULT '[]',
        at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'scheduled', created REAL NOT NULL,
        done_at REAL, error TEXT, shot TEXT, tries INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS rules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, kind TEXT NOT NULL,
        keywords TEXT NOT NULL DEFAULT '', match TEXT NOT NULL DEFAULT 'any', reply TEXT NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1, priority INTEGER NOT NULL DEFAULT 100, created REAL NOT NULL);
    CREATE TABLE IF NOT EXISTS replied (
        conv TEXT NOT NULL, msg_hash TEXT NOT NULL, rule_id INTEGER, at REAL NOT NULL, dry INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (conv, msg_hash));
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, level TEXT NOT NULL, kind TEXT NOT NULL, msg TEXT NOT NULL);
    """)
    for col in ("repeat TEXT", "parent INTEGER"):     # 定期投稿（repeat）と、その実行記録（parent）
        try:
            db.execute("ALTER TABLE posts ADD COLUMN " + col)
        except sqlite3.OperationalError:
            pass


@contextmanager
def db():
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        _init(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()


def get(key):
    with db() as d:
        r = d.execute("SELECT v FROM settings WHERE k=?", (key,)).fetchone()
    return json.loads(r["v"]) if r else DEFAULTS.get(key)


def get_all():
    out = dict(DEFAULTS)
    with db() as d:
        for r in d.execute("SELECT k, v FROM settings"):
            out[r["k"]] = json.loads(r["v"])
    return out


def put(key, value):
    with db() as d:
        d.execute("INSERT INTO settings(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, json.dumps(value, ensure_ascii=False)))


def update_status(**kw):
    st = get("status") or {}
    st.update(kw)
    put("status", st)


def log(level, kind, msg):
    with db() as d:
        d.execute("INSERT INTO events(at, level, kind, msg) VALUES(?,?,?,?)", (time.time(), level, kind, str(msg)[:1000]))
        d.execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 2000 FROM events)")
    print(f"[{level}] {kind}: {msg}", flush=True)   # journalctl でも見られる
