"""標準入力から管理パスワードを受け取り、照合用のハッシュのみを保存する。"""
import grp
import hashlib
import os
from pathlib import Path
import secrets
import sys
import tempfile

password = sys.stdin.buffer.read(4097)
if not password or len(password) > 4096:
    sys.exit("管理パスワードが空か、長すぎます")
salt = secrets.token_bytes(16)
digest = hashlib.scrypt(password, salt=salt, n=16384, r=8, p=1).hex()
path = Path("/etc/paykyash/web-password.hash")
with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
    f.write(f"{salt.hex()}:{digest}\n")
    os.fchown(f.fileno(), 0, grp.getgrnam("paykyash").gr_gid)
    os.fchmod(f.fileno(), 0o640)
os.replace(f.name, path)
