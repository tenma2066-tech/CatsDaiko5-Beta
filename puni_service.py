"""ぷにぷに代行（お店の3つ目の大メニュー）の実行部分。

にゃんこ代行が `engine.py`、ツムツム代行が `tsum_service.py` の位置にあるのと同じ。
ゲーム（妖怪ウォッチぷにぷに）との実際のやり取りは `puni_engine/` に置いた
素のスクリプト群（配布v2・未改変）が行う。

- `puni_engine/` は依頼主の Discord 版から持ってきたもので、中身は変えない。
- 実行時に書き込むファイル（udkey／ログ／マスタキャッシュ）は
  `PUNI_DATA_DIR`（既定：`/data/puni`、無ければ `instance/puni`）に置く。
  リポジトリの中には書き込まない。
- アカウント情報（メールアドレス・パスワード・捨てメール）はログに出さない。
"""
from __future__ import annotations

import os
import shutil
import sys
import threading

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE_SOURCE = os.path.join(_HERE, "puni_engine")
_ENGINE_FILES = ("ywp_auto.py", "ywp_master.py", "proxy_pool.py", "settings.json")
_ENGINE_DATA = "data"

_core = None
_core_lock = threading.Lock()


def data_dir() -> str:
    """実行時に書き込む場所。Fly/VPS では /data（永続ボリューム）の下。"""
    configured = os.environ.get("PUNI_DATA_DIR", "").strip()
    if configured:
        return os.path.abspath(configured)
    if os.path.isdir("/data"):
        return "/data/puni"
    return os.path.join(_HERE, "instance", "puni")


def engine_dir() -> str:
    return os.path.join(data_dir(), "engine")


def _prepare_engine() -> str:
    """素のスクリプトを書き込める場所へ写す（更新されていれば上書きする）。
    スクリプトは自分の隣に状態ファイル（udkeys.json 等）を作るため、
    リポジトリの中では動かさない。"""
    dest = engine_dir()
    os.makedirs(dest, mode=0o700, exist_ok=True)
    for name in _ENGINE_FILES:
        src = os.path.join(_ENGINE_SOURCE, name)
        dst = os.path.join(dest, name)
        if not os.path.isfile(src):
            raise RuntimeError(f"ぷにぷにの実行ファイルがありません: {name}")
        if not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
            shutil.copy2(src, dst)
    src_data = os.path.join(_ENGINE_SOURCE, _ENGINE_DATA)
    dst_data = os.path.join(dest, _ENGINE_DATA)
    os.makedirs(dst_data, exist_ok=True)
    if os.path.isdir(src_data):
        for name in os.listdir(src_data):
            s = os.path.join(src_data, name)
            d = os.path.join(dst_data, name)
            if not os.path.isfile(s):
                continue
            # map_names.json / current_event.json は実行中に更新されうるので上書きしない
            if name in ("map_names.json", "current_event.json", "master_cache.json") and os.path.exists(d):
                continue
            if not os.path.exists(d) or os.path.getmtime(s) > os.path.getmtime(d):
                shutil.copy2(s, d)
    return dest


def core():
    """`puni_engine.ywp_auto` を読み込んで返す（最初の1回だけ準備する）。"""
    global _core
    if _core is not None:
        return _core
    with _core_lock:
        if _core is None:
            dest = _prepare_engine()
            if dest not in sys.path:
                sys.path.insert(0, dest)
            import ywp_auto  # type: ignore
            _core = ywp_auto
    return _core


# ---------------------------------------------------------------- メニュー表
# value が入っているメニューは数量入力が要る。min/max は安全上限。
MENUS = {
    "puni_account":     {"label": "アカウント作成（LEVEL5 ID）", "price": "puni_account",
                         "value": (1, 20), "unit": "件",
                         "desc": "捨てメールで LEVEL5 ID を自動作成します。"
                                 "作成後のメールアドレスとパスワードをお渡しします。",
                         "order": 10},
    "puni_aged":        {"label": "久垢作成（アカ作成＋初回クリアまで）", "price": "puni_aged",
                         "value": (1, 10), "unit": "件",
                         "desc": "アカウント作成に加えて、設定された初回ステージを"
                                 "クリアした状態まで進めてお渡しします。",
                         "order": 20},
    "puni_farm":        {"label": "自動周回（ステージID・周回数指定）", "price": "puni_farm_10",
                         "value": (1, 999), "unit": "周",
                         "desc": "指定ステージを指定回数まで自動で周回します。"
                                 "ひとだまは自動補充します（10周ごとの料金）。",
                         "order": 30},
    "puni_unlimited":   {"label": "回し放題代行（時間制）", "price": "puni_hour",
                         "value": (30, 24 * 60), "unit": "分",
                         "desc": "指定した時間のあいだ、ステージを回し続けます"
                                 "（30分単位・最大24時間）。",
                         "order": 40},
    "puni_hitodama":    {"label": "ひとだま補充", "price": "puni_hitodama",
                         "value": (50, 9999), "unit": "個",
                         "desc": "ゲーム内の Yマネー を使って、指定個数までひとだまを補充します。",
                         "order": 50},
    "puni_coin_gacha":  {"label": "コインガチャ回し切り", "price": "puni_coin_gacha",
                         "desc": "開催中の「まわし放題ガチャ」を回し切ります。",
                         "order": 60},
    "puni_stage_clear": {"label": "ステージクリア（1マップ単発）", "price": "puni_stage_clear",
                         "value": (1, 99_999_999), "unit": "ステージID",
                         "desc": "指定したステージを 1回クリアします（単発）。",
                         "order": 70},
}

# 1つの注文で選べないもの（新規垢を作るメニューは単独）
SOLO_MENUS = ("puni_account", "puni_aged")

# ステージIDの入力が要るメニュー（menus 配列と別に stage: {menu: id} で受ける）
STAGE_MENUS = ("puni_farm", "puni_unlimited", "puni_stage_clear")
STAGE_ID_MIN = 1_000_000
STAGE_ID_MAX = 99_999_999

MODES = ("email", "new")
MODE_LABELS = {"email": "メールアドレスでログイン",
               "new":   "新しいアカウントを作る"}


def menu_label(key: str) -> str:
    menu = MENUS.get(key)
    return menu["label"] if menu else str(key)


def needs_value(key: str) -> bool:
    return "value" in MENUS.get(key, {})


def value_limits(key: str):
    return MENUS.get(key, {}).get("value")


def needs_stage(key: str) -> bool:
    return key in STAGE_MENUS


def sort_menus(keys) -> list:
    """アカウント系→周回→ひとだま→ガチャ→単発 の順で並べる。"""
    known = [k for k in keys if k in MENUS]
    return sorted(dict.fromkeys(known), key=lambda k: MENUS[k]["order"])


def validate_order(menus, qty: dict, stages: dict | None = None):
    """画面から来た内容を検査する。戻り値は (並べ替えたメニュー, 数量, ステージID, エラー文)。
    stages … STAGE_MENUS 用の {メニューkey: ステージID} 辞書。"""
    picked = sort_menus(menus)
    if not picked:
        return [], {}, {}, "ぷにぷに代行のメニューを選んでください。"
    solo = [k for k in picked if k in SOLO_MENUS]
    if solo and len(picked) > 1:
        return [], {}, {}, f"「{menu_label(solo[0])}」はほかのメニューと同時に選べません。"
    clean_qty: dict = {}
    for key in picked:
        limits = value_limits(key)
        if not limits:
            continue
        raw = str((qty or {}).get(key, "")).strip()
        if raw == "":
            return [], {}, {}, f"「{menu_label(key)}」の数量を入力してください。"
        if not (raw.isascii() and raw.isdigit()):
            return [], {}, {}, f"「{menu_label(key)}」の数量は0以上の整数（半角）で入力してください。"
        num = int(raw)
        low, high = limits
        if num < low or num > high:
            return [], {}, {}, f"「{menu_label(key)}」は {low:,}〜{high:,} の範囲で入力してください。"
        # 回し放題は30分単位
        if key == "puni_unlimited" and num % 30 != 0:
            return [], {}, {}, "「回し放題代行」の時間は 30分単位で入力してください。"
        clean_qty[key] = num
    clean_stage: dict = {}
    for key in picked:
        if not needs_stage(key):
            continue
        raw = str((stages or {}).get(key, "")).strip()
        if raw == "":
            return [], {}, {}, f"「{menu_label(key)}」のステージIDを入力してください。"
        if not (raw.isascii() and raw.isdigit()):
            return [], {}, {}, f"「{menu_label(key)}」のステージIDは半角の整数で入力してください。"
        sid = int(raw)
        if sid < STAGE_ID_MIN or sid > STAGE_ID_MAX:
            return [], {}, {}, (f"「{menu_label(key)}」のステージIDは "
                                f"{STAGE_ID_MIN:,}〜{STAGE_ID_MAX:,} の範囲で入力してください。")
        clean_stage[key] = sid
    return picked, clean_qty, clean_stage, ""


def check_account(mode: str, email: str, password: str):
    """アカウント入力の検査。戻り値はエラー文（問題なければ空文字）。"""
    if mode not in MODES:
        return "アカウントの入力方法を選んでください。"
    if mode == "new":
        return ""
    if not email or not password:
        return "メールアドレスとパスワードを入力してください。"
    if "@" not in email:
        return "メールアドレスを入力してください（半角 @ を含む形式）。"
    return ""


def pricing_units(menus, qty: dict) -> list[tuple[str, int]]:
    """料金計算に使う「メニューごとの件数」。戻り値は (PRICES のキー, 件数) の列。
    - puni_farm は 10周ごとに1件（切り上げ）
    - puni_unlimited は 60分ごとに1件（切り上げ）
    - ほかは量に関係なく1件
    料金そのものは pricing.compute_puni_amount が PRICES を掛けて出す。
    """
    out: list[tuple[str, int]] = []
    for key in sort_menus(menus):
        menu = MENUS[key]
        price_key = menu["price"]
        if key == "puni_farm":
            laps = int((qty or {}).get(key, 0) or 0)
            units = (laps + 9) // 10
        elif key == "puni_unlimited":
            minutes = int((qty or {}).get(key, 0) or 0)
            units = (minutes + 59) // 60
        elif key in ("puni_account", "puni_aged"):
            units = int((qty or {}).get(key, 1) or 1)
        else:
            units = 1
        out.append((price_key, max(1, units)))
    return out


# ---------------------------------------------------------------- 実行
class _Progress:
    """ゲーム側の進捗を、お店のジョブ表示に流すための入れ物。"""

    def __init__(self, sink=None):
        self._sink = sink
        self.last = ""

    def update(self, step, text, force=False):
        self.last = str(text)
        if self._sink is not None:
            try:
                self._sink(int(step or 0), str(text))
            except Exception:  # noqa: BLE001  表示で本処理を止めない
                pass


def _mask_email(value: str) -> str:
    s = str(value or "")
    if "@" not in s:
        return "*" * len(s)
    local, _, domain = s.partition("@")
    return (local[:1] + "*" * max(0, len(local) - 1)) + "@" + domain


# メニューごとの実装状況。実装済みに切り替えるときはここだけ True にして、
# _dispatch の分岐で実際の呼び出しに差し替える。
_IMPLEMENTED = frozenset()


def _dispatch(c, menu: str, qty: dict, stages: dict, mode: str,
              login_id: str, password: str, progress: _Progress, log) -> tuple[bool, str]:
    """1つのメニューを実行する。戻り値は (成功, 画面に見せる1行)。
    未実装のメニューは False を返し、呼び出し側でまとめて失敗として報告する。"""
    log(f"「{menu_label(menu)}」はまだサーバ上では準備中です。")
    return False, f"■ {menu_label(menu)}：準備中（運営側で動作確認中）"


def run_order(mode: str, login_id: str, password: str,
              menus, qty: dict, stages: dict | None = None,
              progress=None, log=None) -> dict:
    """ぷにぷに代行1件を実行する。例外は投げず、必ず結果を dict で返す。

    mode … "email"（メールアドレス）／"new"（新規アカウント作成）
    戻り値 … {"ok", "error", "lines", "done", "account", "moved"}
      account … mode == "new" で発行した (email, password, player_name) の辞書
      moved   … ゲームアカウントがこちらに移っているか（今は常に False）
    """
    log = log or (lambda _t: None)
    prog = _Progress(progress)
    out = {"ok": False, "error": "", "lines": [], "done": 0,
           "account": None, "moved": False}
    menus = sort_menus(menus)
    if not menus:
        out["error"] = "実行するメニューがありません。"
        return out

    # 実装完了までは、受け取った注文は実行枠には乗せない。
    # 料金・検査・UI・台帳の路は全部通してある。中身の実行だけ運営側の確認待ち。
    prog.update(0, "受付処理中です…", force=True)
    log(f"モード: {mode}（{MODE_LABELS.get(mode, mode)}）／ユーザー: {_mask_email(login_id)}")
    lines: list[str] = []
    for menu in menus:
        ok, line = _dispatch(core() if False else None, menu, qty or {}, stages or {},
                             mode, login_id, password, prog, log)
        lines.append(line)
        if not ok:
            out["error"] = ("ぷにぷに代行の自動実行は現在準備中です。"
                            "ご注文は受け付けましたので、担当者より個別にご連絡いたします。")
            out["lines"] = lines
            return out
        out["done"] += 1

    out["lines"] = lines
    out["ok"] = True
    prog.update(10, "完了しました", force=True)
    return out
