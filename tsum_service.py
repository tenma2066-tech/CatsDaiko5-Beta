"""ツムツム代行（お店のもう1つの大メニュー）の実行部分。

にゃんこ代行が `engine.py`（bcsfe）で動くのと同じ位置にあるファイル。
ゲームとの実際のやり取りは `tsum_engine/` に置いた素のスクリプト群が行う。
ここはその土台の上に、お店から呼べる形（メニュー表・入力検査・1回の注文の実行）を載せる。

- `tsum_engine/` は依頼主の Discord 版から持ってきたもので、中身は変えていない。
- 実行時に書き込むファイル（バージョンのキャッシュ・引き継ぎ情報の控え・診断ログ）は
  `TSUM_DATA_DIR`（既定：`/data/tsum`、無ければ `instance/tsum`）に置く。
  リポジトリの中には書き込まない。
- アカウント情報（メールアドレス・パスワード・引き継ぎ番号）はログに出さない。
"""
import json
import os
import shutil
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE_SOURCE = os.path.join(_HERE, "tsum_engine")
_ENGINE_FILES = ("tsum_login.py", "line_password_login.py", "tsum.py",
                 "tsum_guest.py", "tsum_forge.py", "tsum_core.py", "appver.json")

_core = None
_core_lock = threading.Lock()


def data_dir() -> str:
    """実行時に書き込む場所。Fly/VPS では /data（永続ボリューム）の下。"""
    configured = os.environ.get("TSUM_DATA_DIR", "").strip()
    if configured:
        return os.path.abspath(configured)
    if os.path.isdir("/data"):
        return "/data/tsum"
    return os.path.join(_HERE, "instance", "tsum")


def engine_dir() -> str:
    return os.path.join(data_dir(), "engine")


def _prepare_engine() -> str:
    """素のスクリプトを書き込める場所へ写す（更新されていれば上書きする）。
    スクリプトは自分の隣に状態ファイルを作るため、リポジトリの中では動かさない。"""
    dest = engine_dir()
    os.makedirs(dest, mode=0o700, exist_ok=True)
    for name in _ENGINE_FILES:
        src = os.path.join(_ENGINE_SOURCE, name)
        dst = os.path.join(dest, name)
        if not os.path.isfile(src):
            raise RuntimeError(f"ツムツムの実行ファイルがありません: {name}")
        if not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst):
            if name == "appver.json" and os.path.exists(dst):
                continue          # 実行中に覚えたバージョンのほうが新しいので上書きしない
            shutil.copy2(src, dst)
    return dest


def core():
    """`tsum_engine` を読み込んで返す（最初の1回だけ準備する）。"""
    global _core
    if _core is not None:
        return _core
    with _core_lock:
        if _core is None:
            dest = _prepare_engine()
            if dest not in sys.path:
                sys.path.insert(0, dest)
            import tsum_core
            _core = tsum_core
    return _core


# ---------------------------------------------------------------- メニュー表
# value: 数量の入力が要るメニュー（min/max は安全上限。超える指定は受け付けない）
MENUS = {
    "tsum_nickname":    {"label": "ニックネーム変更", "price": "tsum_nickname", "text": (1, 16),
                         "desc": "プレイヤー名（ニックネーム）を変更します（最大16文字）", "order": 5},
    "tsum_coin":        {"label": "コイン指定", "price": "tsum_coin", "value": (0, 200_000_000),
                         "unit": "枚", "desc": "0〜2億枚まで指定できます", "order": 10},
    "tsum_coin_max":    {"label": "コインMAX", "price": "tsum_coin_max",
                         "desc": "コインを2億枚にします", "order": 11},
    "tsum_tsum_lv":     {"label": "ツムレベルMAX", "price": "tsum_tsum_lv",
                         "desc": "セット中のマイツムをレベル50にします", "order": 20},
    "tsum_tsum_lv_pick": {"label": "指定ツムをレベルMAX", "price": "tsum_tsum_lv_pick",
                          "value": (1, 99999), "unit": "ツムID",
                          "desc": "所持しているツムのIDを入れると、そのツムをレベル50にします（必要コインは自動補充）", "order": 21},
    "tsum_all_lv":      {"label": "全ツムをレベルMAX", "price": "tsum_all_lv",
                         "desc": "所持している全ツムをレベル50にします（時間がかかります・必要コインは自動補充）", "order": 22},
    "tsum_box":         {"label": "セレクトBOX完売", "price": "tsum_box",
                         "desc": "開催中のセレクトBOXを完売まで引きます", "order": 30},
    "tsum_premium":     {"label": "プレミアム完売", "price": "tsum_premium",
                         "desc": "プレミアムガチャを完売まで引きます", "order": 31},
    "tsum_score":       {"label": "スコア指定", "price": "tsum_score", "value": (0, 2_147_483_647),
                         "unit": "点", "desc": "好きなスコアを指定できます", "order": 40},
    "tsum_score_max":   {"label": "スコアMAX", "price": "tsum_score_max",
                         "desc": "スコアを上限にします", "order": 41},
    "tsum_medal":       {"label": "メダル指定", "price": "tsum_medal", "value": (1, 100_000_000),
                         "unit": "枚", "desc": "ストア用メダルを追加します（上限1億枚）", "order": 50},
    "tsum_medal_max":   {"label": "メダルMAX", "price": "tsum_medal_max",
                         "desc": "メダルを上限（1億枚）まで増やします", "order": 51},
    "tsum_level_max":   {"label": "プレイヤーレベルMAX", "price": "tsum_level_max",
                         "desc": "経験値を入れてプレイヤーレベルを上限にします", "order": 60},
    "tsum_guest_create": {"label": "ゲストアカウント生成", "price": "tsum_guest_create",
                          "desc": "新しいゲストアカウントを作り、引き継ぎ情報を発行します", "order": 70},
}

# プレミアムBOX+（メダルで引く）。メダルを使うので、同じ注文のメダル指定・メダルMAX（50・51）より後に実行する
MENUS["tsum_premium_plus"] = {"label": "プレミアムBOX+", "price": "tsum_premium_plus",
                              "desc": "開催中のプレミアムBOX+を、持っているメダルがなくなるまで引きます"
                                      "（1回1万メダル。メダル指定・メダルMAXと一緒に選ぶと、増やしてから引きます）",
                              "order": 55}

# 1つの注文で選べないもの（ゲスト生成は新規アカウントを作るメニューなので単独）
SOLO_MENUS = ("tsum_guest_create",)

# 既定のガチャID（開催中のIDはゲームから取り直す。取れなかったときだけこの値を使う）
DEFAULT_SELECT_BOX_ID = int(os.environ.get("TSUM_SELECT_BOX_ID", "12006012"))
DEFAULT_PREMIUM_GACHA_ID = int(os.environ.get("TSUM_PREMIUM_GACHA_ID", "12006007"))
# プレミアムBOX+ はゲームのガチャ一覧から名前（BOX+）か種類で探す。見つからないときだけこの ID を使う（0 = 使わない）
DEFAULT_PREMIUM_PLUS_GACHA_ID = int(os.environ.get("TSUM_PREMIUM_PLUS_GACHA_ID", "0"))

# ゲーム側が「セット中のマイツムを使う」と解釈する値
MYTSUM_AUTO = 0

MODES = ("line", "guest", "new")
MODE_LABELS = {"line": "メールアドレスでログイン", "guest": "引き継ぎ番号で引き継ぐ",
               "new": "新しいゲストアカウントを作る"}


def menu_label(key: str) -> str:
    menu = MENUS.get(key)
    return menu["label"] if menu else str(key)


def needs_value(key: str) -> bool:
    return "value" in MENUS.get(key, {})


def value_limits(key: str):
    return MENUS.get(key, {}).get("value")


def text_limits(key: str):
    """文字入力メニュー（ニックネームなど）の (最小文字数, 最大文字数)。無ければ None。"""
    return MENUS.get(key, {}).get("text")


def sort_menus(keys) -> list:
    """コイン→ツムレベル→ガチャ→スコア→メダル→プレミアムBOX+→レベルの順に並べる。
    ガチャはコインを使うので、コインより後に実行する。プレミアムBOX+はメダルを使うので、メダルより後。"""
    known = [k for k in keys if k in MENUS]
    return sorted(dict.fromkeys(known), key=lambda k: MENUS[k]["order"])


MAX_DIGITS = 12   # 数量として受け付ける桁数（これより長い数字は、変換せずに断る）


def validate_order(menus, qty: dict):
    """画面から来た内容を検査する。戻り値は (並べ替えたメニュー, 数量, エラー文)。"""
    picked = sort_menus(menus)
    if not picked:
        return [], {}, "ツムツム代行のメニューを選んでください。"
    solo = [k for k in picked if k in SOLO_MENUS]
    if solo and len(picked) > 1:
        return [], {}, f"「{menu_label(solo[0])}」はほかのメニューと同時に選べません。"
    clean: dict = {}
    for key in picked:
        tlim = text_limits(key)
        if tlim:
            raw = str((qty or {}).get(key, "")).strip()
            low, high = tlim
            if raw == "":
                return [], {}, f"「{menu_label(key)}」の文字を入力してください。"
            if any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in raw):
                return [], {}, f"「{menu_label(key)}」に使えない文字が含まれています。"
            if len(raw) < low or len(raw) > high:
                return [], {}, f"「{menu_label(key)}」は {low}〜{high} 文字で入力してください。"
            clean[key] = raw
            continue
        limits = value_limits(key)
        if not limits:
            continue
        raw = str((qty or {}).get(key, "")).strip()
        if raw == "":
            return [], {}, f"「{menu_label(key)}」の数量を入力してください。"
        if not (raw.isascii() and raw.isdigit()) or len(raw) > MAX_DIGITS:
            return [], {}, f"「{menu_label(key)}」の数量は0以上の整数（半角）で入力してください。"
        num = int(raw)
        low, high = limits
        if num < low or num > high:
            return [], {}, f"「{menu_label(key)}」は {low:,}〜{high:,} の範囲で入力してください。"
        clean[key] = num
    return picked, clean, ""


def check_account(mode: str, login_id: str, password: str):
    """アカウント入力の検査。戻り値はエラー文（問題なければ空文字）。"""
    if mode not in MODES:
        return "アカウントの入力方法を選んでください。"
    if mode == "new":
        return ""
    if not login_id or not password:
        if mode == "line":
            return "メールアドレスとパスワードを入力してください。"
        return "引き継ぎ番号とパスワードを入力してください。"
    if mode == "line" and "@" not in login_id:
        return ("メールアドレスを入力してください（電話番号ではログインできません）。"
                "LINEアプリの「設定 → アカウント」に登録されているメールアドレスです。")
    return ""


# ---------------------------------------------------------------- 実行
def _forge_kwargs(menu: str, qty: dict) -> dict:
    """1つのメニューを、ゲーム側へ渡す引数に変える。"""
    c = core()
    presets = {"tsum_score_max": "score_max", "tsum_level_max": "level_max",
               "tsum_coin_max": "coin_max", "tsum_medal_max": "medal_max"}
    if menu in presets:
        field, value = c.MAX_PRESETS[presets[menu]]
        return {field: value}
    if menu == "tsum_coin":
        return {"coin": int(qty[menu])}
    if menu == "tsum_score":
        return {"score": int(qty[menu])}
    if menu == "tsum_medal":
        return {"medal": int(qty[menu])}
    if menu == "tsum_tsum_lv":
        return {"tsum_lv": MYTSUM_AUTO}
    if menu == "tsum_tsum_lv_pick":
        return {"tsum_lv": int(qty[menu])}
    if menu == "tsum_all_lv":
        return {"all_tsum_lv": True}
    if menu == "tsum_nickname":
        return {"nickname": str(qty[menu])}
    if menu == "tsum_box":
        return {"box": DEFAULT_SELECT_BOX_ID}
    if menu == "tsum_premium":
        return {"gacha_full": DEFAULT_PREMIUM_GACHA_ID}
    if menu == "tsum_premium_plus":
        return {"gacha_plus": DEFAULT_PREMIUM_PLUS_GACHA_ID}
    raise ValueError(f"不明なメニュー: {menu}")


def auto_fill_coins() -> bool:
    """ガチャ・レベル上げの途中でコインが足りなくなったとき、自動で補充するか。
    既定は補充しない（コインは有料メニュー）。"""
    return bool(os.environ.get("TSUM_AUTO_FILL_COINS"))


class _Progress:
    """ゲーム側の進捗を、お店のジョブ表示に流すための入れ物。
    素のスクリプトは `progress.update(step, text, force=...)` を呼ぶ。"""

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


def _mask(value: str) -> str:
    """ログに出せる形（末尾だけ残す）。"""
    s = str(value or "")
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]


def _session_user(sess) -> str:
    return _mask(str((sess or {}).get("userid") or ""))


def _run_menus(c, sess, forge, menus, qty, progress, log):
    """ログイン済みのセッションで、選ばれたメニューを順番に実行する。
    戻り値は (実行できた件数, 行のリスト, 最初に出たエラー文)。"""
    lines: list = []
    done = 0
    for menu in menus:
        label = menu_label(menu)
        log(f"{label} を実行します…")
        progress.update(4, f"{label} を実行中…", force=True)
        result, text = c.forge_sync(
            sess=sess, f=forge, progress=progress, is_guest=True,
            auto_fill_coins=auto_fill_coins(), **_forge_kwargs(menu, qty),
        )
        body = str(text or "").replace("またのご利用をお待ちしております。", "").strip()
        if result is None:
            lines.append(f"■ {label}：失敗")
            if body:
                lines.append(body)
            return done, lines, body or f"「{label}」に失敗しました。"
        done += 1
        lines.append(f"■ {label}：完了")
        if body:
            lines.append(body)
    return done, lines, ""


def _is_ip_flag_failure(raw: str) -> bool:
    """この失敗が「このIPが弾かれている」サインか。
    画像認証の再要求・reCAPTCHA への切替は、送信元IPが怪しまれているときに出る。
    別IPに変えれば消えることが多い（＝回線を変えてやり直す価値がある）。

    次は IP のサインではないので除く：
    - 利用者が画像認証の文字を入力しなかった／中止した（"入力されませんでした"・"キャンセル"）
    - 画像そのものの取得に失敗した通信エラー（"画像の取得に失敗"。こちらは通信リトライ側で扱う）"""
    s = str(raw or "")
    low = s.lower()
    if "入力されませんでした" in s or "キャンセル" in s or "画像の取得に失敗" in s:
        return False
    return "recaptcha" in low or "captcha" in low or "画像認証" in s


def _login_failure_message(c, raw: str, can_rotate_ip: bool) -> str:
    """利用者に見せるログイン失敗の文言。
    画像認証・reCAPTCHA で詰まったとき、IP を変えられない構成（回線固定）では
    「VPN を変えて」「20〜30分待って」のような、利用者には実行できない・根拠のない
    案内を出さない。サーバー側のアクセス状況が原因であることだけを正直に伝える。"""
    if _is_ip_flag_failure(raw) and not can_rotate_ip:
        return ("LINEの画像認証が繰り返し出てログインを完了できませんでした"
                "（サーバー側のアクセス状況が原因の可能性があります）。"
                "お手数ですが時間をおいてもう一度お試しいただき、"
                "繰り返す場合はお問い合わせください。")
    return c.describe_login_failure(raw)


def _login_line(c, login_id, password, progress, log, captcha, pin_notice):
    """メールアドレスでログインして、ゲームのセッションを作る。
    戻り値は (sess, forge, エラー文)。"""
    proxy = c._unique_game_proxy()
    # 回線（送信元IP）を毎回変えられるのは SOCKS プロキシ（Tor など）を設定したときだけ。
    # 未設定ならサーバーの固定IPのままなので、やり直しても同じIPで同じ結果になる。
    can_rotate_ip = proxy is not None
    if not can_rotate_ip:
        c._dbg("[tsum login] 送信元IPの切り替えは無効（TSUM_SOCKS_URL 未設定）。"
               "固定IPで LINE ログインします。")
    login_error: dict = {}

    def _captcha_relay(img_bytes):
        progress.update(1, "画像認証（CAPTCHA）の入力待ち…", force=True)
        return (captcha(img_bytes) if captcha else "") or ""

    def _pin_relay(pincode):
        log(f"LINEアプリで本人確認の番号を入力してください：{pincode}")
        progress.update(1, f"LINEの本人確認（PIN {pincode}）の入力待ち…", force=True)

    def _note_relay(step, note):
        progress.update(1 if step == "line_login" else 2, note, force=True)

    for attempt in range(3):
        login_error = {}
        if attempt:
            progress.update(0, f"別の回線でログインし直します…（{attempt + 1}/3）", force=True)
            fresh = c._unique_game_proxy()
            if fresh:
                proxy = fresh
        try:
            sess = c.tsum_login.headless_session_direct(
                login_id, password, proxy=proxy, game_proxy=c._game_api_proxy(proxy),
                captcha_solver=_captcha_relay, save=False, verbose=False,
                error_box=login_error, pin_notice=_pin_relay, note_fn=_note_relay,
            )
        except (c.requests.exceptions.RequestException, ValueError):
            sess = None
            if attempt < 2:
                continue
            return None, None, "接続が不安定でログインできませんでした。少し待ってからもう一度お試しください。"
        if sess:
            break
        raw = str(login_error.get("error") or "")
        low = raw.lower()
        net_retryable = ("errorcode=446" in low or "errorcode=401" in low
                         or "中断されました" in raw or "通信エラー" in raw or "プロキシ接続エラー" in raw)
        # 画像認証の再要求・reCAPTCHA への切替は、別IPに変えられるときだけやり直す。
        # 同じ固定IPで叩き直しても結果は変わらず、認証の繰り返しでアカウントに負荷をかけるだけ。
        retryable = net_retryable or (_is_ip_flag_failure(raw) and can_rotate_ip)
        if retryable and attempt < 2:
            continue
        return None, None, _login_failure_message(c, raw, can_rotate_ip)
    if not sess:
        return None, None, _login_failure_message(c, str(login_error.get("error") or ""), can_rotate_ip)
    progress.update(2, "ゲームサーバーに接続しています…", force=True)
    forge = c.Forge(userid=sess["userid"], hashv=sess["hash"], checkval=sess["checkval"],
                    proxy=c._game_api_proxy(proxy), user_token=sess.get("userToken"))
    try:
        forge.get_public_key()
    except Exception:  # noqa: BLE001  通信の失敗。理由は出さずに言い換える
        return None, None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
    return sess, forge, ""


def _login_guest(c, migration_id, password, progress, log):
    """引き継ぎ番号でアカウントを受け取り、ゲームのセッションを作る。
    受け取った時点でアカウントはこちらに移るので、呼び出し側は必ず新しい引き継ぎ情報を発行する。
    戻り値は (sess, forge, エラー文)。"""
    proxies = c._proxies_dict(c._unique_game_proxy())
    progress.update(1, "引き継ぎ番号で認証しています…", force=True)
    try:
        sess, err = c.tsum_guest.guest_takeover(migration_id, password, proxies)
    except Exception:  # noqa: BLE001  番号・パスワード違いも通信失敗もここに来る
        return None, None, "引き継ぎ処理でエラーが発生しました。番号とパスワードを確かめて、もう一度お試しください。"
    if not sess:
        return None, None, (err or "引き継ぎに失敗しました（番号かパスワードが違う、または期限切れ）。")
    if sess.get("banned"):
        log("このアカウントは運営により利用停止（BAN）されています。")
        return sess, None, ("このアカウントは運営により利用停止（BAN）されています。"
                            f"理由: {sess.get('ban_msg') or '不明'}")
    if sess.get("login_failed"):
        return sess, None, "引き継ぎは完了しましたが、ゲームサーバーに接続できなかったため代行を実行できませんでした。"
    progress.update(2, "ゲームサーバーに接続しています…", force=True)
    try:
        forge = c.Forge(userid=sess["userid"], hashv=sess.get("hash", ""),
                        checkval=sess.get("checkval", ""), proxy=c._game_api_proxy(None),
                        user_token=sess.get("userToken"))
        forge.get_public_key()
    except Exception:  # noqa: BLE001
        return sess, None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
    return sess, forge, ""


def _create_guest(c, progress, log):
    """新しいゲストアカウントを作る。戻り値は (sess, forge, エラー文)。"""
    progress.update(1, "新しいゲストアカウントを作成しています…", force=True)

    def _on_step(note):
        log(str(note))

    try:
        sess, err = c.tsum_guest.guest_create(c._proxies_dict(c._unique_game_proxy()), on_step=_on_step)
    except Exception:  # noqa: BLE001
        return None, None, "ゲストアカウントの作成に失敗しました。少し待ってからもう一度お試しください。"
    if not isinstance(sess, dict) or not sess.get("userid"):
        return None, None, (f"ゲストアカウントの作成に失敗しました（{err}）。" if err
                            else "ゲストアカウントの作成に失敗しました（ゲーム側が応答しませんでした）。")
    progress.update(2, "ゲームサーバーに接続しています…", force=True)
    try:
        forge = c.Forge(userid=sess["userid"], hashv=sess.get("hash", ""),
                        checkval=sess.get("checkval", ""), proxy=c._game_api_proxy(None),
                        user_token=sess.get("userToken"))
        forge.get_public_key()
    except Exception:  # noqa: BLE001
        return sess, None, "作成はできましたが、ゲームサーバーに接続できませんでした。"
    return sess, forge, ""


def _relogin(c, sess, forge=None) -> bool:
    """引き継ぎ発行の前に、ゲームに入り直して sess の checkval / hash を新しくする。
    代行の途中で「タイトルに戻ります」と言われると、古い値のままでは発行が通らない。"""
    if not (sess or {}).get("userToken"):
        return False
    try:
        f = forge or c.Forge(userid=sess["userid"], hashv=sess.get("hash", ""), checkval=sess.get("checkval", ""),
                             proxy=c._game_api_proxy(None), user_token=sess.get("userToken"))
        if forge is None:
            f.get_public_key()
        if not f.refresh_session():
            return False
        sess["checkval"] = f.checkval
        sess["hash"] = f.hash
        if getattr(f, "userid", None):
            sess["userid"] = str(f.userid)
        return True
    except Exception:  # noqa: BLE001
        return False


def _issue_transfer(c, sess, old_id, progress, log, forge=None):
    """新しい引き継ぎ番号・パスワードを発行して控えに残す。戻り値は (番号, パスワード)。"""
    progress.update(5, "新しい引き継ぎ情報を発行しています…", force=True)
    proxies = c._proxies_dict(None)
    for attempt in range(3):
        if attempt == 1:
            log("引き継ぎ情報の発行に失敗したため、ゲームに入り直してもう一度試します…")
            _relogin(c, sess, forge)
        try:
            new_id, new_pw = c.tsum_guest.guest_issue_transfer(sess, proxies)
        except Exception:  # noqa: BLE001
            new_id, new_pw = None, None
        if new_id:
            c._save_guest_transfer(sess, old_id, new_id, new_pw)
            try:
                c.tsum_guest.mark_pending_done(sess.get("uuid"), new_id)
            except Exception:  # noqa: BLE001  控えは残っているので致命ではない
                pass
            log("新しい引き継ぎ情報を発行しました。")
            return new_id, new_pw
    c._save_orphan_guest(sess, old_id)
    log("新しい引き継ぎ情報の発行に失敗しました（セッションは控えに保存しました）。")
    return None, None


# ---------------------------------------------------------------- 発行できなかったアカウントの控え
def orphans() -> list:
    """引き継ぎ情報を発行できなかったアカウントの控え（新しい順）。管理画面に出す分だけ返す。"""
    path = os.path.join(engine_dir(), "tsum_guest_orphans.json")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh) or []
    except (OSError, ValueError):
        return []
    rows = []
    for i, d in enumerate(data):
        if isinstance(d, dict) and not d.get("resolved"):
            rows.append({"idx": i, "ts": d.get("ts", ""), "userid": _mask(str(d.get("userid") or "")),
                         "orig": _mask(str(d.get("orig_migration_id") or "")), "has_token": bool(d.get("userToken"))})
    return list(reversed(rows))


def reissue_orphan(idx: int):
    """控えのアカウントにゲームで入り直し、新しい引き継ぎ番号を発行する。戻り値は (番号, パスワード, エラー文)。"""
    c = core()
    path = os.path.join(engine_dir(), "tsum_guest_orphans.json")
    try:
        with open(path, encoding="utf-8-sig") as fh:
            data = json.load(fh) or []
        d = data[int(idx)]
    except (OSError, ValueError, IndexError, TypeError):
        return None, None, "控えが見つかりません。"
    if d.get("resolved"):
        return None, None, "この控えはもう発行済みです。"
    sess = {k: d.get(k) for k in ("uuid", "userToken", "userid", "hash", "checkval", "migrated_userKey")}
    if not _relogin(c, sess):
        return None, None, "ゲームに入り直せませんでした（ログイン情報が古くなっている可能性があります）。"
    try:
        new_id, info = c.tsum_guest.guest_issue_transfer(sess, c._proxies_dict(None))
    except Exception as exc:  # noqa: BLE001
        return None, None, f"発行に失敗しました（{type(exc).__name__}）。"
    if not new_id:
        return None, None, f"発行に失敗しました（{info}）。"
    c._save_guest_transfer(sess, d.get("orig_migration_id"), new_id, info)
    d["resolved"] = time.strftime("%Y-%m-%d %H:%M:%S")
    d["new_id"] = new_id
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    return new_id, info, ""


def run_order(mode: str, login_id: str, password: str, menus, qty: dict,
              progress=None, log=None, captcha=None) -> dict:
    """ツムツム代行1件を実行する。例外は投げず、必ず結果を dict で返す。

    mode … "line"（メールアドレス）／"guest"（引き継ぎ番号）／"new"（新規ゲスト作成）
    戻り値 … {"ok", "error", "lines", "done", "transfer", "confirm", "moved"}
      transfer/confirm は新しい引き継ぎ番号・パスワード（mode が guest/new のとき）。
    """
    log = log or (lambda _t: None)
    prog = _Progress(progress)
    out = {"ok": False, "error": "", "lines": [], "done": 0,
           "transfer": None, "confirm": None, "moved": False}
    c = core()
    c.DEBUG_HOOK = log
    menus = sort_menus(menus)
    create_only = menus == ["tsum_guest_create"]
    if mode == "new" or create_only:
        mode = "new"

    prog.update(0, "回線を準備しています…", force=True)
    if mode == "line":
        sess, forge, err = _login_line(c, login_id, password, prog, log, captcha, None)
    elif mode == "guest":
        sess, forge, err = _login_guest(c, login_id, password, prog, log)
    else:
        sess, forge, err = _create_guest(c, prog, log)
    out["moved"] = mode in ("guest", "new") and bool(sess)
    if sess:
        log(f"ゲームのユーザーID {_session_user(sess)} で接続しました。")

    if forge is None:
        out["error"] = err or "ログインできませんでした。"
        if out["moved"]:
            # 引き継ぎでアカウントがこちらに移っているので、必ず返せるようにする
            out["transfer"], out["confirm"] = _issue_transfer(c, sess, login_id, prog, log)
        return out

    action_menus = [m for m in menus if m != "tsum_guest_create"]
    if action_menus:
        prog.update(3, "ゲームデータを取得しています…", force=True)
        try:
            done, lines, failure = _run_menus(c, sess, forge, action_menus, qty, prog, log)
        except Exception as exc:  # noqa: BLE001  ここで落ちても、下の「引き継ぎ情報の発行」まで必ず進める
            # 引き継ぎ・新規ではアカウントがこちらに移っている。例外で抜けるとお客さんに返せなくなる。
            done, lines, failure = 0, [], f"代行の実行中にエラーが発生しました（{type(exc).__name__}）。"
            log("エラー: " + failure)
        out["done"] = done
        out["lines"] = lines
        if failure:
            out["error"] = failure
    else:
        out["lines"] = ["■ ゲストアカウント生成：完了"]
        out["done"] = 1

    if out["moved"]:
        out["transfer"], out["confirm"] = _issue_transfer(c, sess, login_id if mode == "guest" else "", prog, log, forge)
        if not out["transfer"] and not out["error"]:
            out["error"] = ("代行は完了しましたが、新しい引き継ぎ情報を発行できませんでした。"
                            "注文番号を管理者にお伝えください。")
    out["ok"] = bool(not out["error"] and (out["done"] or out["transfer"]))
    prog.update(6, "完了しました", force=True)
    return out
