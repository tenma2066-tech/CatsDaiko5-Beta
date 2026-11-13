"""Password hashes in SQLite; use a persistent path for restart durability."""
import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash


class PasswordStore:
    def __init__(self, path, initial_password):
        self.path = Path(path)
        self.initial_password = initial_password
        self.lock = threading.Lock()
        self.ready = False

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        os.chmod(self.path, 0o600)
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        with self.lock:
            if self.ready:
                return
            with self._connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute('CREATE TABLE IF NOT EXISTS passwords (id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL, password_hash TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                if not db.execute("SELECT 1 FROM metadata WHERE key='initialized'").fetchone():
                    db.execute('INSERT INTO passwords (label, password_hash) VALUES (?, ?)',
                               ('初期パスワード', generate_password_hash(self.initial_password, method='pbkdf2:sha256:600000')))
                    db.execute("INSERT INTO metadata VALUES ('initialized', '1')")
            self.ready = True

    def list(self):
        self.initialize()
        with self._connect() as db:
            return db.execute('SELECT id, label FROM passwords ORDER BY id').fetchall()

    def authenticate(self, password):
        if len(password) > 256:
            return None
        self.initialize()
        with self._connect() as db:
            rows = db.execute('SELECT id, password_hash FROM passwords').fetchall()
        for ident, password_hash in rows:
            if check_password_hash(password_hash, password):
                return ident
        return None

    def contains(self, ident):
        self.initialize()
        with self._connect() as db:
            return db.execute('SELECT 1 FROM passwords WHERE id=?', (ident,)).fetchone() is not None

    def add(self, label, password):
        self.initialize()
        if not label or len(label) > 80 or len(password) < 8 or len(password) > 256:
            raise ValueError('名前は80文字以内、パスワードは8〜256文字にしてください。')
        password_hash = generate_password_hash(password, method='pbkdf2:sha256:600000')
        with self.lock, self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            for (existing_hash,) in db.execute('SELECT password_hash FROM passwords'):
                if check_password_hash(existing_hash, password):
                    raise ValueError('同じパスワードは既に登録されています。')
            db.execute('INSERT INTO passwords (label, password_hash) VALUES (?, ?)', (label, password_hash))

    def delete(self, ident):
        self.initialize()
        with self.lock, self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT COUNT(*) FROM passwords').fetchone()[0] <= 1:
                raise ValueError('最後のパスワードは削除できません。先に追加してください。')
            if not db.execute('DELETE FROM passwords WHERE id=?', (ident,)).rowcount:
                raise ValueError('パスワードが見つかりません。')
