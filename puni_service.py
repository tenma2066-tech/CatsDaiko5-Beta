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


# バトルのリザルトコード。bot.py の同名定数と同じ。
FATAL_CODES = (202, 30)   # これが出たら打ち切る（更新必須・データ異常など）
TOKEN_CODES = (32,)       # token ズレ。再ログインで復帰を試みる
BATTLE_RETRY = 3          # 連続エラー時の再試行回数
RETRY_WAIT = 4.0          # 再試行の間隔（秒）

# 久垢作成でクリアさせるステージ（engine の settings.json → 既定 1001001）。
AGED_STAGE_ID = int(os.environ.get("PUNI_AGED_STAGE_ID", "0") or 0)

# LEVEL5 ID 登録フロー（bot.py 由来）。
_L5_REG_URL = "https://auth.level5-id.com/user_registration?locale=ja"
_L5_VERIFY_SUBJECT = "［LEVEL5 ID］アカウント新規登録用URL送付のお知らせ"
_GM_API_URL = "https://api.guerrillamail.com/ajax.php"
_PASSWORD_LEN = 10
_MAIL_WAIT_TIMEOUT = 120.0
_MAIL_POLL_INTERVAL = 5.0
_PLAYER_NAME_MAX = 16
_BROWSER_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                   "Gecko/20100101 Firefox/126.0"),
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "ja,en-US;q=0.7,en;q=0.3",
    "Content-Type": "application/x-www-form-urlencoded",
    "Upgrade-Insecure-Requests": "1",
}


def _proxy() -> str | None:
    """このサーバーでぷにぷにの通信を通すプロキシ（任意）。
    SOCKS/HTTP どちらも可。未設定ならサーバーの固定IPで直接つなぐ。"""
    return (os.environ.get("PUNI_SOCKS_URL") or os.environ.get("PUNI_PROXY") or "").strip() or None


def _aged_stage(c) -> int:
    """久垢作成でクリアさせるステージID。env 優先、無ければ engine の settings.json、
    それも無ければ 1001001。"""
    if AGED_STAGE_ID:
        return AGED_STAGE_ID
    try:
        import json
        with open(os.path.join(engine_dir(), "settings.json"), encoding="utf-8") as f:
            v = int(json.load(f).get("AGED_STAGE_ID") or 0)
            if v:
                return v
    except (OSError, ValueError, TypeError):
        pass
    return 1001001


def _gen_password(length: int = _PASSWORD_LEN) -> str:
    import random
    import string
    chars = string.ascii_letters + string.digits
    return "".join(random.choice(chars) for _ in range(length))


def _random_player_name() -> str:
    import random
    import string
    return "ぷに" + "".join(random.choice(string.digits) for _ in range(4))


def _find_auth_token(html: str) -> str | None:
    import re
    for pat in (r'name="authenticity_token"[^>]*value="([^"]+)"',
                r'value="([^"]+)"[^>]*name="authenticity_token"',
                r'name="csrf-token"\s+content="([^"]+)"',
                r'content="([^"]+)"[^>]*name="csrf-token"'):
        m = re.search(pat, html)
        if m:
            return m.group(1)
    return None


class _GuerrillaMail:
    """GuerrillaMail 公式API（api.guerrillamail.com/ajax.php）の最小ラッパー。
    bot.py の非同期版を requests で同期に書き直したもの。"""

    def __init__(self, session):
        self.session = session
        self.sid_token = None
        self.address = None

    def _call(self, func: str, **params) -> dict:
        params["f"] = func
        params.setdefault("ip", "127.0.0.1")
        params.setdefault("agent", "guerrillamail-api-client")
        if self.sid_token:
            params.setdefault("sid_token", self.sid_token)
        r = self.session.get(_GM_API_URL, params=params, timeout=30)
        try:
            data = r.json()
        except ValueError:
            raise RuntimeError(f"捨てメールAPIの応答が不正です（status={r.status_code}）")
        if not isinstance(data, dict):
            raise RuntimeError(f"捨てメールAPIの応答が不正です（status={r.status_code}）")
        if data.get("sid_token"):
            self.sid_token = data["sid_token"]
        return data

    def create_address(self) -> str:
        data = self._call("get_email_address")
        self.address = (data.get("email_addr") or "").strip()
        if not self.address:
            raise RuntimeError("捨てメールアドレスを取得できませんでした")
        return self.address

    def check_email(self) -> list:
        return self._call("check_email", seq=0).get("list") or []

    def fetch_body(self, mail_id) -> str:
        return self._call("fetch_email", email_id=str(mail_id)).get("mail_body") or ""


def _wait_verification_email(gm: _GuerrillaMail) -> str | None:
    import time
    time.sleep(10)
    deadline = time.time() + _MAIL_WAIT_TIMEOUT
    while True:
        try:
            items = gm.check_email()
        except Exception:  # noqa: BLE001
            items = []
        for it in items:
            if not isinstance(it, dict) or (it.get("mail_subject") or "") != _L5_VERIFY_SUBJECT:
                continue
            mail_id = it.get("mail_id") or it.get("guid")
            if not mail_id:
                continue
            try:
                body = gm.fetch_body(mail_id)
            except Exception:  # noqa: BLE001
                continue
            if body:
                return body
        if time.time() >= deadline:
            return None
        time.sleep(_MAIL_POLL_INTERVAL)


def _create_level5_id(c_mod, player_name: str, log) -> dict:
    """捨てメールで LEVEL5 ID を作り、紐づくゲームセーブまで作る。
    戻り値は {"email","password","player_name","client","userId"}。
    LEVEL5 ID 登録に失敗したら RuntimeError。セーブ作成だけ失敗した場合は
    client/userId が None（ID は作成済みなので捨てない）。"""
    import re
    import time
    requests = c_mod.requests
    proxy = _proxy()
    session = requests.Session()
    if proxy:
        session.proxies = {"http": proxy, "https": proxy}
    with session:
        log("捨てメールアドレスを生成しています…")
        gm = _GuerrillaMail(session)
        email = gm.create_address()

        log("LEVEL5 ID の仮登録をしています…")
        r = session.get(_L5_REG_URL, headers=_BROWSER_HEADERS, timeout=30)
        token = _find_auth_token(r.text)
        if not token:
            raise RuntimeError("仮登録ページからトークンを取得できませんでした")
        r = session.post(_L5_REG_URL, headers=_BROWSER_HEADERS, timeout=30, data={
            "utf8": "✓", "authenticity_token": token, "form[game_id]": "",
            "form[client_id]": "", "form[country_id]": "287",
            "form[email]": email, "form[mail_received]": "true"})
        if r.status_code != 200:
            raise RuntimeError(f"仮登録に失敗しました（status={r.status_code}）")

        log("登録メールの到着を待っています…")
        body = _wait_verification_email(gm)
        if body is None:
            raise RuntimeError("登録メールが規定時間内に届きませんでした（捨てメール側の都合の可能性）")
        m = re.search(r"https://auth\.level5-id\.com/user_registration/verify/\w+", body)
        if not m:
            raise RuntimeError("メール本文から検証URLを見つけられませんでした")
        verify_url = m.group(0)

        r = session.get(verify_url, headers=_BROWSER_HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"検証ページの取得に失敗しました（status={r.status_code}）")
        verify_html = r.text
        token = _find_auth_token(verify_html)
        if not token:
            raise RuntimeError("検証ページからトークンを取得できませんでした")
        fm = re.search(r'<form[^>]*action="([^"]+)"', verify_html)
        if fm and fm.group(1):
            action = fm.group(1)
            register_url = action if action.startswith("http") else "https://auth.level5-id.com" + action
        else:
            register_url = verify_url.replace("verify", "register") + "?locale=ja"

        log("パスワードを登録しています…")
        password = _gen_password()
        headers = dict(_BROWSER_HEADERS, Referer=verify_url)
        r = session.post(register_url, headers=headers, timeout=30, data={
            "utf8": "✓", "authenticity_token": token,
            "form[password]": password, "form[password_confirmation]": password})
        if r.status_code != 200:
            raise RuntimeError(f"本登録に失敗しました（status={r.status_code}）")
        if "form[password]" in r.text:
            raise RuntimeError("本登録が拒否されました（フォームが再表示されました）")

    log("ゲームのセーブデータを作成しています…")
    try:
        client, uid = c_mod.create_save(player_name, proxy=proxy)
        c_mod.link_email(client.udkey, email, password, proxy=proxy)
        c_mod.save_udkey_for(email, client.udkey)
    except Exception as exc:  # noqa: BLE001  ID は作成済み。セーブ失敗だけ分けて返す
        log(f"セーブ作成に失敗しました（LEVEL5 ID は作成済み）：{type(exc).__name__}")
        return {"email": email, "password": password, "player_name": player_name,
                "client": None, "userId": None}
    return {"email": email, "password": password, "player_name": player_name,
            "client": client, "userId": uid}


def _login(c_mod, email: str, password: str, log):
    """メールアドレスでログインして、ログイン済みの Client を返す。失敗は RuntimeError。"""
    proxy = _proxy()
    client, accs = c_mod.login_email_prepared(email, password, proxy=proxy)
    uid = next((a.get("userId") for a in accs if a.get("userId")), None)
    client.login(uid)
    log("ゲームサーバーに接続しました。")
    return client


def _battle_once(c_mod, client, stage_id: int, log) -> None:
    """1ステージを1回クリアする。ひとだま不足は補充を1度試す。token ズレは再ログイン。
    打ち切るべき失敗は RuntimeError。"""
    import time
    last = ""
    for attempt in range(1, BATTLE_RETRY + 1):
        try:
            code, js = client.battle(stage_id)
        except Exception as exc:  # noqa: BLE001  通信エラー
            last = f"通信エラー（{type(exc).__name__}）"
            time.sleep(RETRY_WAIT)
            continue
        if code == 0:
            return
        if code in FATAL_CODES:
            raise RuntimeError(f"ステージ {stage_id} を開始できません（rc={code}）")
        text = ""
        if isinstance(js, dict):
            text = f"{js.get('dialogTitle') or ''} {js.get('dialogMsg') or ''}"
        if "ひとだま" in text:
            log("ひとだまが不足したので補充します…")
            try:
                client.buy_hitodama(500)
            except Exception:  # noqa: BLE001
                raise RuntimeError("ひとだま不足を補充できませんでした（Yマネー不足）")
            continue
        if code in TOKEN_CODES:
            log("セッションがずれたので再ログインします…")
            try:
                client.login(client.userId)
            except Exception:  # noqa: BLE001
                pass
            continue
        last = f"rc={code}"
        time.sleep(RETRY_WAIT)
    raise RuntimeError(last or f"ステージ {stage_id} をクリアできませんでした")


def _run_action(c_mod, client, menu: str, qty: dict, stages: dict,
                prog: _Progress, log) -> str:
    """ログイン済みの client で1メニューを実行する。戻り値は結果の1行。失敗は RuntimeError。"""
    import time
    label = menu_label(menu)
    if menu in ("puni_farm", "puni_stage_clear", "puni_unlimited"):
        stage_id = int(stages[menu])
        if menu == "puni_stage_clear":
            prog.update(5, f"ステージ {stage_id} をクリア中…", force=True)
            _battle_once(c_mod, client, stage_id, log)
            return f"■ {label}：完了（ステージ {stage_id}）"
        if menu == "puni_farm":
            laps = int(qty[menu])
            done = 0
            for i in range(laps):
                prog.update(5, f"周回中… {i + 1}/{laps}（ステージ {stage_id}）", force=True)
                _battle_once(c_mod, client, stage_id, log)
                done += 1
            return f"■ {label}：完了（ステージ {stage_id} を {done} 周）"
        # puni_unlimited（時間制）
        minutes = int(qty[menu])
        deadline = time.time() + minutes * 60
        done = 0
        while time.time() < deadline:
            left = int((deadline - time.time()) / 60) + 1
            prog.update(5, f"回し放題 実行中…（残り約 {left} 分 / {done} 周）", force=True)
            _battle_once(c_mod, client, stage_id, log)
            done += 1
        return f"■ {label}：完了（ステージ {stage_id} を {minutes} 分・{done} 周）"
    if menu == "puni_hitodama":
        target = int(qty[menu])
        prog.update(5, f"ひとだまを {target:,} 個まで補充中…", force=True)
        added = client.buy_hitodama(target)
        return f"■ {label}：完了（{added:,} 個 補充）"
    if menu == "puni_coin_gacha":
        prog.update(5, "コインガチャを回し中…", force=True)
        try:
            _gid, name, cnt, ids = client.pull_vip_gacha()
        except c_mod.VipUnavailable as exc:
            raise RuntimeError(str(exc))
        return f"■ {label}：完了（{name} を {cnt} 連・妖怪 {len(ids)} 体）"
    raise RuntimeError(f"未知のメニュー: {menu}")


def _create_accounts(c_mod, menu: str, count: int, prog: _Progress, log) -> tuple[list, str]:
    """アカウント作成（puni_account）／久垢作成（puni_aged）。
    戻り値は (作成できたアカウント情報のリスト, 最初に出たエラー文)。"""
    accounts: list = []
    aged = menu == "puni_aged"
    for i in range(count):
        prog.update(3, f"アカウントを作成中… {i + 1}/{count}", force=True)
        try:
            rec = _create_level5_id(c_mod, _random_player_name(), log)
        except Exception as exc:  # noqa: BLE001
            return accounts, f"{i + 1}件目のアカウント作成に失敗しました（{type(exc).__name__}）。"
        entry = {"email": rec["email"], "password": rec["password"],
                 "player_name": rec["player_name"]}
        if aged and rec.get("client") is not None:
            stage = _aged_stage(rec["client"])
            prog.update(5, f"初回ステージ {stage} をクリア中…（{i + 1}/{count}）", force=True)
            try:
                _battle_once(c_mod, rec["client"], stage, log)
                entry["cleared"] = True
            except Exception as exc:  # noqa: BLE001  ID は渡せるので止めない
                entry["cleared"] = False
                log(f"初回クリアに失敗しました（アカウントは作成済み）：{type(exc).__name__}")
        accounts.append(entry)
    return accounts, ""


def run_order(mode: str, login_id: str, password: str,
              menus, qty: dict, stages: dict | None = None,
              progress=None, log=None) -> dict:
    """ぷにぷに代行1件を実行する。例外は投げず、必ず結果を dict で返す。

    mode … "email"（メールアドレス）／"new"（新規アカウント作成）
    戻り値 … {"ok", "error", "lines", "done", "account", "moved"}
      account … mode == "new" で作成したアカウント情報のリスト
      moved   … ゲームアカウントがこちらに移っているか（今は常に False）
    """
    log = log or (lambda _t: None)
    prog = _Progress(progress)
    qty = qty or {}
    stages = stages or {}
    out = {"ok": False, "error": "", "lines": [], "done": 0,
           "account": None, "moved": False}
    menus = sort_menus(menus)
    if not menus:
        out["error"] = "実行するメニューがありません。"
        return out

    try:
        c_mod = core()
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"ぷにぷにの実行環境を準備できませんでした（{type(exc).__name__}）。"
        return out

    prog.update(0, "受付処理中です…", force=True)
    log(f"モード: {mode}（{MODE_LABELS.get(mode, mode)}）")

    # --- 新規アカウント作成系（puni_account / puni_aged） ---
    if mode == "new" or (menus and menus[0] in SOLO_MENUS):
        menu = menus[0]
        count = int(qty.get(menu, 1) or 1)
        accounts, error = _create_accounts(c_mod, menu, count, prog, log)
        out["account"] = accounts
        out["done"] = len(accounts)
        out["lines"] = [f"■ {menu_label(menu)}：{a['email']}"
                        + ("（初回クリア済み）" if a.get("cleared") else
                           "（初回クリア失敗）" if a.get("cleared") is False else "")
                        for a in accounts]
        if error:
            out["error"] = error
        out["ok"] = bool(accounts and not error)
        prog.update(10, "完了しました" if out["ok"] else "一部失敗しました", force=True)
        return out

    # --- ログインが要るメニュー（周回・ひとだま・ガチャなど） ---
    prog.update(1, "ログインしています…", force=True)
    log(f"ユーザー: {_mask_email(login_id)}")
    try:
        client = _login(c_mod, login_id, password, log)
    except Exception as exc:  # noqa: BLE001
        out["error"] = (f"ログインできませんでした（{type(exc).__name__}）。"
                        "メールアドレスとパスワードをご確認ください。")
        return out

    lines: list[str] = []
    for menu in menus:
        try:
            line = _run_action(c_mod, client, menu, qty, stages, prog, log)
        except Exception as exc:  # noqa: BLE001
            msg = str(exc)[:200] or f"「{menu_label(menu)}」の実行に失敗しました。"
            lines.append(f"■ {menu_label(menu)}：失敗（{msg}）")
            out["error"] = msg
            out["lines"] = lines
            out["done"] = len([x for x in lines if "：完了" in x])
            return out
        lines.append(line)
        out["done"] += 1

    out["lines"] = lines
    out["ok"] = True
    prog.update(10, "完了しました", force=True)
    return out
