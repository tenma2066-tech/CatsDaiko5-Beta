"""販売用のソース一式（zip）を作る。

「ソースコード購入」（/source）で渡す zip は、いま動いているこのコードから毎回同じ規則で作る。
- 入れるのは INCLUDE に書いたものだけ（許可リスト方式）。データ・鍵・ログは最初から対象外。
- 持ち主の情報（店名・問い合わせ先・ドメイン・検索の所有確認など）は REPLACE で置き換える。
- 置き換えたあとに持ち主の情報が残っていたら、zip を作らずにエラーにする（verify）。

使い方（手元で中身を確かめる）:  python source_pack.py out.zip
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import os
import sys
import threading
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
PACKAGE_DIR = "shop-source"          # zip を開いたときにできるフォルダの名前

# 入れるもの（リポジトリの先頭からの相対パス。フォルダは中身すべて）
INCLUDE = (
    "*.py", "requirements.txt", "constraints.txt", "Dockerfile", ".dockerignore", ".gitignore", "ruff.toml",
    "event_maps.json", "template_save.sav", "README.md", "MAINTENANCE.md",
    "templates", "static", "puni_engine", "tsum_engine", "scripts", "deploy/vps", "docs",
    ".github/workflows/ci.yml", ".github/workflows/vps-deploy.yml",
)
# 入れないもの（INCLUDE に当たっても外す）
EXCLUDE = (
    "__pycache__", "*.pyc", "*.log", "*.sqlite3", "*.db", ".env", ".env.*", "*.tmp",
    "deploy/vps/known_hosts",                       # 持ち主のサーバーの鍵の指紋
    "static/flow.jpg", "static/flow.webp", "static/tsum-flow.jpg", "static/tsum-flow.webp",   # 店名・連絡先が描かれた図解
    "static/ogp-nyanko.jpg", "static/ogp-tsum.jpg",                                           # 店名が描かれたリンク用画像
    "docs/XBOT_NOTES.md", "docs/XBOT_PLAN.md",      # このお店とは別の仕組みのメモ
    "docs/CLAUDE_GUIDE.md", "docs/SEARCH_INDEXING.md", "docs/VPS_DEPLOY_OLD_ORACLE.md", "docs/BETA_FLY_PAYPAY.md",
    "docs/OLD_README.md", "docs/AUDIT_PLAN.md",     # 昔の版の説明・作業メモ
)
TEXT_EXT = (".py", ".html", ".js", ".css", ".md", ".txt", ".yml", ".yaml", ".json", ".sh", ".toml", ".cfg", ".ini")
TEXT_NAMES = ("Dockerfile", "Caddyfile", ".dockerignore", ".gitignore")

# 持ち主の情報 → 買った人が自分の値を入れる場所。上から順に置き換える。
REPLACE = (
    ("HOaFZOzV5t4jyEXdAr2bMg6f2OVwvdpedlAosg1FXT0", ""),     # Google の所有確認
    ("2119246650996932810", ""),                              # レビューのお願いの返信先ポスト
    ("nyanko-daiko-tnshop.duckdns.org", "your-shop.example.com"),
    ("tenma2066-tech/CatsDaiko5-Beta", "your-name/your-repo"),
    ("tenma2066-tech/CatsDaiko5", "your-name/your-repo"),
    ("tenma2066-tech", "your-name"),
    ('_env("SHOP_X_ID", "paysms1212")', '_env("SHOP_X_ID", "")'),   # 問い合わせ先は、設定するまで出さない
    ("paysms1212", "your_x_id"),
    ("TN SHOP", "MY SHOP"),
)
# 置き換え後に1つでも残っていたら失敗にする語（小文字で比べる）
FORBIDDEN = ("paysms1212", "tnshop.duckdns", "tenma2066", "hoafzozv5t4j", "2119246650996932810", "discord.gg/")

LICENSE_TEXT = """# ご利用条件

このソース一式は、購入したご本人が、ご自身のお店（サービス）を作るために使えます。

- 自分のサーバーに置いて動かす・自分用に書き換える：自由です。
- 第三者への再配布・転売・公開（GitHub の公開リポジトリなどを含む）はできません。
- 無保証です。動作・収益・アカウントの安全について、販売者は責任を負いません。
- PayPay・各ゲームの非公式な仕組みを使います。仕様変更で動かなくなることがあります。
  各サービスの利用規約・法律（特定商取引法の表示など）は、運営するご本人が確認してください。
- 導入の手順は docs/SETUP.md を読んでください。個別の導入サポートは含まれません。
"""

_lock = threading.Lock()
_cache: dict = {"key": None, "data": b""}


def _excluded(rel: str) -> bool:
    """パスで書いた規則（a/b）はその場所だけ、名前で書いた規則（*.log）はどの階層でも外す。"""
    parts = rel.split("/")
    for pat in EXCLUDE:
        if "/" in pat:
            if rel == pat or rel.startswith(pat + "/"):
                return True
        elif any(fnmatch.fnmatch(p, pat) for p in parts):
            return True
    return False


def _included(rel: str) -> bool:
    """パス（a/b）はその場所の下すべて、*.py のような名前は先頭の階層だけ、それ以外はその名前のファイルかフォルダ。"""
    top = rel.split("/")[0]
    for pat in INCLUDE:
        if "/" in pat:
            hit = rel == pat or rel.startswith(pat + "/")
        elif "*" in pat:
            hit = "/" not in rel and fnmatch.fnmatch(rel, pat)
        else:
            hit = top == pat
        if hit:
            return not _excluded(rel)
    return False


def list_files(root: str = ROOT) -> list[str]:
    """zip に入れるファイルの一覧（相対パス・並び順は固定）。"""
    out = []
    for base, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git", "instance", "node_modules"))
        for name in sorted(files):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if _included(rel) and os.path.isfile(full) and not os.path.islink(full):
                out.append(rel)
    return out


def _is_text(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name in TEXT_NAMES or name.endswith(TEXT_EXT)


def _scrub(rel: str, data: bytes) -> bytes:
    """文字のファイルだけ、持ち主の情報を置き換える。"""
    if not _is_text(rel):
        return data
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data
    for old, new in REPLACE:
        text = text.replace(old, new)
    return text.encode("utf-8")


def verify(files: dict[str, bytes]) -> None:
    """持ち主の情報・鍵らしいものが残っていないか確かめる。残っていれば ValueError。"""
    for rel, data in files.items():
        if not _is_text(rel) or rel == "source_pack.py":
            continue
        low = data.decode("utf-8", "replace").lower()
        for word in FORBIDDEN:
            if word in low:
                raise ValueError(f"{rel} に持ち主の情報（{word}）が残っています。source_pack.REPLACE を見直してください。")


def _pack_self(data: bytes) -> bytes:
    """このファイル自身は、持ち主の値の一覧（REPLACE・FORBIDDEN）を空にして入れる。"""
    text = data.decode("utf-8")
    start, end = text.index("REPLACE = ("), text.index("LICENSE_TEXT = ")
    neutral = ('REPLACE = ()      # （持ち主の値）→（置き換える値）の組。自分で配るときに書く\n'
               'FORBIDDEN = ()    # 置き換え後に残っていてはいけない語（小文字）\n\n')
    return (text[:start] + neutral + text[end:]).encode("utf-8")


def collect(root: str = ROOT) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for rel in list_files(root):
        with open(os.path.join(root, rel), "rb") as f:
            data = f.read()
        files[rel] = _pack_self(data) if rel == "source_pack.py" else _scrub(rel, data)
    files["LICENSE.md"] = LICENSE_TEXT.encode("utf-8")
    verify(files)
    return files


def build(root: str = ROOT) -> bytes:
    """zip のバイト列を作る（同じ中身なら毎回同じバイト列になるよう、日時は固定）。"""
    files = collect(root)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in sorted(files):
            info = zipfile.ZipInfo(f"{PACKAGE_DIR}/{rel}", date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if rel.endswith(".sh") else 0o644) << 16
            z.writestr(info, files[rel])
    return buf.getvalue()


def _fingerprint(root: str) -> str:
    h = hashlib.sha256()
    for rel in list_files(root):
        st = os.stat(os.path.join(root, rel))
        h.update(f"{rel}:{st.st_size}:{int(st.st_mtime)}\n".encode())
    return h.hexdigest()


def cached(root: str = ROOT) -> bytes:
    """作った zip を覚えておき、ファイルが変わったときだけ作り直す。"""
    with _lock:
        key = _fingerprint(root)
        if _cache["key"] != key:
            _cache.update(key=key, data=build(root))
        return _cache["data"]


def summary(root: str = ROOT) -> dict:
    """販売ページに出す中身の数（ファイル数・行数・大きさ）。"""
    data = cached(root)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        lines = sum(z.read(n).count(b"\n") for n in names if n.endswith((".py", ".html", ".js", ".css")))
    return {"files": len(names), "lines": lines, "bytes": len(data),
            "tests": sum(1 for n in names if n.rsplit("/", 1)[-1].startswith("test_"))}


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "shop-source.zip"
    blob = build()
    with open(target, "wb") as fh:
        fh.write(blob)
    print(f"{target}: {len(blob):,} bytes / {len(collect())} files")
