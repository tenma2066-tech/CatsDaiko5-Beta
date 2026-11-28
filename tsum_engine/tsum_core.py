"""ツムツム代行の実行ロジック（Discord 版 tsum_bot.py から取り出したもの）。

このファイルの下半分（`_failure_hint_from_response` 以降）は、依頼主が持っている
Discord 版 `tsum_bot.py` の該当部分を**1文字も変えずに**抜き出したもの。
行の範囲は EXTRACTED_FROM を見ること。Discord に依存する部分（チケット・ボタン・
進捗の埋め込み）は抜き出していない。ここに手を入れるときは、元の
`tsum_bot.py` の同じ関数も合わせて直すこと。

上半分（この prelude）は、Discord 版が持っていた土台（設定・ログ・プロキシ・
ファイル保存）を、Web 版（app.py のワーカープロセス）用に置き換えたもの。
"""
import json
import os
import time
import traceback
import unicodedata

import requests  # noqa: F401  抜き出した部分が requests.exceptions を使う

import tsum_guest
import tsum_login
from tsum_forge import Forge, is_pair_tsum, owned_tsum_ids  # noqa: F401  抜き出した部分が使う

EXTRACTED_FROM = (
    "tsum_bot.py 1874-2666 (helpers + forge_sync), 2774-2821 (_proxies_dict/_save_*), "
    "2863-2961 (forge_sync_guest), 3157-3161 (MAX_PRESETS)"
)

# 実行時に書き込む場所（引き継ぎ情報の控え・診断ログ・バージョンキャッシュ）。
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

_CONFIG_FILE = os.path.join(PROJECT_ROOT, "tsum_config.json")


def _jload(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _jdump_atomic(path, obj, **kw):
    kw.setdefault("ensure_ascii", False)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, **kw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _load_config() -> dict:
    """ゲーム側の設定（ガチャIDの既定値・1プレイで送る量・Tor を使うか）。
    tsum_config.json があれば読む。無ければ既定値。"""
    cfg = _jload(_CONFIG_FILE, {})
    if not isinstance(cfg, dict):
        cfg = {}
    socks = str(os.environ.get("TSUM_SOCKS_URL", "") or "").strip()
    if socks:
        cfg["tor_socks_url"] = socks
        cfg.setdefault("tor_enabled", True)
    if os.environ.get("TSUM_TOR_GAME_API"):
        cfg["tor_game_api"] = os.environ["TSUM_TOR_GAME_API"] != "0"
    return cfg


CONFIG = _load_config()

# メダルの上限（メダル指定・メダルMAX）。1回のゲームで送れる量はサーバーが受け付けた量に合わせて増減する。
MEDAL_CAP = 100_000_000
BOT_DATA: dict = {}          # Discord 版のサーバーごとの設定。Web 版では使わない

# 進捗・デバッグの行をどこへ出すか。app.py 側が差し替える（既定は捨てる）。
DEBUG_HOOK = None


class LoginHookAbort(Exception):
    """ログイン直後に処理を止めたいときに on_login から投げる（無料枠の使用済みなど）。"""

    def __init__(self, user_msg: str):
        super().__init__(user_msg)
        self.user_msg = user_msg


def _dbg(msg):
    if DEBUG_HOOK is not None:
        try:
            DEBUG_HOOK(str(msg))
        except Exception:  # noqa: BLE001  ログで本処理を止めない
            pass


def _default_game_proxy():
    """Tor（SOCKS）を使う設定なら、その URL。使わないなら None（直接接続）。"""
    if not CONFIG.get("tor_enabled"):
        return None
    url = str(CONFIG.get("tor_socks_url") or "").strip()
    return url or None


def _game_api_proxy(line_proxy):
    """ゲーム本体の通信を Tor に通すかどうか。既定は通さない（1件で数百〜数千回叩くため）。"""
    if CONFIG.get("tor_game_api", False):
        return line_proxy
    return None


def _unique_game_proxy():
    """ログインをやり直すときに、別の回線（別IP）を割り当てる。Tor 無効なら None。"""
    base = _default_game_proxy()
    if not base:
        return None
    try:
        return tsum_login.fresh_socks_identity(base)
    except Exception:  # noqa: BLE001
        return base


# ===== ここから下は tsum_bot.py からの抜き出し（EXTRACTED_FROM を参照） =====
def _failure_hint_from_response(resp):
    if not isinstance(resp, dict):
        return "原因を特定できませんでした"
    retcode = resp.get("retcode")
    retsubcode = resp.get("retsubcode")
    msg = " ".join(str(resp.get(k, "")) for k in ("retmsg", "message", "errmsg", "error"))
    low = msg.lower()
    if retcode == 8 and retsubcode == 2:
        return "ゲーム開始時の入力パラメータが不正です"
    if "heart" in low or "ハート" in msg:
        return "ハート不足、またはプレイ開始条件が足りていません"
    if any(k in low for k in ("session", "token", "checkval", "hash", "auth", "login")):
        return "ログイン状態が途中で無効になりました"
    if any(k in low for k in ("version", "appver", "resver", "update")) or "更新" in msg:
        return "アプリのバージョン情報が合っていません"
    if any(k in low for k in ("maintenance", "maint")) or "メンテ" in msg:
        return "ゲーム側がメンテナンス中の可能性があります"
    if any(k in low for k in ("ban", "block", "restrict", "limit", "forbidden")) or any(k in msg for k in ("制限", "拒否", "禁止")):
        return "通信またはアカウントが制限されています"
    if "tsum" in low or "ツム" in msg:
        return "ツム設定が合っていない可能性があります"
    if retcode not in (None, 0, "0"):
        return "ゲーム側が処理を拒否しました"
    return "必要なデータが返ってきませんでした"

def describe_tsum_api_failure(stage, resp=None, forge=None, missing_data=False):
    status = getattr(forge, "last_http_status", None)
    error = getattr(forge, "last_error", None)

    if error == "proxy_error":
        return f"{stage}に失敗しました（VPN/プロキシ接続エラー）。VPN設定と接続先を確認して、もう一度お試しください。"
    if error == "timeout":
        return f"{stage}に失敗しました（通信タイムアウト）。VPNまたは回線が不安定な可能性があります。"
    if error == "network_error":
        return f"{stage}に失敗しました（通信エラー）。VPN、回線、接続先IPを確認してください。"
    if status in (401, 403):
        return f"{stage}に失敗しました（通信が拒否されました）。VPNのIP、アカウント制限、ログイン状態のどれかが原因の可能性があります。"
    if status == 404:
        return f"{stage}に失敗しました（接続先が見つかりません）。アプリのバージョン情報が合っていない可能性があります。"
    if status and status >= 500:
        return f"{stage}に失敗しました（ゲーム側の応答エラー）。少し待ってからもう一度お試しください。"
    if error == "decrypt_failed":
        return f"{stage}に失敗しました（ゲーム側の応答を読み取れませんでした）。VPN/IP制限、ログイン状態、アプリのバージョン情報を確認してください。"
    if isinstance(resp, str):
        return f"{stage}に失敗しました（想定外の応答）。VPN/IP制限、ログイン状態、アプリのバージョン情報を確認してください。"

    hint = _failure_hint_from_response(resp)
    if missing_data and hint == "必要なデータが返ってきませんでした":
        return f"{stage}に失敗しました（開始用データが返ってきませんでした）。ログイン状態、VPN/IP、アカウント状態を確認してください。"
    return f"{stage}に失敗しました（{hint}）。もう一度お試しください。"

def describe_login_failure(reason):
    reason = str(reason or "").strip()
    low = reason.lower()
    if "PIN" in reason.upper() or "pincode" in low or "端末認証" in reason:
        return "LINEの本人確認（PIN）が完了しませんでした。LINEアプリで番号を入力してから、もう一度お試しください"
    # LINE 認証は通ったが、ゲーム（ツムツム）側のログインで失敗したケース。
    # LINE には「ログインしました」が届いているので、資格情報エラーと誤表示しない。実際のコードを見せる。
    if "ゲーム（ツムツム）側のログイン" in reason or "ツムツムログインに失敗" in reason:
        return reason if "コード" in reason else "ゲーム（ツムツム）側のログインに失敗しました。時間をおいてもう一度お試しください。繰り返す場合はお問い合わせください。"
    # 画像認証（CAPTCHA）まわりは、実際に確認できた失敗だけを伝える。
    # 「LINE側の一時制限」「20〜30分」などの推測は根拠がないので書かない。
    if "recaptcha" in low:
        return ("LINEが画像ではないreCAPTCHA（Google）を要求したため、自動ではログインできませんでした。"
                "通信経路（VPN/IP）を変えるか、時間をおいてからお試しください。")
    if "画像の取得に失敗" in reason:
        return "画像認証の画像を取得できませんでした（通信の問題）。もう一度お試しください。"
    if "入力されませんでした" in reason or "キャンセル" in reason:
        return "画像認証の文字が入力されなかったため、ログインを中止しました。もう一度お試しください。"
    if "captcha" in low or "画像認証" in reason:
        return ("入力した画像認証の文字が合っていなかったか、LINEがもう一度画像認証を求めたため、"
                "ログインできませんでした。もう一度お試しください。")
    if "通信エラー" in reason or "接続が不安定" in reason or "プロキシ" in reason:
        return "接続が不安定でログインできませんでした。少し待ってからもう一度お試しください"
    if "アカウント情報が違う" in reason or "errorcode=445" in low:
        return "メールアドレスかパスワードが違います（※電話番号ではログインできません。LINEアプリの「設定」→「アカウント」に登録されているメールアドレスを入力してください）"
    if "停止" in reason or "BAN" in reason.upper() or "不正利用" in reason or "retcode=29" in low:
        return "アカウントが運営により利用停止（BAN）されています。代行を実行できません。"
    if ("中断されました" in reason or "追加認証" in reason or "一時制限" in reason
            or "連続試行" in reason or "errorcode=446" in low or "errorcode=401" in low):
        return "LINE側で一時的にログイン制限中です。時間をおいて再度お試しください"
    return "メールアドレスかパスワードが違います（※電話番号ではログインできません。LINEアプリの「設定」→「アカウント」に登録されているメールアドレスを入力してください）"

def _to_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def _is_owned_tsum(row):
    if not isinstance(row, dict):
        return False
    if row.get("setflg") in (1, "1", True):
        return True
    for key in ("getflg", "ownflg", "haveflg", "possessionflg"):
        if row.get(key) in (1, "1", True):
            return True
    return _to_int(row.get("lv") or row.get("level")) > 0

def select_play_tsumids(forge, requested_tsumid=None):
    requested = _to_int(requested_tsumid)
    info = forge.get_info()
    if not isinstance(info, dict):
        return None, None, describe_tsum_api_failure("ツム情報取得", info, forge)

    tsum_rows = [t for t in (info.get("tsuminfo") or []) if isinstance(t, dict)]
    owned = [t for t in tsum_rows if _is_owned_tsum(t) and _to_int(t.get("tsumid"))]
    candidates = []

    def add_candidate(tsumid):
        tsumid = _to_int(tsumid)
        if tsumid and tsumid not in candidates:
            candidates.append(tsumid)

    if requested:
        for t in owned:
            if _to_int(t.get("tsumid")) == requested:
                add_candidate(requested)

    # 現在のマイツム(setflg)を最優先候補として追加
    for t in owned:
        tid_val = _to_int(t.get("tsumid"))
        if t.get("setflg") in (1, "1", True):
            add_candidate(tid_val)
    if owned:
        for t in owned:
            tid_val = _to_int(t.get("tsumid"))
            add_candidate(tid_val)
    if candidates:
        return candidates[:20], info, None
    return None, info, "使用できるツムが見つかりませんでした。ゲームを開いてマイツムをセットしてから、もう一度お試しください。"

def heart_count_from_info(info):
    ui = (info.get("userinfo") or {}) if isinstance(info, dict) else {}
    return _to_int(ui.get("bheart")) + _to_int(ui.get("pheart"))

def heart_state_from_info(info):
    ui = (info.get("userinfo") or {}) if isinstance(info, dict) else {}
    bheart = _to_int(ui.get("bheart"))
    pheart = _to_int(ui.get("pheart"))
    return bheart, pheart, bheart + pheart

def heart_info_is_reliable(info):
    """getInfo が本当に成功して userinfo を返したときだけ True。
    retcode 201 などのエラー応答も dict なので、そのまま bheart/pheart を読むと
    「ハート0＝ハート不足」と誤判定してしまう。それを防ぐためのガード。"""
    if not isinstance(info, dict):
        return False
    rc = info.get("retcode")
    if rc is None or _to_int(rc) != 0:
        return False
    ui = info.get("userinfo")
    return isinstance(ui, dict) and ("bheart" in ui or "pheart" in ui)

def hearttype_candidates_from_info(info):
    bheart, pheart, total = heart_state_from_info(info)
    if total < 1:
        return []
    candidates = []
    if bheart > 0:
        candidates.append(0)
    if pheart > 0:
        candidates.append(1)
    for value in (0, 1):
        if value not in candidates:
            candidates.append(value)
    return candidates

_EVENT_CACHE = {}

def _cached_event_candidates(f, ttl=900):
    now = time.time()
    shard = str(getattr(f, "userid", "") or "")[:3]
    entry = _EVENT_CACHE.get(shard)
    if entry and (now - entry["ts"] < ttl):
        return entry["cands"]
    cands = f.event_candidates()
    if cands:
        _EVENT_CACHE[shard] = {"cands": cands, "ts": now}
    return cands or ["0", "9999"]


_GACHA_CACHE = {"ts": 0.0, "ids": None}

def _cached_gacha_ids(f, ttl=600):
    now = time.time()
    if _GACHA_CACHE["ids"] and (now - _GACHA_CACHE["ts"] < ttl):
        return _GACHA_CACHE["ids"]
    m = f.get_mast()
    if not isinstance(m, dict) or m.get("retcode") not in (0, None):
        return None
    rows = m.get("gachamst")
    if not isinstance(rows, list) or not rows:
        return None
    by_type = {}
    plus_by_name = None
    plus_row = None
    for g in rows:
        if not isinstance(g, dict):
            continue
        try:
            t = int(g.get("type"))
            gid = int(g.get("gachaid"))
        except Exception:
            continue
        by_type.setdefault(t, gid)
        # プレミアムBOX+ は名前で見分ける（「BOX+」「BOX＋」「BOXプラス」のどれか。全角・小文字も同じとみなす）
        if plus_by_name is None:
            for v in g.values():
                if isinstance(v, str) and _is_box_plus_name(v):
                    plus_by_name = gid
                    plus_row = g
                    break
    box_id = by_type.get(41)
    if not box_id and CONFIG.get("box_fallback_premium_box"):
        box_id = by_type.get(64)
    plus_type = int(CONFIG.get("premium_box_plus_type", 64))
    ids = {"premium": by_type.get(11), "box": box_id, "has_select_box": bool(by_type.get(41)),
           "premium_plus": plus_by_name or by_type.get(plus_type),
           "premium_plus_by": "name" if plus_by_name else ("type" if by_type.get(plus_type) else None),
           "premium_plus_row": plus_row or next((g for g in rows if isinstance(g, dict)
                                                 and str(g.get("type")) == str(plus_type)), None)}
    if ids["premium"] or ids["box"] or ids["premium_plus"]:
        _GACHA_CACHE["ids"] = ids
        _GACHA_CACHE["ts"] = now
    return ids


def premium_plus_paytype():
    """プレミアムBOX+ の支払い方法の番号。実機で 1 がメダル払いと確かめた（2026-11-28：メダル不足のとき
    paytype=1 で「メダルの使用に失敗しました」が返る。2 以降は入力エラー）。
    仕様が変わったときだけ TSUM_PREMIUM_PLUS_PAYTYPE / 設定 premium_box_plus_paytype で上書きする。"""
    raw = os.environ.get("TSUM_PREMIUM_PLUS_PAYTYPE") or CONFIG.get("premium_box_plus_paytype")
    try:
        return int(raw) if raw not in (None, "") else 1
    except (TypeError, ValueError):
        return 1


def premium_plus_price(f, gachaid):
    """プレミアムBOX+ 1回の値段（メダル）。ゲームのガチャ一覧の currprice。取れなければ None。"""
    try:
        row = (_cached_gacha_ids(f) or {}).get("premium_plus_row") or {}
        if int(row.get("gachaid") or 0) != int(gachaid):
            return None
        price = int(row.get("currprice") or row.get("listprice") or 0)
        return price if price > 0 else None
    except Exception:
        return None


def _is_box_plus_name(text):
    t = unicodedata.normalize("NFKC", str(text)).upper().replace(" ", "")
    return "BOX+" in t or "BOXプラス" in t


def resolve_gacha_id(f, kind, fallback):
    try:
        ids = _cached_gacha_ids(f)
        if ids is not None:
            gid = ids.get(kind)
            if gid:
                if int(gid) != int(fallback):
                    _dbg(f"[gacha] {kind}: config={fallback} → 現行={gid} に自動切替")
                return int(gid), "auto"
            _dbg(f"[gacha] {kind}: 現在開催されていません(config={fallback}は古い可能性)")
            return None, "not_running"
    except Exception as e:
        print(f"[gacha] 自動取得に失敗→configの値を使用: {type(e).__name__}", flush=True)
    return int(fallback), "fallback"


def is_coin_free(guild_id=None):
    """サーバーまたは全体設定でコイン代行が無料(0円)かどうか判定する"""
    if guild_id is not None:
        try:
            gid = str(int(guild_id))
            gs = (BOT_DATA.get("guild_settings") or {}).get(gid) or (CONFIG.get("guild_settings") or {}).get(gid)
            if isinstance(gs, dict):
                mp = gs.get("menu_prices")
                if isinstance(mp, dict):
                    c_price = mp.get("coin_max", mp.get("coin"))
                    if c_price is not None:
                        return int(c_price) == 0
        except Exception:
            pass
    cfg_prices = BOT_DATA.get("menu_prices") or CONFIG.get("menu_prices") or {}
    c_price = cfg_prices.get("coin_max", cfg_prices.get("coin", 0))
    return int(c_price) == 0


def forge_sync(score=None, coin=None, exp=None, medal=None, tsumid=860, tsum_lv=None, box=None,
               login_id=None, password=None, captcha_solver=None, proxy=None, sess=None, f=None,
               pin_notice=None, gacha_full=None, on_login=None, stage_box=None, progress=None,
               is_guest=False, auto_fill_coins=None, all_tsum_lv=False, nickname=None, gacha_plus=None):
    if auto_fill_coins is None:
        auto_fill_coins = is_coin_free()
    # stage_box: 失敗がログイン段階か実行段階かを呼び出し元に伝えるための箱
    if stage_box is None:
        stage_box = {}
    stage_box["stage"] = "login"
    act_stage = 3 if is_guest else 4
    if progress and not is_guest:
        progress.update(0, "回線を準備しています...", force=True)
    if sess is None or f is None:
        if not login_id or not password:
            return None, "アカウント情報を入力してください。"
        sess = None
        login_error = {}
        pin_state = {"shown": False}
        def _pin_relay(pincode):
            pin_state["shown"] = True
            if progress:
                progress.update(1, "PINコードの確認待ち...", force=True)
            if pin_notice:
                try:
                    pin_notice(pincode)
                except Exception:
                    pass
        captcha_state = {"shown": False}
        def _captcha_relay(img_bytes):
            captcha_state["shown"] = True
            if progress:
                progress.update(1, "CAPTCHA認証中...", force=True)
            return captcha_solver(img_bytes) if captcha_solver else ""
        _AUTH_STEP_STAGE = {"line_login": 1, "trident": 2, "game_login": 2}
        def _note_relay(step, note):
            if progress:
                progress.update(_AUTH_STEP_STAGE.get(step, 0), note, force=True)
        for _try in range(3):
            login_error = {}
            if _try > 0:
                # リトライは新しいTor回線(=別IP)で試す。同じ回線のまま3回叩くと、
                # たまたま繋がりの悪い出口ノードに当たっただけで3回とも同じ理由で
                # 失敗してしまうため。
                if progress:
                    progress.update(0, f"別のTor回線に切り替えています... ({_try+1}/3)", force=True)
                fresh = _unique_game_proxy()
                if fresh:
                    proxy = fresh
                    _dbg(f"[login] リトライ{_try+1}回目: 別のTor回線(未使用IP)に切替")
            try:
                sess = tsum_login.headless_session_direct(
                    login_id, password, proxy=proxy, game_proxy=_game_api_proxy(proxy),
                    captcha_solver=_captcha_relay, save=False, verbose=False, error_box=login_error,
                    pin_notice=_pin_relay, note_fn=_note_relay,
                )
            except (requests.exceptions.RequestException, ValueError) as e:
                sess = None
                _dbg(f"[login例外] id={str(login_id)[:3]}*** try={_try+1} {type(e).__name__}: {str(e)[:150]}")
                if _try < 2 and not pin_state["shown"] and not captcha_state["shown"]:
                    time.sleep(0.6)
                    continue
                return None, "接続が不安定でログインできませんでした。少し待ってからもう一度お試しください。"
            if sess:
                if on_login:
                    try:
                        on_login(str(sess.get("userid") or ""))
                    except LoginHookAbort as _abort:
                        # 例: このゲームアカウントは無料枠を使用済み → ここで確実に止める
                        _dbg(f"[on_login] 中止: {_abort.user_msg}")
                        return None, _abort.user_msg
                    except Exception as _e:
                        _dbg(f"[on_login] 想定外の例外(続行): {type(_e).__name__}: {str(_e)[:150]}")
                break
            _raw = login_error.get("error") or ""
            _dbg(f"[login失敗] id={str(login_id)[:3]}*** try={_try+1} raw={_raw!r}")
            low = _raw.lower()
            ip_blocked = ("errorcode=446" in low or "errorcode=401" in low or "中断されました" in _raw
                          or "プロキシ接続エラー" in _raw or "通信エラー" in _raw
                          or "captcha" in low or "画像認証" in _raw or "recaptcha" in low)
            # CAPTCHAの明示的キャンセル・タイムアウト・未入力・画像認証エラーの場合は無限ループを防ぐためリトライせず終了
            if captcha_state["shown"] or any(k in _raw for k in ("キャンセル", "タイムアウト", "入力されませんでした", "画像認証", "CAPTCHA")):
                return None, describe_login_failure(_raw)
            if ip_blocked and _try < 2 and not pin_state["shown"] and not captcha_state["shown"]:
                continue
            return None, describe_login_failure(_raw)
        if not sess:
            return None, describe_login_failure(login_error.get("error"))
        f = Forge(userid=sess["userid"], hashv=sess["hash"], checkval=sess["checkval"],
                  proxy=_game_api_proxy(proxy), user_token=sess.get("userToken"))
        if progress:
            progress.update(2, "ゲームサーバーの公開鍵を取得しています...", force=True)
        try:
            f.get_public_key()
        except Exception as e:
            _dbg(f"[getPublicKey例外] {type(e).__name__}: {str(e)[:150]}")
            return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
    stage_box["stage"] = "action"
    if progress and not is_guest:
        progress.update(3, "ゲームデータを取得しています", force=True)

    if nickname is not None:
        name = str(nickname).strip()
        if not name:
            return None, "ニックネームが空です。名前を入力してからお試しください。"
        if progress:
            progress.update(act_stage, "ニックネームを変更しています...", force=True)
        res = f.set_profile(nickname=name)
        if isinstance(res, dict) and res.get("retcode") == 0:
            return res, (f"ニックネームを「{name}」に変更しました\n完了しました。\n"
                         "またのご利用をお待ちしております。")
        return None, describe_tsum_api_failure("ニックネーム変更", res if isinstance(res, dict) else None, f)

    if coin is not None:
        try:
            cur_c = f.resources()["bcoin"]
            safe_max = 200_000_000
            coin_req = int(coin)
            if cur_c + coin_req > safe_max:
                coin = max(1, safe_max - cur_c)
                _dbg(f"[coin_cap] BAN防止上限調整: 現在={cur_c:,} 要求={coin_req:,} -> 付与={coin:,} (付与後合計={cur_c+coin:,})")
            else:
                coin = coin_req
        except Exception as _ce:
            _dbg(f"[coin_cap] resources取得例外: {_ce}")


    if tsum_lv is not None:
        # レベルMAXは、各レベルの上限解放にコインを使う（レベルが上がるほど必要枚数が増える）。
        # ここでのコインは売り物ではなく「レベルを上げるための手段」なので、メニューの価格や
        # TSUM_AUTO_FILL_COINS 設定に関わらず、足りなければ自動で必要枚数を補充する。
        # （補充しないと途中の上限解放で retcode 22「コイン残数が一致しません」で止まる。）
        lv_auto_fill = True
        if lv_auto_fill:
            try:
                cur_c = f.resources()["bcoin"]
                if cur_c < 50_000:
                    _dbg(f"[tsum_lv] レベル解放用コイン補充: 現在={cur_c:,} -> 50万補充")
                    f.fill_coins(cap=500_000)
            except Exception:
                pass
        tid = int(tsum_lv)
        if tid == 0:
            # セット中(setflg)のマイツムを最優先。無ければ所持しているツムから選ぶ。
            # setflg だけで判定すると、ホーム未設定（前回の「タイトルに戻ります」等で解除）のとき
            # 所持ツムがあっても「見つかりません」になってしまうため、堅牢な選択器を使う。
            candidates, info, err = select_play_tsumids(f)
            if not isinstance(info, dict):
                return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
            if not candidates:
                return None, (err or "使用できるツムが見つかりませんでした。"
                              "ゲームでツムを手に入れてから、もう一度お試しください。")
            tid = candidates[0]
        else:
            # 指定ツム：そのツムを所持しているか確認する（所持していないツムは上げられない）。
            info = f.get_info()
            if not isinstance(info, dict):
                return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
            owned = owned_tsum_ids(info)
            if owned and tid not in owned:
                return None, (f"指定したツム（ID {tid}）を所持していません。"
                              "所持しているツムのIDを入力してください。")
        if progress:
            progress.update(act_stage, "ツムのレベルを上げています...", force=True)
        res = f.max_tsum_level(tid, target=50, min_heart=10, sleep_s=0.3, log=_dbg, auto_fill_coins=lv_auto_fill)
        st = res.get("status")
        # 失敗系はデバッグログに詳細(サーバー応答含む)を残す
        if st != "done":
            _dbg(f"[tsum_lv失敗] tsumid={tid} status={st} detail={res}")
        if st == "done":
            return res, "セット中のツムをレベルMAXにしました\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
        if st == "api_error":
            return None, describe_tsum_api_failure("ツムのレベルMAX", res.get("resp"), f, missing_data=True)
        if st == "insufficient_heart":
            return None, f"ハートが足りないため中断しました（所持 {res['have']} / 必要 {res['need']}）。ハートを貯めてからもう一度お試しください。"
        if st == "heart_ran_out":
            return None, "ハートが切れたため途中で終了しました。ハートを補充すると続きから実行できます。"
        if st == "stuck":
            return None, "途中で中断しました。もう一度お試しください。"
        if st == "level_error":
            return None, ("プレイ開始をサーバーに拒否されました（アカウントがチュートリアル未完了/"
                          "イベントIDやハート状態の不一致の可能性）。ゲームをホームまで進めてから再度お試しください。"
                          f"\n[詳細] {str(res.get('resp'))[:150]}")
        if st == "release_error":
            return None, ("レベル上限の開放をサーバーに拒否されました（コイン不足などの可能性）。"
                          f"\n[詳細] {str(res.get('resp'))[:150]}")
        if st == "max_iter":
            return None, f"規定回数を試しましたがレベルMAXに届きませんでした（現在lv={res.get('lv')}）。もう一度お試しください。"
        return None, f"ツムのレベルMAXに失敗しました（status={st}）。もう一度お試しください。"

    if all_tsum_lv:
        # 所持している全ツムをレベル50にする。コインは手段なので必要なだけ自動補充する。
        # 既にMAXのツムは max_tsum_level 側で即 done になるので、再実行でも速い（途中まで済んだ分は続きから）。
        info = f.get_info()
        if not isinstance(info, dict):
            return None, "接続が不安定なため実行できませんでした。少し待ってからもう一度お試しください。"
        owned = owned_tsum_ids(info)
        if not owned:
            return None, "所持しているツムが見つかりませんでした。ゲームでツムを手に入れてから、もう一度お試しください。"
        total = len(owned)
        done_n = 0
        failed = []
        for idx, tid in enumerate(owned, 1):
            if progress:
                progress.update(act_stage, f"全ツムをレベルMAX中… {idx}/{total}（ツム{tid}）", force=True)
            try:
                res = f.max_tsum_level(tid, target=50, min_heart=1, sleep_s=0.2, log=_dbg, auto_fill_coins=True)
            except Exception as _e:  # noqa: BLE001
                _dbg(f"[all_lv] tsum{tid} 例外: {type(_e).__name__}: {str(_e)[:120]}")
                failed.append(tid)
                continue
            st = res.get("status") if isinstance(res, dict) else ""
            if st == "done":
                done_n += 1
            elif st in ("insufficient_heart", "heart_ran_out"):
                # ハート切れ：できた分を返して打ち切る（ハートが戻ってから再実行で続きから）。
                _dbg(f"[all_lv] ハート切れで中断 ({idx}/{total}, 完了={done_n})")
                return ({"status": "partial", "done": done_n, "total": total},
                        f"ハートが切れたため途中で終了しました（{done_n}/{total} 体完了）。"
                        "ハートが回復してから、もう一度お試しください（続きから実行します）。")
            else:
                _dbg(f"[all_lv] tsum{tid} status={st} detail={str(res)[:120]}")
                failed.append(tid)
        if done_n == 0 and failed:
            return None, f"どのツムもレベルMAXにできませんでした（{len(failed)} 体失敗）。もう一度お試しください。"
        msg = f"全ツムをレベルMAXにしました（{done_n}/{total} 体完了"
        msg += f"／{len(failed)} 体スキップ）" if failed else "）"
        msg += "\n完了しました。\nまたのご利用をお待ちしております。"
        return {"status": "done", "done": done_n, "total": total}, msg

    if box is not None:
        need = 30_000_000
        box, _st = resolve_gacha_id(f, "box", box)
        if _st == "not_running":
            return None, "現在セレクトBOXが開催されていないため実行できません。"
        if progress:
            progress.update(act_stage, "セレクトBOX完売処理を開始します...", force=True)

        res = None
        for _refill_try in range(3):
            if auto_fill_coins:
                try:
                    cur_c = f.resources()["bcoin"]
                    if cur_c < 300_000:
                        if progress:
                            progress.update(act_stage, "ガチャ用コインを補充中 (3000万)...", force=True)
                        _dbg(f"[box] コイン不足検知: 現在={cur_c:,} -> 3000万補充")
                        f.fill_coins(cap=cur_c + 30_000_000)
                except Exception as _fe:
                    _dbg(f"[box] fill_coins例外: {_fe}")

            def _on_box_p(drawn, cur_coin, is_single):
                if progress:
                    mode_str = "単発スパート中" if is_single else "10連引出中"
                    progress.update(act_stage, f"セレクトBOX引出中 ({mode_str}): {drawn:,}連 (残コイン: {cur_coin:,})")

            res = f.complete_box(int(box), min_coin=need if not auto_fill_coins else 300_000,
                                 sleep_s=0, log=_dbg, on_progress=_on_box_p)
            st = res.get("status")
            if st in ("coin_ran_out", "insufficient_coin") and auto_fill_coins and _refill_try < 2:
                _dbg(f"[box] コイン不足({st})のため補充して再開 (試行{_refill_try+2}/3)")
                continue
            break

        st = res.get("status") if isinstance(res, dict) else ""
        if st in ("complete", "already_complete"):
            return res, f"セレクトBOXを完売しました ({res.get('draws','-')}連)\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return None, "接続が不安定なため途中で止まりました。少し待ってからもう一度お試しください（引いた分は反映済みなので続きから完売します）。"
        if st == "api_error":
            return None, describe_tsum_api_failure("セレクトBOXの完売", res.get("resp"), f, missing_data=True)
        if st == "insufficient_coin":
            return None, f"コインが足りないため中断しました（所持 {res['have']:,} / 必要 {res['need']:,}）。先にコインを増やしてからお試しください。"
        if st == "coin_ran_out":
            return None, "コインが切れたため途中で終了しました。もう一度お試しください。"
        _dbg(f"[box失敗] gachaid={box} status={st} detail={res}")
        if st == "draw_error":
            return None, ("ガチャの抽選をサーバーに拒否されました"
                          f"（{res.get('draws', 0)}連時点）。\n"
                          "開催中の別のBOXに切り替わった可能性があります。\n"
                          f"[詳細] {str(res.get('resp'))[:150]}")
        if st == "max_reached":
            return None, f"上限まで引きましたが完売しませんでした（{res.get('draws','-')}連）。もう一度お試しください。"
        return None, f"セレクトBOXの完売に失敗しました（status={st}）。もう一度お試しください。"

    if gacha_full is not None:
        gacha_full, _st = resolve_gacha_id(f, "premium", gacha_full)
        if _st == "not_running":
            return None, "現在プレミアムガチャが開催されていないため実行できません。"
        if progress:
            progress.update(act_stage, "プレミアムガチャ完売処理を開始します...", force=True)

        res = None
        for _refill_try in range(5):
            if auto_fill_coins:
                try:
                    cur_c = f.resources()["bcoin"]
                    if cur_c < 300_000:
                        if progress:
                            progress.update(act_stage, "ガチャ用コインを補充中 (3000万)...", force=True)
                        _dbg(f"[premium] コイン不足検知: 現在={cur_c:,} -> 3000万補充")
                        f.fill_coins(cap=cur_c + 30_000_000)
                except Exception as _fe:
                    _dbg(f"[premium] fill_coins例外: {_fe}")

            def _on_prem_p(drawn, cur_coin, is_single):
                if progress:
                    mode_str = "単発スパート中" if is_single else "10連引出中"
                    progress.update(act_stage, f"プレミアムガチャ引出中 ({mode_str}): {drawn:,}連 (残コイン: {cur_coin:,})")

            res = f.complete_box(int(gacha_full), min_coin=300_000, sleep_s=0, log=_dbg, on_progress=_on_prem_p)
            st = res.get("status")
            if st in ("coin_ran_out", "insufficient_coin") and auto_fill_coins and _refill_try < 4:
                _dbg(f"[premium] コイン不足({st})のため補充して再開 (試行{_refill_try+2}/5)")
                continue
            break

        st = res.get("status") if isinstance(res, dict) else ""
        if st in ("complete", "already_complete"):
            return res, f"プレミアムガチャを完売しました ({res.get('draws','-')}連)\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "network_error":
            return None, "接続が不安定なため途中で止まりました。少し待ってからもう一度お試しください（引いた分は反映済みなので続きから完売します）。"
        if st == "api_error":
            return None, describe_tsum_api_failure("プレミアムガチャの完売", res.get("resp"), f, missing_data=True)
        if st == "insufficient_coin":
            return None, f"コインが足りません（所持 {res['have']:,}）。先にコインを増やしてからお試しください。"
        if st == "coin_ran_out":
            return None, f"コインが足りず途中で終了しました（{res.get('draws','-')}連）。コインを増やしてからお試しください。"
        _dbg(f"[premium失敗] gachaid={gacha_full} status={st} detail={res}")
        if st == "draw_error":
            return None, ("ガチャの抽選をサーバーに拒否されました"
                          f"（{res.get('draws', 0)}連時点）。\n"
                          "開催中の別のガチャに切り替わった可能性があります。\n"
                          f"[詳細] {str(res.get('resp'))[:150]}")
        if st == "max_reached":
            return None, f"上限まで引きましたが完売しませんでした（{res.get('draws','-')}連）。もう一度お試しください。"
        return None, f"プレミアムガチャの完売に失敗しました（status={st}）。もう一度お試しください。"

    if gacha_plus is not None:
        gacha_plus, _st = resolve_gacha_id(f, "premium_plus", gacha_plus)
        if _st == "not_running":
            return None, "現在プレミアムBOX+が開催されていないため実行できません。"
        if not gacha_plus:
            return None, "プレミアムBOX+の情報をゲームから取得できませんでした。少し待ってからもう一度お試しください。"
        paytype = premium_plus_paytype()
        price = premium_plus_price(f, gacha_plus)
        max_draws = int(CONFIG.get("premium_box_plus_max_draws", 10_000))
        _dbg(f"[premium_plus] gachaid={gacha_plus} paytype={paytype} 値段={price} 上限={max_draws}連")
        if progress:
            progress.update(act_stage, "プレミアムBOX+をメダルで引いています...", force=True)

        def _on_plus_p(drawn, cur_medal, is_single):
            if progress:
                mode_str = "単発" if is_single else "10連"
                progress.update(act_stage, f"プレミアムBOX+引出中 ({mode_str}): {drawn:,}連 (残メダル: {cur_medal:,})")

        res = f.draw_with_medals(int(gacha_plus), price=price, paytype=paytype, max_draws=max_draws,
                                 log=_dbg, on_progress=_on_plus_p)
        st = res.get("status") if isinstance(res, dict) else ""
        total = int(res.get("draws") or 0) if isinstance(res, dict) else 0
        _dbg(f"[premium_plus] 結果 status={st} detail={res}")
        left = res.get("medal") if isinstance(res, dict) else None
        price_s = f"{int(res.get('price')):,}" if isinstance(res, dict) and res.get("price") else "不明"
        left_s = f"\n残りメダル: {left:,}枚" if isinstance(left, int) else ""
        used_s = f"\n使ったメダル: {int(res.get('spent_medal') or 0):,}枚" if total > 0 else ""
        tail = "\n完了しました。\nまたのご利用をお待ちしております。"
        if st == "done":
            return res, f"プレミアムBOX+をメダルがなくなるまで引きました（{total:,}連）" + used_s + left_s + tail
        if st == "max_reached":
            return res, (f"プレミアムBOX+を1回の注文の上限まで引きました（{total:,}連）" + used_s + left_s
                         + "\nメダルが残っている場合は、もう一度ご注文いただくと続きを引けます。" + tail)
        if st == "no_medal":
            return None, (f"メダルが足りないため引けませんでした（所持 {left or 0:,}枚 ／ 1回 {price_s}枚）。"
                          "先に「メダル指定」や「メダルMAX」でメダルを増やしてからお試しください。")
        if st == "present_pending":
            head = (f"プレミアムBOX+を {total:,}連 引いたところで止まりました。" + used_s + left_s + "\n") if total else ""
            return (res if total else None), (head + "ゲーム内のプレゼントBOXに受け取っていないツムがあるため、"
                                              "これ以上引けません。ゲームでプレゼントを受け取ってから、もう一度ご注文ください。"
                                              + (tail if total else ""))
        if st == "wrong_currency":
            return None, (f"プレミアムBOX+を引いたところ、メダル以外が減った可能性があるため止めました（{total:,}連）。"
                          "お手数ですがゲーム内でコイン・ルビーをお確かめのうえ、お問い合わせください。")
        if st == "network_error":
            return None, ((f"接続が不安定なため {total:,}連 で止まりました。" if total else "接続が不安定なため実行できませんでした。")
                          + "少し待ってからもう一度お試しください" + ("（引いた分は反映済みです）。" if total else "。"))
        if st == "api_error":
            return None, describe_tsum_api_failure("プレミアムBOX+", res.get("resp"), f, missing_data=True)
        if st == "stopped" and total > 0:
            return res, (f"プレミアムBOX+を引けるところまで引きました（{total:,}連。それ以上はサーバーが受け付けませんでした）"
                         + used_s + left_s + tail)
        if st == "stopped":
            return None, ("プレミアムBOX+をサーバーに断られました。" + left_s
                          + f"\n[詳細] retcode={res.get('retcode')} {str(res.get('msg') or '')[:120]}")
        return None, f"プレミアムBOX+を引けませんでした（status={st}）。もう一度お試しください。"

    if progress:
        progress.update(act_stage, "代行処理を実行しています...", force=True)
    candidates, _info, _err = select_play_tsumids(f, tsumid)
    tsum_candidates = list(candidates or [])[:3]
    if isinstance(_info, dict):
        for t in (_info.get("tsuminfo") or []):
            tid = _to_int(t.get("tsumid")) if isinstance(t, dict) else 0
            if tid and tid not in tsum_candidates and len(tsum_candidates) < 5:
                tsum_candidates.append(tid)
    if tsumid and tsumid not in tsum_candidates:
        tsum_candidates.append(tsumid)
    if not tsum_candidates:
        tsum_candidates = [tsumid]

    bheart, pheart, total_heart = heart_state_from_info(_info)
    if heart_info_is_reliable(_info) and total_heart < 1:
        return None, "ハートが足りないため開始できませんでした。ハートが回復してから、もう一度お試しください。"
    ht_candidates = hearttype_candidates_from_info(_info) or [0, 1]
    _set_tsum = next((t.get("tsumid") for t in (_info.get("tsuminfo") or [])
                      if isinstance(t, dict) and t.get("setflg") in (1, "1", True)), None) if isinstance(_info, dict) else None
    uid = str(sess.get("userid") or "")
    active_ev = "0"
    if isinstance(_info, dict) and isinstance(_info.get("eventinfo"), dict):
        cur_ev = str(_info["eventinfo"].get("eventid") or "")
        if cur_ev and cur_ev != "0":
            active_ev = cur_ev
    if active_ev == "0":
        try:
            active_ev = f.detect_active_event()
        except Exception:
            pass
    ev_order = []
    if active_ev and active_ev != "0":
        ev_order.append(active_ev)
    for e in ("9999", "0", "157"):
        if e not in ev_order:
            ev_order.append(e)
    try:
        for e in _cached_event_candidates(f):
            if e not in ev_order:
                ev_order.append(e)
    except Exception:
        pass

    # イベントカードの演出保留やクリア報酬保留による gameStart ロックを事前に自動解除
    try:
        f.unblock_event_state(ev_order, info=_info)
    except Exception as _ue:
        _dbg(f"[gs] unblock_event_state: {_ue}")

    _dbg(f"[gs v7] userid={uid[:3]}… set_tsum={_set_tsum} bheart={bheart} pheart={pheart} "
         f"ht={ht_candidates} tsum候補={tsum_candidates[:4]} eventid候補={ev_order}")

    r1 = None
    playcode = None
    won_tid = None
    won_ht = None
    won_ev = None
    won_boxcnt = ""
    attempts = []
    def _make_event_boxcnt(ev_info):
        if not isinstance(ev_info, dict):
            return ""
        if ev_info.get("gametype") == 1:
            bl = ev_info.get("boxlist") or []
            parts = [f"{b.get('type', 0)},0|" for b in bl if isinstance(b, dict) and b.get("type") is not None]
            return "".join(parts) if parts else "0,0|"
        return ""

    owned_tids = owned_tsum_ids(_info) if isinstance(_info, dict) else []

    orig_set_tsum = _set_tsum
    cur_hometsum = _set_tsum

    play_tids = []
    # 1. ユーザーの現在装備マイツム (_set_tsum) が通常単体ツムなら最優先
    if _set_tsum and (not owned_tids or _set_tsum in owned_tids) and not is_pair_tsum(_set_tsum):
        play_tids.append(_set_tsum)

    # 2. 指定された tsumid が所持かつ非ペアツムなら採用
    if tsumid and not is_pair_tsum(tsumid) and tsumid not in play_tids and (not owned_tids or tsumid in owned_tids):
        play_tids.append(tsumid)

    # 3. ミッキー(1)が所持なら最優先追加 (チュートリアル保証)
    if 1 in owned_tids and 1 not in play_tids:
        play_tids.append(1)

    # 4. その他所持通常ツム (ペアツム除外)
    for t in (list(tsum_candidates) + owned_tids):
        if t and t not in play_tids and (not owned_tids or t in owned_tids) and not is_pair_tsum(t):
            play_tids.append(t)
            if len(play_tids) >= 4:
                break

    if not play_tids:
        play_tids = [1]

    def _attempt(tid, ht, ev):
        nonlocal r1, playcode, won_tid, won_ht, won_ev, won_boxcnt, cur_hometsum
        if cur_hometsum != tid and tid != orig_set_tsum:
            try:
                sp_res = f.set_profile(hometsum=tid)
                if isinstance(sp_res, dict) and sp_res.get("retcode") == 0:
                    _dbg(f"[mytsum] マイツム同期 {cur_hometsum} -> {tid} 完了")
                    cur_hometsum = tid
                else:
                    _dbg(f"[mytsum] set_profile失敗 tid={tid}: {sp_res}")
            except Exception as _spe:
                _dbg(f"[mytsum] set_profile例外 tid={tid}: {_spe}")
        r1 = f.game_start(tsumid=tid, hearttype=ht, probmstver="2", eventid=ev)
        rc = (r1.get("retcode"), r1.get("retsubcode")) if isinstance(r1, dict) else None
        attempts.append((tid, ht, f"ev{ev}", rc))
        playcode = (r1.get("userinfo") or {}).get("playcode") or r1.get("playcode") if isinstance(r1, dict) else None
        if playcode:
            won_tid = tid; won_ht = ht; won_ev = ev
            # BOXイベント(gametype=1)は boxlist の type に応じた boxcnt を設定
            won_boxcnt = _make_event_boxcnt(r1.get("eventinfo") if isinstance(r1, dict) else None)
        elif isinstance(r1, dict) and r1.get("retcode") == 6:
            f.refresh_session()
        elif isinstance(r1, dict) and r1.get("retcode") == 997:
            # サーバー側で前回のセッションが残っている可能性をクリア
            try:
                f.game_retire()
            except Exception:
                pass
        return playcode

    for tid in play_tids[:4]:
        for ht in ht_candidates:
            for ev in ev_order:
                if _attempt(tid, ht, ev):
                    break
                time.sleep(0.15)
            if playcode:
                break
        if playcode:
            break


    before_coin = (r1.get("userinfo") or {}).get("bcoin") if isinstance(r1, dict) else None
    before_medal = (r1.get("userinfo") or {}).get("bmedal") if isinstance(r1, dict) else None
    if not playcode:
        if orig_set_tsum and cur_hometsum != orig_set_tsum:
            try:
                f.set_profile(hometsum=orig_set_tsum)
            except Exception:
                pass
        _EVENT_CACHE["cands"] = None
        try:
            ui = (_info.get("userinfo") or {}) if isinstance(_info, dict) else {}
            set_tsum = next((t.get("tsumid") for t in (_info.get("tsuminfo") or [])
                             if isinstance(t, dict) and t.get("setflg") in (1, "1", True)), None) if isinstance(_info, dict) else None
            dump = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "userid": sess.get("userid"),
                "hashlen": len(str(sess.get("hash") or "")),
                "checkval_present": bool(sess.get("checkval")),
                "getInfo_retcode": _info.get("retcode") if isinstance(_info, dict) else f"non-dict({type(_info).__name__})",
                "getInfo_keys": sorted(_info.keys()) if isinstance(_info, dict) else None,
                "userinfo": ui,
                "n_tsum": len([t for t in (_info.get("tsuminfo") or []) if isinstance(t, dict)]) if isinstance(_info, dict) else 0,
                "set_tsum": set_tsum,
                "sample_tsums": [t for t in ((_info.get("tsuminfo") or [])[:5]) if isinstance(t, dict)] if isinstance(_info, dict) else [],
                "attempts": [list(a) for a in attempts],
                "gameStart_response": r1 if isinstance(r1, dict) else repr(r1),
            }
            with open(os.path.join(PROJECT_ROOT, "tsum_gs_fulldump.json"), "w", encoding="utf-8") as df:
                json.dump(dump, df, ensure_ascii=False, indent=2)
            with open(os.path.join(PROJECT_ROOT, "tsum_gs_diag.log"), "a", encoding="utf-8") as lf:
                lf.write(f"{dump['ts']} GS_FAIL userid={dump['userid']} getInfo_rc={dump['getInfo_retcode']} "
                         f"n_tsum={dump['n_tsum']} set_tsum={set_tsum} bheart={bheart} pheart={pheart} "
                         f"attempts={attempts}  (full -> tsum_gs_fulldump.json)\n")
        except Exception as _e:
            print(f"[gs_diag] dump failed: {_e}")
        if heart_info_is_reliable(_info) and total_heart < 1:
            return None, "ハートが足りないため開始できませんでした。ハートが回復してから、もう一度お試しください。"
        if not heart_info_is_reliable(_info):
            # getInfo 自体が拒否されている場合は、ハートではなく本当の原因を返す
            return None, describe_tsum_api_failure("ゲーム開始", _info if isinstance(_info, dict) else r1,
                                                   f, missing_data=True)
        return None, describe_tsum_api_failure("ゲーム開始", r1, f, missing_data=True)
    extra_owned = [t for t in owned_tids if t != won_tid][:4]
    vanishcnt = f"{won_tid},0|" + "".join(f"{t},0|" for t in extra_owned) if won_tid else None

    chunk_field = "exp" if exp is not None else ("medal" if medal is not None else None)
    target = int(exp) if exp is not None else (int(medal) if medal is not None else 0)
    if chunk_field == "medal":
        max_medal_cap = MEDAL_CAP
        curr_m = int(before_medal or 0)
        if curr_m >= max_medal_cap:
            return None, f"現在のメダル所持数（{curr_m:,}枚）が安全上限（{max_medal_cap:,}枚）に達しているため、これ以上増やせません。"
        target = min(target, max_medal_cap - curr_m)
    if chunk_field:
        default_chunk = 40_000_000 if chunk_field == "exp" else 50_000
        chunk = int(CONFIG.get(f"{chunk_field}_per_game", default_chunk))
        min_chunk = 1_000_000 if chunk_field == "exp" else 10_000
        # メダルは上限が大きいので、通った量を倍々に増やす（拒否されたら半分に戻し、その量は二度と超えない）。
        # 経験値は従来どおり固定量。
        grow = chunk_field == "medal"
        ceiling = int(CONFIG.get("medal_chunk_max", 10_000_000)) if grow else chunk
        max_games = int(CONFIG.get("medal_max_games", 120)) if grow else 30
        credited = 0
        games = 0
        r2 = None
        pc = playcode
        while credited < target and games < max_games:
            send = min(chunk, target - credited)
            if pc is None:
                rs = f.game_start(tsumid=won_tid, hearttype=won_ht, probmstver="2", eventid=won_ev)
                pc = (rs.get("userinfo") or {}).get("playcode") or rs.get("playcode") if isinstance(rs, dict) else None
                if pc:
                    won_boxcnt = _make_event_boxcnt(rs.get("eventinfo") if isinstance(rs, dict) else None)
                if not pc:
                    break
            ge = dict(score=score if score is not None else 95000,
                      coin=coin if coin is not None else 61,
                      medal=send if chunk_field == "medal" else (int(medal) if medal is not None else 5),
                      exp=send if chunk_field == "exp" else 120,
                      vanishcnt=vanishcnt, boxcnt=won_boxcnt)
            rr = f.game_end(pc, **ge)
            pc = None
            if isinstance(rr, dict) and rr.get("retcode") == 0:
                r2 = rr; credited += send; games += 1
                if grow and send == chunk:
                    chunk = min(chunk * 2, ceiling)
                if progress and chunk_field:
                    pct = int(credited / target * 100) if target > 0 else 0
                    field_label = "経験値" if chunk_field == "exp" else "メダル"
                    progress.update(act_stage, f"{field_label}送信中: {credited:,} / {target:,} ({pct}%)")
            else:
                # gameEnd が拒否された場合はサーバー側にセッションが残るため gameRetire で掃除
                try:
                    f.game_retire()
                except Exception:
                    pass
                if grow:
                    ceiling = max(min_chunk, send // 2)
                if chunk > min_chunk:
                    # 送信量が実際に減るまで一気に縮める。
                    new_chunk = chunk // 2
                    while new_chunk > min_chunk and min(new_chunk, target - credited) == send:
                        new_chunk //= 2
                    chunk = new_chunk
                    if min(chunk, target - credited) == send:
                        break
                else:
                    break
            time.sleep(0.2)
        _dbg(f"[{chunk_field}] target={target} credited={credited} games={games} last_chunk={chunk}")
        if credited <= 0 or r2 is None:
            return None, "送信に失敗しました。もう一度試してください。"
    else:
        r2 = f.game_end(playcode,
                        score=score if score is not None else 95000,
                        coin=coin if coin is not None else 61,
                        medal=int(medal) if medal is not None else 5, exp=120,
                        vanishcnt=vanishcnt,
                        boxcnt=won_boxcnt)
        if not isinstance(r2, dict) or r2.get("retcode") != 0:
            try:
                f.game_retire()
            except Exception:
                pass
            return None, "送信に失敗しました。もう一度試してください。"
    if progress:
        fin_stage = 3 if is_guest else 5
        progress.update(fin_stage, "結果を反映しています...", force=True)
    ui = r2.get("userinfo") or {}
    after_coin = ui.get("bcoin")
    after_medal = ui.get("bmedal")
    lines = []
    if coin is not None:
        if before_coin is not None and after_coin is not None:
            lines.append(f"コイン: {before_coin:,} → {after_coin:,} ( +{after_coin - before_coin:,} )")
        else:
            lines.append(f"コイン: +{coin:,}")
    if medal is not None:
        if before_medal is not None and after_medal is not None:
            lines.append(f"メダル: {before_medal:,} → {after_medal:,} ( +{after_medal - before_medal:,} )")
        else:
            lines.append(f"メダル: +{credited if chunk_field == 'medal' else int(medal):,}")
        if chunk_field == "medal" and credited < target:
            lines.append(f"※1回の注文で送れる量の上限に達したため、{credited:,}枚まで付与しました"
                         f"（残り{target - credited:,}枚）。もう一度ご注文いただくと続きを付与できます。")
    if score is not None:
        lines.append(f"スコア: {score:,}")
    if exp is not None:
        lines.append("プレイヤーレベルMAX" if credited >= target else f"経験値を付与しました（{credited:,}）")
    if orig_set_tsum and cur_hometsum != orig_set_tsum:
        try:
            f.set_profile(hometsum=orig_set_tsum)
            _dbg(f"[mytsum] 元のマイツム({orig_set_tsum})への復元完了")
        except Exception as _re:
            _dbg(f"[mytsum] マイツム復元例外: {_re}")
    lines.append("完了しました。")
    lines.append("またのご利用をお待ちしております。")
    return r2, "\n".join(lines)

def _proxies_dict(proxy):
    return {"http": proxy, "https": proxy} if proxy else None


def _save_orphan_guest(sess, migration_id):
    try:
        path = os.path.join(PROJECT_ROOT, "tsum_guest_orphans.json")
        data = []
        if os.path.exists(path):
            try:
                data = json.load(open(path, encoding="utf-8-sig")) or []
            except Exception:
                data = []
        data.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "orig_migration_id": migration_id,
            "uuid": sess.get("uuid"),
            "userToken": sess.get("userToken"),
            "userid": sess.get("userid"),
            "hash": sess.get("hash"),
            "checkval": sess.get("checkval"),
            "migrated_userKey": sess.get("migrated_userKey"),
        })
        _jdump_atomic(path, data, indent=2)
        print(f"[guest] orphan session saved -> {path} (uuid={sess.get('uuid')} userid={sess.get('userid')})")
    except Exception:
        traceback.print_exc()


def _save_guest_transfer(sess, old_migration_id, new_id, new_pw):
    """発行できた新しい引き継ぎ情報を必ずディスクに残す。
    Discord への送信が失敗するとアカウントに二度と入れなくなるため、送信より先に保存する。"""
    path = os.path.join(PROJECT_ROOT, "tsum_guest_transfers.json")
    try:
        data = _jload(path, [])
        if not isinstance(data, list):
            data = []
        data.append({
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "userid": (sess or {}).get("userid"),
            "old_migration_id": old_migration_id,
            "new_id": new_id,
            "new_pw": new_pw,
        })
        _jdump_atomic(path, data, ensure_ascii=False, indent=2)
        print(f"[guest] 新しい引き継ぎ情報を保存 -> {path} (userid={(sess or {}).get('userid')})")
    except Exception:
        traceback.print_exc()

def forge_sync_guest(migration_id, password, score=None, coin=None, exp=None, medal=None,
                     tsumid=860, tsum_lv=None, box=None, proxy=None, gacha_full=None,
                     progress=None, auto_fill_coins=None, gacha_plus=None, **_ignore):
    if not migration_id or not password:
        return None, "引き継ぎ番号とパスワードを入力してください。", {"moved": False}
    if progress:
        progress.update(0, "Tor回線を準備しています...", force=True)
    proxies = _proxies_dict(proxy)
    if progress:
        progress.update(1, f"引き継ぎ番号 {migration_id} で認証中...", force=True)
    try:
        sess, err = tsum_guest.guest_takeover(migration_id, password, proxies)
    except Exception:
        traceback.print_exc()
        if progress:
            progress.update(1, "引き継ぎ処理でエラーが発生しました", force=True)
        return None, "引き継ぎ処理でエラーが発生しました。番号とパスワードを確認して、もう一度お試しください。", {"moved": False}
    if not sess:
        if progress:
            progress.update(1, err or "引き継ぎ認証に失敗しました", force=True)
        return None, (err or "引き継ぎに失敗しました（番号かパスワードが違う/期限切れ）。"), {"moved": False}

    info = {"moved": True, "new_id": None, "new_pw": None}
    result_obj, text = None, ""
    try:
        if sess.get("banned"):
            if progress:
                progress.update(2, "アカウントが利用停止（BAN）されています", force=True)
            b_msg = sess.get("ban_msg") or "不正利用が確認されたため、アカウントが停止されております。"
            text = f"⚠️ このアカウントは運営により利用停止（BAN）されています。\n理由: {b_msg}\n利用停止中のため代行および引き継ぎ情報の発行はできません。"
            _dbg(f"[guest] BANアカウント userid={sess.get('userid')} msg={b_msg}")
            return None, text, {"moved": True, "banned": True}
        elif sess.get("login_failed"):
            # 引き継ぎ自体は完了しているが checkval が取れていない。
            # この状態では全 API が必ず失敗するので、盛り処理は飛ばして引き継ぎ発行だけ試す。
            if progress:
                progress.update(2, "ゲーム認証に失敗したため、代行処理をスキップします", force=True)
            text = "引き継ぎは完了しましたが、ゲームサーバーへの接続に失敗したため代行を実行できませんでした。"
            _dbg(f"[guest] login_failed セッション userid={sess.get('userid')} → 盛り処理をスキップ")
        else:
            if progress:
                progress.update(2, "ゲームサーバー認証中...", force=True)
            g_proxy = _game_api_proxy(proxy)
            f = Forge(userid=sess["userid"], hashv=sess.get("hash", ""), checkval=sess.get("checkval", ""), proxy=g_proxy, user_token=sess.get("userToken"))
            f.get_public_key()
            if progress:
                progress.update(3, "代行処理を実行中...", force=True)
            result_obj, text = forge_sync(score=score, coin=coin, exp=exp, medal=medal, tsumid=tsumid,
                                          tsum_lv=tsum_lv, box=box, proxy=g_proxy, sess=sess, f=f,
                                          gacha_full=gacha_full, gacha_plus=gacha_plus,
                                          progress=progress, is_guest=True,
                                          auto_fill_coins=auto_fill_coins)
    except Exception:
        traceback.print_exc()
        try:
            with open(os.path.join(PROJECT_ROOT, "guest_forge_error.log"), "a", encoding="utf-8") as _lf:
                _lf.write("\n==== guest盛りエラー coin=%s score=%s exp=%s box=%s gacha_full=%s tsum_lv=%s ====\n"
                          % (coin, score, exp, box, gacha_full, tsum_lv))
                _lf.write(traceback.format_exc())
        except Exception:
            pass
        result_obj, text = None, "盛り処理でエラーが発生しました。"

    if progress:
        progress.update(4, "新しい引き継ぎコードを発行中...", force=True)
    new_id = new_pw = None
    for _ in range(3):
        try:
            nid, npw = tsum_guest.guest_issue_transfer(sess, proxies)
        except Exception:
            traceback.print_exc()
            nid, npw = None, None
        if nid:
            new_id, new_pw = nid, npw
            break
        time.sleep(0.4)
    info["new_id"], info["new_pw"] = new_id, new_pw

    if new_id:
        if progress:
            progress.update(4, f"引き継ぎコード発行完了: {new_id}", force=True)
        # Discord に出す前に保存する（送信が失敗しても失われないように）
        _save_guest_transfer(sess, migration_id, new_id, new_pw)
        try:
            tsum_guest.mark_pending_done(sess.get("uuid"), new_id)
        except Exception:
            pass
        text += ("\n\n──────────\n**新しい引き継ぎ情報**\n"
                 f"引き継ぎ番号: `{new_id}`\nパスワード: `{new_pw}`\n"
                 "ゲームの『設定 → 引き継ぎ』からこの情報で引き継いでください。必ず控えてください。")
    else:
        if progress:
            progress.update(4, "新しい引き継ぎ情報の発行に失敗しました", force=True)
        _save_orphan_guest(sess, migration_id)
        if sess.get("banned"):
            text += ("\n\n──────────\n⚠️アカウントが利用停止（BAN）されているため、新しい引き継ぎ情報は発行できません。")
        else:
            text += ("\n\n──────────\n⚠️新しい引き継ぎ情報の発行に失敗しました。"
                     "時間をおいてから再度お試しいただくか、ゲーム内でお確かめください。")
    return result_obj, (text or "処理に失敗しました。"), info

MAX_PRESETS = {
    "score_max": ("score", 2_147_483_647),
    "level_max": ("exp", 100_000_000),
    "coin_max":  ("coin", 200_000_000),
    "medal_max": ("medal", MEDAL_CAP),   # メダルを上限（1億枚）まで
}


# ===== 抜き出した部分の差し替え（Web 版用） =====
# Discord 版の is_coin_free は「サーバーごとの料金設定でコインが0円なら無料」を見る。
# Web 版ではコイン代行は有料なので、コインの自動補充は呼び出し側（tsum_service）が
# auto_fill_coins で明示的に決める。
def is_coin_free(guild_id=None):   # noqa: F811
    return bool(os.environ.get("TSUM_AUTO_FILL_COINS"))
