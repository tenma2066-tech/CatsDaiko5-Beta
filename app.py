"""お店のサーバー本体（Flask）。画面を出し、注文を受け、PayPay の入金を確かめ、実行して結果を返す。

このファイルの並び（上から。見出しは「# ----」で検索できる）:
  1. 設定・料金の読み込み             … 環境変数、管理画面で変えた料金（prices.json）
  2. にゃんこのメニュー定義・キャラ名   … ITEMS / SUBS / OTHERS、キャラ番号、名前データの取得
  3. 状態管理・実行枠                 … 同時に動かす件数、順番待ち、別プロセスでの実行（_launch_job）
  4. にゃんこの実行                   … _apply_edits / _run_job（セーブの編集は engine.py）
  5. 認証・連打よけ・お店の設定         … 管理者ログイン、_rate_ok、メンテ表示、初期垢の中身
  6. 管理画面                         … /admin（_admin_post が操作、_admin_page が表示）
  7. 注文（決済してから実行）           … _orders、保存、_reconcile_orders（結末の反映）
  8. 画面のページ                     … / /nyanko /tsum /puni、robots・sitemap
     （実績＝星と感想 /reviews・/api/reviews は 10 の手前にある）
  9. 注文を作る                       … /run → 各部門の検査 → _checkout → _place_order（クーポンもここ）
 10. Q&A・チケット・支援・ソースコード  … /api/qa、/ticket /support /source（_deliver_product）
 11. 注文の一覧・支払い・進み具合       … /api/orders/…（_pay_order → _start_paid_order）、/status

ほかのファイル: 料金＝pricing.py／PayPay と台帳＝paypay_gateway.py／にゃんこ＝engine.py／
ツムツム＝tsum_service.py／ぷにぷに＝puni_service.py／Q&A＝qa_bot.py／店名など＝site_config.py／販売用zip＝source_pack.py
全体の説明は README.md、注文と支払いの決まりは docs/REBUILD_FOR_OTHERS.md。
"""
from __future__ import annotations

import base64
import hashlib
import datetime
import hmac
import importlib.metadata
import json
import multiprocessing
import os
import re
import secrets
import shutil
import threading
import tempfile
import time
import unicodedata
import uuid
import fcntl
from contextlib import ExitStack
from collections import deque
from types import SimpleNamespace

from flask import Flask, Response, g, jsonify, redirect, render_template_string, request, session
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as xml_escape

import engine
import item_catalog
import asyncio
import logging
import paypay_gateway
import kyash_gateway
import payments
import save_audit
import save_repair
import tsum_service
import puni_service
import qa_bot
import admin_2fa
import admin_ops
import human
import review_ai
import review_filter
import site_config
import source_pack
from pricing import (  # noqa: F401  料金の計算（pricing.py）。app.py からも同じ名前で使えるようにしている
    NOT_SERVICE_PREFIXES, SOURCE_KEEP_DAYS,
    SUPPORT_COUPON_DAYS, SUPPORT_COUPON_PCT, SUPPORT_MAX, SUPPORT_MIN, TICKET_MAX_DAYS, TICKET_ONCE_DAYS, ticket_period_price,
    COUPON_DAYS, COUPON_MIN, CRASH_ITEMS, DB_OFFSET, EV_PREFIX, EVENT_BY_ID, EVENT_MAPS, EVENT_SELECTABLE, PRICE_LABELS, PRICES,
    PT_KEY, PT_MAX_MINUTES, PT_MIN_MINUTES, RESTART_KEY, _ALL_EVENT_MAPS, _event_ids, _legend_kinds, _load_event_maps,
    _one_legend_tier, chara_units, compute_amount, compute_puni_amount, compute_tsum_amount,
)

APP_VERSION = "1.33"
GAME_DATA_VERSION = engine.GAME_DATA_VERSION
CAT_NAMES_CACHE_SCHEMA = 3   # 3: 配布元にまだ無い新キャラ（engine.SUPPLEMENT_CATS）を足すようにした

# ---------------------------------------------------------------- 1. 設定・料金の読み込み
def _read_admin_password() -> str:
    """ADMIN_PASSWORD_FILE があればそのファイルから読む（記号を含むパスワードでも安全に渡すため）。"""
    path = os.environ.get("ADMIN_PASSWORD_FILE", "")
    if path:
        try:
            with open(path, encoding="utf-8") as f:
                return f.read().rstrip("\r\n")
        except OSError:
            return ""
    return os.environ.get("ADMIN_PASSWORD", "")


ADMIN_PASSWORD = _read_admin_password()
PAYMENTS_ENABLED = os.environ.get("PAYMENTS_ENABLED", "1") != "0"  # 0 = 決済なし（動作確認用）
# 料金（円）
_PRICES_FILE = os.path.join(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay"), "prices.json")


def _load_prices() -> None:
    """管理画面で変更した料金を読み込む（なければ既定値のまま）。"""
    try:
        with open(_PRICES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        for k in PRICES:
            v = data.get(k)
            if isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 100000:
                PRICES[k] = v
    except (OSError, ValueError):
        pass


def _save_prices(new: dict) -> None:
    clean = {}
    for k in PRICES:
        if k not in new:  # 古い画面から送られた等で欄が無い → 現在の値のまま
            clean[k] = PRICES[k]
            continue
        raw = str(new.get(k, "")).strip()
        if not (raw.isascii() and raw.isdigit()):
            raise ValueError(f"「{PRICE_LABELS[k]}」は半角の数字で入力してください。")
        v = int(raw)
        if not 1 <= v <= 100000:
            raise ValueError(f"「{PRICE_LABELS[k]}」は 1〜100,000 円で入力してください。")
        clean[k] = v
    os.makedirs(os.path.dirname(_PRICES_FILE) or ".", exist_ok=True)
    tmp = _PRICES_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean, f)
    os.replace(tmp, _PRICES_FILE)
    PRICES.update(clean)  # 以後の新しい注文から反映（作成済みの注文の金額は変わらない）


_load_prices()
ORDER_TTL = 1800

import mimetypes
mimetypes.add_type("image/webp", ".webp")   # 本番のコンテナでは .webp の種類が登録されていないことがある
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024   # 送られてくる内容は最大2MB（巨大な入力でサーバーを固めさせない）
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("nyanko-web:" + ADMIN_PASSWORD).encode()  # ワーカープロセス間でも同じ値になるよう固定（本番は SECRET_KEY を設定）
).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("FLY_APP_NAME") or os.environ.get("SESSION_COOKIE_SECURE")),
                  PERMANENT_SESSION_LIFETIME=30 * 86400)

@app.context_processor
def _shop_context() -> dict:
    """すべての画面で使える値：店名・問い合わせ先など（site_config.py）。"""
    return dict(site_config.template_context(), csp_nonce=_csp_nonce(), turnstile_key=human.site_key(),
                pay_methods=payments.available_json,   # 支払いパネルで使える方法（呼んだときだけ読む）
                pay_notice=_pay_notice)


def _pay_notice():
    """「Kyashの決済に移行しました」のお知らせを出すか（管理者以外・Kyash を受け付けているときだけ）。
    出すなら {"paypay": PayPay もまだ使えるか}、出さないなら None。"""
    if not PAYMENTS_ENABLED or _is_admin():
        return None
    try:
        av = payments.available()
    except Exception:  # noqa: BLE001  お知らせのために画面を落とさない
        return None
    return {"paypay": av["paypay"], "off": payments.paypay_off_pct()} if av["kyash"] else None


def _csp_nonce() -> str:
    """この応答のスクリプトだけに付ける合い言葉（毎回変わる）。これが付いていないスクリプトは、ブラウザが動かさない。"""
    if not getattr(g, "csp_nonce", None):
        g.csp_nonce = secrets.token_urlsafe(16)
    return g.csp_nonce


try:        # 使っているサーバーソフトの名前を、応答ヘッダーで教えない
    import gunicorn.http.wsgi as _gunicorn_wsgi
    _gunicorn_wsgi.SERVER = "web"
except Exception:  # noqa: BLE001
    pass


# ---------------------------------------------------------------- 2. にゃんこのメニュー定義・キャラ名
ITEMS = [
    "猫缶", "XP", "NP", "にゃんチケ", "レアチケ", "プラチケ", "プラチナのかけら", "レジェチケ",
    "イベチケ&福チケ", "バトルアイテム", "ネコビタン", "城素材", "キャッツアイ",
    "マタタビ", "本能玉", "リーダーシップ", "地底メダル",
]
SUBS = [
    "全キャラ解放&エラーキャラ削除", "全キャラレベルMAX", "全キャラ最高形態",
    "全キャラ本能解放", "全ステージクリア&全お宝金",
    "日本編 第1章クリア", "日本編 第2章クリア", "日本編 第3章クリア",
    "未来編 第1章クリア", "未来編 第2章クリア", "未来編 第3章クリア",
    "宇宙編 第1章クリア", "宇宙編 第2章クリア", "宇宙編 第3章クリア",
    "ゾンビステージクリア",
    "旧レジェンド 冠1クリア", "旧レジェンド 冠2までクリア", "旧レジェンド 冠3までクリア", "旧レジェンド 冠4までクリア",
    "旧レジェンド イディまでクリア",
    "真レジェンド 冠1クリア", "真レジェンド 冠2までクリア", "真レジェンド 冠3までクリア", "真レジェンド 冠4までクリア",
    "真レジェンド ノーヴァまでクリア",
    "ゼロレジェンドステージクリア", "魔界編全クリア", "にゃんこ塔全クリア",
    "イベントステージ全クリア", "ガマトトLvMax", "ガマトト助手全員レジェンド化",
    "にゃんこ神社LvMax",
]
OTHERS = [
    "プレイ時間カンスト", "ゴールド会員化", "編成スロット最大",
    "にゃんこメダル全解放", "敵キャラ図鑑全埋め", "施設全強化", "ミッション全クリア",
    "ユーザーランク報酬全受け取り", "チュートリアルクリア",
    "ガマトトのクラッシュ修正", "オトート砲台のクラッシュ修正",
    "未所持キャラのガチャ・図鑑記録を整理",
]
_SUB_PRIORITY = {
    "全キャラ解放&エラーキャラ削除": 0,
    "全キャラ最高形態": 1,
    "全キャラレベルMAX": 2,
    **{k: (10 if v[0] == "old" else 11) for k, v in engine.LEGEND_TIERS.items()},   # 旧レジェンド → 真レジェンド の順
}


# bcsfe の配布元（PyPI / GitHub）の説明に「BANされることがある」と書かれている項目。
# これ以外が安全という意味ではない（公開情報が無いだけ）。
# プラチナのかけらは10個でプラチケ1枚になるため、プラチケと同じ扱いにする。
HIGH_RISK_ITEMS = ["猫缶", "レアチケ", "プラチケ", "プラチナのかけら", "レジェチケ"]

# ---------------------------------------------------------------- キャラ番号
# にゃんこDBの番号は1始まり（ネコ=1）。セーブ内部のIDは0始まり（ネコ=0）なので1引く。
MAX_CAT_NUMBER = 9999
MAX_CAT_COUNT = 2000


def parse_cat_numbers(text: str):
    """「1, 5, 100-120」のような入力を番号の一覧にする。戻り値は (番号リスト, エラー文 or None)。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"[~〜～ー−―‐–—]", "-", t)
    nums: set[int] = set()
    for tok in [x for x in re.split(r"[\s,、，;；・]+", t) if x]:
        m = re.fullmatch(r"(\d{1,6})(?:-(\d{1,6}))?", tok)   # 桁数を限る（巨大な数字の並びで固まらないように）
        if not m:
            return [], f"「{tok}」は読み取れません。"
        a = int(m.group(1))
        b = int(m.group(2)) if m.group(2) else a
        if a > b:
            a, b = b, a
        if a < 1 or b > MAX_CAT_NUMBER:
            return [], f"番号は1〜{MAX_CAT_NUMBER}の範囲で入力してください（「{tok}」）。"
        nums.update(range(a, b + 1))
        if len(nums) > MAX_CAT_COUNT:
            return [], f"一度に指定できるのは{MAX_CAT_COUNT}体までです。"
    return sorted(nums), None


def _compress(nums) -> str:
    """[1,2,3,7] → '1-3, 7'"""
    out, i, nums = [], 0, sorted(nums)
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        out.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(out)


# ---------------------------------------------------------------- キャラ名データ
_NAMES_FILE = "/tmp/bcsfe_cat_names.json"
# state: none=未取得 / loading=取得中 / ready=取得済み / error=取得できなかった
_names: dict = {"state": "none", "items": [], "method": None, "error": None,
                "version": None, "updated": 0.0, "refreshing": False, "tried": 0.0}
NAMES_TTL = 12 * 3600          # 名前データはこの時間ごとに自動で取り直す
_VERSION_FILE = "/tmp/bcsfe_game_version.json"
# 実行中に見かけた「より新しいゲームバージョン」を覚えておく（自動更新）
_learned: dict = {"version": None}
# bcsfe の互換チェック結果（None=未確認 / []=問題なし / [...]=足りない機能）
_api: dict = {"missing": None}
_catalog_lock = threading.RLock()


def _vkey(v) -> tuple:
    return engine.version_key(v)


def _effective_version() -> str:
    """名前データ・図鑑に使うゲームバージョン。固定値と「見かけた最新」の新しいほう。"""
    v = _learned["version"]
    return v if v and _vkey(v) > _vkey(GAME_DATA_VERSION) else GAME_DATA_VERSION


def _load_learned() -> None:
    try:
        with open(_VERSION_FILE, encoding="utf-8") as f:
            v = json.load(f).get("version")
        if v and engine.version_to_string(v):
            _learned["version"] = v
    except Exception:  # noqa: BLE001
        pass


def _learn_version(save) -> None:
    """処理したセーブのゲームバージョンが今より新しければ覚える。次回から名前データもその版で作る。"""
    with _catalog_lock:
        try:
            v = engine.version_to_string(getattr(save, "game_version", None))
            if not v or _vkey(v) <= _vkey(_effective_version()):
                return
            base = _vkey(GAME_DATA_VERSION)
            if _vkey(v)[0] > base[0] + 1:      # 桁違いに新しい値は、読み違いの可能性があるので採用しない
                return
            _learned["version"] = v
            with open(_VERSION_FILE, "w", encoding="utf-8") as f:
                json.dump({"version": v}, f)
        except Exception:  # noqa: BLE001
            pass


def _gv_for(version: str, fallback):
    try:
        return engine.core.GameVersion.from_string(version)
    except Exception:  # noqa: BLE001
        return fallback


def _bcsfe_version():
    try:
        return importlib.metadata.version("bcsfe")
    except Exception:  # noqa: BLE001
        return None


def _set_names(names: dict, method, version=None) -> None:
    with _catalog_lock:
        items = []
        for cid in sorted(names):
            forms = names[cid]
            items.append({"n": cid + DB_OFFSET, "name": forms[0], "alt": " / ".join(forms[1:])})
        # 配布元のデータにまだ無い新キャラ。解放できるのは、Ver16.0 以降のゲームのセーブ（引き継ぎ）のときだけ
        for cid, forms in sorted(engine.SUPPLEMENT_CATS.items()):
            if cid not in names:
                items.append({"n": cid + DB_OFFSET, "name": forms[0], "alt": " / ".join(forms[1:]), "new": 1})
        version = version or _effective_version()
        _names.update(state="ready", items=items, method=method, error=None,
                      version=version, updated=time.time(), refreshing=False)
        try:
            with open(_NAMES_FILE, "w", encoding="utf-8") as f:
                json.dump({"game_version": version, "cache_schema": CAT_NAMES_CACHE_SCHEMA, "items": items,
                           "method": method, "updated": _names["updated"]}, f, ensure_ascii=False)
        except Exception:  # noqa: BLE001
            pass


def _load_names_cache() -> None:
    try:
        with open(_NAMES_FILE, encoding="utf-8") as f:
            d = json.load(f)
        if (d.get("game_version") == _effective_version()
                and d.get("cache_schema") == CAT_NAMES_CACHE_SCHEMA
                and d.get("items")):
            _names.update(state="ready", items=d["items"], method=d.get("method"), error=None,
                          version=d.get("game_version"), updated=float(d.get("updated") or 0.0))
    except Exception:  # noqa: BLE001
        pass


def _snap(save) -> dict:
    """変更内容の表示用。取れなくても本処理は止めない。"""
    try:
        return engine.snapshot(save)
    except Exception:  # noqa: BLE001
        return {}


def _harvest_names(save) -> None:
    """実行中に読み込んだセーブから、ついでにキャラ名を控える（追加メモリ不要）。"""
    sv = engine.version_to_string(getattr(save, "game_version", None))
    if _names["state"] == "ready":
        # すでにある名前データより新しい版のセーブのときだけ、取り直す
        if not sv or not _names["version"] or _vkey(sv) <= _vkey(_names["version"]):
            return
    try:
        if not sv or engine.core.core_data.get_game_data_getter(save).version != sv:
            return
        names, method = engine.get_cat_names(save)
        if names:
            _set_names(names, method, sv or _effective_version())
    except Exception:  # noqa: BLE001
        pass


def _select_game_data(cc, gv_base, version: str):
    """Select an exact dataset without changing the save serialization version."""
    target = _gv_for(version, gv_base)
    getter = engine.core.GameDataGetter(cc, target, do_print=False)
    if getter.version != version:
        raise RuntimeError(f"ゲームデータ {version} は配布元で確認できません（取得可能: {getter.version or '不明'}）。")
    # BCSFE caches the first getter and parsed data globally. Reset them before
    # constructing a catalog for another version in the parent process.
    engine.core.core_data.init_data()
    engine.core.core_data.game_data_getter = getter
    return target


def _build_names(cc, gv_base, version: str):
    """Build the catalog from a fresh target-version save, not the template."""
    target = _select_game_data(cc, gv_base, version)
    save = engine.core.SaveFile(cc=cc, gv=target)
    engine.ensure_cat_roster(save)
    return engine.get_cat_names(save)


def _names_thread(keep: bool = False) -> None:
    """キャラ名一覧を作る。実行中のジョブとは同時に動かさない。
    keep=True は自動の取り直し：失敗しても今のデータはそのまま残す。"""
    if not _run_lock.acquire(blocking=False):
        if keep:
            _names["refreshing"] = False
        else:
            _names.update(state="none", error="他の処理が動いているため、あとでもう一度試します。")
        return
    try:
        cc, gv = _get_bcsfe()
        want = _effective_version()
        names, method, used = {}, None, want
        try:
            names, method = _build_names(cc, gv, want)
        except Exception:  # noqa: BLE001
            if want == GAME_DATA_VERSION:
                raise
        if not names and want != GAME_DATA_VERSION:       # 新しい版で作れなければ、固定バージョンで作り直す
            used = GAME_DATA_VERSION
            names, method = _build_names(cc, gv, used)
        if names:
            _set_names(names, method, used)
        elif not keep:
            _names.update(state="error", error="このbcsfeのバージョンではキャラ名を取り出せませんでした。番号で入力してください。")
    except Exception as e:  # noqa: BLE001
        if not keep:
            _names.update(state="error", error="キャラ名の取得に失敗しました：" + str(e))
    finally:
        _run_lock.release()            # 先にロックを返してから「更新中ではない」にする
        _names["refreshing"] = False


def _start_names() -> None:
    if _names["state"] in ("none", "error"):
        _names.update(state="loading", error=None)
        threading.Thread(target=_names_thread, daemon=True).start()
    elif _names["state"] == "ready" and not _names["refreshing"]:
        # 自動更新：一定時間たった、または新しいゲームバージョンを見かけたら、裏で取り直す
        now = time.time()
        stale = (now - (_names["updated"] or 0) > NAMES_TTL
                 or _names["version"] != _effective_version())
        # 取り直しに失敗し続けても、30分に1回しか試さない
        if stale and now - (_names["tried"] or 0) > 1800:
            _names["tried"] = now
            _names["refreshing"] = True
            threading.Thread(target=_names_thread, kwargs={"keep": True}, daemon=True).start()


def _latest_catalog_thread() -> None:
    """Publish a verified catalog only after the entire build succeeds.

    The endpoint reserves _run_lock before starting this thread. Existing jobs
    use separate processes; this lock protects the parent's BCSFE global state.
    """
    try:
        cc, gv = _get_bcsfe()
        response = engine.core.RequestHandler(engine.core.GameDataGetter.repo_url()).get()
        if response is None or response.status_code != 200:
            raise RuntimeError("配布元に接続できませんでした。時間をおいて試してください。")
        metadata = response.json()
        versions = engine.core.GameDataGetter.get_versions(metadata).get(cc.get_code(), {})
        candidates = [v for v in versions if isinstance(v, str) and engine.version_to_string(v)]
        if not candidates:
            raise RuntimeError("日本版の配布データが見つかりませんでした。")
        version = max(candidates, key=_vkey)
        if _vkey(version) < _vkey(_effective_version()):
            raise RuntimeError("配布元のデータが現在の要求版より古いため、切り替えませんでした。")
        if _vkey(version)[0] > _vkey(GAME_DATA_VERSION)[0] + 1:
            raise RuntimeError("大きなバージョン変更のため、BCSFEの互換性確認が必要です。")
        names, method = _build_names(cc, gv, version)
        if not names:
            raise RuntimeError("キャラ名データを取得できませんでした。")
        with _catalog_lock:
            if _vkey(version) < _vkey(_effective_version()):
                raise RuntimeError("より新しい版のセーブを処理したため、更新を保留しました。")
            _learn_version(SimpleNamespace(game_version=version))
            _set_names(names, method, version)
        _catalog_update.update(status="ready", version=version, count=len(names), error=None)
    except Exception as exc:  # noqa: BLE001
        # Metadata/HTTP errors can contain URLs or bodies: expose only our messages.
        message = str(exc) if type(exc) is RuntimeError else "最新データの確認に失敗しました。現在のカタログを引き続き使用します。"
        _catalog_update.update(status="error", error=message)
    finally:
        _run_lock.release()


# ---------------------------------------------------------------- 3. 状態管理・実行枠
_started_at = time.time()
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()          # 親プロセス内でキャラ名の取得を重複させない
_init_lock = threading.Lock()
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "4"))
if MAX_CONCURRENT_JOBS < 1:
    raise ValueError("MAX_CONCURRENT_JOBS must be at least 1")
_slots: list[str | None] = [None] * MAX_CONCURRENT_JOBS      # ふつうの実行枠（にゃんこ・ツムツム・短いぷにぷに）
# 長時間レーン：ぷにぷにの回し放題・長い周回（puni_service.is_long）だけが使う、別の実行枠。
# 何時間も動く注文が、数分で終わる注文の枠を塞がないようにする。
# 周回中はほとんどが待ち時間で、1件あたりのメモリは約70MB。0 にすると、長時間もふつうの枠を使う。
LONG_JOB_SLOTS = max(0, int(os.environ.get("LONG_JOB_SLOTS", "3")))
_long_slots: list[str | None] = [None] * LONG_JOB_SLOTS
LONG_SLOT_BASE = 100   # 枠の番号：ふつうの枠は 0〜、長時間レーンは 100〜（作業フォルダやロックの名前に使う）


def _free_slot_locked(long: bool):
    """空いている枠の番号（無ければ None）。呼び出し側で _jobs_lock を持つこと。"""
    lane, base = (_long_slots, LONG_SLOT_BASE) if long and _long_slots else (_slots, 0)
    return next((base + i for i, value in enumerate(lane) if value is None and not _slot_held_elsewhere(base + i)), None)


_SLOT_DEFAULT = {"normal": MAX_CONCURRENT_JOBS, "long": LONG_JOB_SLOTS}   # 起動のときの数（.env）。管理画面で空欄にすると戻る
_slot_target = {"normal": None, "long": None}


def _resize_slots_locked() -> None:
    """実行枠の数を、管理画面で決めた数に合わせる。呼び出し側で _jobs_lock を持つこと。
    増やすのはすぐ。減らすのは、うしろの枠が空いてから（動いている実行は止めない）。"""
    global MAX_CONCURRENT_JOBS, LONG_JOB_SLOTS
    for lane, key, base in ((_slots, "normal", 0), (_long_slots, "long", LONG_SLOT_BASE)):
        if _slot_target[key] is None:     # 管理画面で決めていない＝起動のときの数のまま（触らない）
            continue
        want = max(1 if key == "normal" else 0, int(_slot_target[key]))
        while len(lane) < want:
            lane.append(None)
        while len(lane) > want and lane[-1] is None:
            lane.pop()
    MAX_CONCURRENT_JOBS, LONG_JOB_SLOTS = len(_slots), len(_long_slots)


def _apply_slot_settings() -> None:
    try:
        want = admin_ops.slots()
    except Exception:  # noqa: BLE001
        return
    with _jobs_lock:
        for key in ("normal", "long"):   # 空欄にした（初期値に戻す）ときは、起動のときの数を目標にする
            if want.get(key) is not None or _slot_target[key] is not None:
                _slot_target[key] = want.get(key) if want.get(key) is not None else _SLOT_DEFAULT[key]
        _resize_slots_locked()
        _start_queued_jobs_locked()


def _set_slot_locked(slot: int, value) -> None:
    """枠を使用中にする（value＝実行の番号）／空ける（None）。呼び出し側で _jobs_lock を持つこと。"""
    if slot >= LONG_SLOT_BASE:
        _long_slots[slot - LONG_SLOT_BASE] = value
    else:
        _slots[slot] = value


def _is_long(job_id: str) -> bool:
    return bool((_jobs.get(job_id) or {}).get("long"))
_active_transfers: set[bytes] = set()
MAX_QUEUED_JOBS = 20
QUEUE_TTL = 15 * 60
LONG_QUEUE_TTL = 6 * 3600        # 長時間レーンの順番待ちは、前の人が終わるまで待てるよう長くする
_pending_jobs = deque()  # Arguments stay in memory only while waiting.
_cleanup_lock = threading.Lock()
_cleanup_started = False
_worker_pipe = None
_last_worker_catalog = None
_last_worker_version = None
_bcsfe_base = "/tmp/bcsfe"
_bcsfe_ready: tuple | None = None
# ゲームデータ（bcsfe）の準備状況。idle=未準備 / loading=準備中 / ready=OK / error=失敗
_bcsfe_state: dict = {"status": "idle", "error": None, "since": None, "took": None}
_warm_cache: dict = {"status": "idle", "prepared": 0, "version": None, "error": None}
_warm_cache_lock = threading.Lock()
_catalog_update = {"status": "idle", "version": None, "count": 0, "error": None}
_load_learned()                        # 前回までに見かけた最新ゲームバージョン
_load_names_cache()                   # 前回取得したキャラ名があれば読み込む
JOB_TTL = 30 * 60                     # 結果は30分で消す
TOTAL_STEPS = 4
TSUM_TOTAL_STEPS = 6                  # ツムツム代行の段取り（回線→ログイン→接続→取得→実行→発行）
TSUM_CAPTCHA_WAIT = 240               # 画像認証（CAPTCHA）の入力を待つ秒数
PUNI_TOTAL_STEPS = 10                 # ぷにぷに代行の段取り（受付→ログイン→実行→完了）
_job_pipes: dict[str, object] = {}    # 実行中のジョブへ送る口（画像認証の答えを渡すのに使う）
_job_pipes_lock = threading.Lock()


def _parallel_state_locked() -> dict:
    """Return an atomic, privacy-safe snapshot of the shared execution slots.

    _slots is also used briefly by warmup. Keep warmup reservations separate
    from actual worker processes so the UI never labels preparation as a job.
    The caller must hold _jobs_lock.
    """
    states = []
    running = warmup = occupied = 0
    normal = _slots
    long_running = sum(1 for value in _long_slots if value is not None)
    queued_long = sum(1 for entry in _pending_jobs if _is_long(entry[0]))
    for index, value in enumerate(normal, 1):
        if value is None:
            state = "free"
        elif value == "warmup":
            state = "warmup"
            warmup += 1
            occupied += 1
        else:
            state = "running"
            running += 1
            occupied += 1
        states.append({"slot": index, "state": state})
    return {
        "active_jobs": running,
        "running_jobs": running,
        "warmup_slots": warmup,
        "occupied_slots": occupied,
        "available_slots": len(normal) - occupied,
        "max_concurrent_jobs": len(normal),
        "slot_states": states,
        # 順番待ちは、ふつうの枠の分だけ数える（長時間レーンの待ちで「混雑中」と見せない・デプロイも待たせない）
        "queued_jobs": len(_pending_jobs) - queued_long,
        "long_running_jobs": long_running, "long_slots": LONG_JOB_SLOTS, "long_queued_jobs": queued_long,
    }

def _gc_jobs() -> None:
    now = time.time()
    with _jobs_lock:
        for entry in list(_pending_jobs):
            job_id, _, transfer_key = entry
            if now - _jobs[job_id]["created"] >= (LONG_QUEUE_TTL if _is_long(job_id) else QUEUE_TTL):
                _pending_jobs.remove(entry)
                if transfer_key is not None:
                    _active_transfers.discard(transfer_key)
                _jobs[job_id].update(status="error", error="待機時間が長いため処理を中止しました。", finished=now)
        for k in [k for k, v in _jobs.items() if v["finished"] is not None and now - v["finished"] > JOB_TTL]:
            del _jobs[k]


@app.before_request
def _ensure_cleanup() -> None:
    global _cleanup_started
    with _cleanup_lock:
        if _cleanup_started:
            return
        _cleanup_started = True
        def cleanup_loop():
            while True:
                time.sleep(1)
                _gc_jobs()
                try:
                    _reconcile_orders()
                except Exception:
                    pass
        threading.Thread(target=cleanup_loop, daemon=True).start()
        threading.Thread(target=_hold_watcher, daemon=True).start()
        threading.Thread(target=_kyash_keeper, daemon=True).start()
        _apply_slot_settings()   # 管理画面で変えた実行枠の数（再起動しても続く）


PAGE_SHOP = {"/": None, "/nyanko": "nyanko", "/tsum": "tsum", "/puni": "puni", "/ticket": "ticket", "/support": "support",
             "/source": "source", "/reviews": None, "/box": None}
_BANNER_STYLE = ("display:block;margin:0;padding:9px 14px;font:700 14px/1.55 system-ui,-apple-system,'Hiragino Sans',sans-serif;"
                 "text-align:center;border-bottom:2px solid #17181c;word-break:break-word;white-space:pre-wrap;")
_BANNER_COLORS = {"info": "background:#e7f0ff;color:#17181c", "warn": "background:#fff3dc;color:#17181c",
                  "sale": "background:#ffe3ec;color:#17181c", "pause": "background:#fdebeb;color:#17181c"}


def _page_banners(path: str) -> str:
    """お店の画面の上に出す帯：お知らせ・セール・一時停止中のもの（管理画面で決める）。文字はすべてエスケープする。"""
    from markupsafe import escape
    try:
        shop = PAGE_SHOP[path]
        bars = []
        n = admin_ops.notice()
        if n:
            bars.append(("warn" if n["level"] == "warn" else "info", "📢 " + n["text"]))
        off_pct = payments.paypay_off_pct()
        if off_pct and shop != "support":
            bars.append(("sale", f"🎉 PayPay休止中につき、全品{off_pct}%オフ！お支払いの金額から自動で割り引きます（支援BOXは対象外）"))
        sale = admin_ops.sale()
        if sale and (shop is None or shop in sale["shops"]):
            names = "・".join(admin_ops.SHOP_LABELS[x] for x in sale["shops"])
            bars.append(("sale", f"🎉 {sale.get('label') or 'セール'}：{names}が {sale['pct']}%引き（{_jst(sale['until'])} まで）。"
                                 "お支払いの金額から自動で割り引きます"))
        if shop:
            if admin_ops.paused_shop(shop):
                bars.append(("pause", "⏸ " + (admin_ops.paused_message() or f"{admin_ops.SHOP_LABELS[shop]}は、ただいま受付を一時停止しています。")))
            else:
                names = dict(_pausable_menus())
                prefix = {"nyanko": "nyanko:", "tsum": "tsum_", "puni": "puni_"}.get(shop)
                stop = [names[m].split("：", 1)[-1] for m in admin_ops.paused_menus() if prefix and m.startswith(prefix) and m in names]
                if stop:
                    bars.append(("pause", "⏸ 調整中のため受付を止めています：" + "・".join(stop)
                                 + ("\n" + admin_ops.paused_message() if admin_ops.paused_message() else "")))
    except Exception:  # noqa: BLE001  帯が作れなくても、画面は出す
        _log_orders.exception("page banner failed")
        return ""
    return "".join(f'<div class="ops-bar" role="status" style="{_BANNER_STYLE}{_BANNER_COLORS[kind]}">{escape(text)}</div>' for kind, text in bars)


@app.after_request
def _private_response(response):
    if (request.path == "/admin" or request.path.startswith(("/api/", "/status/", "/admin/"))
            or request.path == "/health" or response.status_code >= 400):
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
    html = (response.mimetype or "") == "text/html"
    if request.path.startswith(("/status/", "/api/", "/admin/")) or html:
        # 画面と API の中身は、端末にもブラウザの「戻る」用の控えにも残さない（結果のコードが保存されないように）
        response.headers.setdefault("Cache-Control", "no-store")
    if html:
        # 中身のセキュリティ方針（CSP）：自分のサイトのファイルと、合い言葉つきのスクリプトだけを動かす。
        # ほかのサイトのスクリプト・埋め込み・送信先はすべて止める（書き込まれた文字がスクリプトとして動く攻撃を防ぐ）
        nonce = getattr(g, "csp_nonce", None)
        cf = " " + human.WIDGET_ORIGIN if human.enabled() else ""    # 人間確認（Turnstile）のチェック画面だけは通す
        response.headers.setdefault("Content-Security-Policy", "; ".join((
            "default-src 'self'", "script-src 'self'" + (f" 'nonce-{nonce}'" if nonce else "") + cf,
            "frame-src" + (cf or " 'none'"),
            "style-src 'self' 'unsafe-inline'", "img-src 'self' data: blob:", "connect-src 'self'",
            "object-src 'none'", "base-uri 'none'", "form-action 'self'", "frame-ancestors 'none'")))
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=()")
    if (html and request.method == "GET" and response.status_code == 200 and request.path in PAGE_SHOP
            and not response.direct_passthrough and not response.is_streamed):
        bars = _page_banners(request.path)
        if bars:
            body = response.get_data(as_text=True)
            body, hit = re.subn(r"(<body\b[^>]*>)", lambda m: m.group(1) + bars, body, count=1)
            if hit:
                response.set_data(body)
    # 基本の守り：ほかのサイトの枠の中に表示させない・種類のなりすましをさせない・行き先に URL の詳細を渡さない
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if request.headers.get("X-Forwarded-Proto") == "https":   # 本番（Caddy 経由の HTTPS）だけ：以後は必ず HTTPS で来てもらう
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return response


def _log(job: dict, text: str) -> None:
    """Keep the user-facing log concise, bounded, and free of repeated lines."""
    entries = job.setdefault("log", [])
    if entries and entries[-1].endswith(text):
        return
    entries.append(time.strftime("%H:%M:%S ") + text)
    if len(entries) > 60:
        del entries[:-60]
    _publish_job(job)


def _report_save_audit(job: dict, audit, detailed: bool) -> None:
    """Log full audit findings only in explicit integrity-check mode.

    Ordinary runs still perform the complete audit and stop on critical
    corruption, but keep advisory findings out of the user-facing log.
    """
    findings = list(audit.findings)
    if detailed:
        _log(job, f"保全検査: キャラ {audit.cats_checked} 件 / "
                  f"重大 {audit.critical_count} 件 / 注意 {audit.warning_count} 件")
        for finding in findings:
            if finding.severity == "warning":
                job["warn"].append("セーブ構造検査: " + finding.message)
    else:
        visible = [item for item in findings if item.severity == "critical"]
        if not visible:
            return
        _log(job, f"重大なデータ異常を {len(visible)} 件検出しました")

    # Only actionable critical findings get individual log lines.
    for finding in findings:
        if finding.severity == "critical":
            _log(job, f"異常 {finding.path}: {finding.message}")


def _step(job: dict, n: int, text: str) -> None:
    job["step"] = n
    _log(job, f"[{n}/{TOTAL_STEPS}] {text}")


def _get_bcsfe():
    """bcsfeの初期化は重いので、最初の1回だけ行う。"""
    global _bcsfe_ready
    with _init_lock:
        if _bcsfe_ready is None:
            t0 = time.time()
            _bcsfe_state.update(status="loading", error=None, since=t0, took=None)
            try:
                _bcsfe_ready = engine.bcsfe_init(_bcsfe_base)
            except Exception as e:  # noqa: BLE001
                _bcsfe_state.update(status="error", error=str(e))
                raise
            _bcsfe_state.update(status="ready", took=round(time.time() - t0, 1))
            try:
                _api["missing"] = engine.check_bcsfe_api()
            except Exception:  # noqa: BLE001
                _api["missing"] = None
        return _bcsfe_ready


def _warmup_thread() -> None:
    try:
        with _run_lock:
            cc, gv = _get_bcsfe()
            version = _effective_version()
            names, _ = _build_names(cc, gv, version)
            if not names:
                raise RuntimeError("キャラ名データを取得できませんでした。")
            source = os.path.join(engine.core.GameDataGetter.get_game_data_dir().to_str(), "jp", version)
            if not os.path.isfile(os.path.join(source, "downloaded")):
                raise RuntimeError("指定版のゲームデータを確認できませんでした。")
            root = os.environ.get("BCSFE_PARALLEL_ROOT", "/tmp/bcsfe/parallel")
            prepared = 0
            for slot in range(MAX_CONCURRENT_JOBS):
                with _jobs_lock:
                    if slot >= len(_slots) or _slots[slot] is not None or _pending_jobs:
                        continue
                    _slots[slot] = "warmup"
                try:
                    lock_dir = os.path.join(root, "locks")
                    os.makedirs(lock_dir, mode=0o700, exist_ok=True)
                    with open(os.path.join(lock_dir, f"slot-{slot}"), "a") as lock_file:
                        try:
                            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        except BlockingIOError:
                            continue
                        parent = os.path.join(root, str(slot), "data", "game_data", "jp")
                        os.makedirs(parent, exist_ok=True)
                        destination = os.path.join(parent, version)
                        if not os.path.exists(destination):
                            with tempfile.TemporaryDirectory(dir=parent) as temp:
                                staging = os.path.join(temp, "data")
                                shutil.copytree(source, staging)
                                os.rename(staging, destination)
                        if os.path.isfile(os.path.join(destination, "downloaded")):
                            prepared += 1
                finally:
                    with _jobs_lock:
                        _slots[slot] = None
                        _start_queued_jobs_locked()
            _warm_cache.update(status="ready", prepared=prepared, version=version, error=None)
    except Exception as exc:  # noqa: BLE001
        _warm_cache.update(status="error", error=str(exc))
    finally:
        _warm_cache_lock.release()


def _publish_job(job: dict) -> None:
    """Send progress and any new catalog data from a worker to the web process."""
    global _last_worker_catalog, _last_worker_version
    if _worker_pipe is None:
        return
    message = {"job": job, "bcsfe": dict(_bcsfe_state), "api": _api["missing"]}
    version = _learned["version"]
    if version != _last_worker_version:
        message["version"] = version
        _last_worker_version = version
    catalog = (_names["version"], _names["updated"])
    if _names["state"] == "ready" and catalog != _last_worker_catalog:
        message["catalog"] = dict(_names)
        _last_worker_catalog = catalog
    try:
        _worker_pipe.send(message)
    except (BrokenPipeError, EOFError, OSError):
        pass


def _lock_subject(job: dict, arguments: tuple) -> str:
    """同じアカウントを同時に処理しないための鍵。にゃんこは引継ぎコード、
    ツムツム・ぷにぷにはログインID。"""
    if job.get("mode") in ("tsum", "puni"):
        payload = arguments[0] if arguments else {}
        login = str((payload or {}).get("login") or "")
        # 新しいアカウントを作る注文はログインIDが無い。空のまま鍵にすると、作成の注文どうしが1件ずつしか動かなくなる
        return f"{job['mode']}:{login}" if login else ""
    return str(arguments[0] or "") if arguments else ""


def _tsum_captcha(job: dict, img_bytes: bytes) -> str:
    """画像認証（CAPTCHA）を利用者に見せて、入力された文字を受け取る。
    画像はジョブに持たせて画面へ流し、答えは親プロセスからパイプで届く。"""
    if _worker_pipe is None:
        return ""
    try:
        image = "data:image/png;base64," + base64.b64encode(img_bytes or b"").decode("ascii")
    except Exception:  # noqa: BLE001
        return ""
    try:   # 前の画像への答えが遅れて届いていたら捨てる（今回の画像の答えと取り違えない）
        while _worker_pipe.poll(0):
            _worker_pipe.recv()
    except (EOFError, OSError):
        return ""
    ask_id = secrets.token_hex(6)
    job["captcha"] = {"id": ask_id, "image": image, "asked": time.time(), "wait": TSUM_CAPTCHA_WAIT}
    _log(job, "画像認証（CAPTCHA）が出ました。画面に出た文字を入力してください。")
    text = ""
    deadline = time.time() + TSUM_CAPTCHA_WAIT
    try:
        while time.time() < deadline:
            if _worker_pipe.poll(1.0):
                message = _worker_pipe.recv()
                if isinstance(message, dict) and "captcha" in message and message.get("id") == ask_id:
                    text = str(message.get("captcha") or "").strip()[:32]
                    break
    except (EOFError, OSError):
        pass
    job["captcha"] = None
    if not text:
        _log(job, "画像認証の入力がありませんでした。")
    _publish_job(job)
    return text


def _run_tsum_job(job: dict, payload: dict) -> None:
    """ツムツム代行1件を実行する（ワーカープロセスの中）。アカウント情報はログに出さない。"""
    def log(text: str) -> None:
        _log(job, str(text))

    def progress(step: int, text: str) -> None:
        job["step"] = max(0, min(int(step or 0), TSUM_TOTAL_STEPS))
        _log(job, str(text))

    def captcha(img_bytes):
        return _tsum_captcha(job, img_bytes)

    try:
        out = tsum_service.run_order(
            payload.get("mode", "line"), payload.get("login", ""), payload.get("password", ""),
            payload.get("menus") or [], payload.get("qty") or {},
            progress=progress, log=log, captcha=captcha,
        )
    except Exception as exc:  # noqa: BLE001  中身にアカウント情報が入りうるので種類だけ出す
        job.update(status="error", error=f"ツムツム代行の実行中にエラーが発生しました（{type(exc).__name__}）。"
                                        "もう一度お試しいただくか、注文番号を管理者にお伝えください。",
                   finished=time.time())
        _log(job, "エラー: 実行中に想定外のエラーが発生しました。")
        _publish_job(job)
        return
    job["tsum"] = {"lines": out["lines"], "moved": bool(out["moved"]), "done": int(out["done"]),
                   "menus": [tsum_service.menu_label(k) for k in (payload.get("menus") or [])]}
    if out["transfer"]:
        job["result"] = {"transfer": out["transfer"], "confirm": out["confirm"]}
    if out["ok"]:
        job.update(status="done", step=TSUM_TOTAL_STEPS)
        _log(job, "完了しました。")
    else:
        job["status"] = "error"
        job["error"] = out["error"] or "ツムツム代行を実行できませんでした。"
        # 引き継ぎでアカウントがこちらに移ったあとの失敗は、新しい引き継ぎ情報が結果欄に出る
        job["outcome_unknown"] = bool(out["moved"] and not out["transfer"])
        _log(job, "エラー: " + job["error"])
    job["finished"] = time.time()
    _publish_job(job)


def _run_puni_job(job: dict, payload: dict) -> None:
    """ぷにぷに代行1件を実行する（ワーカープロセスの中）。
    アカウント情報（メールアドレス・パスワード）はログに出さない。"""
    def log(text: str) -> None:
        _log(job, str(text))

    def progress(step: int, text: str) -> None:
        job["step"] = max(0, min(int(step or 0), PUNI_TOTAL_STEPS))
        _log(job, str(text))

    def checkpoint(state: dict) -> None:
        # 途中経過（済んだメニュー・周数・使った時間）。親プロセスが注文に写して保存する＝止まっても続きから再開できる
        job["checkpoint"] = state
        _publish_job(job)

    def should_stop() -> bool:
        if _parent_gone():   # サーバー本体が異常終了した：結果を返す先が無いまま何時間も回り続けない（続きは再開のときに）
            return True
        if _stop_requested() and not job.get("stopping"):
            job["stopping"] = True
            _log(job, "「途中で終了」を受け付けました。いまの1周が終わったら終了します。")
        return bool(job.get("stopping"))

    try:
        out = puni_service.run_order(
            payload.get("mode", "email"), payload.get("login", ""), payload.get("password", ""),
            payload.get("menus") or [], payload.get("qty") or {}, payload.get("stages") or {},
            progress=progress, log=log,
            resume=payload.get("resume"), checkpoint=checkpoint, should_stop=should_stop,
        )
    except Exception as exc:  # noqa: BLE001
        job.update(status="error", error=f"ぷにぷに代行の実行中にエラーが発生しました（{type(exc).__name__}）。"
                                        "もう一度お試しいただくか、注文番号を管理者にお伝えください。",
                   finished=time.time())
        _log(job, "エラー: 実行中に想定外のエラーが発生しました。")
        _publish_job(job)
        return
    job["puni"] = {"lines": out["lines"], "done": int(out["done"]),
                   "menus": [puni_service.menu_label(k) for k in (payload.get("menus") or [])]}
    if out.get("account"):
        job["result"] = {"puni_account": out["account"]}
    if out.get("ok"):
        job.update(status="done", step=PUNI_TOTAL_STEPS)
        _log(job, "完了しました。")
    else:
        job["status"] = "error"
        job["error"] = out["error"] or "ぷにぷに代行を実行できませんでした。"
        # 一部でも提供したあとの失敗は、最初からやり直すと提供しすぎになる（P-02）。
        # 続きからやり直せるもの（済んだ分を checkpoint に記録してある）は、払い直しなしの再実行に回す。
        # それ以外（アカウント作成の途中など）は「結果不明」にして、自動では再実行させない。
        job["outcome_unknown"] = bool((out.get("delivered") and not out.get("resumable"))
                                      or (out.get("moved") and not out.get("account")))
        _log(job, "エラー: " + job["error"])
    job["finished"] = time.time()
    _publish_job(job)


_worker_parent = {"pid": None}


def _parent_gone() -> bool:
    """実行用のプロセスの中で：起動したサーバー本体がもういないか（異常終了すると、親が別のプロセスに付け替わる）。"""
    return _worker_parent["pid"] is not None and os.getppid() != _worker_parent["pid"]


def _slot_held_elsewhere(slot: int) -> bool:
    """その実行枠の鍵を、まだ動いている前の実行（サーバー本体の異常終了で取り残されたもの）が握っているか。
    握られている枠に新しい実行を入れると、開始してすぐ失敗するので、空いていても選ばない。"""
    path = os.path.join(os.environ.get("BCSFE_PARALLEL_ROOT", "/tmp/bcsfe/parallel"), "locks", f"slot-{slot}")
    try:
        with open(path, "a") as lock_file:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(lock_file, fcntl.LOCK_UN)
    except OSError:
        pass
    return False


def _worker_entry(pipe, job, arguments, slot, learned_version, names):
    """Run BCSFE in its own process and its own persistent slot directory."""
    global _worker_pipe, _bcsfe_base, _NAMES_FILE, _VERSION_FILE
    _worker_pipe = pipe
    _worker_parent["pid"] = os.getppid()
    try:
        root = os.environ.get("BCSFE_PARALLEL_ROOT", "/tmp/bcsfe/parallel")
        _bcsfe_base = os.path.join(root, str(slot))
        os.makedirs(_bcsfe_base, exist_ok=True)
        lock_dir = os.path.join(root, "locks")
        os.makedirs(lock_dir, mode=0o700, exist_ok=True)
        with ExitStack() as locks:
            paths = [os.path.join(lock_dir, f"slot-{slot}")]
            subject = _lock_subject(job, arguments)
            if subject:
                digest = hmac.new(app.secret_key.encode(), subject.encode(), hashlib.sha256).hexdigest()
                paths.append(os.path.join(lock_dir, f"transfer-{digest}"))
            for path in paths:
                lock_file = locks.enter_context(open(path, "a"))
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("同じ実行枠または引継ぎコードの処理がまだ実行中です。") from exc

            if job.get("mode") == "tsum":
                # ツムツム代行は bcsfe（にゃんこのセーブ編集）を使わないので、その準備はしない
                _run_tsum_job(job, arguments[0])
                return
            if job.get("mode") == "puni":
                # ぷにぷに代行も bcsfe を使わない（ゲームサーバへ直接ログイン）
                _run_puni_job(job, arguments[0])
                return

            original_config = engine.core.Path.get_config_folder().to_str()
            os.environ["XDG_CONFIG_HOME"] = os.path.join(_bcsfe_base, "config-home")
            os.environ["XDG_STATE_HOME"] = os.path.join(_bcsfe_base, "state-home")
            isolated_config = engine.core.Path.get_config_folder().to_str()
            for source, destination in (
                (os.path.join(original_config, "max_values.json"), os.path.join(isolated_config, "max_values.json")),
                ("/tmp/bcsfe/config.yaml", os.path.join(_bcsfe_base, "config.yaml")),
            ):
                if os.path.isfile(source) and not os.path.exists(destination):
                    shutil.copy2(source, destination)
            data_dir = os.path.join(_bcsfe_base, "data")
            os.makedirs(data_dir, exist_ok=True)
            engine.core.set_data_dir_path(engine.core.Path(data_dir))
            _NAMES_FILE = os.path.join(_bcsfe_base, "cat_names.json")
            _VERSION_FILE = os.path.join(_bcsfe_base, "game_version.json")
            _learned["version"] = learned_version
            _names.update(names)
            _install_net_timeouts()
            _run_job(job, *arguments)
    except BaseException as exc:
        job.update(status="error", error=f"処理を開始できませんでした: {exc}", finished=time.time())
        _publish_job(job)
    finally:
        pipe.close()


def _merge_worker_state(message: dict) -> None:
    with _catalog_lock:
        state = message.get("bcsfe", {})
        if (state.get("status") in ("ready", "error")
                and _bcsfe_state["status"] != "loading"
                and not (state["status"] == "error" and _bcsfe_state["status"] == "ready")):
            _bcsfe_state.update(state)
        if message.get("api") is not None:
            _api["missing"] = message["api"]
        if message.get("version"):
            _learn_version(SimpleNamespace(game_version=message["version"]))
        catalog = message.get("catalog")
        if (catalog and catalog.get("items")
                and (_vkey(catalog.get("version")) > _vkey(_names.get("version"))
                     or (_vkey(catalog.get("version")) == _vkey(_names.get("version"))
                         and (catalog.get("updated") or 0) > (_names.get("updated") or 0)))):
            names = {item["n"] - DB_OFFSET: [item["name"]] +
                     (item["alt"].split(" / ") if item.get("alt") else [])
                     for item in catalog["items"]}
            _set_names(names, catalog.get("method"), catalog.get("version"))


def _finish_job(job_id: str, slot: int, transfer_key: bytes | None) -> None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job and job["status"] == "running":
            job.update(status="error", error=("コード発行中に処理プロセスが終了し、発行結果が不明です。再実行する前にゲーム側の状態を確認してください。"
                                                   if job.get("issuing_codes") else "処理プロセスが途中で終了しました。"),
                       outcome_unknown=bool(job.get("issuing_codes")), finished=time.time())
        _set_slot_locked(slot, None)
        if transfer_key is not None:
            _active_transfers.discard(transfer_key)
        _resize_slots_locked()
        _start_queued_jobs_locked()


def _start_queued_jobs_locked() -> None:
    """Called with _jobs_lock held; never persist queued credentials.
    順番待ちは先頭から見て、その実行の種類（ふつう／長時間）の枠が空いていれば始める。
    長時間レーンがいっぱいでも、うしろのふつうの注文は先に進める（逆も同じ）。"""
    for entry in list(_pending_jobs):
        job_id, arguments, transfer_key = entry
        job = _jobs[job_id]
        long = bool(job.get("long"))
        if time.time() - job["created"] >= (LONG_QUEUE_TTL if long else QUEUE_TTL):
            _pending_jobs.remove(entry)
            job.update(status="error", error="待機時間が長いため処理を中止しました。", finished=time.time())
            if transfer_key is not None:
                _active_transfers.discard(transfer_key)
            continue
        slot = _free_slot_locked(long)
        if slot is None:
            continue
        _pending_jobs.remove(entry)
        _set_slot_locked(slot, job_id)
        job["status"] = "running"
        try:
            _launch_job(job_id, job, arguments, slot, transfer_key)
        except Exception:
            _set_slot_locked(slot, None)
            if transfer_key is not None:
                _active_transfers.discard(transfer_key)
            job.update(status="error", error="処理を開始できませんでした。", finished=time.time())


def _monitor_worker(pipe, process, job_id: str, slot: int, transfer_key: bytes | None) -> None:
    with _job_pipes_lock:
        _job_pipes[job_id] = pipe
    try:
        while True:
            message = pipe.recv()
            _merge_worker_state(message)
            with _jobs_lock:
                if job_id in _jobs:
                    _jobs[job_id] = message["job"]
    except (EOFError, OSError):
        pass
    finally:
        with _job_pipes_lock:
            _job_pipes.pop(job_id, None)
        pipe.close()
        process.join()
        _finish_job(job_id, slot, transfer_key)


def _launch_job(job_id: str, job: dict, arguments: tuple, slot: int, transfer_key: bytes | None) -> None:
    # 双方向にしてあるのは、ツムツム代行の画像認証（CAPTCHA）の答えを実行中のプロセスへ渡すため。
    # にゃんこ代行の実行では親から何も送らないので、動きは以前と同じ。
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=True)
    with _catalog_lock:
        version, names = _learned["version"], dict(_names)
    process = ctx.Process(target=_worker_entry, args=(sender, job, arguments, slot, version, names))
    process.daemon = True
    try:
        process.start()
    except BaseException:
        receiver.close()
        sender.close()
        raise
    sender.close()
    threading.Thread(target=_monitor_worker, args=(receiver, process, job_id, slot, transfer_key), daemon=True).start()


# ---------------------------------------------------------------- 4. にゃんこの実行
def _apply_edits(job: dict, save, items, subs, others, qty, cat_nums, cat_settings=None) -> None:
    """選ばれた項目をセーブに書き込む（複製モードでは呼ばない）。"""
    def warn_failures(failures):
        for item in failures or []:
            message = f"「{item}」の適用に失敗した可能性があります。"
            job["warn"].append(message)
            _log(job, "注意: " + message)

    qty = dict(qty or {})
    pt_add = int(qty.pop(PT_KEY, 0) or 0)
    restart = RESTART_KEY in others
    ev_ids = _event_ids(others)
    others = [x for x in others if x != RESTART_KEY and not str(x).startswith(EV_PREFIX)]
    if ev_ids:                                       # イベント・コラボステージのクリア（前提が終わっていなければ、前提も一緒に）
        done = 0
        for i in ev_ids:
            m = EVENT_BY_ID[i]
            try:
                for req in m.get("reqs") or []:
                    if req not in ev_ids and not engine.event_map_cleared(save, req, EVENT_BY_ID[req]["stages"]):
                        engine.clear_event_map(save, req, EVENT_BY_ID[req]["stars"], EVENT_BY_ID[req]["stages"])
                        ev_ids.append(req)   # 同じ前提を二度クリアしない
                        job["warn"].append(f"「{EVENT_BY_ID[req]['name']}」が未クリアだったため、一緒にクリアしました（料金はかかりません）。")
                engine.clear_event_map(save, i, m["stars"], m["stages"])
                done += 1
            except Exception:  # noqa: BLE001
                job["warn"].append(f"「{m['name']}」をクリアにできませんでした。")
        _log(job, f"イベントステージ {done}件 をクリアにしました。")
        # クリアしたマップで手に入るドロップキャラ（1=キャラ本体、3=第3形態）も獲得済みにする
        base, third = [], []
        for i in ev_ids:
            for cid, form in EVENT_BY_ID[i].get("drops") or []:
                (third if int(form) >= 3 else base).append(int(cid))
        try:
            if base or third:
                r = engine.unlock_cats_by_ids(save, sorted(set(base + third)))
                if third:
                    engine.apply_cat_customizations(save, {c: {"form": 3} for c in sorted(set(third))})
                _log(job, f"ドロップキャラ：{len(r.get('unlocked', []))}体を獲得、第3形態 {len(set(third))}体。")
        except Exception:  # noqa: BLE001
            job["warn"].append("イベントステージのドロップキャラの一部を入れられなかった可能性があります。")
    if restart:                                      # 初期垢作成：リスタートパックと同じ中身を入れる（ほかの指定はこのあと上書き）
        rp = restart_pack()
        try:
            if rp["items"]:
                warn_failures(engine.apply_item_amounts(save, list(rp["items"]), quantity_map=rp["items"]))
            if rp["subs"]:                         # 日本編のクリアなど（お宝も金になる）
                warn_failures(engine.apply_subs_and_others(save, sorted(rp["subs"], key=lambda x: _SUB_PRIORITY.get(x, 99)), []))
            if rp["cats"]:
                ids = [n - DB_OFFSET for n in rp["cats"]]
                if rp["cat_level"]:
                    engine.apply_cat_customizations(save, {i: {"base_level": rp["cat_level"]} for i in ids})
                else:
                    engine.unlock_cats_by_ids(save, ids)
            if rp["facility_level"] and not engine.set_facility_levels(save, rp["facility_level"]):
                job["warn"].append("施設のレベルを上げられなかった可能性があります。")
            _log(job, "初期垢作成：リスタートパックの内容を入れました。")
        except Exception:  # noqa: BLE001
            job["warn"].append("リスタートパックの内容の一部を入れられなかった可能性があります。")
    warn_failures(engine.apply_item_amounts(save, items))
    if qty:                                          # 数量指定（同じ項目は数量指定を優先）
        warn_failures(engine.apply_item_amounts(save, list(qty), quantity_map=qty))
    kinds = _legend_kinds(subs)
    if "uncanny" in kinds and "old" not in kinds:   # 真レジェンドだけ：旧レジェンドの冠1が本当に終わっているか、セーブで確かめる
        try:
            if not engine.old_legend_crown1_done(save):
                engine.clear_legend_crowns(save, "old", 1)
                _log(job, "旧レジェンドの冠1が終わっていなかったので、真レジェンドが遊べるよう一緒にクリアしました（料金はかかりません）。")
                job["warn"].append("旧レジェンドの冠1が終わっていなかったため、一緒にクリアしました。")
        except Exception:  # noqa: BLE001
            job["warn"].append("旧レジェンドの冠1が終わっているか確かめられませんでした。")
    subs_sorted = sorted(subs, key=lambda x: _SUB_PRIORITY.get(x, 99))
    warn_failures(engine.apply_subs_and_others(save, subs_sorted, others))
    if pt_add:                                       # プレイ時間を増やす（セーブは1/30秒単位。上限で止める）
        try:
            op = save.officer_pass
            before = int(op.play_time or 0)
            op.play_time = min(2147483647, before + pt_add * 60 * 30)
            _log(job, f"プレイ時間を {pt_add // 60}時間{pt_add % 60}分 増やしました。")
        except Exception:  # noqa: BLE001
            warn_failures(["プレイ時間を増やす"])

    if cat_nums:                                     # 番号指定のキャラ解放・個別強化
        try:
            settings = cat_settings or {}
            if settings:
                r = engine.apply_cat_customizations(save, settings)
            else:
                r = engine.unlock_cats_by_ids(save, [n - DB_OFFSET for n in cat_nums])
        except Exception:  # キャラ解放だけ失敗しても、他の変更は止めない
            r = None
            msg = "キャラ解放に失敗しました（他の項目は反映済み）。"
            job["warn"].append(msg)
            _log(job, "注意: " + msg)
        if r is not None:
            if r["failed"]:
                label = "キャラ解放・個別設定" if settings else "キャラ解放"
                msg = f"{label}に失敗: {_compress([i + DB_OFFSET for i in r['failed']])}"
                job["warn"].append(msg)
                _log(job, "注意: " + msg)
            newest = [i for i in r["missing"] + r["hidden"] if i in engine.SUPPLEMENT_CATS]
            if newest:                               # 新キャラ：セーブが古い版のゲームのもの → 最新版にしてから
                r["missing"] = [i for i in r["missing"] if i not in newest]
                r["hidden"] = [i for i in r["hidden"] if i not in newest]
                msg = (f"新キャラ {_compress([i + DB_OFFSET for i in newest])} は、ゲームを最新版"
                       f"（Ver{engine.SUPPLEMENT_MIN_VERSION.rsplit('.0', 1)[0]}以降）にしたアカウントでだけ解放できます。今回は解放していません。")
                job["warn"].append(msg)
                _log(job, "注意: " + msg)
            for key, label in (
                ("missing", "セーブに存在しない番号"),
                ("error_chars", "エラーキャラのため除外"),
                ("hidden", "図鑑に無いため除外"),
            ):
                if r[key]:
                    msg = f"{label}: {_compress([i + DB_OFFSET for i in r[key]])}"
                    job["warn"].append(msg)
            _log(job, f"指定キャラ: 解放 {len(r['unlocked'])}体・既所持 {len(r['already'])}体")
            if settings:
                _log(job, f"個別強化: レベル {len(r.get('leveled', []))}体 / 形態 {len(r.get('formed', []))}体 / 本能 {len(r.get('talented', []))}体")
                if r.get("unsupported"):
                    msg = f"非対応項目をスキップ: {_compress([i + DB_OFFSET for i in r['unsupported']])}"
                    job["warn"].append(msg)
                    _log(job, "注意: " + msg)
    engine.clear_beacon_events(save)
    engine.clear_item_packs(save)
    engine.clear_scheme_items(save)
    engine.clear_all_ads_and_popups(save)


# ゲームのサーバーとの通信: bcsfe はセーブのアップロードを「待ち時間の上限なし」で行うため、
# 接続が途中で止まると実行が永久に終わらない。上限を付け、失敗が「接続の段階」か「送ったあと」かを記録する。
_NET = {"last_fail": None}   # None / "connect"（何も送っていない）/ "after_send"（送った後に失敗＝結果不明）
NET_CONNECT_TIMEOUT = 20
NET_READ_TIMEOUT = 120  # TLSのハンドシェイクもこの値で打ち切られる


def _install_net_timeouts() -> None:
    import requests
    if getattr(requests, "_nyanko_timeouts", False):
        return
    def wrap(fn):
        def inner(*args, **kwargs):
            if kwargs.get("timeout") is None:
                kwargs["timeout"] = (NET_CONNECT_TIMEOUT, NET_READ_TIMEOUT)
            try:
                response = fn(*args, **kwargs)
            except (requests.exceptions.ConnectTimeout, requests.exceptions.SSLError):
                _NET["last_fail"] = "connect"
                raise
            except requests.exceptions.ConnectionError as exc:
                text = repr(exc)
                _NET["last_fail"] = "connect" if ("NewConnectionError" in text or "Failed to establish" in text
                                                  or "Name or service not known" in text) else "after_send"
                raise
            except requests.exceptions.Timeout as exc:
                # TLSのハンドシェイク中に止まった場合も ReadTimeout になる。原因の連鎖に "handshake" があれば、まだ何も送っていない。
                chain, c = [], exc
                while c is not None and len(chain) < 8:
                    chain.append(str(c)); c = c.__cause__ or c.__context__
                _NET["last_fail"] = "connect" if any("handshake" in x.lower() for x in chain) else "after_send"
                raise
            _NET["last_fail"] = None
            return response
        return inner
    requests.post, requests.get = wrap(requests.post), wrap(requests.get)
    requests._nyanko_timeouts = True


def _issue_codes_once(job: dict, handler, label: str):
    # BCSFE itself retries unless tries=1. Do not repeat a write whose result
    # may have been lost after the server accepted it.
    # ただし「接続の段階で失敗した（何も送っていない）」ときだけは、最大3回まで安全にやり直す。
    job["issuing_codes"] = True
    _publish_job(job)
    for attempt in range(3):
        _NET["last_fail"] = None
        try:
            codes = handler.get_codes(upload_managed_items=False, tries=1)
            if codes:
                job["issuing_codes"] = False
                return codes
        except Exception:  # noqa: BLE001
            pass  # Never expose a transport exception containing request data.
        if _NET["last_fail"] == "connect" and attempt < 2:
            _log(job, "ゲームのサーバーにつながりませんでした（データは送られていません）。少し待ってやり直します…")
            _publish_job(job)
            time.sleep(10 * (attempt + 1))
            continue
        break
    job["issuing_codes"] = False
    if _NET["last_fail"] == "connect":
        # 3回とも「接続の段階」で失敗＝何も送っていない。結果不明にはせず、支払い済みのまま再実行できるようにする。
        raise RuntimeError(label + "：ゲームのサーバーにつながらず、コードを発行できませんでした（データは送っていません）。"
                           "お支払いは有効です。少し待ってから、再送金なしでもう一度実行できます。")
    job["outcome_unknown"] = True
    raise RuntimeError(label + "のコード発行結果を確認できませんでした。サーバー側で処理済みの可能性があるため、自動再試行はしていません。再実行する前にゲーム側の状態を確認してください。")


def _run_job(job: dict, transfer: str, confirm: str, items, subs, others, qty=None, cat_nums=None,
             cat_settings=None, dry=False, clone=False, integrity_only=False) -> None:
    qty = qty or {}
    cat_nums = cat_nums or []
    cat_settings = cat_settings or {}
    core = engine.core
    try:
        _step(job, 1, "ゲームデータを準備中…（初回は時間がかかります）")
        if clone:
            _log(job, "独立アカウント複製の実験モードです。元と複製を別々のアカウントとして登録します。")
        if dry:
            _log(job, "試運転です。コードは発行せず、変更内容だけ確認します。")
        if job["skipped"]:
            msg = "、".join(job["skipped"])
            _log(job, f"安全モード: {msg} はスキップします。")
            job["warn"].append(f"安全モードのためスキップ: {msg}")
        cc, gv = _get_bcsfe()
        catalog_version = _names.get("version")
        if (not clone and not integrity_only and _names["state"] == "ready"
                and _vkey(catalog_version) > _vkey(GAME_DATA_VERSION)):
            gv = _select_game_data(cc, gv, catalog_version)

        if not confirm:
            _step(job, 2, "初期アカウントを準備中…")
            handler = engine.build_initial_account_handler(cc)
            if handler is None:
                save_new = core.SaveFile(cc=cc, gv=gv)
                handler = core.ServerHandler(save_new, print=False)
                if not handler.create_new_account() or not handler.save_file:
                    raise RuntimeError("初期垢の生成に失敗しました（サーバーとの通信に失敗）。")
            elif not handler.save_file:
                raise RuntimeError("テンプレートセーブの読み込みに失敗しました。")
        else:
            _step(job, 2, "セーブデータをダウンロード中…")
            handler = None
            for attempt in range(3):
                try:
                    h, _ = core.ServerHandler.from_codes(transfer, confirm, cc, gv)
                    if h and h.save_file:
                        handler = h
                        break
                except Exception:
                    pass
                if attempt < 2:
                    _log(job, f"再試行します（{attempt + 1}/2）")
                    time.sleep(3)
            if handler is None:
                raise RuntimeError("ダウンロード失敗：引継ぎコードまたは暗証番号が違うか、サーバーに繋がりませんでした。")

        save = handler.save_file

        # Read-only structural preflight. Never delete or rewrite records here.
        audit = save_audit.audit_save(save, known_error_cat_ids=engine.ERROR_CAT_IDS)
        job["audit"] = audit.as_dict()
        _report_save_audit(job, audit, detailed=integrity_only)
        if not audit.ok:
            raise RuntimeError(
                f"セーブデータに重大な構造異常が {audit.critical_count} 件あるため、"
                "安全のため変更・アップロードを中止しました。元データは変更していません。"
            )

        before = _snap(save)
        _learn_version(save)             # より新しいゲームバージョンのセーブなら覚える（自動更新）
        if not clone and not integrity_only:  # 保全専用モードでは図鑑枠を追加しない
            engine.ensure_cat_roster(save)
        _harvest_names(save)

        clone_handler = None
        if clone:
            _step(job, 3, "元セーブを独立した複製データに変換中…")
            serialized = save.to_data()
            cloned_save = core.SaveFile(core.Data(serialized.get_bytes()), cc=cc)
            if cloned_save is save or cloned_save.data is save.data:
                raise RuntimeError("独立したセーブデータを作成できませんでした。")
            if cloned_save.inquiry_code != save.inquiry_code:
                raise RuntimeError("複製前の問い合わせコードが一致しません。")
            clone_handler = core.ServerHandler(cloned_save, print=False)
            _log(job, "元データと複製データを別インスタンスとして作成しました。")
        else:
            if integrity_only:
                _step(job, 3, "セーブの検査と既知異常の確認中…（バン保証）")
            else:
                _step(job, 3, "データを書き換え中…")
                visibility_repair_requested = (
                    "未所持キャラのガチャ・図鑑記録を整理" in others
                )
                ordinary_others = [
                    item for item in others
                    if item != "未所持キャラのガチャ・図鑑記録を整理"
                ]
                _apply_edits(job, save, items, subs, ordinary_others, qty, cat_nums, cat_settings)

                if visibility_repair_requested:
                    _step(job, 3, "未所持キャラのガチャ・図鑑記録を整理中…")
                    def _serialize_visibility_save(target):
                        return target.to_data().get_bytes()

                    def _deserialize_visibility_save(payload):
                        return core.SaveFile(core.Data(payload), cc=cc)

                    try:
                        verified_save, visibility_result, visibility_audit = (
                            save_repair.repair_unowned_visibility_with_roundtrip_verification(
                                save,
                                _serialize_visibility_save,
                                _deserialize_visibility_save,
                                save_audit.audit_save,
                            )
                        )
                        handler.save_file = verified_save
                        save = verified_save
                        job["visibility_repair"] = visibility_result.as_dict()
                        job["visibility_post_audit"] = visibility_audit.as_dict()
                        _log(
                            job,
                            "未所持キャラの記録を整理しました: "
                            f"対象確認 {visibility_result.checked_unowned}体 / "
                            f"変更 {visibility_result.changed_count}体 "
                            f"(ガチャ {visibility_result.gacha_flags_cleared}件、"
                            f"図鑑 {visibility_result.encyclopedia_flags_cleared}件) / "
                            f"判定保留 {visibility_result.skipped_ambiguous}体。"
                        )
                        _log(job, "バックアップ・再読み込み・所持キャラ状態確認・構造検査に合格しました。")
                    except save_repair.RepairVerificationError as repair_error:
                        if repair_error.restored_save is not None:
                            handler.save_file = repair_error.restored_save
                            _log(job, "修復前のバックアップからセーブを復元しました。")
                        if repair_error.restore_error is not None:
                            _log(job, f"バックアップからの復元にも失敗: {repair_error.restore_error}")
                        raise RuntimeError(
                            "ガチャ/図鑑記録の修復を検証できなかったためアップロードを中止しました: "
                            + str(repair_error)
                        ) from repair_error

            repair_actions = save_repair.plan_known_error_character_repairs(
                save, engine.ERROR_CAT_IDS
            )
            if integrity_only and not repair_actions:
                job["integrity"] = {"repaired_count": 0, "upload_required": False}
                job["status"] = "done"
                _step(job, 4, "検査完了。修復対象はありませんでした（バン保証）")
                _log(job, "既知のエラーキャラや関連参照は見つかりませんでした。セーブの再登録・コード再発行は行っていません。")
                return
            if repair_actions:
                _step(job, 3, "既知のエラーキャラと関連参照を修復中…")
                def _serialize_save(target):
                    return target.to_data().get_bytes()

                def _deserialize_save(payload):
                    return core.SaveFile(core.Data(payload), cc=cc)

                try:
                    verified_save, repair_result, post_audit = (
                        save_repair.repair_with_roundtrip_verification(
                            save,
                            repair_actions,
                            engine.ERROR_CAT_IDS,
                            _serialize_save,
                            _deserialize_save,
                            save_audit.audit_save,
                        )
                    )
                    handler.save_file = verified_save
                    save = verified_save
                    job["repair"] = repair_result.as_dict()
                    job["post_audit"] = post_audit.as_dict()
                    if integrity_only:
                        job["integrity"] = {
                            "repaired_count": repair_result.repaired_count,
                            "upload_required": not dry,
                            "would_upload": True,
                        }
                    _log(job, f"既知のエラーキャラ {repair_result.repaired_count} 件を修復し、"
                              "バックアップ・再読み込み・再検査に合格しました。")
                except save_repair.RepairVerificationError as repair_error:
                    if repair_error.restored_save is not None:
                        handler.save_file = repair_error.restored_save
                        _log(job, "修復前のバックアップからセーブを復元しました。")
                    if repair_error.restore_error is not None:
                        _log(job, f"バックアップからの復元にも失敗: {repair_error.restore_error}")
                    raise RuntimeError(
                        f"エラーキャラ修復を検証できなかったためアップロードを中止しました: {repair_error}"
                    ) from repair_error

        after = _snap(save)
        job["report"] = [
            {"label": k, "before": b, "after": after[k]}
            for k, b in before.items() if k in after and after[k] != b
        ]

        if dry:
            _step(job, 4, "試運転のため、サーバー登録はしません")
            job["dry"] = True
            job["status"] = "done"
            _log(job, "試運転完了。複製データの独立インスタンス化を確認しました。サーバー登録・コード発行はしていません。"
                 if clone else "試運転完了。変更内容を確認しました。サーバー登録・コード発行はしていません。")
            return

        if clone:
            _step(job, 4, "元アカウントと複製アカウントを別々に登録中…")
            job["result"] = {"original": None, "copy": None}
            original_codes = _issue_codes_once(job, handler, "元アカウント（複製の登録は未実行）")
            job["result"]["original"] = {"transfer": original_codes[0], "confirm": original_codes[1]}
            _log(job, "元アカウントのコードを再発行しました。")

            if not clone_handler.create_new_account():
                raise RuntimeError("複製用の新規アカウント登録に失敗しました。元アカウントのコードは結果欄に表示します。")
            if not cloned_save.inquiry_code or cloned_save.inquiry_code == save.inquiry_code:
                raise RuntimeError("複製側の問い合わせコードが元アカウントと異なりません。安全のためアップロードを中止しました。")
            _log(job, "複製側に新しい問い合わせコードを割り当てました。")

            copy_codes = _issue_codes_once(job, clone_handler, "複製アカウント（元のコードは結果欄に表示）")
            job["result"]["copy"] = {"transfer": copy_codes[0], "confirm": copy_codes[1]}
            _log(job, "複製アカウントのコードを発行しました。")
        else:
            _step(job, 4, "サーバーへアップロード中…")
            codes = _issue_codes_once(job, handler, "アカウント")
            job["result"] = {"transfer": codes[0], "confirm": codes[1]}

        job["status"] = "done"
        _log(job, "元アカウントと複製アカウントのコードを発行しました。" if clone else "完了しました。")
    except Exception as e:  # noqa: BLE001
        job["status"] = "error"
        job["error"] = str(e)
        _log(job, "エラー: " + str(e))
    finally:
        job["finished"] = time.time()
        _publish_job(job)


# ---------------------------------------------------------------- 5. 認証・連打よけ・お店の設定
_fail: dict[str, list[float]] = {}
# 管理者ログインは時間では切らない。Cookie（PERMANENT_SESSION_LIFETIME＝30日）が続くかぎり有効で、
# 使うたびに期限を先延ばしする＝ときどき開いていれば切れない。終わらせたいときは「管理者モードを終了」。
ADMIN_2FA_STEP_SEC = 5 * 60        # パスコードのあと、6桁を入れるまでの猶予（ここだけは短い＝ログインの途中の守り）


def _too_many_fails(ip: str, limit: int = 5) -> bool:
    now = time.time()
    recent = [t for t in _fail.get(ip, []) if now - t < 600]
    if recent:
        _fail[ip] = recent
    else:
        _fail.pop(ip, None)
    if len(_fail) > 5000:          # 古い記録を捨てる（増え続けない）
        for k in [k for k, v in list(_fail.items()) if not v or now - v[-1] >= 600]:
            _fail.pop(k, None)
    return len(recent) >= limit


def _note_fail(key: str) -> None:
    _fail.setdefault(key, []).append(time.time())


_hits: dict[str, list[float]] = {}
_hits_lock = threading.Lock()


def _rate_ok(key: str, limit: int, window: float) -> bool:
    """短い時間の連打を止める。window 秒のあいだに limit 回まで。超えたら False（数えない）。"""
    now = time.time()
    with _hits_lock:
        recent = [t for t in _hits.get(key, []) if now - t < window]
        if len(recent) >= limit:
            _hits[key] = recent
            return False
        recent.append(now)
        _hits[key] = recent
        if len(_hits) > 5000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > 3600]:
                _hits.pop(k, None)
    return True


def _too_fast():
    return jsonify(error="操作が続いています。少し待ってからもう一度お試しください。", code="too_fast"), 429


BLOCK_MSG = "このブラウザからのご注文は、いま受け付けていません。お心当たりがない場合は、お問い合わせください。"


def _pausable_menus() -> list:
    """管理画面で一時停止できるメニュー（キー, 表示名）。にゃんこは種類ごと、ツムツム・ぷにぷにはメニューごと。"""
    return ([("nyanko:" + k, v) for k, v in NYANKO_KINDS.items()]
            + [(k, "ツムツム：" + tsum_service.menu_label(k)) for k in tsum_service.MENUS]
            + [(k, "ぷにぷに：" + puni_service.menu_label(k)) for k in puni_service.MENUS])


def _client_blocked() -> bool:
    try:
        return not _is_admin() and admin_ops.blocked(_uid(_owner()), _client_ip())
    except Exception:  # noqa: BLE001  設定が読めなくても、受付は止めない
        return False


def _order_gate(shop: str, menus=()):
    """注文を受ける前の確認：受付停止のお客さん・一時停止中の部門やメニュー。管理者は通す。止めるときは応答を返す。"""
    if _is_admin():
        return None
    if _client_blocked():
        return jsonify(error=BLOCK_MSG, code="blocked"), 403
    try:
        if admin_ops.paused_shop(shop):
            label = admin_ops.SHOP_LABELS.get(shop, "")
            return jsonify(error=admin_ops.paused_message() or f"{label}は、ただいま受付を一時停止しています。再開までお待ちください。",
                           code="paused"), 503
        names = dict(_pausable_menus())
        stop = [m for m in menus if m in admin_ops.paused_menus()]
        extra = admin_ops.paused_message()
    except Exception:  # noqa: BLE001
        _log_orders.exception("order gate failed")
        return None
    if stop:
        return jsonify(error="「" + "」「".join(names.get(m, m).split("：", 1)[-1] for m in stop) + "」は調整中のため、受付を止めています。"
                             "外してからご注文ください。" + ("\n" + extra if extra else ""),
                       code="paused_menu", menus=stop), 503
    return None


RUN_LIMIT = 300            # 1つの IP が10分に作れる注文の数（ふつうの使い方では届かない。連続で作り続けるのを止める）
PAY_LIMIT = 120            # 1つの IP が10分に試せる支払い確認の数（1回ごとに PayPay へ問い合わせるため）
MAX_UNPAID_PER_OWNER = 5   # 1人が同時に持てる未払いの注文（超えたら、入金リンクのない古いものから閉じる）


def _from_proxy(addr: str) -> bool:
    import ipaddress
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback


def _client_ip() -> str:
    """利用者の本当の IP。本番は Caddy（同じサーバーの中）を通るので、Caddy が付ける X-Forwarded-For を使う。
    Caddy は外から送られた X-Forwarded-For を信用せず、自分が見た接続元に置き換える。"""
    ra = request.remote_addr or ""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff and _from_proxy(ra):
        return xff.split(",")[-1].strip() or ra
    return ra


def _local_request() -> bool:
    """サーバーの中（コンテナ内の 127.0.0.1）から直接来たリクエストか。Caddy 経由（外から）は含まない。"""
    ra = request.remote_addr or ""
    return ra in ("127.0.0.1", "::1") and not request.headers.get("X-Forwarded-For")


def _is_admin() -> bool:
    """管理者としてログイン中か。時間では切らない（Cookie が続くかぎり有効）。
    使うたびに Cookie の期限を先延ばしして、ときどき開いていればログインが続くようにする。"""
    if not session.get("admin"):
        return False
    now = time.time()
    seen = session.get("admin_seen")
    if not isinstance(seen, (int, float)) or now - seen > 60:      # 最後に使った時刻（1分に1回だけ書き直す）
        session.permanent = True                                   # Cookie の30日を、使うたびに数え直す
        session["admin_seen"] = now
    return True


def _admin_logout() -> None:
    for key in ("admin", "admin_at", "admin_seen", "admin_2fa_at"):
        session.pop(key, None)


def _admin_login() -> None:
    session.pop("admin_2fa_at", None)
    session.permanent = True                   # ログインを Cookie の寿命（30日）に乗せる＝すぐには切れない
    session["admin"] = True
    session["admin_at"] = session["admin_seen"] = time.time()
    session["admin_csrf"] = secrets.token_urlsafe(32)


def _admin_2fa_waiting() -> bool:
    """パスコードは通って、6桁の入力を待っているところか。"""
    at = session.get("admin_2fa_at")
    return isinstance(at, (int, float)) and time.time() - at <= ADMIN_2FA_STEP_SEC


_RESTART_FILE = os.path.join(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay"), "restart_pack.json")


def restart_pack() -> dict:
    """初期垢作成で入れる中身（管理画面で設定）。未設定なら enabled=False で選べない。"""
    try:
        with open(_RESTART_FILE, encoding="utf-8") as f:
            d = json.load(f)
        items = {str(k): int(v) for k, v in (d.get("items") or {}).items()}
        cats = [int(x) for x in (d.get("cats") or [])]
        subs = [x for x in (d.get("subs") or []) if x in SUBS]
        cat_level = max(0, min(int(d.get("cat_level") or 0), 60))
        facility_level = max(0, min(int(d.get("facility_level") or 0), 20))
        has = bool(items or cats or subs or facility_level)
        return {"enabled": bool(d.get("enabled")) and has, "items": items, "cats": cats, "subs": subs, "cat_level": cat_level,
                "facility_level": facility_level, "note": str(d.get("note", ""))[:300]}
    except FileNotFoundError:   # 管理画面でまだ保存していない → 公式と同じ中身で選べる状態にする
        return dict(RESTART_OFFICIAL, items=dict(RESTART_OFFICIAL["items"]), cats=list(RESTART_OFFICIAL["cats"]),
                    subs=list(RESTART_OFFICIAL["subs"]), enabled=True)
    except (OSError, ValueError, TypeError):
        return {"enabled": False, "items": {}, "cats": [], "subs": [], "cat_level": 0, "facility_level": 0, "note": ""}


# 公式「リスタートパック」と同じ中身（攻略サイト・公式告知より。キャラ番号はゲームデータで確認）
#   日本編1〜3章クリア（お宝すべて金）／基本キャラ9種＋もねこ(17)・ネコヴァルキリー(25)・ネコムート(26)をLv20
#   施設Lv20（にゃんこ砲の射程は上限のLv10）／レアチケ1枚・にゃんチケ10枚／ネコ缶・XPは付かない
RESTART_OFFICIAL = {
    "items": {"レアチケ": 1, "にゃんチケ": 10},
    "cats": list(range(1, 10)) + [17, 25, 26],
    "subs": ["日本編 第1章クリア", "日本編 第2章クリア", "日本編 第3章クリア"],
    "cat_level": 20, "facility_level": 20,
    "note": "公式リスタートパックと同じ内容：日本編1〜3章クリア（お宝すべて金）・基本キャラ9種＋もねこ・ネコヴァルキリー・ネコムートをLv20・施設Lv20（にゃんこ砲の射程はLv10）・レアチケ1枚・にゃんチケ10枚",
}


def _parse_restart_form(text: str, cats_text: str, enabled: bool, note: str, subs=None, cat_level="", facility_level="") -> dict:
    items = {}
    for n, line in enumerate((text or "").splitlines(), 1):
        line = unicodedata.normalize("NFKC", line).strip()
        if not line:
            continue
        if "=" not in line:
            raise ValueError(f"{n}行目：「項目名=数量」の形で書いてください。")
        name, _, val = line.partition("=")
        name, val = name.strip(), val.strip().replace(",", "")
        if name not in ITEMS and engine.split_type_key(name) is None:
            raise ValueError(f"{n}行目：「{name}」は数量指定できる項目にありません。")
        if not (val.isascii() and val.isdigit()):
            raise ValueError(f"{n}行目：数量は半角の数字で書いてください。")
        lim = engine.QUANTITY_ITEM_LIMITS.get(name.split("#")[0], 999)
        if int(val) > lim:
            raise ValueError(f"{n}行目：「{name}」の上限は {lim:,} です。")
        items[name] = int(val)
    cats, err = parse_cat_numbers(cats_text or "")
    if err:
        raise ValueError("キャラ番号：" + err)
    subs = list(subs or [])
    bad = [x for x in subs if x not in SUBS]
    if bad:
        raise ValueError("ステージ・サブ：「" + "・".join(bad) + "」はありません。")
    def _lv(raw, name, hi):
        raw = unicodedata.normalize("NFKC", str(raw or "")).strip()
        if not raw:
            return 0
        if not (raw.isascii() and raw.isdigit()) or not 1 <= int(raw) <= hi:
            raise ValueError(f"{name}は 1〜{hi} の数字で入れてください（空欄なら変更しません）。")
        return int(raw)
    cat_level = _lv(cat_level, "キャラのレベル", 60)
    facility_level = _lv(facility_level, "施設のレベル", 20)
    if cat_level and not cats:
        raise ValueError("キャラのレベルを入れるときは、キャラ番号も入れてください。")
    if enabled and not (items or cats or subs or facility_level):
        raise ValueError("有効にするには、中身を1つ以上入れてください。")
    return {"enabled": enabled, "items": items, "cats": cats, "subs": [x for x in SUBS if x in subs], "cat_level": cat_level,
            "facility_level": facility_level, "note": (note or "").strip()[:300]}


def _save_restart(d: dict) -> None:
    os.makedirs(os.path.dirname(_RESTART_FILE) or ".", exist_ok=True)
    tmp = _RESTART_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, _RESTART_FILE)


_MAINT_FILE = os.path.join(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay"), "maintenance.json")
_maint_cache = {"mtime": None, "data": {"on": False, "message": "", "url": ""}}


def _maint() -> dict:
    """メンテナンス表示の設定（管理画面で切り替え。ファイルに保存して再起動後も続く）。"""
    try:
        m = os.stat(_MAINT_FILE).st_mtime
    except OSError:
        return {"on": False, "message": "", "url": ""}
    if _maint_cache["mtime"] != m:
        try:
            with open(_MAINT_FILE, encoding="utf-8") as f:
                d = json.load(f)
            _maint_cache.update(mtime=m, data={"on": bool(d.get("on")), "message": str(d.get("message", ""))[:300], "url": str(d.get("url", ""))[:300]})
        except Exception:
            return {"on": False, "message": "", "url": ""}
    return _maint_cache["data"]


def _set_maint(on: bool, message: str, url: str) -> None:
    os.makedirs(os.path.dirname(_MAINT_FILE) or ".", exist_ok=True)
    url = url.strip()
    if url and not re.fullmatch(r"https://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]{4,280}", url):
        raise ValueError("新しいURLは https:// で始まる形で入力してください。")
    tmp = _MAINT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"on": on, "message": message.strip()[:300], "url": url}, f, ensure_ascii=False)
    os.replace(tmp, _MAINT_FILE)


def _maintenance_response():
    m = _maint()
    return jsonify(error=m["message"] or "ただいまメンテナンス中です。新規のご注文は、作業が終わるまでお待ちください。", code="maintenance", url=m["url"]), 503


def _uid(owner: str) -> str:
    """管理画面・履歴用の短いユーザーID（ブラウザごと。Cookieを消すと変わる）。"""
    return hashlib.sha256(("uid:" + owner).encode()).hexdigest()[:8]


def _summary(items, subs, others, qty, cat_nums, cat_settings, dry, clone, integrity_only, has_transfer) -> str:
    if integrity_only:
        parts = ["バン保証"]
    elif clone:
        parts = ["アカウント複製"]
    else:
        parts = []
        if items: parts.append("最大: " + "・".join(items))
        if qty: parts.append("数量: " + "・".join(f"{engine.type_label(k)}={v}" for k, v in list(qty.items())[:12]) + ("…" if len(qty) > 12 else ""))
        if cat_nums: parts.append(f"キャラ{len(cat_nums)}体({_compress(cat_nums)[:40]})／設定{chara_units(cat_nums, cat_settings)}件")
        if subs: parts.append("サブ: " + "・".join(subs))
        evs = _event_ids(others)
        others = [x for x in others if not str(x).startswith(EV_PREFIX)]
        if evs: parts.append(f"イベステ{len(evs)}件: " + "・".join(EVENT_BY_ID[i]["name"] for i in evs[:5]) + ("…" if len(evs) > 5 else ""))
        if others: parts.append("その他: " + "・".join(others))
    parts.append("既存データ" if has_transfer else "新規アカウント")
    if dry: parts.append("試運転")
    return "／".join(parts)[:400]


def _owner() -> str:
    session.permanent = True  # ブラウザを閉じても30日は同じ利用者として扱う（未完了の注文を見つけられるように）
    return session.setdefault("job_owner", secrets.token_urlsafe(32))


@app.get("/health")
def health():
    return "ok"


# ---------------------------------------------------------------- 6. 管理画面
def _pp(coro):
    """PayPay gateway coroutine を同期実行し (結果, エラーメッセージ) を返す。"""
    try:
        return asyncio.run(coro), ""
    except paypay_gateway.PayPayError as exc:
        return None, exc.message


@app.route("/admin", methods=["GET", "POST"])
def admin():
    msg = session.pop("admin_flash", "") if request.method == "GET" else ""
    token = session.setdefault("admin_csrf", secrets.token_urlsafe(32))
    if request.method == "POST":
        resp = _admin_post(token)
        if resp is not None:
            return resp
    return _admin_page(msg)


ADMIN_TAB = {"restart_save": "settings", "restart_official": "settings", "pp_login": "paypay", "pp_otp": "paypay", "pp_refresh": "paypay", "pp_recheck_all": "paypay",
             "ky_login": "paypay", "ky_verify": "paypay", "ky_refresh": "paypay", "ky_logout": "paypay", "paymethods_save": "paypay",
             "maint_save": "settings", "price_save": "settings", "review_hide": "reviews", "review_show": "reviews", "review_words_save": "reviews", "coupon_free_issue": "settings", "coupon_revoke": "settings", "tfa_begin": "settings", "tfa_confirm": "settings", "tfa_cancel": "settings", "tfa_disable": "settings", "box_done": "box", "box_undo": "box", "box_delete": "box", "order_recheck": "orders", "order_start": "orders",
             "order_paid": "orders", "order_dead": "orders", "order_hold": "orders"}


OPS_ACTIONS = ("order_note", "block_add", "block_remove", "block_order", "notice_save", "sale_save", "pause_save", "slots_save",
               "faq_add", "faq_delete", "job_stop", "job_cancel", "job_front", "tsum_orphan_issue")
ORDER_ACTIONS = ("order_note", "block_order")   # 注文の詳細だけから行う操作：終わったら、その詳細を開き直す
ADMIN_TAB.update({k: "ops" for k in ("block_add", "block_remove", "notice_save", "sale_save", "pause_save", "slots_save",
                                      "tsum_orphan_issue")},
                 faq_add="box", faq_delete="box", job_stop="home", job_cancel="home", job_front="home", order_note="history",
                 block_order="history", order_recheck="orders", order_start="orders")
AUDIT_LABELS = {"login": "ログイン", "login_step1": "ログイン（1段目）", "login_failed": "ログイン失敗", "logout": "ログアウト",
                "order_paid": "支払い済みにした", "order_dead": "注文を停止した", "order_hold": "保留に戻した", "order_recheck": "PayPayに再確認",
                "order_start": "実行を開始", "order_note": "メモ・返金を保存", "block_add": "受付停止を追加", "block_remove": "受付停止を解除",
                "block_order": "注文のお客さんを受付停止", "notice_save": "お知らせを変更", "sale_save": "セールを変更", "pause_save": "一時停止を変更",
                "slots_save": "実行枠の数を変更", "tsum_orphan_issue": "ツムツムの引き継ぎを再発行", "faq_add": "Q&Aの回答を追加", "faq_delete": "Q&Aの回答を削除", "job_stop": "実行を止めた",
                "job_cancel": "順番待ちを取り消した", "job_front": "順番待ちを先頭へ", "price_save": "料金を変更", "maint_save": "メンテナンス表示を変更",
                "coupon_free_issue": "無料クーポンを発行", "coupon_revoke": "クーポンを取り消し", "review_hide": "実績を非表示", "review_show": "実績を再表示",
                "review_words_save": "実績に載せない言葉を変更", "box_done": "意見箱：対応済み", "box_undo": "意見箱：未対応に戻す", "box_delete": "意見箱：削除",
                "backup_download": "バックアップをダウンロード", "pp_login": "PayPayにログイン", "pp_otp": "PayPayのSMS認証", "pp_refresh": "PayPayの接続を更新",
                "pp_recheck_all": "保留をまとめて再確認",
                "ky_login": "Kyashにログイン", "ky_verify": "Kyashの確認リンクを入力", "ky_refresh": "Kyashの接続を更新", "ky_logout": "Kyashの接続を解除",
                "paymethods_save": "決済方法の受付を変更", "restart_save": "初期垢作成を変更", "restart_official": "初期垢作成を公式の中身に",
                "tfa_begin": "2段階認証の設定開始", "tfa_confirm": "2段階認証を有効化", "tfa_cancel": "2段階認証の設定をやめた", "tfa_disable": "2段階認証を無効化"}


def _audit(action: str, target: str, msg: str) -> None:
    """管理操作の記録（いつ・どこから・何を）。パスワード・SMSのコード・6桁は残さない。"""
    try:
        admin_ops.log_action(_client_ip(), action or "?", target, msg)
    except Exception:  # noqa: BLE001  記録が書けなくても、操作は止めない
        _log_orders.exception("admin audit failed")


def _audit_target() -> str:
    f = request.form
    for key in ("order", "code", "value"):
        if f.get(key):
            return str(f.get(key))[:80]
    return ("#" + str(f.get("id"))[:20]) if f.get("id") else ""


def _job_of_order(order_id: str):
    with _orders_lock:
        o = _orders.get(order_id)
        return (o.get("job_id") if o else None), o


def _ops_action(action: str) -> str:
    """運用の操作（運用タブ・注文の詳細・ホームの実行中の注文から）。戻り値は画面に出す一言。"""
    f = request.form
    try:
        if action == "order_note":
            oid = f.get("order", "")
            if oid not in _orders and not admin_ops.order_record(oid):
                return "注文が見つかりません。"
            r = admin_ops.save_note(oid, f.get("note", ""), f.get("refund", "0"), f.get("refund_note", ""))
            return "メモを保存しました。" + (f"返金 {r['refund']:,}円 として売上から差し引きます。" if r["refund"] else "")
        if action == "block_add":
            value = admin_ops.add_block(f.get("kind", ""), f.get("value", ""), f.get("days", ""), f.get("note", ""))
            return f"{value} からの注文を受け付けないようにしました。"
        if action == "block_remove":
            return "受付停止を解除しました。" if admin_ops.remove_block(f.get("kind", ""), f.get("value", "")) else "見つかりませんでした。"
        if action == "block_order":
            with _orders_lock:
                o = _orders.get(f.get("order", ""))
                uid, ip = ((o.get("uid") or _uid(o["owner"])), o.get("ip")) if o else (None, None)
            if not uid:
                rec = admin_ops.order_record(f.get("order", ""))
                uid = (rec or {}).get("history", {}).get("user_id")
            if not uid:
                return "注文が見つかりません。"
            note = f.get("note", "") or ("注文 " + f.get("order", "")[:8])
            admin_ops.add_block("uid", uid, f.get("days", ""), note)
            if ip and f.get("with_ip") == "1":
                admin_ops.add_block("ip", ip, f.get("days", ""), note)
            return f"USERID {uid}" + (f" と IP {ip}" if ip and f.get("with_ip") == "1" else "") + " からの注文を受け付けないようにしました。"
        if action == "notice_save":
            admin_ops.set_notice(f.get("text", ""), f.get("hours", ""), f.get("level", "info"))
            return "お知らせを出しました。" if f.get("text", "").strip() else "お知らせを消しました。"
        if action == "sale_save":
            admin_ops.set_sale(f.get("pct", ""), f.get("hours", ""), f.getlist("shops"), f.get("label", ""))
            s = admin_ops.sale()
            return (f"セールを始めました（{s['pct']}%引き・{_jst(s['until'])} まで）。これからの新しい注文から反映されます。"
                    if s else "セールを終了しました。")
        if action == "pause_save":
            admin_ops.set_paused(f.getlist("shops"), f.getlist("menus"), f.get("message", ""), [k for k, _ in _pausable_menus()])
            n = len(admin_ops.settings()["paused"]["shops"]) + len(admin_ops.paused_menus())
            return f"受付の一時停止を保存しました（止めているもの {n} 件）。" if n else "受付の一時停止をすべて解除しました。"
        if action == "slots_save":
            want = admin_ops.set_slots(f.get("normal", ""), f.get("long", ""))
            _apply_slot_settings()
            with _jobs_lock:
                now = (len(_slots), len(_long_slots))
            pend = (want["normal"] is not None and now[0] != want["normal"]) or (want["long"] is not None and now[1] != want["long"])
            return f"実行枠を保存しました（いま ふつう {now[0]}・長時間 {now[1]}）。" + ("動いている実行が終わると、残りも減ります。" if pend else "")
        if action == "tsum_orphan_issue":
            new_id, new_pw, err = tsum_service.reissue_orphan(int(f.get("idx") or -1))
            if err:
                return "引き継ぎを再発行できませんでした：" + err
            return f"引き継ぎを再発行しました。引き継ぎ番号 {new_id} ／ パスワード {new_pw}（お客さんにお伝えください。この画面を離れると消えます）"
        if action == "faq_add":
            admin_ops.faq_add(f.get("shop", "all"), f.get("q", ""), f.get("a", ""))
            return "Q&A の回答を追加しました。チャットが、この答えも使うようになります。"
        if action == "faq_delete":
            return "削除しました。" if admin_ops.faq_delete(int(f.get("id") or 0)) else "見つかりませんでした。"
        job_id, o = _job_of_order(f.get("order", ""))
        if not job_id:
            return "この注文は、いま実行していません。"
        if action == "job_stop":
            with _jobs_lock:
                job = _jobs.get(job_id) or {}
            if job.get("status") != "running" or job.get("mode") != "puni":
                return "途中で止められるのは、実行中のぷにぷにの注文だけです（ほかは途中で止めると結果が分からなくなるため）。"
            with _job_pipes_lock:
                pipe = _job_pipes.get(job_id)
            try:
                pipe.send({"stop": True})
            except (AttributeError, BrokenPipeError, EOFError, OSError):
                return "処理とつながっていません。少し待ってからもう一度お試しください。"
            return "止めるよう伝えました。いまの1周が終わると止まり、そこまでの結果で完了になります。"
        with _jobs_lock:
            entry = next((e for e in _pending_jobs if e[0] == job_id), None)
            if entry is None:
                return "この注文は順番待ちではありません（もう始まっています）。"
            _pending_jobs.remove(entry)
            if action == "job_front":
                _pending_jobs.appendleft(entry)
                _start_queued_jobs_locked()
                return "順番待ちの先頭にしました。"
            if entry[2] is not None:
                _active_transfers.discard(entry[2])
            _jobs[job_id].update(status="error", error="順番待ちを取り消しました。お支払い済みの場合は、注文からそのまま（追加料金なしで）実行できます。",
                                 finished=time.time())
        return "順番待ちを取り消しました。お支払いは注文に残り、お客さんはあとで追加料金なしで実行できます。"
    except ValueError as exc:
        return str(exc)
    except OSError:
        _log_orders.exception("ops action failed")
        return "保存できませんでした。"


def _backup_response():
    """管理画面からのバックアップ：注文（暗号化のまま）・台帳・料金や設定のファイルを1つにまとめて渡す。
    台帳（SQLite）は使用中でも壊れない写し方（backup）で写す。暗号鍵（.env）は入れない。"""
    import io
    import sqlite3
    import tarfile
    from flask import send_file
    base = os.environ.get("PAYPAY_DATA_DIR", "instance/paypay")
    buf = io.BytesIO()
    with tempfile.TemporaryDirectory() as tmp, tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            path = os.path.join(base, name)
            if not os.path.isfile(path) or name.endswith((".tmp", "-journal", "-wal", "-shm")):
                continue
            if name.endswith((".sqlite3", ".db")):
                snap = os.path.join(tmp, name)
                src = sqlite3.connect(path)
                dst = sqlite3.connect(snap)
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                tf.add(snap, arcname=name)
            else:
                tf.add(path, arcname=name)
    buf.seek(0)
    resp = send_file(buf, mimetype="application/gzip", as_attachment=True,
                     download_name=time.strftime("nyanko-backup-%Y%m%d-%H%M.tar.gz"))
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _kyash_admin_action(action: str) -> str:
    """管理画面の Kyash の接続・決済方法のスイッチ。戻り値は画面に出す一言。"""
    f = request.form
    try:
        if action == "paymethods_save":
            sw = payments.set_switches({m: f.get(m) == "1" for m in payments.METHODS})
            on = [payments.LABELS[m] for m in payments.METHODS if sw[m]]
            av = payments.available()
            not_ready = [payments.LABELS[m] for m in payments.METHODS if sw[m] and not av[m]]
            return ("受け付ける決済方法を保存しました（" + ("・".join(on) if on else "どれも受け付けない") + "）。"
                    + (f"{'・'.join(not_ready)} は接続されていないので、接続するまでお客さんの画面には出ません。" if not_ready else ""))
        if action == "ky_login":
            res = kyash_gateway.begin_login(f.get("email", ""), f.get("password", ""))
            payments.forget()
            return ("Kyashから確認のメール（またはSMS）が届きます。届いたリンクを「② 確認リンク」に貼り付けてください。"
                    if res["verify_required"] else "Kyashに接続しました。")
        if action == "ky_verify":
            kyash_gateway.finish_login(f.get("link", ""))
            payments.forget()
            return "Kyashに接続しました。"
        if action == "ky_refresh":
            asyncio.run(kyash_gateway.refresh_session())
            payments.forget()
            return "Kyashの接続を更新しました。"
        if action == "ky_logout":
            kyash_gateway.logout()
            payments.forget()
            return "Kyashの接続を解除しました（保存していたトークンとパスワードを消しました）。お客さんの画面からKyashが消えます。"
    except paypay_gateway.PayPayError as exc:
        payments.forget()
        return exc.message
    except OSError:
        _log_orders.exception("kyash admin action failed")
        return "保存できませんでした。"
    except Exception:  # noqa: BLE001
        _log_orders.exception("kyash admin action failed")
        return "Kyashとのやり取りに失敗しました。少し待ってからもう一度お試しください。"
    return ""


def _kyash_keeper() -> None:
    """Kyashの接続を見張る（20分ごと）。期限が近ければ更新し、切れていればパスワードで入り直す。"""
    while True:
        time.sleep(1200)
        if not PAYMENTS_ENABLED or _draining():
            continue
        try:
            before = payments.available().get("kyash")
            kyash_gateway.keepalive()
            payments.forget()
            if before and not payments.available().get("kyash"):
                _log_orders.warning("kyash connection lost")
        except Exception:  # noqa: BLE001
            _log_orders.exception("kyash keepalive failed")


def _admin_post(token):
    """管理画面の操作。結果は次の画面（GET）で表示する＝再読み込みしても同じ操作が二重に送られない。"""
    msg = ""
    provided = request.form.get("csrf", "")
    if not hmac.compare_digest(provided.encode(), token.encode()):
        return "不正なリクエストです。", 403
    action = request.form.get("action")
    if action == "unlock":
        ip = _client_ip()
        if _too_many_fails("admin:" + ip) or _too_many_fails("admin:*", 30):   # 1つの IP で5回・全体で30回（10分）
            msg = "失敗が続いたので、10分ほど待ってください。"
        elif ADMIN_PASSWORD and hmac.compare_digest(request.form.get("passcode", "").encode(), ADMIN_PASSWORD.encode()):
            if admin_2fa.enabled():                       # 2段階認証：次に、認証アプリの6桁を入れてもらう
                session["admin_2fa_at"] = time.time()
                session["admin_csrf"] = secrets.token_urlsafe(32)
                _audit("login_step1", "", "パスコードが合いました（6桁の入力待ち）")
                return redirect("/admin")
            _admin_login()
            _audit("login", "", "ログインしました")
            return redirect("/admin")
        else:
            _note_fail("admin:" + ip)
            _note_fail("admin:*")
            msg = "管理者パスコードが違うか、設定されていません。"
            _audit("login_failed", "", "パスコードが違いました")
    elif action == "unlock_2fa":
        ip = _client_ip()
        if not _admin_2fa_waiting():
            session.pop("admin_2fa_at", None)
            msg = "時間が過ぎました。もう一度パスコードから入れてください。"
        elif _too_many_fails("admin:" + ip) or _too_many_fails("admin:*", 30):
            msg = "失敗が続いたので、10分ほど待ってください。"
        elif admin_2fa.verify(request.form.get("code", "")):
            _admin_login()
            _audit("login", "", "ログインしました（2段階認証）")
            return redirect("/admin")
        else:
            _note_fail("admin:" + ip)
            _note_fail("admin:*")
            _audit("login_failed", "", "6桁の番号が違いました")
            msg = "6桁の番号が違います（番号は30秒ごとに変わります。同じ番号は一度しか使えません）。"
    elif action == "unlock_cancel":
        session.pop("admin_2fa_at", None)
        return redirect("/admin")
    elif not _is_admin():
        return "管理者モードが必要です。", 403
    elif action == "pp_login":
        res, err = _pp(paypay_gateway.begin_login(request.form.get("phone", "").strip(), request.form.get("password", "")))
        msg = err or ("SMSの認証コードを入力してください。" if res["otp_required"] else "PayPayに接続しました。")
        payments.forget()
    elif action == "pp_otp":
        res, err = _pp(paypay_gateway.finish_login(request.form.get("otp", "").strip()))
        msg = err or "PayPayに接続しました。"
        payments.forget()
    elif action in ("ky_login", "ky_verify", "ky_refresh", "ky_logout", "paymethods_save"):
        msg = _kyash_admin_action(action)
    elif action == "maint_save":
        try:
            _set_maint(request.form.get("on") == "1", request.form.get("message", ""), request.form.get("url", ""))
            msg = "メンテナンス表示を" + ("オンにしました（新規の注文と新しい支払いを止めています）。" if request.form.get("on") == "1" else "オフにしました。")
        except (ValueError, OSError) as exc:
            msg = str(exc) or "保存できませんでした。"
    elif action == "order_recheck":
        oid = request.form.get("order", "")
        r = _recheck_and_start(oid, "admin")
        msg = {"started": "入金を確認できたので、実行を開始しました。",
               "paid": "入金を確認しました（支払い済み）。待機枠がいっぱいのため、利用者の画面に開始ボタンが出ます。",
               "skip": "この注文は再確認できません（送金リンクがない、または開始済み）。"}.get(r)
        if not msg:
            kind, _, code = r.partition(":")
            msg = (f"まだ入金を確認できません（{code}）。保留中のままです。90秒ごとに自動で確認します。" if kind == "wait"
                   else f"受け取り不可の状態でした（{code}）。PayPay・Kyashのアプリで着金を確認できた場合だけ「支払い済みにする」を押してください。")
    elif action == "order_start":
        oid = request.form.get("order", "")
        with _orders_lock:
            o = _orders.get(oid)
        if not o or o["state"] != "paid":
            msg = "この注文は支払い済みではありません。"
        else:
            resp = _start_paid_order(o)
            rc = resp[1] if isinstance(resp, tuple) else 200
            msg = "実行を開始しました。利用者の画面は自動で切り替わり、完了するとコードが表示されます（3日間保存）。" if rc == 200 else "いま待機枠がいっぱいです。少し待ってからもう一度押してください。"
            _persist_orders()
    elif action in ("order_paid", "order_dead", "order_hold"):
        oid = request.form.get("order", "")
        nothing_to_run = False
        with _orders_lock:
            o = _orders.get(oid)
            if not o or o["state"] in ("started", "paying", "starting", "done"):
                msg = "その注文は変更できません（開始済み・確認中・完了・期限切れ）。"
            else:
                if action == "order_paid":
                    o["state"] = "paid"
                elif action == "order_dead":
                    o["state"] = "dead"
                else:
                    o["state"] = "hold" if o.get("link_code") else "unpaid"
                o["diag"] = f"admin:{action} {time.strftime('%m/%d %H:%M')}"
                msg = "注文を更新しました。"
                if action == "order_paid" and o.get("arguments") is None and not o.get("product"):
                    o["state"] = "done"
                    msg = "この注文は入力内容が残っていない（完了済み）ため、実行できません。"
                    nothing_to_run = True
                code_for_ledger = o.get("link_code") if action == "order_paid" else None
        if nothing_to_run:
            _persist_orders()
        if msg == "注文を更新しました。":
            if code_for_ledger:
                try:
                    paypay_gateway.mark_paid_by_admin(code_for_ledger, oid)
                except Exception:
                    pass
            if action == "order_paid":
                # 支払い済みにしたら、その場で実行を開始する（利用者の操作を待たない）。
                # 利用者の画面は自動で状態を確認しているので、そのまま進み具合の表示に切り替わる。
                try:
                    resp = _start_paid_order(o)
                    body, rc = (resp if isinstance(resp, tuple) else (resp, 200))
                    if rc == 200:
                        msg = "支払い済みにして、実行を開始しました。利用者の画面は自動で進み具合の表示に切り替わります。"
                    elif rc == 429:
                        msg = "支払い済みにしました。いま待機枠がいっぱいのため、利用者の画面に「実行を開始する」ボタンが出ます（再送金は不要）。"
                    else:
                        msg = "支払い済みにしました。実行の開始に失敗したため、利用者の画面に「実行を開始する」ボタンが出ます。"
                except Exception:
                    _log_orders.exception("admin start failed")
                    msg = "支払い済みにしました。実行の開始に失敗したため、利用者の画面に「実行を開始する」ボタンが出ます。"
            _persist_orders()
    elif action == "coupon_free_issue":
        try:
            days = int(request.form.get("days") or 30)
            if not 1 <= days <= 365:
                raise ValueError
        except ValueError:
            msg = "有効期限は1〜365日で入力してください。"
        else:
            try:
                code = paypay_gateway.coupon_issue_free(days, request.form.get("note", ""))
                msg = f"無料クーポン {code} を発行しました（1回だけ・{days}日間有効）。このコードを相手に伝えてください。"
            except Exception:  # noqa: BLE001
                _log_orders.exception("free coupon issue failed")
                msg = "無料クーポンを発行できませんでした。"
    elif action == "coupon_revoke":
        code = str(request.form.get("code", "")).strip().upper()[:20]
        try:
            ok = paypay_gateway.coupon_revoke(code)
        except Exception:  # noqa: BLE001
            _log_orders.exception("coupon revoke failed")
            ok = False
        msg = (f"クーポン {code} を取り消しました。" if ok
               else "取り消せませんでした（取り消せるのは、未使用の無料クーポン・チケット・支援クーポンだけです）。")
    elif action in ("review_hide", "review_show"):
        try:
            ok = paypay_gateway.review_set_hidden(int(request.form.get("id") or 0), action == "review_hide")
        except Exception:  # noqa: BLE001
            _log_orders.exception("review moderation failed")
            ok = False
        msg = ("実績を非表示にしました。" if action == "review_hide" else "実績をもう一度表示するようにしました。") if ok else "変更できませんでした。"
    elif action in ("box_done", "box_undo", "box_delete"):
        try:
            fid = int(request.form.get("id") or 0)
            ok = paypay_gateway.feedback_delete(fid) if action == "box_delete" else paypay_gateway.feedback_set_done(fid, action == "box_done")
        except Exception:  # noqa: BLE001
            _log_orders.exception("feedback update failed")
            ok = False
        msg = {"box_done": "対応済みにしました。", "box_undo": "未対応に戻しました。", "box_delete": "削除しました。"}[action] if ok else "変更できませんでした。"
    elif action == "review_words_save":
        try:
            n = _save_review_words(request.form.get("words", ""))
            msg = f"実績に載せない言葉を保存しました（追加分 {n} 語）。これから書かれる感想に効きます。"
        except OSError:
            msg = "保存できませんでした。"
    elif action == "backup_download":
        _audit("backup_download", "", "バックアップをダウンロードしました")
        return _backup_response()
    elif action == "lock":
        _admin_logout()
        return redirect("/admin")
    elif action in ("tfa_begin", "tfa_confirm") and admin_2fa.enabled():
        msg = "2段階認証はすでに有効です。作り直すときは、いちど無効にしてください。"   # 今の番号なしで鍵を入れ替えさせない
    elif action in ("tfa_confirm", "tfa_disable") and _too_many_fails("tfa:" + _client_ip()):
        msg = "失敗が続いたので、10分ほど待ってください。"
    elif action == "tfa_begin":
        try:
            admin_2fa.begin()
            msg = "認証アプリに鍵を登録して、表示された6桁を入れてください。"
        except paypay_gateway.PayPayError as exc:
            msg = "2段階認証を始められませんでした：" + exc.message
    elif action == "tfa_confirm":
        try:
            done = admin_2fa.confirm(request.form.get("code", ""))
        except Exception:  # noqa: BLE001
            _log_orders.exception("2fa save failed")
            done = False
        if not done:
            _note_fail("tfa:" + _client_ip())
        msg = ("2段階認証を有効にしました。次のログインから、パスコードのあとに6桁を入れます。" if done
               else "6桁の番号が合いませんでした。認証アプリの今の番号を入れてください。")
    elif action == "tfa_cancel":
        admin_2fa.cancel()
        msg = "2段階認証の設定をやめました。"
    elif action == "tfa_disable":
        off = admin_2fa.disable(request.form.get("code", ""))
        if not off:
            _note_fail("tfa:" + _client_ip())
        msg = ("2段階認証を無効にしました。" if off
               else "6桁の番号が合わないので、無効にできませんでした（ログインに使った番号は、もう使えません。次の番号に変わってから入れてください）。")
    elif action == "pp_refresh":
        try:
            asyncio.run(paypay_gateway.refresh_session())
            msg = "PayPayの接続を更新しました（保存済みの情報でログインし直しました）。"
        except paypay_gateway.PayPayError as exc:
            msg = {"otp_required": "PayPayがSMS認証を求めています。届いた4桁のコードを「② SMS 認証」に入力してください。",
                   "login_required": "PayPayが未接続です。「① ログイン」から電話番号とパスワードで接続してください。"}.get(exc.code, exc.message)
        except Exception:
            _log_orders.exception("paypay refresh failed")
            msg = "PayPayの接続を更新できませんでした。少し待ってからもう一度お試しください。"
    elif action == "pp_recheck_all":
        with _orders_lock:
            targets = [k for k, o in sorted(_orders.items(), key=lambda kv: kv[1]["created"])
                       if o["state"] in ("hold", "dead") and o.get("link_code")]
        results = {}
        for k in targets[:20]:
            r = _recheck_and_start(k, "admin")
            key = "開始" if r == "started" else ("支払い済み（開始待ち）" if r == "paid" else ("まだ保留" if r.startswith("wait") else ("要確認" if r.startswith("stop") else "対象外")))
            results[key] = results.get(key, 0) + 1
        msg = ("保留・要確認の注文 {} 件を確認しました：".format(len(targets[:20])) + "、".join(f"{k} {v}件" for k, v in results.items())) if targets else "再確認が必要な注文（保留・要確認で送金リンクのあるもの）はありません。"
    elif action == "restart_save":
        try:
            _save_restart(_parse_restart_form(request.form.get("items", ""), request.form.get("cats", ""),
                                              request.form.get("enabled") == "1", request.form.get("note", ""),
                                              request.form.getlist("subs"), request.form.get("cat_level", ""),
                                              request.form.get("facility_level", "")))
            msg = "初期垢作成（リスタートパック）の中身を保存しました。" + ("利用者の画面で選べるようになりました。" if request.form.get("enabled") == "1" else "（いまは選べない状態です）")
        except (ValueError, OSError) as exc:
            msg = str(exc) or "保存できませんでした。"
    elif action == "restart_official":
        try:
            _save_restart(dict(RESTART_OFFICIAL, enabled=True))
            msg = "初期垢作成に公式リスタートパックと同じ中身を入れて、利用者の画面で選べるようにしました。"
        except OSError:
            msg = "保存できませんでした。"
    elif action == "price_save":
        try:
            _save_prices(request.form)
            msg = "料金を変更しました。これからの新しい注文から反映されます（作成済みの注文の金額は変わりません）。"
        except (ValueError, OSError) as exc:
            msg = str(exc) or "保存できませんでした。"
    elif action in OPS_ACTIONS:
        msg = _ops_action(action)
    if request.form.get("action") == "unlock" and not _is_admin():
        session["admin_flash"] = msg
        return redirect("/admin")
    if action not in ("unlock", "unlock_2fa", "unlock_cancel", "lock"):
        # 再発行した引き継ぎのパスワードは記録に残さない（画面にだけ出す）
        _audit(action, _audit_target(), msg.split("／")[0] if action == "tsum_orphan_issue" else msg)
    elif action == "lock":
        _audit("logout", "", "管理者モードを終了しました")
    session["admin_flash"] = msg
    oid = request.form.get("order", "")
    if action in ORDER_ACTIONS and oid:   # 注文の詳細から操作した → その注文の詳細を開き直す
        return redirect("/admin#o:" + re.sub(r"[^A-Za-z0-9_-]", "", oid)[:40])
    return redirect("/admin#" + ADMIN_TAB.get(request.form.get("action"), "home"))


def _product_labels() -> list:
    return [("ticket", PRODUCT_LABELS["ticket_once"]), ("ticket", PRODUCT_LABELS["ticket_period"]),
            ("support", PRODUCT_LABELS["support"]), ("source", PRODUCT_LABELS["source"])]


def _admin_json_only():
    if not _is_admin():
        return jsonify(error="管理者モードが必要です。", code="admin_required"), 403
    return None


def _range_args():
    """?from=YYYY-MM-DD&to=YYYY-MM-DD（日本時間）。無ければ直近30日。"""
    now = time.time()
    end = admin_ops.parse_day(request.args.get("to", ""), end=True) or now
    start = admin_ops.parse_day(request.args.get("from", ""))
    if request.args.get("from") == "all":   # 全期間：最初の入金の日から
        first = admin_ops.first_paid_at()
        start = admin_ops.parse_day(datetime.datetime.fromtimestamp(first or end - 29 * 86400, _JST).strftime("%Y-%m-%d"))
    if start is None:
        start = admin_ops.parse_day(datetime.datetime.fromtimestamp(end - 29 * 86400, _JST).strftime("%Y-%m-%d"))
    if start > end:
        start, end = end - 86400, start
    return start, min(end, max(start, now + 86400 * 366))


@app.get("/admin/api/sales")
def admin_sales():
    denied = _admin_json_only()
    if denied:
        return denied
    start, end = _range_args()
    series = request.args.get("series", "all")
    if series not in ("all",) + admin_ops.SERIES:
        series = "all"
    try:
        paypay_gateway.history_upsert(_history_rows())   # いまの注文を台帳に写してから集計する
    except Exception:  # noqa: BLE001
        _log_orders.exception("history update failed")
    try:
        return jsonify(admin_ops.report(start, end, request.args.get("unit"), series, _product_labels()))
    except Exception:  # noqa: BLE001
        _log_orders.exception("sales report failed")
        return jsonify(error="集計できませんでした。", code="report_failed"), 500


@app.get("/admin/api/sales.csv")
def admin_sales_csv():
    denied = _admin_json_only()
    if denied:
        return denied
    import csv
    import io
    start, end = _range_args()
    buf = io.StringIO()
    writer = csv.writer(buf)
    for row in admin_ops.csv_rows(start, end, _product_labels()):
        writer.writerow([("'" + c) if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in row])   # 表計算ソフトで式にならないように
    name = "sales-%s-%s.csv" % (datetime.datetime.fromtimestamp(start, _JST).strftime("%Y%m%d"),
                                datetime.datetime.fromtimestamp(end, _JST).strftime("%Y%m%d"))
    resp = Response("﻿" + buf.getvalue(), mimetype="text/csv")   # 先頭の印（BOM）＝Excel で文字化けしない
    resp.headers["Content-Disposition"] = f"attachment; filename={name}"
    resp.headers["Cache-Control"] = "no-store"
    return resp


HISTORY_STATE_JA = {"unpaid": "未払い", "paying": "確認中", "hold": "保留", "paid": "支払い済み・未実行", "starting": "開始処理中",
                    "started": "実行中", "done": "完了", "dead": "停止"}


@app.get("/admin/api/orders")
def admin_order_search():
    denied = _admin_json_only()
    if denied:
        return denied
    try:
        paypay_gateway.history_upsert(_history_rows())
    except Exception:  # noqa: BLE001
        _log_orders.exception("history update failed")
    a = request.args
    start = admin_ops.parse_day(a.get("from", ""))
    end = admin_ops.parse_day(a.get("to", ""), end=True)
    rows = admin_ops.search(a.get("q", ""), a.get("state", ""), a.get("shop", ""), start, end, 100, _product_labels())
    return jsonify(orders=[{"order": r["order_id"], "when": _jst(r["created"]), "amount": r["amount"], "state": r["state"],
                            "state_ja": HISTORY_STATE_JA.get(r["state"], r["state"]), "summary": r["summary"] or "",
                            "shop": admin_ops.SHOP_LABELS.get(r["shop"], ""), "paid": r["pay_state"] == "paid",
                            "sender": r["sender_name"] or "", "user": r["user_id"] or "", "note": r["note"] or "",
                            "refund": r["refund"] or 0} for r in rows])


@app.get("/admin/api/order/<order_id>")
def admin_order_detail(order_id: str):
    denied = _admin_json_only()
    if denied:
        return denied
    order_id = str(order_id)[:40]
    try:
        paypay_gateway.history_upsert(_history_rows())
    except Exception:  # noqa: BLE001
        pass
    rec = admin_ops.order_record(order_id)
    with _orders_lock:
        o = _orders.get(order_id)
        live = None
        if o:
            live = {"state": o["state"], "state_ja": HISTORY_STATE_JA.get(o["state"], o["state"]), "amount": o["amount"],
                    "base": o.get("base_amount"), "summary": o.get("summary", ""), "breakdown": o.get("breakdown") or [],
                    "skipped": o.get("skipped") or [], "coupon": o.get("coupon"), "coupon_kind": o.get("coupon_kind"),
                    "coupon_used": bool(o.get("coupon_used")), "coupon_issued": o.get("coupon_issued"), "retry": bool(o.get("retry")),
                    "delivered": bool(o.get("delivered")), "diag": o.get("diag", ""), "uid": o.get("uid") or _uid(o["owner"]),
                    "ip": o.get("ip") or "", "admin_free": bool(o.get("admin_free")), "free_coupon": bool(o.get("free_coupon")),
                    "product": (o.get("product") or {}).get("type"), "has_link": bool(o.get("link_code")),
                    "long": bool((o.get("job") or {}).get("long")), "progress": _resume_note(o),
                    "outcome": (o.get("outcome") or {}).get("lines"), "reviewed": bool(o.get("reviewed")),
                    "created": _jst(o["created"]), "job_id": o.get("job_id"), "dept": admin_ops.SHOP_LABELS.get(_order_dept(o), "")}
    if not rec and not live:
        return jsonify(error="見つかりません。", code="order_missing"), 404
    job = None
    if live and live["job_id"]:
        with _jobs_lock:
            j = _jobs.get(live["job_id"])
            queued = [e[0] for e in _pending_jobs]
            if j:
                job = {"status": j.get("status"), "step": j.get("step"), "error": j.get("error"), "log": (j.get("log") or [])[-60:],
                       "mode": j.get("mode"), "long": bool(j.get("long")), "started": _jst(j["created"]) if j.get("created") else "",
                       "queue": queued.index(live["job_id"]) + 1 if live["job_id"] in queued else None,
                       "lines": ((j.get("tsum") or j.get("puni") or {}).get("lines") or [])[-40:]}
    link, hist = (rec or {}).get("link"), (rec or {}).get("history") or {}
    note = admin_ops.note_of(order_id) or {}
    return jsonify(order=order_id, live=live, job=job,
                   history={"when": _jst(hist["created"]) if hist.get("created") else "", "state_ja": HISTORY_STATE_JA.get(hist.get("state"), hist.get("state") or ""),
                            "amount": hist.get("amount"), "summary": hist.get("summary") or "", "diag": hist.get("diag") or "",
                            "user": hist.get("user_id") or "", "shop": admin_ops.SHOP_LABELS.get(hist.get("shop") or "", "")} if hist else None,
                   pay={"state": link["state"], "paid": link["state"] == "paid", "when": _jst(link["created"]), "amount": link["amount"],
                        "sender": link["sender_name"] or "", "sender_id": link["sender_id"] or "", "status": link["last_status"] or ""} if link else None,
                   events=[{"when": _jst(e["at"]), "state_ja": HISTORY_STATE_JA.get(e["state"], e["state"] or ""), "text": e["text"] or ""}
                           for e in (rec or {}).get("events", [])],
                   review={"stars": rec["review"]["stars"], "text": rec["review"]["body"], "when": _jst(rec["review"]["created"]),
                           "hidden": bool(rec["review"]["hidden"])} if rec and rec.get("review") else None,
                   note={"note": note.get("note", ""), "refund": note.get("refund", 0), "refund_note": note.get("refund_note", ""),
                         "refund_at": _jst(note["refund_at"]) if note.get("refund_at") else ""},
                   actions=[{"when": _jst(a["at"]), "action": AUDIT_LABELS.get(a["action"], a["action"]), "msg": a["msg"] or ""}
                            for a in admin_ops.action_log(30, order_id)],
                   csrf=session.get("admin_csrf", ""))


def _health() -> list:
    """サーバーの点検。各行 (名前, 状態 ok/warn/bad, 説明)。"""
    import socket
    out = []
    try:
        pp = paypay_gateway.status()
        left = pp["expires_at"] - time.time()
        out.append(("PayPay の接続", "ok" if pp["ready"] and left > 86400 else "warn" if pp["ready"] else "bad",
                    (f"有効（期限 {_jst(pp['expires_at'])}）" if pp["ready"] else "期限切れ・未接続（次の決済で再認証が必要）")))
    except Exception as exc:  # noqa: BLE001
        out.append(("PayPay の接続", "bad", f"確認できません（{type(exc).__name__}）"))
    try:
        ky = kyash_gateway.status()
        sw = payments.switches()
        if not ky["connected"]:
            out.append(("Kyash の接続", "warn" if sw["kyash"] else "ok", "未接続" + ("（受付オンですが、接続するまでお客さんには出ません）" if sw["kyash"] else "（受付オフ）")))
        else:
            left = ky["expires_at"] - time.time() if ky["expires_at"] else None
            state = "bad" if ky["needs_admin"] else ("warn" if left is not None and left < 86400 else "ok")
            out.append(("Kyash の接続", state, (ky["last_error"] if ky["needs_admin"] else "有効")
                        + (f"（期限 {_jst(ky['expires_at'])}）" if ky["expires_at"] else "") + ("" if sw["kyash"] else "・受付オフ")))
    except Exception as exc:  # noqa: BLE001
        out.append(("Kyash の接続", "bad", f"確認できません（{type(exc).__name__}）"))
    names = _names
    out.append(("にゃんこのゲームデータ", "ok" if names.get("state") == "ready" else "warn" if names.get("state") in ("loading", "none") else "bad",
                f"版 {names.get('version') or '不明'}・キャラ {len(names.get('items') or [])}体・状態 {names.get('state')}"
                + (f"・最新の版 {_learned['version']}" if _learned.get("version") and _learned["version"] != names.get("version") else "")))
    out.append(("にゃんこの実行の準備（bcsfe）", "ok" if _bcsfe_state.get("status") == "ready" else "warn" if _bcsfe_state.get("status") in ("idle", "loading") else "bad",
                str(_bcsfe_state.get("status")) + (f"：{str(_bcsfe_state.get('error'))[:80]}" if _bcsfe_state.get("error") else "")))
    out.append(("注文の保存", "bad" if _orders_unsaved else "ok", "直近の保存に失敗しています（ログを確認）" if _orders_unsaved else "保存できています"))
    base = os.environ.get("PAYPAY_DATA_DIR", "instance/paypay")
    try:
        du = shutil.disk_usage(base if os.path.isdir(base) else ".")
        pct = du.free * 100 / du.total
        out.append(("ディスクの空き", "ok" if pct >= 15 else "warn" if pct >= 5 else "bad", f"{du.free / 2**30:.1f}GB 空き（{pct:.0f}%）"))
    except OSError:
        out.append(("ディスクの空き", "warn", "確認できません"))
    try:
        mem = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
        pct = mem["MemAvailable"] * 100 / mem["MemTotal"]
        load = os.getloadavg()[0]
        out.append(("メモリと負荷", "ok" if pct >= 15 else "warn" if pct >= 7 else "bad",
                    f"空き {mem['MemAvailable'] / 2**20:.1f}GB（{pct:.0f}%）・負荷 {load:.2f}（CPU {os.cpu_count()}個）"))
    except (OSError, KeyError, ValueError, ZeroDivisionError):
        pass
    socks = os.environ.get("TSUM_SOCKS_URL", "")
    if socks:
        target = urlsplit(socks)
        try:
            with socket.create_connection((target.hostname, target.port or 9050), timeout=1.5):
                pass
            out.append(("Tor（LINEログインの回線）", "ok", "つながります"))
        except OSError:
            out.append(("Tor（LINEログインの回線）", "bad", "つながりません（ツムツムの LINE ログインが失敗しやすくなります）"))
    stamp = os.path.join(os.path.dirname(os.path.abspath(base)), "last_backup")
    try:
        with open(stamp) as fh:
            at = float(fh.read().strip() or 0)
        age = time.time() - at
        out.append(("サーバーの自動バックアップ", "ok" if age < 36 * 3600 else "warn" if age < 4 * 86400 else "bad", f"最後 {_jst(at)}"))
    except (OSError, ValueError):
        out.append(("サーバーの自動バックアップ", "warn", "記録がありません（次のデプロイのあと、毎日の自動保存から記録されます）"))
    out.append(("Q&A・実績の AI", "ok" if os.environ.get("ANTHROPIC_API_KEY") else "warn",
                "鍵あり" if os.environ.get("ANTHROPIC_API_KEY") else "鍵なし（固定文の案内・決まった検査だけで動きます）"))
    out.append(("購入時の人間確認（Turnstile）", "ok" if human.enabled() else "warn", "有効" if human.enabled() else "無効（鍵が未設定）"))
    out.append(("管理画面の2段階認証", "ok" if admin_2fa.enabled() else "warn", "有効" if admin_2fa.enabled() else "無効（設定タブから有効にできます）"))
    return [{"name": n, "level": lv, "text": t} for n, lv, t in out]


@app.get("/admin/api/log")
def admin_log_api():
    denied = _admin_json_only()
    if denied:
        return denied
    return jsonify(items=[{"when": _jst(a["at"]), "label": AUDIT_LABELS.get(a["action"], a["action"]), "failed": a["action"] == "login_failed",
                           "target": a["target"] or "", "msg": a["msg"] or "", "ip": a["ip"] or ""} for a in admin_ops.action_log(100)])


@app.get("/admin/api/health")
def admin_health():
    denied = _admin_json_only()
    if denied:
        return denied
    with _jobs_lock:
        par = _parallel_state_locked()
    return jsonify(items=_health(), version=APP_VERSION, uptime=int(time.time() - _started_at),
                   slots={"normal": len(_slots), "long": len(_long_slots), "running": par.get("running_jobs", 0),
                          "queued": par.get("queued_jobs", 0), "long_running": par.get("long_running_jobs", 0),
                          "long_queued": par.get("long_queued_jobs", 0)})


FAQ_SHOPS = {"all": "全部の部門", "nyanko": "にゃんこ", "tsum": "ツムツム", "puni": "ぷにぷに"}


def _admin_page(msg):
    unlocked = _is_admin()
    pp, ky, paym = {}, {}, {}
    if unlocked:
        try:
            pp = paypay_gateway.status()
        except paypay_gateway.PayPayError as exc:
            msg = msg or exc.message
        try:
            ky = kyash_gateway.status()
        except paypay_gateway.PayPayError as exc:
            ky = {"error": exc.message}
        if ky.get("expires_at"):
            ky["expires_ja"] = _jst(ky["expires_at"])
        payments.forget()
        paym = {"switch": payments.switches(), "live": payments.available()}
    orders = []
    if unlocked:
        _gc_orders()
        with _orders_lock:
            snap = [(k, dict(v)) for k, v in _orders.items() if v["state"] != "started"]
        names = {"unpaid": "支払い待ち", "paying": "確認中", "hold": "保留中", "paid": "支払い済み・未開始", "starting": "開始処理中", "dead": "要確認（停止）"}
        for k, v in sorted(snap, key=lambda kv: -kv[1]["created"]):
            # 完了は出さない。ただし「結果不明」で完了扱いになったもの（コードの発行中に止まった・引き継ぎ後に失敗）は、
            # お客さんが困っている可能性が高いので、7日間は要対応として出す
            unknown = v["state"] == "done" and str(v.get("diag", "")).startswith("結果不明") and time.time() - v["created"] < 7 * 86400
            if v["state"] == "done" and not unknown:
                continue
            orders.append({"unknown": unknown, "id": k, "uid": v.get("uid") or _uid(v["owner"]), "summary": v.get("summary", ""), "amount": v["amount"], "state": v["state"], "state_ja": "結果不明（完了扱い）" if unknown else names.get(v["state"], v["state"]),
                           "created": time.strftime("%m/%d %H:%M", time.localtime(v["created"])),
                           "diag": v.get("diag", ""), "ledger": paypay_gateway.link_diag(v.get("link_code") or ""),
                           "provider": payments.LABELS[payments.provider_of(v.get("link_code"))] if v.get("link_code") else "",
                           "has_link": bool(v.get("link_code")), "superseded": str(v.get("diag", "")).startswith("superseded")})
    history, hist_user = [], (request.args.get("user", "") or "").strip()[:80]
    if unlocked:
        _reconcile_orders()
        names_h = {"unpaid": "未払い", "paying": "確認中", "hold": "保留", "paid": "支払い済み・未実行", "starting": "開始処理中", "started": "実行中", "done": "完了", "dead": "停止"}
        try:
            paypay_gateway.history_upsert(_history_rows())
            for r in paypay_gateway.history_list(200, hist_user):
                r["when"] = time.strftime("%m/%d %H:%M", time.localtime(r["created"]))
                r["state_ja"] = names_h.get(r["state"], r["state"])
                r["received"] = r.get("pay_state") == "paid"
                history.append(r)
        except Exception:
            _log_orders.exception("history view failed")
            msg = msg or "取引履歴を読み込めませんでした。"
    dash = {}
    if unlocked:
        with _jobs_lock:
            par = _parallel_state_locked()
        now = time.time()
        lt = time.localtime(now)
        day0 = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        try:
            allrows = paypay_gateway.history_list(500, "")
        except Exception:
            allrows = []
        def sales(since):
            rows = [r for r in allrows if r.get("pay_state") == "paid" and r["created"] >= since]
            return {"count": len(rows), "yen": sum(int(r.get("amount") or 0) for r in rows)}
        counts = {}
        for o in orders:
            counts[o["state"]] = counts.get(o["state"], 0) + 1
        with _orders_lock:   # 実行中の注文（長時間の周回は途中経過つき）
            running_orders = [{"id": k, "summary": v.get("summary", ""), "amount": v["amount"], "long": bool((v.get("job") or {}).get("long")),
                               "created": time.strftime("%m/%d %H:%M", time.localtime(v["created"])), "job_id": v.get("job_id"),
                               "puni": (v.get("job") or {}).get("mode") == "puni", "progress": _resume_note(v)}
                              for k, v in sorted(_orders.items(), key=lambda kv: -kv[1]["created"]) if v["state"] == "started"][:20]
        with _jobs_lock:
            queue = [e[0] for e in _pending_jobs]
            for r in running_orders:
                j = _jobs.get(r["job_id"]) or {}
                r["status"] = j.get("status")
                r["queue"] = queue.index(r["job_id"]) + 1 if r["job_id"] in queue else None
                r["step"] = (j.get("log") or [""])[-1][9:80] if j.get("log") else ""
        dash = {"running": par.get("running_jobs", 0), "queued": par.get("queued_jobs", 0), "max_jobs": par.get("max_concurrent_jobs", 0),
                "long_running": par.get("long_running_jobs", 0), "long_queued": par.get("long_queued_jobs", 0),
                "long_slots": par.get("long_slots", 0), "running_orders": running_orders,
                "today": sales(day0), "week": sales(now - 7 * 86400), "uptime": int(now - _started_at), "version": APP_VERSION,
                "attention": [o for o in orders if o["state"] in ("dead", "paid", "hold") or o["unknown"]],
                "counts": counts, "open_orders": len(orders)}
    coupons = []
    if unlocked:
        try:
            for c in paypay_gateway.coupon_list(100):
                c["when"] = time.strftime("%m/%d %H:%M", time.localtime(c["created"]))
                c["until"] = time.strftime("%m/%d", time.localtime(c["expires"]))
                c["state_ja"] = {"active": "未使用", "reserved": "使用中（支払い待ち）", "used": "使用済み"}.get(c["state"], c["state"])
                c["free"] = c.get("kind") in ("free", "period", "pct30")   # 取り消せる種類
                c["kind_ja"] = {"free": "1回無料", "period": "期間無料", "pct30": "支援30%引き"}.get(c.get("kind"), "")
                if c.get("kind") == "period" and c["expires"] - c["created"] > 3650 * 86400:
                    c["until"] = "無制限"
                if c["state"] == "used" and c.get("used_order") == "revoked":
                    c["state_ja"] = "取り消し"
                if c["state"] == "active" and time.time() > c["expires"]:
                    c["state_ja"] = "期限切れ"
                coupons.append(c)
        except Exception:
            _log_orders.exception("coupon list failed")
    reviews = []
    if unlocked:
        try:
            for r in paypay_gateway.review_list(100, include_hidden=True):
                reviews.append({"id": r["id"], "stars": r["stars"], "text": r["body"], "hidden": bool(r["hidden"]),
                                "shop": SHOP_LABELS.get(r["shop"], ""), "when": time.strftime("%m/%d %H:%M", time.localtime(r["created"]))})
        except Exception:
            _log_orders.exception("review list failed")
    box = []
    if unlocked:
        try:
            for r in paypay_gateway.feedback_list(200):
                box.append({"id": r["id"], "kind": BOX_KINDS.get(r["kind"], r["kind"]), "text": r["body"], "contact": r["contact"],
                            "user": r["user_id"], "done": bool(r["done"]), "when": _jst(r["created"])})
        except Exception:
            _log_orders.exception("feedback list failed")
    ops = {}
    if unlocked:
        try:
            st = admin_ops.settings()
            notice = st["notice"]
            sale = admin_ops.sale()
            ops = {"notice": notice, "notice_on": bool(admin_ops.notice()),
                   "notice_until": _jst(notice["until"]) if notice.get("until") else "",
                   "sale": sale, "sale_until": _jst(sale["until"]) if sale else "", "sale_shops": admin_ops.SALE_SHOPS,
                   "paused": st["paused"], "pausable": _pausable_menus(),
                   "shops": [(k, admin_ops.SHOP_LABELS[k]) for k in admin_ops.SHOPS],
                   "blocks": [dict(b, until_ja=_jst(b["until"]) if b.get("until") else "無期限", at_ja=_jst(b.get("at") or 0)) for b in admin_ops.blocks()],
                   "slots": admin_ops.slots(), "slot_now": (len(_slots), len(_long_slots)), "slot_default": _SLOT_DEFAULT,
                   "slot_limits": admin_ops.SLOT_LIMITS,
                   "jobs": admin_ops.job_summary(),
                   "faq": admin_ops.faq_list(),
                   "tsum_orphans": tsum_service.orphans(),
                   "paused_count": len(st["paused"].get("shops", [])) + len(st["paused"].get("menus", []))}
        except Exception:  # noqa: BLE001
            _log_orders.exception("ops view failed")
            msg = msg or "運用の設定を読み込めませんでした。"
    rp = restart_pack()
    rp_text = "\n".join(f"{k}={v}" for k, v in rp["items"].items())
    tfa = {"on": admin_2fa.enabled(), "waiting": not unlocked and _admin_2fa_waiting(),
           "setup": admin_2fa.pending("admin", site_config.SHOP_NAME) if unlocked and not admin_2fa.enabled() else None}
    return render_template_string(ADMIN_HTML, tfa=tfa, ops=ops, faq_shops=FAQ_SHOPS, coupons=coupons, reviews=reviews, box=box, box_open=sum(1 for b in box if not b["done"]), review_words="\n".join(review_extra_words()), review_stats=_review_stats() if unlocked else None, rp=rp, all_subs=SUBS, rp_text=rp_text, rp_cats=_compress(rp["cats"]) if rp["cats"] else "",
                                  maint=_maint(), msg=msg, csrf=session["admin_csrf"], unlocked=unlocked, pp=pp, ky=ky, paym=paym, orders=orders,
                                  history=history, hist_user=hist_user, dash=dash,
                                  prices=PRICES, price_labels=PRICE_LABELS, payments=PAYMENTS_ENABLED,
                                  coupon_min=COUPON_MIN, coupon_days=COUPON_DAYS)


# ---------------------------------------------------------------- 7. 注文（決済してから実行）
_orders: dict[str, dict] = {}
_orders_lock = threading.Lock()
# 保存（暗号化してディスクへ）を直列化する。snapshot と書き込みを1つの単位にして、
# 2スレッドの保存が競合して「古いスナップショットが新しいものを上書きする」のを防ぐ。
_persist_lock = threading.Lock()


# 状態ごとに注文を残す時間。保留・支払い済み・要確認は7日、実行中は1日、完了は3日。未払いは ORDER_TTL（30分）。
ORDER_KEEP = {"hold": 7 * 86400, "paid": 7 * 86400, "paying": 7 * 86400, "starting": 7 * 86400, "dead": 7 * 86400,
              "started": 86400, "done": 3 * 86400}
_log_orders = logging.getLogger("orders")


def _order_id_of(order: dict) -> str:
    with _orders_lock:
        return next((k for k, v in _orders.items() if v is order), "")


def _order_keep(o: dict) -> float:
    """この注文を残しておく秒数（最後に状態が変わった時から。_expired）。
    - 個別に指定があればそれ（ソースコード購入は30日）
    - 送金リンクを受け付けたことのある注文は、支払い待ちでも7日
    - それ以外は状態ごと（ORDER_KEEP）。未払いは30分（ORDER_TTL）"""
    if o.get("keep"):
        return float(o["keep"])
    return 7 * 86400 if o.get("link_code") else ORDER_KEEP.get(o["state"], ORDER_TTL)


IN_MOTION = ("paying", "starting", "started")   # 確認中・開始処理中・実行中。掃除（期限切れの削除）の対象にしない


def _expired(o: dict, now: float) -> bool:
    """この注文を掃除してよいか。期限は「最後に状態が変わった時」から数える。
    作成からの時間で数えると、何日か前の支払い済み注文を再実行したときに、動いている最中の注文を消してしまう。"""
    if o["state"] in IN_MOTION:
        return False
    return now - float(o.get("touched") or o["created"]) > _order_keep(o)


def _gc_orders() -> None:
    now = time.time()
    with _orders_lock:
        for v in _orders.values():   # 状態が変わっていたら、その時刻を控える（ここが期限の起点）
            if v.get("seen_state") != v["state"]:
                if "seen_state" in v:
                    v["touched"] = now
                v["seen_state"] = v["state"]
        expiring = [(k, v.get("coupon")) for k, v in _orders.items()
                    if v.get("coupon") and not v.get("coupon_used") and _expired(v, now)]
    for k, c in expiring:   # 使われないまま消える注文（未払い・保留・要確認など）のクーポンは、使えるように戻す
        try:
            paypay_gateway.coupon_release(c, k)
        except Exception:
            pass
    with _orders_lock:
        for k in [k for k, v in _orders.items() if _expired(v, now)]:
            del _orders[k]


def _is_tsum_arguments(args) -> bool:
    """ツムツム代行・ぷにぷに代行の注文は、実行用の内容を1つの辞書で持つ
    （にゃんこは11個の並び）。保存／復元ではどちらも同じ扱いでよいので一緒に判定する。"""
    return bool(args) and len(args) == 1 and isinstance(args[0], dict)


def _order_to_json(o: dict) -> dict:
    out = {k: v for k, v in o.items() if k not in ("arguments", "transfer_key")}
    args = o.get("arguments")
    if args is not None:
        a = list(args)
        if not _is_tsum_arguments(args):
            a[7] = {str(k): v for k, v in a[7].items()}  # cat_settings のキーを文字列に
        out["arguments"] = a
    tk = o.get("transfer_key")
    out["transfer_key"] = tk.hex() if tk else None
    return out


def _order_from_json(o: dict) -> dict:
    out = dict(o)
    if out.get("arguments") is not None:
        a = list(out["arguments"])
        if not _is_tsum_arguments(a):
            a[7] = {int(k): v for k, v in a[7].items()}
        out["arguments"] = tuple(a)
    out["transfer_key"] = bytes.fromhex(out["transfer_key"]) if out.get("transfer_key") else None
    if out.get("state") == "paying":  # 決済確認中に再起動した → 同じリンクを再確認できる保留へ
        out["state"] = "hold" if out.get("link_code") else "unpaid"
    if out.get("state") == "starting":  # 開始処理の途中で再起動した → 支払い済みに戻す（再送金なしで再開できる）
        out.update(state="paid", retry=True)
    return out


_orders_unsaved = False   # 直近の保存が失敗して未保存の変更が残っているか（監視用）


def _persist_orders() -> None:
    """注文を暗号化して保存する。失敗してもサービスは止めない（ログのみ）。

    _persist_lock で直列化し、ロックの中で snapshot→保存まで行う。こうすると、
    後から始まった保存ほど新しい状態を書くので、競合で古い状態に巻き戻らない（S2）。
    保存は毎回「全注文の最新スナップショット」を書くので、1回失敗しても次の状態変化で
    まとめて書き直される（自己修復）。支払いの正は PayPay 台帳（payments.sqlite3）側に
    あるので、ここが落ちても入金・リンクは台帳から復元できる。"""
    global _orders_unsaved
    if not PAYMENTS_ENABLED:
        return
    with _persist_lock:
        with _orders_lock:
            snapshot = {k: _order_to_json(v) for k, v in _orders.items()}
        try:
            paypay_gateway.save_orders(snapshot)
            _orders_unsaved = False
        except Exception:
            _orders_unsaved = True
            _log_orders.exception("order persistence failed")
    try:
        paypay_gateway.history_upsert(_history_rows())
    except Exception:
        _log_orders.exception("history update failed")


def _reconcile_orders() -> None:
    """実行を開始した注文の結末を反映する。
    - 完了 → done（引継ぎコードなどの入力は消す）
    - 失敗・取り消し・中断（サーバー再起動など）→ 支払い済みに戻す（払い直しなしで再実行できる）
    - コード発行中に結果不明 → done 扱い（自動では再実行させない。管理画面で対応）"""
    changed = False
    issue_for = []
    stats = []        # 実行の結末（メニュー別の成功率に使う）：(部門, メニュー, 成功か, 理由, 注文)
    stamp = time.strftime('%m/%d %H:%M')
    progressed = False
    with _orders_lock:
        # 実行の一覧は、注文のロックを持ったまま写す。先に写すと、その直後に始まった実行が写しに無く、
        # 動いている注文を「中断」と見なして支払い済みに戻してしまう（＝同じ支払いで2回実行できてしまう）。
        with _jobs_lock:
            jobs = {k: (v.get("status"), v.get("error"), bool(v.get("outcome_unknown")), _has_result(v.get("result")), _job_delivered(v))
                    for k, v in _jobs.items()}
            results = {k: (v.get("result"), bool(v.get("clone")), bool(v.get("dry"))) for k, v in _jobs.items() if _has_result(v.get("result"))}
            outcomes = {k: _job_outcome(v) for k, v in _jobs.items() if _job_outcome(v)}
            checkpoints = {k: v["checkpoint"] for k, v in _jobs.items() if v.get("checkpoint")}
        for k_order, o in _orders.items():
            if o["state"] != "started":
                continue
            cp = checkpoints.get(o.get("job_id"))
            if cp and cp != o.get("checkpoint"):   # 長い周回の途中経過を注文に写す（止まっても続きから再開できるように）
                o["checkpoint"] = cp
                progressed = True
            info = jobs.get(o.get("job_id"))
            if info and info[0] in ("running", "queued"):
                continue
            if info and info[0] == "done":
                stats += [(_order_dept(o), m, True, None, k_order) for m in _stat_menus(o)]
                o.update(state="done", arguments=None, transfer_key=None, diag=f"done {stamp}")
                o.pop("checkpoint", None)
                if not o.get("admin_free"):
                    o["gate"] = True   # 結果は、評価と感想を受け取ってから渡す（_review_locked）
                if not o.get("admin_free") and int(o.get("amount") or 0) >= COUPON_MIN and not o.get("coupon_issued"):
                    issue_for.append((k_order, o))
                if o.get("job_id") in results:  # 発行されたコードを注文に残す（暗号化して保存。あとから開いても見られる）
                    o["result"], o["clone"], o["dry"] = results[o["job_id"]]
                if o.get("job_id") in outcomes:  # コードの出ない注文（周回など）も、何をしたかの控えを残す＝実行の記録が消えたあとも一覧に出せる
                    o["outcome"] = outcomes[o["job_id"]]
                o.pop("delivered", None)
            elif info and (info[2] or info[3]):
                stats += [(_order_dept(o), m, False, "結果不明：" + str(info[1] or "")[:80], k_order) for m in _stat_menus(o)]
                o.update(state="done", arguments=None, transfer_key=None, diag=f"結果不明: {str(info[1])[:80]} {stamp}")
                if o.get("job_id") in results:
                    o["result"], o["clone"], o["dry"] = results[o["job_id"]]
            else:
                reason = str(info[1])[:80] if info else "中断（サーバー再起動など）"
                if not reason.startswith("順番待ちを取り消しました"):   # お客さんの取り消しは失敗に数えない
                    stats += [(_order_dept(o), m, False, reason, k_order) for m in _stat_menus(o)]
                o.update(state="paid", retry=True, job_id=None, diag=f"失敗→再実行可: {reason} {stamp}")
                if info and info[4]:
                    o["delivered"] = True   # 一部は提供済み：この支払いを別の内容に充てさせない（_place_order・_adopt_existing_link）
                _carry_progress(o, interrupted=info is None)
            changed = True
    if stats:
        try:
            admin_ops.record_jobs(stats)
        except Exception:  # noqa: BLE001  集計の記録が書けなくても、注文の処理は止めない
            _log_orders.exception("job stats failed")
    for k_order, o in issue_for:   # 200円以上の購入にはクーポンを1枚発行（次回200円以上で10%引き・1回きり）
        try:
            o["coupon_issued"] = paypay_gateway.coupon_issue(k_order, COUPON_DAYS)
        except Exception:
            _log_orders.exception("coupon issue failed")
    global _checkpoint_saved
    if changed:
        _persist_orders()
    elif progressed and time.time() - _checkpoint_saved >= CHECKPOINT_SAVE_EVERY:
        _checkpoint_saved = time.time()   # 途中経過だけの変化は、ときどきまとめて保存する（1周ごとには書かない）
        _persist_orders()


CHECKPOINT_SAVE_EVERY = 60     # 長い周回の途中経過をディスクに書く間隔（秒）。再起動で失うのは最大この時間ぶん


NYANKO_KINDS = {"clone": "にゃんこ：アカウント複製", "integrity": "にゃんこ：バン保証", "restart": "にゃんこ：初期垢作成",
                "download": "にゃんこ：引き継ぎ", "new": "にゃんこ：新規アカウント"}


def _nyanko_kind(job: dict, arguments=None) -> str:
    if job.get("clone"):
        return "clone"
    if job.get("integrity_only") or job.get("mode") == "integrity":
        return "integrity"
    others = arguments[4] if arguments and not _is_tsum_arguments(arguments) and len(arguments) > 4 else []
    if RESTART_KEY in (others or []):
        return "restart"
    return "download" if job.get("mode") == "download" else "new"


def _stat_menus(o: dict) -> list:
    """成功率の集計に使うメニュー名（ツムツム・ぷにぷにはメニューごと、にゃんこは種類ごと）。呼ぶ側で _orders_lock を持つ。"""
    if o.get("product"):
        return []
    job, args = o.get("job") or {}, o.get("arguments")
    if job.get("mode") == "tsum" and _is_tsum_arguments(args):
        return [tsum_service.menu_label(m) for m in args[0].get("menus") or []][:10] or ["ツムツム"]
    if job.get("mode") == "puni" and _is_tsum_arguments(args):
        return [puni_service.menu_label(m) for m in args[0].get("menus") or []][:10] or ["ぷにぷに"]
    if job.get("mode") in ("tsum", "puni"):
        return ["ツムツム" if job.get("mode") == "tsum" else "ぷにぷに"]
    return [NYANKO_KINDS[_nyanko_kind(job, args)]]


def _has_result(result) -> bool:
    """渡すもの（コード・アカウント情報）が実際に入っているか。複製は、発行の前に空の枠だけ先に作るので、中身で見る。"""
    if isinstance(result, dict):
        return any(v is not None for v in result.values())
    return result is not None


def _progress_made(checkpoint) -> bool:
    return bool(isinstance(checkpoint, dict) and (checkpoint.get("finished") or checkpoint.get("done") or checkpoint.get("elapsed")))


def _job_delivered(job: dict) -> bool:
    """失敗した実行が、すでに一部を提供しているか（ツムツム・ぷにぷに：済んだメニューや周回がある）。
    にゃんこは、最後のアップロードで初めて反映されるので、失敗＝何も提供していない。"""
    part = job.get("tsum") or job.get("puni") or {}
    return bool(part.get("done")) or _progress_made(job.get("checkpoint"))


def _job_outcome(job: dict):
    """完了した実行の控え（何をしたかの行。秘密は入らない）。ツムツム・ぷにぷにだけ。"""
    part = job.get("tsum") or job.get("puni")
    if not part or not part.get("lines"):
        return None
    return {"lines": [str(x)[:200] for x in part["lines"]][:40], "menus": [str(x)[:60] for x in (part.get("menus") or [])][:40]}


def _partly_delivered(o: dict) -> bool:
    """失敗して「支払い済み」に戻った注文が、すでに一部を提供しているか。"""
    return bool(o.get("delivered") or _progress_made(o.get("checkpoint")))


def _same_content(o: dict, job: dict, arguments) -> bool:
    """同じ内容の入れ直しか（部門・メニュー・数量が同じ。ログイン情報やステージ番号の直しは、同じ内容とみなす）。"""
    old = o.get("arguments")
    if not (_is_tsum_arguments(old) and _is_tsum_arguments(arguments)):
        return False
    return ((o.get("job") or {}).get("mode") == job.get("mode") and (old[0].get("menus") or []) == (arguments[0].get("menus") or [])
            and (old[0].get("qty") or {}) == (arguments[0].get("qty") or {}))
AUTO_RESUME_MAX = 5            # 再起動で止まった長時間の注文を、自動で続きから始める回数の上限
_checkpoint_saved = 0.0


def _carry_progress(o: dict, interrupted: bool) -> None:
    """実行が止まった注文に、次の実行のための「続き」を持たせる。呼び出し側で _orders_lock を持つこと。
    - 途中経過（checkpoint）があれば、実行内容に resume として入れる（済んだメニュー・周回は繰り返さない）
    - サーバーの再起動で止まった長時間の注文は、起動後に自動で続きから始める（お客さんの操作を待たない）"""
    args = o.get("arguments")
    if not _is_tsum_arguments(args) or (o.get("job") or {}).get("mode") != "puni":
        return
    if o.get("checkpoint"):
        args[0]["resume"] = o["checkpoint"]
    if interrupted and (o.get("job") or {}).get("long") and int(o.get("auto_resumed") or 0) < AUTO_RESUME_MAX:
        o["auto_resume"] = True


def _auto_resume_orders() -> None:
    """起動のあと：再起動で止まった長時間の注文を、続きから始める。
    デプロイは長時間の周回が終わるのを待たずに入れ替えるので、その続きをここで拾う。"""
    time.sleep(8)   # ほかの準備（注文の読み込み・照合）が落ち着いてから
    with _orders_lock:
        targets = [o for o in _orders.values() if o.get("auto_resume") and o["state"] == "paid" and o.get("arguments") is not None]
        for o in targets:
            o.pop("auto_resume", None)
            o["auto_resumed"] = int(o.get("auto_resumed") or 0) + 1
    for o in targets:
        try:
            with app.app_context():
                _start_paid_order(o)
        except Exception:
            _log_orders.exception("auto resume failed")
    if targets:
        _persist_orders()


def _resume_note(o: dict):
    """「続きから」のときに画面へ出す一言（例：120周まで完了）。途中経過が無ければ None。"""
    cp = o.get("checkpoint") or {}
    if cp.get("menu") == "puni_farm" and cp.get("done"):
        return f"{int(cp['done'])}周まで完了"
    if cp.get("menu") == "puni_unlimited" and cp.get("elapsed"):
        return f"{int(cp['elapsed']) // 60}分ぶん完了"
    if cp.get("finished"):
        return f"{len(cp['finished'])}件のメニューが完了"
    return None


def _rescue_stranded_orders() -> None:
    """旧バージョンの不具合で「支払い待ち」に戻された注文（送金リンクを受け付け済み）を、管理者の確認待ちにする。"""
    with _orders_lock:
        cands = [(k, o) for k, o in _orders.items() if o["state"] == "unpaid" and o.get("link_code")]
    changed = False
    for k, o in cands:
        try:
            if paypay_gateway.link_order_id(o["link_code"]) == k:
                with _orders_lock:
                    if o["state"] == "unpaid":
                        o["state"] = "dead"
                        o["diag"] = "要確認: 旧バージョンで停止（「PayPayに再確認する」を押してください）"
                        changed = True
        except Exception:
            pass
    if changed:
        _persist_orders()


def _order_dept(o: dict) -> str:
    """売上の集計に使う部門（nyanko / tsum / puni / ticket / support / source）。"""
    product = (o.get("product") or {}).get("type")
    if product:
        return "ticket" if str(product).startswith("ticket") else product
    mode = (o.get("job") or {}).get("mode")
    return mode if mode in ("tsum", "puni") else "nyanko"


def _history_rows() -> list:
    with _orders_lock:
        return [{"order_id": k, "created": v["created"], "user_id": v.get("uid") or _uid(v["owner"]), "amount": v["amount"],
                 "state": v["state"], "summary": v.get("summary", ""), "diag": v.get("diag", ""),
                 "shop": _order_dept(v), "base": int(v.get("base_amount") if v.get("base_amount") is not None else v["amount"]),
                 "lines": json.dumps([{"label": str(b.get("label", ""))[:60], "count": b.get("count"), "unit": b.get("unit"),
                                       **({"coupon": True} if b.get("coupon") else {})}
                                      for b in (v.get("breakdown") or [])[:40] if isinstance(b, dict)], ensure_ascii=False),
                 "coupon_kind": v.get("coupon_kind")} for k, v in _orders.items()]


def _load_orders() -> None:
    if not PAYMENTS_ENABLED:
        return
    try:
        data = paypay_gateway.load_orders()
    except Exception:
        _log_orders.exception("order load failed")
        return
    with _orders_lock:
        for k, v in data.items():
            try:
                _orders.setdefault(k, _order_from_json(v))
            except Exception:
                _log_orders.warning("skipped unreadable order")
    _gc_orders()


# 実行用の子プロセス（spawn）もこのモジュールを読み込むため、注文の読み込み・照合はサーバー本体でだけ行う。
# （子プロセスで行うと、実行中の注文を「中断された」と誤判定してディスクに書き戻してしまう）
def _is_server_process() -> bool:
    # spawn の子プロセスは、モジュール読み込み中はまだ parent_process() が設定されていない。
    # 読み込み前にプロセス名が子の名前に変わり、_inheriting が立つので、それで判定する。
    cur = multiprocessing.current_process()
    return cur.name == "MainProcess" and not getattr(cur, "_inheriting", False)


if _is_server_process():
    _load_orders()
    try:
        _reconcile_orders()  # 再起動で中断された実行を「支払い済み」に戻す
        _rescue_stranded_orders()
        threading.Thread(target=_auto_resume_orders, daemon=True).start()   # 長時間の周回は、続きから自動で再開
    except Exception:
        _log_orders.exception("reconcile at startup failed")


# ---------------------------------------------------------------- 8. 画面のページ
@app.get("/")
def home():
    """トップ：正方形のカードが並ぶ入口（代行3部門・チケット・支援BOX・ソースコード）。
    それぞれ別のページなので、ブラウザの戻るボタンで素直に行き来できる。"""
    if request.args.get("game") == "nyanko":        # 以前の入口（/?game=nyanko）
        return redirect("/nyanko", code=301)
    return render_template_string(HOME_HTML, prices=PRICES, low=_lowest_prices(), maint=_maint(),
                                  reviews=_review_stats() if site_config.REVIEWS_ENABLED else None,
                                  version=APP_VERSION, site=_site())


def _lowest_prices() -> dict:
    """トップの「○円〜」に出す、部門ごとのいちばん安い料金（管理画面で変えた料金に追従する）。"""
    def cheapest(service) -> int:
        return min((PRICES.get(m["price"], 0) for m in service.MENUS.values()), default=0)
    return {"nyanko": min(PRICES["other"], PRICES["crash"], PRICES["chara"]),
            "tsum": cheapest(tsum_service), "puni": cheapest(puni_service)}


@app.get("/nyanko")
def index():
    p = PRICES
    groups = [
        ("qty", "アイテム", f"個数を決めて付与します。最大まで入れたいときは「すべて最大値」を押してください。空欄は変更しません（1項目 {p['other']}円）", ITEMS),
        ("chars", "キャラ", f"キャラを選び、レベル・形態・本能を1体ずつ設定します（1体・1項目ごとに {p['chara']}円）", []),
        ("subs", "サブ", f"キャラ・ステージなどをまとめて解放・強化します（1項目 {p['other']}円。レジェンドストーリーは段階が上がるごとに +{p['other']}円）", SUBS),
        ("events", "イベステ", f"イベントステージを、マップごとにクリア済みにします（1マップ {p['other']}円）。「キャラ付き」のマップは、そこでドロップするキャラも獲得済みにします。大狂乱・絶など、前のステージのクリアが必要なものは、それを選ぶと押せるようになります", EVENT_MAPS),
        ("others", "その他", f"会員状態やミッションなどを設定します（1項目 {p['other']}円・クラッシュ修正は {p['crash']}円）", OTHERS),
    ]
    totals = {k: len(v) for k, _, _, v in groups}
    return render_template_string(
        MAIN_HTML, ev_names={m["id"]: m["name"] for m in EVENT_MAPS}, legend_levels={k: v[1] for k, v in engine.LEGEND_TIERS.items()}, is_admin=_is_admin() and PAYMENTS_ENABLED, restart=restart_pack(), pt_min=PT_MIN_MINUTES, coupon_min=COUPON_MIN, maint=_maint(), uid=_uid(_owner()), payments=PAYMENTS_ENABLED, prices=PRICES, crash_items=CRASH_ITEMS, groups=groups, totals=totals, version=APP_VERSION,
        limits=engine.QUANTITY_ITEM_LIMITS, high=HIGH_RISK_ITEMS,
        shop_reviews=_shop_reviews("nyanko"),
        type_names={k: v for k, v in engine.ITEM_TYPE_NAMES.items() if k != "本能玉"},
        orb_kinds=item_catalog.ORB_KIND_NAMES, orb_grades=item_catalog.ORB_GRADES,
        error_nums=sorted(i + DB_OFFSET for i in engine.ERROR_CAT_IDS), site=_site(),
    )


def _site() -> str:
    """検索とリンクプレビューで共通の公開 URL。独自ドメインは SITE_URL で固定する。"""
    configured = os.environ.get("SITE_URL", "").strip().rstrip("/")
    if configured:
        parsed = urlsplit(configured)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username or parsed.password or parsed.path
                or parsed.query or parsed.fragment):
            raise ValueError("SITE_URL must be an absolute http(s) origin without a path")
        return configured
    scheme = "https" if (request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure) else request.scheme
    return f"{scheme}://{request.host}"


@app.get("/robots.txt")
def robots_txt():
    return Response(
        "User-agent: *\nAllow: /\nDisallow: /admin\nDisallow: /api/\n"
        "Disallow: /status/\nDisallow: /health\nDisallow: /dya/\nDisallow: /osero/\nDisallow: /hunt/\n"
        f"Sitemap: {_site()}/sitemap.xml\n",
        mimetype="text/plain",
    )


@app.get("/sitemap.xml")
def sitemap_xml():
    urls = "".join(f"<url><loc>{xml_escape(_site() + path)}</loc></url>"
                   for path in ("/", "/nyanko", "/tsum", "/puni", "/ticket", "/support", "/source", "/box")
                   + (("/reviews",) if site_config.REVIEWS_ENABLED else ()))
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        + urls + '</urlset>', mimetype="application/xml",
    )


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


def _shop_reviews(shop: str):
    """そのお店の画面に出す実績のまとめ（件数と平均）。実績の機能を止めている・まだ0件なら None（何も出さない）。"""
    if not site_config.REVIEWS_ENABLED:
        return None
    stats = _review_stats(shop)
    return stats if stats["count"] else None


def _service_menus(service) -> list:
    """ツムツム・ぷにぷにの画面に渡すメニュー一覧（表示順・いまの料金つき）。"""
    return [dict(key=k, **{kk: vv for kk, vv in v.items() if kk != "price"}, amount=PRICES.get(v["price"], 0))
            for k, v in sorted(service.MENUS.items(), key=lambda kv: kv[1]["order"])]


@app.get("/tsum")
def tsum_page():
    """ツムツム代行の画面（にゃんこ代行とは別の大メニュー）。"""
    menus = _service_menus(tsum_service)
    return render_template_string(
        TSUM_HTML, menus=menus, modes=tsum_service.MODE_LABELS, solo=list(tsum_service.SOLO_MENUS),
        is_admin=_is_admin() and PAYMENTS_ENABLED, maint=_maint(), payments=PAYMENTS_ENABLED,
        coupon_min=COUPON_MIN, uid=_uid(_owner()), version=APP_VERSION, total_steps=TSUM_TOTAL_STEPS, site=_site(),
        shop_reviews=_shop_reviews("tsum"),
    )


@app.get("/puni")
def puni_page():
    """ぷにぷに代行の画面（にゃんこ・ツムツムに次ぐ3つ目の大メニュー）。"""
    menus = _service_menus(puni_service)
    return render_template_string(
        PUNI_HTML, menus=menus, modes=puni_service.MODE_LABELS, solo=list(puni_service.SOLO_MENUS),
        stage_menus=list(puni_service.STAGE_MENUS), long_farm_laps=puni_service.LONG_FARM_LAPS,
        is_admin=_is_admin() and PAYMENTS_ENABLED, maint=_maint(), payments=PAYMENTS_ENABLED,
        coupon_min=COUPON_MIN, uid=_uid(_owner()), version=APP_VERSION, total_steps=PUNI_TOTAL_STEPS, site=_site(),
        shop_reviews=_shop_reviews("puni"),
    )


_drain_until = 0.0


def _draining() -> bool:
    """アップデート直前。新しい決済確認・実行の開始を少し待ってもらう（画面側が自動で再試行する）。"""
    return time.time() < _drain_until


def _updating_response():
    resp = jsonify(error="ただいまアップデート中です。数十秒〜数分で自動的に続きます。", code="updating")
    resp.headers["Retry-After"] = "5"
    return resp, 503


@app.post("/api/drain")
def api_drain():
    """デプロイ用: 管理者パスコードで呼ぶと、15分間（再起動まで）新しい実行の開始を待たせる。"""
    global _drain_until
    if not _local_request():          # サーバーの中（デプロイの手順）からだけ。外からは存在しないのと同じ扱い
        return jsonify(error="not found"), 404
    if _too_many_fails("drain"):
        return jsonify(error="too many"), 429
    token = request.form.get("token", "")
    if not ADMIN_PASSWORD or not hmac.compare_digest(token.encode(), ADMIN_PASSWORD.encode()):
        _note_fail("drain")
        return jsonify(error="forbidden"), 403
    _drain_until = 0.0 if request.form.get("off") else time.time() + 900
    return jsonify(ok=True, draining=_draining())


def _count_paying() -> int:
    with _orders_lock:
        return sum(1 for o in _orders.values() if o["state"] in ("paying", "starting"))


@app.get("/api/state")
def api_state():
    """画面の接続表示用。軽い処理だけを返す。"""
    with _jobs_lock:
        parallel = _parallel_state_locked()
    return jsonify(
        ok=True,
        version=APP_VERSION,
        game_data_version=_effective_version(),
        pinned_version=GAME_DATA_VERSION,
        learned_version=_learned["version"] if _effective_version() != GAME_DATA_VERSION else None,
        bcsfe_version=_bcsfe_version(),
        api_missing=_api["missing"],
        uptime=int(time.time() - _started_at),
        started_at=int(_started_at),
        # busy/active_jobs intentionally mean real worker jobs only.
        busy=parallel["running_jobs"] > 0,
        paying_orders=_count_paying(), draining=_draining(),  # 決済確認中の注文数（デプロイ前の待ち合わせに使う）
        **parallel,
        warm_cache=dict(_warm_cache),
        catalog_version=_names.get("version"),
        catalog_update=dict(_catalog_update),
        bcsfe={
            "status": _bcsfe_state["status"],
            "error": _bcsfe_state["error"],
            "took": _bcsfe_state["took"],
            "elapsed": int(time.time() - _bcsfe_state["since"])
            if _bcsfe_state["status"] == "loading" and _bcsfe_state["since"]
            else None,
        },
    )


@app.post("/api/warmup")
def api_warmup():
    """ゲームデータを先に準備しておく（実行ボタンを押す前に待ち時間を済ませる）。"""
    if not _rate_ok("warmup:" + _client_ip(), 10, 600):
        return _too_fast()
    if _warm_cache_lock.acquire(blocking=False):
        _warm_cache.update(status="loading", prepared=0, error=None)
        threading.Thread(target=_warmup_thread, daemon=True).start()
    return jsonify(status=_warm_cache["status"])


@app.get("/api/cats")
def api_cats():
    """キャラ名の一覧（検索用）。未取得なら裏で取りにいく。"""
    _start_names()
    return jsonify(version=_names.get("version"), updated=_names.get("updated"), state=_names["state"], count=len(_names["items"]), method=_names["method"],
                   error=_names["error"], items=_names["items"] if _names["state"] == "ready" else [])


@app.post("/api/cats/latest")
def api_cats_latest():
    if not _rate_ok("latest:" + _client_ip(), 2, 600) or not _rate_ok("latest:*", 6, 3600):   # 外の配布元へ取りに行くので控えめに
        return _too_fast()
    if not _run_lock.acquire(blocking=False):
        return jsonify(error="データを準備中です。完了してからもう一度押してください。"), 409
    _catalog_update.update(status="loading", error=None)
    try:
        threading.Thread(target=_latest_catalog_thread, daemon=True).start()
    except Exception:
        _catalog_update.update(status="error", error="更新を開始できませんでした。")
        _run_lock.release()
        return jsonify(error=_catalog_update["error"]), 503
    return jsonify(status="loading"), 202


@app.post("/api/cats/refresh")
def api_cats_refresh():
    if not _rate_ok("refresh:" + _client_ip(), 3, 600):
        return _too_fast()
    if _names["state"] == "ready":
        # いまの一覧は使えている → 残したまま、裏で作り直す（だれでも押せるので、捨ててしまうと全員の検索が空になる）
        if not _names["refreshing"]:
            _names.update(refreshing=True, tried=time.time())
            threading.Thread(target=_names_thread, kwargs={"keep": True}, daemon=True).start()
    elif _names["state"] in ("error", "none"):
        _names.update(state="none", items=[], method=None, error=None)
        _start_names()
    return jsonify(state=_names["state"])


# ---------------------------------------------------------------- 9. 注文を作る
_SURROGATES = re.compile("[\ud800-\udfff]")
JSON_MAX_DEPTH = 12


def _scrub_json(value, depth: int = 0):
    """受け取った JSON を無害にする：文字として壊れたもの（単独のサロゲート）を取り除き、深すぎる入れ子は捨てる。
    壊れた文字が1つでも注文に入ると、注文の保存（UTF-8）・台帳・管理画面がまとめて止まる。"""
    if isinstance(value, str):
        return _SURROGATES.sub("", value)
    if depth >= JSON_MAX_DEPTH:
        return None
    if isinstance(value, list):
        return [_scrub_json(v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {(_SURROGATES.sub("", k) if isinstance(k, str) else k): _scrub_json(v, depth + 1) for k, v in value.items()}
    return value


def _read_json():
    """送られてきた JSON（形が変・深すぎる・壊れた文字入りでも、例外にしない）。読めなければ None。"""
    try:
        return _scrub_json(request.get_json(silent=True))
    except RecursionError:      # [[[[…]]]] のような深すぎる入れ子
        return None


def _json_body() -> dict:
    """送られてきた JSON を辞書として受け取る。配列・文字列・数だけが送られてきても、空の辞書として扱う（500 にしない）。"""
    data = _read_json()
    return data if isinstance(data, dict) else {}


MAX_NUMBER_DIGITS = 12   # 数として受け付ける桁数。これより長い数字の並びは、変換せずに断る（巨大な数で固まらないように）


def _clean_run_body(data):
    """/run に送られてきた内容の形をそろえる（画面以外から変な形で送られても、500 にしない）。"""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None
    out = dict(data)
    for k in ("items", "subs", "others", "events"):          # 文字（または数字）の一覧だけを受け付ける
        v = out.get(k)
        out[k] = [x for x in v if isinstance(x, (str, int)) and not isinstance(x, bool)][:2000] if isinstance(v, list) else []
        if k != "events":
            out[k] = [str(x) for x in out[k]]
    for k in ("qty", "cat_settings"):
        if not isinstance(out.get(k), dict):
            out[k] = {}
    for k in ("transfer", "confirm", "chars", "coupon"):
        v = out.get(k)
        out[k] = v if isinstance(v, str) else ("" if v is None or isinstance(v, (list, dict, bool)) else str(v))
    return out




_PERIOD_BUSY = ("paying", "starting", "started")   # 期間チケットの注文が「進行中」とみなす状態（失敗して止まったままの注文では塞がない）


def _place_order(job: dict, arguments: tuple, transfer_key, amount: int, breakdown: list, summary: str,
                 skipped: list, coupon_raw: str):
    """注文を作る（支払い待ち）。クーポン・未払い注文の整理・支払い済みの充当は、にゃんこ代行と
    ツムツム代行で共通。金額と内訳と要約は、メニューごとに呼び出し側が計算して渡す。"""
    _gc_orders()
    _reconcile_orders()
    coupon = unicodedata.normalize("NFKC", str(coupon_raw or "")).strip().upper().replace(" ", "")[:20]
    base_amount = amount
    free_coupon = False
    coupon_kind = None
    stale: list = []   # 同じクーポンを押さえている、この人の古い（まだ始まっていない）注文

    def keep_old_orders() -> None:
        """新しい注文でクーポンを使わないことになった → 古い注文がまたクーポンを使えるよう、押さえ直す。"""
        if coupon_kind == "period":   # 期間チケットは何度も使うので、押さえない（押さえると、以後の注文が「使用中」で通らなくなる）
            return
        for k in stale[:1]:
            try:
                paypay_gateway.coupon_reserve(coupon, k)
            except Exception:  # noqa: BLE001
                _log_orders.exception("coupon re-reserve failed")

    def reject(body, status):
        """注文を作らずに断る。古い注文は生かしたままにする（断ったのに前の注文まで消える、を防ぐ）。"""
        keep_old_orders()
        return body, status

    own_credit = None   # 入力されたコードを、この人の「実行できなかった注文」がもう使っている → その注文（の無料）を今回に充てる
    if coupon:
        me_own = _owner()
        with _orders_lock:
            own_credit = next((k for k, o in _orders.items() if o["owner"] == me_own and o.get("coupon") == coupon and o.get("coupon_used")
                               and o["state"] == "paid" and o.get("retry") and o.get("coupon_kind") != "period"), None)
        if own_credit:
            coupon = ""   # コードはもう使用済み。もう一度入れた＝「あの無料を今回に使う」というお客さんの意思とみなす（下の credit）
    if coupon:
        # 同じ人が支払う前に注文を作り直したとき：前の（未払いの）注文が押さえているクーポンを、新しい注文へ移す。
        # 先に押さえを外して確認し、新しい注文ができた時点で古い注文を閉じる（下の close_old_orders）。
        me0 = _owner()
        with _orders_lock:
            # 無料クーポン・期間チケットで開始に失敗したまま（お金は動いていない・一度も始まっていない）の注文も同じ扱い
            stale.extend(k for k, o in _orders.items() if o["owner"] == me0 and o.get("coupon") == coupon and not o.get("coupon_used")
                         and (o["state"] == "unpaid" or (o["state"] == "paid" and o.get("free_coupon") and not o.get("retry"))))
        for k in stale:
            try:
                paypay_gateway.coupon_release(coupon, k)
            except Exception:  # noqa: BLE001
                _log_orders.exception("coupon release failed")
        ok, why = paypay_gateway.coupon_check(coupon)
        coupon_kind = paypay_gateway.coupon_kind(coupon) if ok else None
        if coupon_kind in ("free", "period"):
            if _draining():
                return reject(*_updating_response())
            free_coupon = True
            amount = 0           # 1回無料（管理者発行・チケット）／期間無料チケット：金額に関係なく0円
            label = "期間無料チケット" if coupon_kind == "period" else "無料クーポン"
            breakdown.append({"label": f"{label} {coupon}", "count": 1, "unit": -base_amount, "coupon": True})
        elif coupon_kind == "pct30":
            amount = base_amount * (100 - SUPPORT_COUPON_PCT) // 100   # 支援BOXのお礼：30%引き（最低金額なし）
            breakdown.append({"label": f"支援クーポン（{SUPPORT_COUPON_PCT}%引き）{coupon}", "count": 1, "unit": amount - base_amount, "coupon": True})
        else:
            if not ok:   # 使えないコード（使用済み・期限切れ・見つからない）は、金額の条件より先に伝える
                return reject(jsonify(error=why, code="coupon_invalid"), 400)
            if base_amount < COUPON_MIN:
                return reject(jsonify(error=f"クーポンは {COUPON_MIN}円以上のご注文で使えます（いまは {base_amount}円）。", code="coupon_min"), 400)
            amount = base_amount * 9 // 10   # 10%引き（1円未満切り捨て）
            breakdown.append({"label": f"クーポン（10%引き）{coupon}", "count": 1, "unit": amount - base_amount, "coupon": True})
    sale_pct = 0 if free_coupon else admin_ops.sale_pct(_order_dept({"job": job}))
    if sale_pct:   # セール（管理画面で期間を決める）：クーポンのあとの金額から、さらに割り引く
        sale_info = admin_ops.sale() or {}
        discounted = amount * (100 - sale_pct) // 100
        breakdown.append({"label": f"{sale_info.get('label') or 'セール'}（{sale_pct}%引き）", "count": 1,
                          "unit": discounted - amount, "coupon": True, "sale": True})
        amount = discounted
    off_pct = 0 if free_coupon else payments.paypay_off_pct()
    if off_pct and amount > 0:   # PayPay が使えないあいだは全品割引（Kyash で払うので）：ここまでの金額から、さらに割り引く
        discounted = amount * (100 - off_pct) // 100
        breakdown.append({"label": f"{payments.PAYPAY_OFF_LABEL}（{off_pct}%引き）", "count": 1,
                          "unit": discounted - amount, "coupon": True, "sale": True})
        amount = discounted
    me = _owner()
    trimmed_coupons = []
    with _orders_lock:
        # 期間チケットは1件ずつ順番に使う（使い回し・共有での同時大量注文を防ぐ）。確認と注文の作成を同じロックの中で行う
        if coupon_kind == "period" and any(o.get("coupon") == coupon and o["state"] in _PERIOD_BUSY for o in _orders.values()):
            return reject(jsonify(error="この期間チケットは、いま別のご注文で使用中です。その注文が終わると次の注文に使えます（1件ずつ順番に使えます）。",
                           code="coupon_busy"), 409)
        # 支払い済みなのに実行できなかった注文があれば、その支払いを今回の内容に充てる（払い直し不要）。
        # ・同じ部門で同じ金額（入力を直して送り直した）→ そのまま充てる
        # ・同じ部門で今回のほうが安い → お客さんに聞いてから（use_credit）。黙って充てると、差額と、前の注文をやり直す権利が知らないうちに消える
        # ・別の部門 → 充てない（前の注文は、その部門の画面に残る）
        # ・無料クーポンの注文は、同じコードをもう一度入れたとき（own_credit）に、金額に関係なく充てる
        # 比べるのは「割引前の金額どうし」。今回のクーポンで安くなった金額と比べると、安い注文の支払いで
        # 高い注文が通ってしまう（しかもクーポンは使われずに残る）。
        credit = next((k for k, o in sorted(_orders.items(), key=lambda kv: kv[1]["created"])
                       if o["owner"] == me and o["state"] == "paid" and o.get("retry") and not o.get("product")
                       and coupon_kind != "period"   # 期間チケットなら0円になる。以前の支払いは使わずに残す
                       # 期間チケットで0円だった注文は、その内容をやり直せるだけ（期限が切れたあとの別の注文には充てない）
                       and o.get("coupon_kind") != "period"
                       # 一部を提供済みの注文は、同じ内容の入れ直し（ログイン情報などの直し）にだけ充てる。
                       # 別の内容に充てると、1回の支払いで何度でも受け取れてしまう
                       and (not _partly_delivered(o) or _same_content(o, job, arguments))
                       and (k == own_credit or (_order_shop(o) == _order_shop({"job": job})   # 同じ部門（にゃんこ／ツムツム／ぷにぷに）
                                                and not o.get("free_coupon")
                                                and int(o.get("base_amount") or o["amount"]) >= base_amount))), None)
        if credit and credit != own_credit and int(_orders[credit].get("base_amount") or _orders[credit]["amount"]) != base_amount:
            choice = _json_body().get("use_credit")   # True＝充てる／False＝充てずに新しく支払う／無し＝まだ聞いていない
            if choice is False:
                credit = None
            elif choice is not True:
                paid = int(_orders[credit].get("base_amount") or _orders[credit]["amount"])
                return reject(jsonify(code="credit_confirm", paid_amount=paid, amount=base_amount,
                                      error=(f"お支払い済みで実行できなかったご注文（{paid:,}円）があります。そのお支払いを、今回のご注文（{base_amount:,}円）に充てますか？\n\n"
                                             f"［OK］充てる：追加のお支払いなしで実行します。差額の {paid - base_amount:,}円 は戻らず、前のご注文は実行できなくなります。\n"
                                             "［キャンセル］充てない：今回のご注文を新しくお支払いいただきます。前のご注文はそのまま残ります。")), 409)
        if credit:
            o = _orders[credit]
            breakdown = [b for b in breakdown if not b.get("coupon")]   # 支払い済みを充てるので、今回のクーポンは使わない（そのまま残る）
            if _partly_delivered(o) and o.get("checkpoint") and job.get("mode") == "puni":
                arguments[0]["resume"] = o["checkpoint"]   # 同じ内容の入れ直し：済んだ分は繰り返さず、続きから
            else:
                o.pop("checkpoint", None)                  # 内容が変わった：前の内容の途中経過は持ち越さない
            o.pop("auto_resume", None)
            o.update(job=job, arguments=arguments, transfer_key=transfer_key, breakdown=breakdown, skipped=skipped, summary=summary)
            paid_amount = o["amount"]
        else:
            # 1人の未払いの注文が多すぎたら、入金リンクのない古いものから閉じる（メモリ・保存ファイルが増え続けないように）
            mine = sorted(((k, o) for k, o in _orders.items() if o["owner"] == me and o["state"] == "unpaid" and not o.get("link_code")),
                          key=lambda kv: kv[1]["created"])
            for k, o in mine[:max(0, len(mine) - (MAX_UNPAID_PER_OWNER - 1))]:
                _orders.pop(k, None)
                if o.get("coupon") and not o.get("coupon_used"):
                    trimmed_coupons.append((o["coupon"], k))
            order_id = secrets.token_urlsafe(16)
            _orders[order_id] = {"owner": me, "uid": _uid(me), "state": "paying" if coupon_kind == "period" else "unpaid",
                                 "amount": amount, "created": time.time(),
                                 "job": job, "arguments": arguments, "transfer_key": transfer_key,
                                 "breakdown": breakdown, "skipped": skipped, "summary": summary,
                                 "coupon": coupon or None, "base_amount": base_amount, "ip": _client_ip()}
    for c, k in trimmed_coupons:          # 閉じた古い注文が押さえていたクーポンは、使えるように戻す
        try:
            paypay_gateway.coupon_release(c, k)
        except Exception:  # noqa: BLE001
            _log_orders.exception("coupon release failed")
    if coupon and not credit and coupon_kind != "period":   # 期間チケットは何度も使うので押さえない（上で1件ずつに制限）
        if not paypay_gateway.coupon_reserve(coupon, order_id):   # 同時に2つの注文で使われないよう押さえる
            with _orders_lock:
                _orders.pop(order_id, None)
            return reject(jsonify(error="このクーポンはすでに使われたか、ほかの注文で使用中です。", code="coupon_invalid"), 400)
    if credit:
        keep_old_orders()   # 以前の支払いを充てたので、今回のクーポンは使っていない
    elif stale:
        with _orders_lock:  # 新しい注文ができた → クーポンを移し終えた古い注文を閉じる
            for k in stale:
                if k in _orders and _orders[k]["state"] in ("unpaid", "paid"):
                    _orders[k].update(state="dead", coupon=None, diag="superseded: クーポンを新しい注文へ移しました")
    if free_coupon and not credit:
        # 無料クーポン：支払いは無いので、そのまま「支払い済み」にして開始する（クーポンは開始時に使用済みになる）
        with _orders_lock:
            o = _orders[order_id]
            o.update(state="paid", free_coupon=True, coupon_kind=coupon_kind,
                     summary=("【期間チケット】" if coupon_kind == "period" else "【無料クーポン】") + summary)
        resp = _start_paid_order(o)
        _persist_orders()
        return resp
    _persist_orders()
    if credit:
        return jsonify(order=credit, amount=paid_amount, skipped=skipped, breakdown=breakdown, state="paid", prepaid=True)
    return jsonify(order=order_id, amount=amount, skipped=skipped, breakdown=breakdown)


def _checkout(job: dict, arguments: tuple, transfer_key, summary: str, skipped: list, price, coupon_raw: str, admin_free: bool):
    """内容の検査が済んだ注文を、次の段階へ進める（全部門共通）。
    - 支払いなしの動作確認モード → そのまま実行
    - 管理者でログイン中 → 0円の「支払い済み」注文にして実行
    - それ以外 → 金額を計算して「支払い待ち」の注文を作る（price は (合計, 内訳の行) を返す関数）"""
    if not PAYMENTS_ENABLED:
        if _draining():
            return _updating_response()
        return _start_job(job, arguments, transfer_key)
    if admin_free:
        if _draining():
            return _updating_response()
        me = _owner()
        free = {"owner": me, "uid": _uid(me), "state": "paid", "amount": 0, "created": time.time(), "admin_free": True,
                "job": job, "arguments": arguments, "transfer_key": transfer_key, "breakdown": [], "skipped": skipped,
                "summary": "【管理者・無料】" + summary}
        with _orders_lock:
            _orders[secrets.token_urlsafe(16)] = free
        resp = _start_paid_order(free)
        _persist_orders()
        return resp
    amount, rows = price()
    breakdown = [{"label": label, "count": n, "unit": p} for label, n, p in rows]
    return _place_order(job, arguments, transfer_key, amount, breakdown, summary, skipped, coupon_raw)


def _tsum_summary(mode: str, menus: list, qty: dict) -> str:
    """管理画面に出す注文の要約。アカウント情報（メールアドレス・パスワード）は入れない。"""
    parts = [tsum_service.MODE_LABELS.get(mode, mode)]
    for key in menus:
        value = qty.get(key)
        if isinstance(value, int):
            suffix = f"={value:,}"
        elif value is not None:
            suffix = f"={value}"          # 文字入力（ニックネームなど）はそのまま
        else:
            suffix = ""
        parts.append(tsum_service.menu_label(key) + suffix)
    return ("ツムツム代行／" + "／".join(parts))[:400]


def _new_tsum_job(mode: str, menus: list) -> dict:
    return {"status": "running", "step": 0, "log": [], "warn": [], "report": [], "skipped": [],
            "dry": False, "clone": False, "integrity_only": False, "result": None, "error": None,
            "created": time.time(), "finished": None, "owner": _owner(),
            "mode": "tsum", "tsum_mode": mode, "count": len(menus)}


def _run_tsum_order(data: dict, admin_free: bool):
    """ツムツム代行の注文を受け取る（/run のツムツム分）。支払いと実行枠はにゃんこ代行と共通。"""
    request_body = data.get("tsum") or {}
    mode = str(request_body.get("mode", "line")).strip()[:8]
    login = str(request_body.get("login", "")).strip()[:160]
    password = str(request_body.get("password", ""))[:160]
    raw_menus = request_body.get("menus") if isinstance(request_body.get("menus"), list) else []
    raw_qty = request_body.get("qty") if isinstance(request_body.get("qty"), dict) else {}
    menus, qty, error = tsum_service.validate_order(
        [str(x) for x in raw_menus if isinstance(x, (str, int)) and not isinstance(x, bool)][:40], raw_qty)
    if error:
        return jsonify(error=error), 400
    if menus == ["tsum_guest_create"]:
        mode = "new"
    error = tsum_service.check_account(mode, login, password)
    if error:
        return jsonify(error=error), 400
    if mode == "new":
        login, password = "", ""
    if not _rate_ok("tsumrun:" + _client_ip(), 20, 600):   # 1件ごとに LINE へのログインが走るので、連打は止める
        return _too_fast()
    payload = {"mode": mode, "login": login, "password": password, "menus": menus, "qty": qty}
    job = _new_tsum_job(mode, menus)
    arguments = (payload,)
    transfer_key = hmac.new(app.secret_key.encode(), ("tsum:" + login).encode(), hashlib.sha256).digest() if login else None
    if transfer_key is not None:
        with _jobs_lock:
            if transfer_key in _active_transfers:
                return jsonify(error="このアカウントの処理はすでに実行中です。完了までお待ちください。"), 409
    gate = _order_gate("tsum", menus)
    if gate:
        return gate
    return _checkout(job, arguments, transfer_key, _tsum_summary(mode, menus, qty), [],
                     lambda: compute_tsum_amount(menus, qty), data.get("coupon", ""), admin_free)


def _puni_summary(mode: str, menus: list, qty: dict, stages: dict) -> str:
    """管理画面に出す注文の要約。アカウント情報（メールアドレス・パスワード）は入れない。"""
    parts = [puni_service.MODE_LABELS.get(mode, mode)]
    for key in menus:
        label = puni_service.menu_label(key)
        sfx = ""
        if key in puni_service.STAGE_MENUS and stages.get(key) is not None:
            sfx += f"@{stages[key]}"
        value = qty.get(key)
        if isinstance(value, int):
            sfx += f"={value:,}"
        elif value is not None:
            sfx += f"={value}"
        parts.append(label + sfx)
    return ("ぷにぷに代行／" + "／".join(parts))[:400]


def _new_puni_job(mode: str, menus: list) -> dict:
    return {"status": "running", "step": 0, "log": [], "warn": [], "report": [], "skipped": [],
            "dry": False, "clone": False, "integrity_only": False, "result": None, "error": None,
            "created": time.time(), "finished": None, "owner": _owner(),
            "mode": "puni", "puni_mode": mode, "count": len(menus)}


def _stop_requested() -> bool:
    """実行中のプロセスの中で：お客さんが「途中で終了」を押したか（親プロセスからパイプで届く）。"""
    global _stop_flag
    if _stop_flag or _worker_pipe is None:
        return _stop_flag
    try:
        while _worker_pipe.poll(0):
            message = _worker_pipe.recv()
            if isinstance(message, dict) and message.get("stop"):
                _stop_flag = True
    except (EOFError, OSError):
        pass
    return _stop_flag


_stop_flag = False


def _run_puni_order(data: dict, admin_free: bool):
    """ぷにぷに代行の注文を受け取る（/run のぷにぷに分）。支払い・実行枠はにゃんこ・ツムツムと共通。"""
    request_body = data.get("puni") or {}
    mode = str(request_body.get("mode", "email")).strip()[:8]
    login = str(request_body.get("login", "")).strip()[:160]
    password = str(request_body.get("password", ""))[:160]
    raw_menus = request_body.get("menus") if isinstance(request_body.get("menus"), list) else []
    raw_qty = request_body.get("qty") if isinstance(request_body.get("qty"), dict) else {}
    raw_stages = request_body.get("stages") if isinstance(request_body.get("stages"), dict) else {}
    menus, qty, stages, error = puni_service.validate_order(
        [str(x) for x in raw_menus if isinstance(x, (str, int)) and not isinstance(x, bool)][:40],
        raw_qty, raw_stages)
    if error:
        return jsonify(error=error), 400
    # モードはメニューから決める（クライアントの mode を実行経路の判断に使わない）。
    # 作成系（SOLO）は new、それ以外は必ず email（ログイン）。mode のなりすましで
    # 周回注文がアカウント作成に化けるのを防ぐ。
    mode = "new" if (menus and menus[0] in puni_service.SOLO_MENUS) else "email"
    error = puni_service.check_account(mode, login, password)
    if error:
        return jsonify(error=error), 400
    if mode == "new":
        login, password = "", ""
    if not _rate_ok("punirun:" + _client_ip(), 20, 600):
        return _too_fast()
    payload = {"mode": mode, "login": login, "password": password,
               "menus": menus, "qty": qty, "stages": stages}
    job = _new_puni_job(mode, menus)
    job["long"] = puni_service.is_long(menus, qty)   # 長時間の注文は専用の枠（長時間レーン）で動かす
    arguments = (payload,)
    transfer_key = hmac.new(app.secret_key.encode(), ("puni:" + login).encode(), hashlib.sha256).digest() if login else None
    if transfer_key is not None:
        with _jobs_lock:
            if transfer_key in _active_transfers:
                return jsonify(error="このアカウントの処理はすでに実行中です。完了までお待ちください。"), 409
    gate = _order_gate("puni", menus)
    if gate:
        return gate
    return _checkout(job, arguments, transfer_key, _puni_summary(mode, menus, qty, stages), [],
                     lambda: compute_puni_amount(menus, qty), data.get("coupon", ""), admin_free)


@app.post("/run")
def run():
    _gc_jobs()
    admin_free = _is_admin() and PAYMENTS_ENABLED   # 管理者でログイン中のブラウザは全メニュー無料
    if _maint()["on"] and PAYMENTS_ENABLED and not admin_free:
        return _maintenance_response()
    data = _clean_run_body(_read_json())
    if data is None:
        return jsonify(error="注文の内容を読み取れませんでした。画面を再読み込みしてから、もう一度お試しください。"), 400
    if isinstance(data.get("tsum"), dict):
        return _run_tsum_order(data, admin_free)
    if isinstance(data.get("puni"), dict):
        return _run_puni_order(data, admin_free)
    if not _rate_ok("run:" + _client_ip(), RUN_LIMIT, 600):   # 注文の作りすぎを止める（1件ごとに全注文を保存し直すため）
        return _too_fast()
    coupon_raw = data.get("coupon", "")   # 下で data を作り直す（バン保証・複製）前に控える。控えないと、クーポンが黙って無視される
    transfer = str(data.get("transfer", "")).strip()[:64]
    confirm = str(data.get("confirm", "")).strip()[:16]
    if bool(transfer) != bool(confirm):
        return jsonify(error="引継ぎコードと暗証番号は「両方入力」か「両方空欄」にしてください。"), 400
    clone = bool(data.get("clone", False))
    integrity_only = bool(data.get("integrity_only", False))
    if integrity_only and clone:
        return jsonify(error="バン保証とアカウント複製は同時に実行できません。"), 400
    if integrity_only and not (transfer and confirm):
        return jsonify(error="バン保証には、既存セーブの引継ぎコードと暗証番号が必要です。"), 400
    if clone:  # 実験的な独立アカウント複製：編集項目は使わない。引継ぎコードが必須
        if not (transfer and confirm):
            return jsonify(error="複製には、引継ぎコードと暗証番号が必要です。"), 400
        data = {"clone": True, "dry": False, "safe": False}
    safe = False   # 安全モード・試運転は廃止（送られてきても使わない）
    dry = False
    if integrity_only:
        safe = False
        data = {"transfer": transfer, "confirm": confirm, "dry": dry}
    items = [x for x in data.get("items", []) if x in ITEMS]
    subs = _one_legend_tier([x for x in data.get("subs", []) if x in SUBS])
    others = [x for x in data.get("others", []) if x in OTHERS]
    if not clone and not integrity_only:
        ev = []
        for x in data.get("events") or []:
            try:
                i = int(x)
            except (TypeError, ValueError):
                continue
            if i in EVENT_SELECTABLE and i not in ev:
                ev.append(i)
        if len(ev) > 400:
            return jsonify(error="イベントステージは1回に400件までにしてください。"), 400
        for i in ev:   # 前提（狂乱→大狂乱、○○→絶・○○、絶→亜種 など）を満たさないものは頼めない
            missing = [r for r in EVENT_BY_ID[i].get("reqs") or [] if r not in ev]
            if missing and not (transfer and data.get("events_pre_done")):
                need = "・".join(EVENT_BY_ID[r]["name"] for r in missing[:3]) + ("ほか" if len(missing) > 3 else "")
                return jsonify(error=f"「{EVENT_BY_ID[i]['name']}」は、先に「{need}」をクリアしている必要があります。"
                                     f"一緒に選ぶか、クリア済みなら「前提のステージはクリア済み」にチェックしてください。", code="need_event_pre"), 400
        others = others + [f"{EV_PREFIX}{i}" for i in ev]
    kinds = _legend_kinds(subs)
    if "uncanny" in kinds and "old" not in kinds and not clone and not integrity_only:
        # 真レジェンドは、旧レジェンドの冠1が終わっていないと遊べない
        if not transfer:
            return jsonify(error="真レジェンドは、旧レジェンドの冠1まで終わっていないと遊べません。新しいアカウントでは、旧レジェンドの段階（冠1クリア以上）も一緒に選んでください。",
                           code="need_old_legend"), 400
        if not data.get("old_legend_done"):
            return jsonify(error="真レジェンドは、旧レジェンドの冠1まで終わっていないと遊べません。旧レジェンドの段階も一緒に選ぶか、すでに終わっている場合は「旧レジェンドの冠1はクリア済み」にチェックしてください。",
                           code="need_old_legend"), 400
    if not clone and not integrity_only and data.get("restart_pack"):
        rp = restart_pack()
        if not rp["enabled"]:
            return jsonify(error="初期垢作成は準備中です。"), 400
        if transfer or confirm:
            return jsonify(error="初期垢作成は新しいアカウントを作るメニューです。引継ぎコードと暗証番号は空欄にしてください。"), 400
        others.append(RESTART_KEY)
    cat_nums, cat_err = parse_cat_numbers(str(data.get("chars", "")))
    if not cat_err and not transfer:
        # 配布元のデータにまだ無い新キャラ（engine.SUPPLEMENT_CATS）は、新しく作るアカウントには枠が無い → 料金だけかかって解放されない
        known = {i["n"] for i in _names["items"] if not i.get("new")} if _names["state"] == "ready" else set()
        fresh = [n for n in cat_nums if (n - DB_OFFSET) in engine.SUPPLEMENT_CATS and n not in known]
        if fresh:
            return jsonify(error=f"新キャラ（{_compress(fresh)}）は、ゲームを最新版にしたアカウントの引き継ぎでだけ解放できます。"
                                 "番号を外すか、引継ぎコードと暗証番号を入れてください。", code="new_cat_needs_transfer"), 400
    if cat_err:
        return jsonify(error="キャラ管理：" + cat_err), 400
    raw_cat_settings = data.get("cat_settings") or {}
    if not isinstance(raw_cat_settings, dict):
        return jsonify(error="キャラ個別設定の形式が正しくありません。"), 400
    allowed_cat_fields = {"base_level", "plus_level", "form", "talents_all"}
    cat_settings = {}
    for raw_number, raw_setting in raw_cat_settings.items():
        try:
            if len(str(raw_number)) > MAX_NUMBER_DIGITS:
                raise ValueError
            number = int(raw_number)
        except (TypeError, ValueError):
            return jsonify(error="キャラ個別設定の番号が正しくありません。"), 400
        if number not in cat_nums or not isinstance(raw_setting, dict):
            return jsonify(error="個別設定は選択中のキャラにだけ指定できます。"), 400
        unknown_fields = set(raw_setting) - allowed_cat_fields
        if unknown_fields:
            return jsonify(error=f"{number}番の個別設定に未対応の項目があります。"), 400
        setting = {}
        for field, low, high in (("base_level", 1, 100), ("plus_level", 0, 999), ("form", 1, 4)):
            value = raw_setting.get(field)
            if value is None or str(value).strip() == "":
                continue
            if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit() or len(str(value)) > MAX_NUMBER_DIGITS:
                return jsonify(error=f"{number}番の{field}は整数で指定してください。"), 400
            value = int(value)
            if not low <= value <= high:
                return jsonify(error=f"{number}番の{field}は{low}〜{high}の範囲で指定してください。"), 400
            setting[field] = value
        if raw_setting.get("talents_all") not in (None, False, True):
            return jsonify(error=f"{number}番の本能設定が正しくありません。"), 400
        if raw_setting.get("talents_all") is True:
            setting["talents_all"] = True
        cat_settings[number - DB_OFFSET] = setting
    qty: dict[str, int] = {}
    raw_qty = data.get("qty") or {}
    if isinstance(raw_qty, dict):
        for k, v in raw_qty.items():
            typed = engine.split_type_key(k)
            if k not in ITEMS and typed is None:
                continue
            sv = str(v).strip()
            if sv == "":
                continue
            if not (sv.isascii() and sv.isdigit()) or len(sv) > MAX_NUMBER_DIGITS:
                return jsonify(error=f"「{engine.type_label(k)}」の数量は0以上の整数で入力してください。"), 400
            n = int(sv)
            lim = engine.QUANTITY_ITEM_LIMITS.get(typed[0] if typed else k, 999)
            if n > lim:
                return jsonify(error=f"「{engine.type_label(k)}」の上限は {lim:,} です。"), 400
            qty[k] = n
    raw_pt = data.get("playtime_add_min")
    if not clone and not integrity_only and raw_pt not in (None, "", 0, "0"):
        sv = str(raw_pt).strip()
        if not (sv.isascii() and sv.isdigit()) or len(sv) > MAX_NUMBER_DIGITS:
            return jsonify(error="プレイ時間は半角の数字（分）で指定してください。"), 400
        pt = int(sv)
        if pt < PT_MIN_MINUTES:
            return jsonify(error="プレイ時間は1時間以上で指定してください。"), 400
        if pt % 10:
            return jsonify(error="プレイ時間は10分単位で指定してください。"), 400
        if pt > PT_MAX_MINUTES:
            return jsonify(error="プレイ時間が長すぎます。"), 400
        if "プレイ時間カンスト" in others:
            return jsonify(error="「プレイ時間カンスト」と「プレイ時間を増やす」は同時に選べません。"), 400
        qty[PT_KEY] = pt
    skipped: list[str] = []
    if safe:  # 安全モード：高リスク項目を外す
        skipped = [x for x in HIGH_RISK_ITEMS if x in items or x in qty]
        items = [x for x in items if x not in skipped]
        qty = {k: v for k, v in qty.items() if k not in skipped}
    if not clone and not integrity_only and not (items or subs or others or qty or cat_nums):
        if skipped:
            return jsonify(error="安全モードで高リスク項目（" + "、".join(skipped) + "）を外した結果、実行する項目が残りませんでした。"), 400
        return jsonify(error="項目を選ぶか、数量・キャラ番号を入力してください。"), 400
    job = {
        "status": "running", "step": 0, "log": [], "warn": [], "report": [], "skipped": skipped, "dry": dry, "clone": clone, "integrity_only": integrity_only,
        "result": None, "error": None,
        "created": time.time(), "finished": None, "owner": _owner(),
        "mode": "integrity" if integrity_only else ("download" if confirm else "new"),
        "count": len(items) + len(subs) + len(others) + len(qty) + len(cat_nums),
    }
    transfer_key = hmac.new(app.secret_key.encode(), transfer.encode(), hashlib.sha256).digest() if transfer else None
    arguments = (transfer, confirm, items, subs, others, qty, cat_nums, cat_settings, dry, clone, integrity_only)
    gate = _order_gate("nyanko", ["nyanko:" + _nyanko_kind(job, arguments)])
    if gate:
        return gate
    if transfer_key is not None:
        with _jobs_lock:
            if transfer_key in _active_transfers:
                return jsonify(error="この引継ぎコードの処理はすでに実行中です。完了までお待ちください。"), 409
    summary = _summary(items, subs, others, qty, cat_nums, cat_settings, dry, clone, integrity_only, bool(transfer))
    return _checkout(job, arguments, transfer_key, summary, skipped,
                     lambda: compute_amount(items, subs, others, qty, cat_nums, clone, integrity_only, cat_settings),
                     coupon_raw, admin_free)


def _start_job(job: dict, arguments: tuple, transfer_key):
    job_id = uuid.uuid4().hex
    # 再実行のときに前回の途中経過が残らないよう、毎回コピーしたまっさらな状態から始める（前回のジョブ表示を書き換えない）
    job = dict(job)
    job.update(status="running", step=0, log=[], warn=[], report=[], result=None, error=None, finished=None)
    for stale in ("outcome_unknown", "issuing_codes", "integrity", "audit", "repair", "post_audit"):
        job.pop(stale, None)
    job["created"] = time.time()
    with _jobs_lock:
        if transfer_key is not None and transfer_key in _active_transfers:
            return jsonify(error="この引継ぎコードの処理はすでに実行中です。完了までお待ちください。"), 409
        slot = _free_slot_locked(bool(job.get("long")))
        if slot is None:
            if len(_pending_jobs) >= MAX_QUEUED_JOBS:
                return jsonify(error="待機枠がいっぱいです。しばらくしてからもう一度お試しください。"), 429
            job["status"] = "queued"
            _jobs[job_id] = job
            if transfer_key is not None:
                _active_transfers.add(transfer_key)
            _pending_jobs.append((job_id, arguments, transfer_key))
            return jsonify(job=job_id)
        _set_slot_locked(slot, job_id)
        if transfer_key is not None:
            _active_transfers.add(transfer_key)
        _jobs[job_id] = job
        try:
            _launch_job(job_id, job, arguments, slot, transfer_key)
        except Exception:
            _set_slot_locked(slot, None)
            _active_transfers.discard(transfer_key)
            del _jobs[job_id]
            return jsonify(error="処理を開始できませんでした。もう一度お試しください。"), 503
    return jsonify(job=job_id)


# ---------------------------------------------------------------- 注文の状態の分類・Q&A に渡す「いまの状況」
RESUMABLE = ("unpaid", "hold", "paid")
VISIBLE = RESUMABLE + ("dead",)  # dead は再開できないが、注文番号を管理者に伝えられるよう表示する
# 注文番号で呼び戻せる状態。完了（done）も含める＝一覧から消した／別のブラウザでも、
# 番号を知っていれば完了注文の結果（引き継ぎ情報・アカウント）をもう一度表示できる。
# started/starting/paying（実行・確認の最中）は呼び戻せない（番号の持ち主＝本人なので安全）。
CLAIMABLE = VISIBLE + ("done",)
# 一覧（/api/orders/mine）に出す状態。確認中・開始処理中も出す＝サーバーが保留を再確認している数秒のあいだに
# 画面の一覧から注文が消えて、自動の見張りが止まってしまうのを防ぐ。
LISTED = VISIBLE + ("paying", "starting")
# 利用者が自分の一覧から消せる状態（お金が動いていない・進行中でないものだけ）。
# 管理台帳（PayPay）には残すので、消えるのは利用者の画面上の表示だけ。消しても番号で呼び戻せる。
FORGETTABLE = ("done", "dead", "unpaid")


def _public_order(order_id: str, o: dict) -> dict:
    return {"order": order_id, "amount": o["amount"], "state": o["state"], "created": int(o["created"]), "retry": bool(o.get("retry")),
            "tsum": (o.get("job") or {}).get("mode") == "tsum",   # ツムツム代行の注文（画面が別なので区別する）
            "puni": (o.get("job") or {}).get("mode") == "puni",   # ぷにぷに代行の注文
            "product": (o.get("product") or {}).get("type"),       # チケット・支援BOX（代行ではない買い物）
            "reviewed": bool(o.get("reviewed")),                    # 実績（星と感想）を書いたか
            "long": bool((o.get("job") or {}).get("long")),         # 長時間の周回（専用の枠で動く）
            "resume": _resume_note(o) if o["state"] == "paid" else None,   # 続きから始められるときの一言
            "result": o.get("result") if o["state"] == "done" and not o.get("dry") and not _review_locked(o) else None,
            # コードの出ない注文（周回など）の控え：何をしたかの行。実行の記録が消えたあとも見られる
            "outcome": o.get("outcome") if o["state"] == "done" and not o.get("dry") and not _review_locked(o) else None,
            "unknown": o["state"] == "done" and str(o.get("diag", "")).startswith("結果不明"),   # 途中で結果が分からなくなった（管理者に確認）
            "locked": _review_locked(o),                            # 評価と感想を送ると、結果を渡す
            "clone": bool(o.get("clone")),
            "coupon_issued": o.get("coupon_issued"),
            "breakdown": o.get("breakdown") or [], "skipped": o.get("skipped") or []}


_QA_STATE_JA = {"unpaid": "お支払い待ち", "hold": "保留中", "paid": "お支払い済み・開始待ち",
                "paying": "確認中", "starting": "開始処理中", "started": "実行中",
                "done": "完了", "dead": "要確認"}


def _qa_orders_for(shop: str) -> list:
    """Q&Aに渡す、このお客様の注文の要約（機密は含めない・新しい順に最大 MAX_ORDERS 件）。
    状態のキー・経過時間・順番待ちの位置・進み具合も渡し、その時の状況に合わせて答えられるようにする。
    注文番号・送金リンク・ログイン情報・結果（コード等）は渡さない。"""
    me = _owner()
    now = time.time()
    picked = []
    with _orders_lock:
        for v in _orders.values():
            if v.get("owner") != me or v.get("hidden_by_owner") or v.get("product"):
                continue
            mode = (v.get("job") or {}).get("mode")
            is_tsum, is_puni = mode == "tsum", mode == "puni"
            dept = "tsum" if is_tsum else "puni" if is_puni else "nyanko"
            if dept != shop:
                continue
            picked.append({"state": v.get("state"), "retry": bool(v.get("retry")),
                           "amount": v.get("amount"), "created": v.get("created") or 0,
                           "summary": (v.get("summary") or "")[:120], "job_id": v.get("job_id"),
                           "coupon_issued": bool(v.get("coupon_issued")),
                           "coupon_used": bool(v.get("coupon") and v.get("coupon_used"))})
    picked.sort(key=lambda o: o["created"], reverse=True)
    picked = picked[:qa_bot.MAX_ORDERS]
    with _jobs_lock:   # 実行中の注文は、順番待ちの位置と進み具合も渡す
        pending = [entry[0] for entry in _pending_jobs]
        for o in picked:
            job = _jobs.get(o["job_id"]) if o["job_id"] else None
            if o["state"] == "started" and job:
                o["job_status"] = job.get("status")
                if o["job_id"] in pending:
                    o["queue_position"] = pending.index(o["job_id"]) + 1
                total = {"tsum": TSUM_TOTAL_STEPS, "puni": PUNI_TOTAL_STEPS}.get(job.get("mode"), TOTAL_STEPS)
                o["progress"] = f"{min(int(job.get('step') or 0), total)}/{total}"
    out = []
    for o in picked:
        state = o["state"]
        row = {"state": state, "state_ja": _QA_STATE_JA.get(state, state), "retry": o["retry"],
               "amount": o["amount"],
               "date": time.strftime("%m/%d %H:%M", time.localtime(o["created"])),
               "age_min": max(0, int((now - o["created"]) // 60)),
               "summary": o["summary"], "coupon_issued": o["coupon_issued"], "coupon_used": o["coupon_used"]}
        if state == "unpaid":
            row["expires_in_min"] = max(0, int((o["created"] + ORDER_TTL - now) // 60))
        for k in ("job_status", "queue_position", "progress"):
            if k in o:
                row[k] = o[k]
        out.append(row)
    return out


def _qa_prices(shop: str) -> list:
    """その部門の現在の料金（管理画面で変えた PRICES をそのまま使う＝常に最新）。"""
    rows = []
    try:
        if shop in ("tsum", "puni"):
            svc = tsum_service if shop == "tsum" else puni_service
            prefix = "ツム：" if shop == "tsum" else "ぷに："
            for k in svc.sort_menus(list(svc.MENUS)):
                pk = svc.MENUS[k]["price"]
                label = PRICE_LABELS.get(pk, svc.MENUS[k]["label"]).removeprefix(prefix)
                rows.append(f"{label}：{PRICES.get(pk, 0)}円")
        else:
            for pk, label in PRICE_LABELS.items():
                if not pk.startswith(("tsum_", "puni_") + NOT_SERVICE_PREFIXES):
                    rows.append(f"{label}：{PRICES.get(pk, 0)}円")
    except Exception:  # noqa: BLE001  料金が取れなくても回答は続ける
        _log_orders.exception("qa prices failed")
    return rows


# 料金の数え方（pricing.compute_*_amount と同じ規則）。合計は選んだ内容で画面に自動で出る。
_QA_PRICE_NOTE = {
    "tsum": "1メニューごとの料金で、コインの枚数などの数量では値段は変わらない。",
    "puni": "自動周回は10周ごと（端数は切り上げ）、回し放題は1時間ごと、アカウント作成・久垢作成は作る件数ぶん。"
            "ほかは数量に関係なく1件分。",
    "nyanko": "選んだ項目ごとに上の料金がかかる（キャラは1体・1項目ごと、プレイ時間は10分ごと）。",
}


def _qa_live(shop: str) -> dict:
    """Q&Aに渡す「いまの状況」（料金・メンテ・混み具合・クーポン規則）。個人情報は含めない。"""
    m = _maint()
    with _jobs_lock:
        p = _parallel_state_locked()
    return {"now": time.strftime("%m/%d %H:%M"),
            "maintenance": bool(m.get("on")), "maintenance_message": m.get("message", ""),
            "updating": _draining(), "payments": PAYMENTS_ENABLED,
            "long_running": p["long_running_jobs"], "long_slots": p["long_slots"], "long_queued": p["long_queued_jobs"],
            "running": p["running_jobs"], "slots": p["max_concurrent_jobs"],
            "free_slots": p["available_slots"], "queued": p["queued_jobs"],
            "prices": _qa_prices(shop), "price_note": _QA_PRICE_NOTE.get(shop, ""),
            "tickets": {"once": PRICES["ticket_once"], "day1": ticket_period_price(1), "day7": ticket_period_price(7),
                        "day30": ticket_period_price(30), "unlimited": ticket_period_price(0)},
            "source_price": PRICES["source_code"],
            "coupon_min": COUPON_MIN, "coupon_days": COUPON_DAYS,
            "unpaid_ttl_min": ORDER_TTL // 60}


# ---------------------------------------------------------------- 10. チケット・支援BOX・ソースコード購入（docs/TICKETS.md）
PRODUCT_LABELS = {"ticket_once": "1回なんでも無料チケット", "ticket_period": "期間無料チケット", "support": "支援BOX",
                  "source": "ソースコード一式"}


def _period_label(days: int) -> str:
    return "無制限" if days == 0 else f"{days}日間"


def _create_product_order(product: dict, amount: int, summary: str):
    """チケット・支援の注文（支払い待ち）を作る。支払いは代行と同じ PayPay 送金リンクの流れ。
    クーポンは使えない（チケットをチケットで買う・割引で買うのを防ぐ）。"""
    if not PAYMENTS_ENABLED:
        return jsonify(error="このサーバーは支払いなしの動作確認モードのため、購入はできません。", code="payments_off"), 400
    if _maint()["on"]:
        return _maintenance_response()
    if not _rate_ok("product:" + _client_ip(), 10, 600):
        return _too_fast()
    gate = _order_gate("ticket" if str(product.get("type", "")).startswith("ticket") else str(product.get("type", "")))
    if gate:
        return gate
    _gc_orders()
    me = _owner()
    with _orders_lock:
        mine = sorted(((k, o) for k, o in _orders.items() if o["owner"] == me and o["state"] == "unpaid"
                       and o.get("product") and not o.get("link_code")), key=lambda kv: kv[1]["created"])
        for k, _o in mine[:max(0, len(mine) - (MAX_UNPAID_PER_OWNER - 1))]:
            _orders.pop(k, None)
        order_id = secrets.token_urlsafe(16)
        breakdown = [{"label": summary, "count": 1, "unit": amount}]
        base_amount = amount
        # PayPay が使えないあいだの全品割引。支援BOXは金額をお客さんが決める（お礼の気持ち）ので割り引かない
        off_pct = payments.paypay_off_pct() if product.get("type") != "support" else 0
        if off_pct and amount > 0:
            discounted = amount * (100 - off_pct) // 100
            breakdown.append({"label": f"{payments.PAYPAY_OFF_LABEL}（{off_pct}%引き）", "count": 1, "unit": discounted - amount,
                              "coupon": True, "sale": True})
            amount = discounted
        _orders[order_id] = {"owner": me, "uid": _uid(me), "state": "unpaid", "amount": amount, "created": time.time(),
                             "job": None, "arguments": None, "transfer_key": None, "product": product,
                             "breakdown": breakdown, "skipped": [], "summary": summary, "coupon": None, "base_amount": base_amount,
                             "ip": _client_ip()}
    _persist_orders()
    return jsonify(order=order_id, amount=amount, breakdown=breakdown)


def _deliver_product(order: dict):
    """入金が確認できたチケット・支援の注文に、クーポンを発行して完了にする（同じ注文からは同じコード）。"""
    with _orders_lock:
        if order["state"] != "paid":
            return jsonify(error="この注文は発行できる状態ではありません。画面を開き直してください。", code="started"), 409
        order["state"] = "starting"
    order_id = _order_id_of(order)
    p = order["product"]
    if not order_id:   # 注文が一覧から消えている（期限切れなど）。番号なしで発行すると、ほかの人と同じコードになってしまう
        with _orders_lock:
            order["state"] = "paid"
        return jsonify(error="注文が見つかりません。画面を開き直してください。", code="order_missing"), 404
    if p["type"] == "source":   # クーポンではなく、ダウンロードの権利を渡す（購入から30日は番号で呼び戻せる）
        result = {"product": "source", "download": True, "version": APP_VERSION,
                  "expires": int(order["created"] + SOURCE_KEEP_DAYS * 86400)}
        with _orders_lock:
            order.update(state="done", result=result, keep=SOURCE_KEEP_DAYS * 86400,
                         diag=f"購入 source {time.strftime('%m/%d %H:%M')}")
        try:
            paypay_gateway.mark_used(order.get("link_code"))
        except Exception:
            _log_orders.exception("mark_used failed")
        _persist_orders()
        return jsonify(order=order_id, state="done", result=result)
    try:
        if p["type"] == "ticket_once":
            code, exp = paypay_gateway.coupon_issue_kind("free", TICKET_ONCE_DAYS, "ticket:" + order_id, "チケット購入")
        elif p["type"] == "ticket_period":
            days = int(p.get("days") or 0)
            code, exp = paypay_gateway.coupon_issue_kind("period", days or paypay_gateway.UNLIMITED_DAYS, "ticket:" + order_id,
                                                         "期間チケット " + _period_label(days))
        else:
            code, exp = paypay_gateway.coupon_issue_kind("pct30", SUPPORT_COUPON_DAYS, "support:" + order_id,
                                                         f"支援 {order['amount']}円")
    except Exception:
        _log_orders.exception("product coupon issue failed")
        with _orders_lock:
            order["state"] = "paid"   # 支払いは有効のまま。もう一度押せば再送金なしで発行し直す
        return jsonify(error="コードの発行に失敗しました。もう一度ボタンを押してください（再送金は不要です）。",
                       code="start_failed", paid=True), 503
    result = {"product": p["type"], "coupon": code, "expires": int(exp),
              "unlimited": p["type"] == "ticket_period" and not p.get("days"), "days": p.get("days")}
    with _orders_lock:
        order.update(state="done", result=result, diag=f"発行 {code[:2]} {time.strftime('%m/%d %H:%M')}")
    try:
        paypay_gateway.mark_used(order.get("link_code"))
    except Exception:
        _log_orders.exception("mark_used failed")
    _persist_orders()
    return jsonify(order=order_id, state="done", result=result)


def _extra_page(mode: str, **more):
    """チケット購入・支援BOX・ソースコード購入は同じひな形（templates/extra.html）。mode で中身を切り替える。"""
    periods = []
    if mode == "ticket":
        periods = [{"days": d, "price": ticket_period_price(d)} for d in range(1, TICKET_MAX_DAYS + 1)]
        periods.append({"days": 0, "price": ticket_period_price(0)})
    ctx = dict(mode=mode, prices=PRICES, periods=periods, once_days=TICKET_ONCE_DAYS, avg=_typical_order_price(),
               maint=_maint(), payments=PAYMENTS_ENABLED, uid=_uid(_owner()), version=APP_VERSION, site=_site(),
               support_min=SUPPORT_MIN, support_max=SUPPORT_MAX, support_pct=SUPPORT_COUPON_PCT,
               support_days=SUPPORT_COUPON_DAYS, source_days=SOURCE_KEEP_DAYS, pack=None)
    ctx.update(more)
    return render_template_string(EXTRA_HTML, **ctx)


@app.get("/ticket")
def ticket_page():
    """チケット購入（1回なんでも無料・期間無料）。買うとクーポンコードが出て、全部門の注文で使える。"""
    return _extra_page("ticket")


@app.get("/support")
def support_page():
    """支援BOX（100円から。1回の支援ごとに30%引きクーポン）。"""
    return _extra_page("support")


def _typical_order_price() -> int:
    """比較グラフの初期値：代行メニューの単品料金の中央値（いまの料金表から計算）。"""
    vals = sorted(v for k, v in PRICES.items() if not k.startswith(NOT_SERVICE_PREFIXES) and v > 0)
    return vals[len(vals) // 2] if vals else 50


@app.post("/api/ticket/order")
def ticket_order():
    data = _json_body()
    plan = str(data.get("plan", ""))
    if plan == "once":
        return _create_product_order({"type": "ticket_once"}, int(PRICES["ticket_once"]), PRODUCT_LABELS["ticket_once"])
    if plan == "period":
        try:
            days = int(data.get("days"))
            amount = ticket_period_price(days)
        except (TypeError, ValueError, OverflowError):
            return jsonify(error=f"期間は1〜{TICKET_MAX_DAYS}日、または無制限から選んでください。", code="bad_days"), 400
        return _create_product_order({"type": "ticket_period", "days": days}, amount,
                                     f"{PRODUCT_LABELS['ticket_period']}（{_period_label(days)}）")
    return jsonify(error="チケットの種類を選んでください。", code="bad_plan"), 400


@app.get("/source")
def source_page():
    """ソースコード購入（このお店の仕組み一式を zip で渡す）。"""
    try:
        pack = source_pack.summary()
    except Exception:  # noqa: BLE001  中身の数が出せなくても、ページは出す
        _log_orders.exception("source pack summary failed")
        pack = None
    return _extra_page("source", pack=pack)


@app.post("/api/source/order")
def source_order():
    return _create_product_order({"type": "source"}, int(PRICES["source_code"]), PRODUCT_LABELS["source"])


@app.get("/api/source/download/<order_id>")
def source_download(order_id: str):
    """購入済みの人だけがダウンロードできる。zip はいま動いているコードから作る（source_pack.py）。"""
    from flask import send_file
    import io
    if not _rate_ok("srcdl:" + _client_ip(), 10, 600):
        return _too_fast()
    with _orders_lock:
        o = _orders.get((order_id or "").strip())
        ok = bool(o and o["owner"] == _owner() and o["state"] == "done"
                  and (o.get("product") or {}).get("type") == "source")
    if not ok:
        return jsonify(error="ダウンロードできる注文が見つかりません。別の端末で購入した場合は、注文番号で呼び戻してください。",
                       code="order_missing"), 404
    try:
        data = source_pack.cached()
    except Exception:  # noqa: BLE001
        _log_orders.exception("source pack build failed")
        return jsonify(error="ファイルを用意できませんでした。時間をおいてもう一度お試しください。", code="pack_failed"), 503
    resp = send_file(io.BytesIO(data), mimetype="application/zip", as_attachment=True,
                     download_name=f"shop-source-v{APP_VERSION}.zip")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/support/order")
def support_order():
    data = _json_body()
    try:
        amount = int(data.get("amount"))
    except (TypeError, ValueError, OverflowError):
        amount = 0
    if not SUPPORT_MIN <= amount <= SUPPORT_MAX:
        return jsonify(error=f"支援は {SUPPORT_MIN}円〜{SUPPORT_MAX:,}円でお願いします。", code="bad_amount"), 400
    return _create_product_order({"type": "support"}, amount, f"{PRODUCT_LABELS['support']}（{amount}円）")


# ---------------------------------------------------------------- 実績（星と感想）
SHOP_LABELS = {"nyanko": "にゃんこ代行", "tsum": "ツムツム代行", "puni": "ぷにぷに代行"}


_REVIEW_WORDS_FILE = os.path.join(os.environ.get("PAYPAY_DATA_DIR", "instance/paypay"), "review_words.json")
_review_words_cache: dict = {"mtime": None, "words": []}


def review_extra_words() -> list:
    """管理画面で足した「実績に載せない言葉」（最初から入っている一覧は review_filter.py）。"""
    try:
        m = os.stat(_REVIEW_WORDS_FILE).st_mtime
    except OSError:
        return []
    if _review_words_cache["mtime"] != m:
        try:
            with open(_REVIEW_WORDS_FILE, encoding="utf-8") as f:
                words = json.load(f)
            _review_words_cache.update(mtime=m, words=[str(w) for w in words if isinstance(w, str)][:200])
        except Exception:  # noqa: BLE001
            return []
    return _review_words_cache["words"]


def _save_review_words(raw: str) -> int:
    words = review_filter.clean_words(raw)
    os.makedirs(os.path.dirname(_REVIEW_WORDS_FILE) or ".", exist_ok=True)
    tmp = _REVIEW_WORDS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(words, f, ensure_ascii=False)
    os.replace(tmp, _REVIEW_WORDS_FILE)
    return len(words)


def _review_locked(o: dict) -> bool:
    """この注文の結果を、まだ渡さないか（評価と感想を待っている）。
    - 対象は、ふつうに完了した代行の注文だけ（o["gate"]。途中で止まって結果が出た注文・チケット等・管理者の無料実行は対象外）
    - 実績を書いた（reviewed）か、保存の不調が続いて免除した（review_waived）ら渡す
    画面で隠すのではなく、サーバーが結果を返さない＝開発者ツールを使っても先には読めない。"""
    return bool(site_config.REVIEWS_ENABLED and o.get("gate") and o["state"] == "done"
                and (_has_result(o.get("result")) or bool(o.get("outcome"))) and not o.get("dry")   # 渡す結果・控えがあるときだけ
                and not o.get("reviewed") and not o.get("review_waived"))


def _job_review_locked(job_id: str, job: dict) -> bool:
    """実行（/status）の結果を、まだ渡さないか。完了が注文に反映される前の一瞬も含めて、注文から判断する。"""
    if not site_config.REVIEWS_ENABLED or job.get("status") != "done" or job.get("dry"):
        return False
    if not (_has_result(job.get("result")) or _job_outcome(job)):   # 渡すものが無い完了（修復不要のバン保証など）は、評価を待たない
        return False
    with _orders_lock:
        o = next((v for v in _orders.values() if v.get("job_id") == job_id), None)
        return bool(o and not o.get("admin_free") and not o.get("product")
                    and not o.get("reviewed") and not o.get("review_waived"))


REVIEW_SAVE_TRIES = 2   # 実績の保存にこの回数続けて失敗したら、書けなかった人にも結果を渡す（お店側の不調で止めない）


def _order_shop(o: dict) -> str:
    """その注文がどの部門のものか（nyanko / tsum / puni）。"""
    mode = (o.get("job") or {}).get("mode")
    return mode if mode in ("tsum", "puni") else "nyanko"


def _clean_review_text(raw) -> str:
    """感想の文章を整える：制御文字を除き、空白の連続と4行以上の空行をつめる。"""
    text = "".join(ch for ch in _SURROGATES.sub("", str(raw or "")) if ch >= " " or ch == "\n")
    text = re.sub(r"[ \t\u3000]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _review_target(me: str, order_id: str, job_id: str):
    """実績を書ける注文を探す：本人の、完了した代行の注文で、まだ実績を書いていないもの。
    注文番号 → 実行の番号 → いちばん新しいもの、の順に当てはめる。呼び出し側で _orders_lock を持つこと。"""
    def ok(o):
        return (o["owner"] == me and o["state"] == "done" and not o.get("product") and not o.get("reviewed")
                and not o.get("admin_free"))   # 管理者の無料実行は、実績の対象にしない
    if order_id and order_id in _orders and ok(_orders[order_id]):
        return order_id
    if job_id:
        hit = next((k for k, o in _orders.items() if o.get("job_id") == job_id and ok(o)), None)
        if hit:
            return hit
    cands = sorted(((k, o) for k, o in _orders.items() if ok(o)), key=lambda kv: -kv[1]["created"])
    return cands[0][0] if cands else None


@app.post("/api/reviews")
def review_post():
    """完了した注文に、星（1〜5）と感想（20文字以上）を1件だけ書ける。
    戻り値の saved は「保存したか」。対象の注文が無い・すでに書いた場合も ok（画面はコードの表示へ進める）。"""
    if not site_config.REVIEWS_ENABLED:
        return jsonify(error="not found"), 404
    data = _json_body()
    stars = data.get("stars")
    if type(stars) is not int or not 1 <= stars <= 5:
        return jsonify(error="星を1〜5で選んでください。", code="bad_stars"), 400
    text = _clean_review_text(data.get("text"))
    low, high = site_config.REVIEW_MIN_CHARS, site_config.REVIEW_MAX_CHARS
    if len(text) < low:
        return jsonify(error=f"感想は{low}文字以上でお願いします（いま {len(text)} 文字）。", code="too_short"), 400
    if len(text) > high:
        return jsonify(error=f"感想は{high}文字までにしてください。", code="too_long"), 400
    if len(set(text)) < 5:
        return jsonify(error="同じ文字のくり返しではなく、感想を書いてください。", code="too_plain"), 400
    fine, why, message = review_filter.check(text, review_extra_words())   # 汚い言葉・宣伝・連絡先は載せない
    if not fine:
        return jsonify(error=message, code=why), 400
    if not _rate_ok("review:" + _client_ip(), 8, 600):
        return _too_fast()
    me = _owner()
    want = (str(data.get("order") or "").strip(), str(data.get("job") or "").strip())
    with _orders_lock:
        target = _review_target(me, *want)
    if not target:
        return jsonify(ok=True, saved=False)
    # AI（Haiku・エフォート低）の確認：でたらめな入力・感想になっていない文・危険な内容。判断できないときは通す
    if not _rate_ok("reviewai:" + _client_ip(), 6, 600):
        return _too_fast()
    verdict, message = review_ai.judge(text, stars)
    if verdict not in (None, "ok"):
        return jsonify(error=message, code=verdict), 400
    with _orders_lock:
        oid = _review_target(me, *want)
        if oid != target:      # 確認しているあいだに、同じ送信がもう1つ先に通った → 別の注文に付け替えない
            oid = None
        if oid:
            _orders[oid]["reviewed"] = True          # 先に印を付ける（二度押しで2件にならない）
            shop = _order_shop(_orders[oid])
    if not oid:
        return jsonify(ok=True, saved=False)
    try:
        saved = paypay_gateway.review_add(oid, shop, stars, text)
    except Exception:  # noqa: BLE001
        _log_orders.exception("review save failed")
        with _orders_lock:
            waived = False
            if oid in _orders:
                o = _orders[oid]
                o.pop("reviewed", None)
                o["review_fails"] = int(o.get("review_fails") or 0) + 1
                waived = o["review_fails"] >= REVIEW_SAVE_TRIES
                if waived:   # お店側の不調が続いた → 書けなかったお客さんにも結果を渡す
                    o["review_waived"] = True
        if waived:
            _persist_orders()
            return jsonify(ok=True, saved=False, waived=True)
        return jsonify(error="保存できませんでした。もう一度お試しください。", code="save_failed"), 503
    _persist_orders()
    return jsonify(ok=True, saved=saved)


def _public_reviews(rows) -> list:
    return [{"id": r["id"], "stars": r["stars"], "text": r["body"], "shop": SHOP_LABELS.get(r["shop"], ""),
             "date": _jst(r["created"])} for r in rows]


_JST = datetime.timezone(datetime.timedelta(hours=9))


def _jst(ts) -> str:
    """記入した日時（日本時間。サーバーの時計の地域に左右されない）。"""
    return datetime.datetime.fromtimestamp(float(ts), _JST).strftime("%Y/%m/%d %H:%M")


def _review_stats(shop: str | None = None) -> dict:
    try:
        return paypay_gateway.review_stats(shop)
    except Exception:  # noqa: BLE001  台帳が読めなくても、ページは出す
        _log_orders.exception("review stats failed")
        return {"count": 0, "average": 0.0, "dist": {i: 0 for i in range(1, 6)}}


@app.get("/api/reviews")
def review_list():
    """実績の続き（「もっと見る」）。before＝この id より前。"""
    if not site_config.REVIEWS_ENABLED:
        return jsonify(error="not found"), 404
    try:
        before = int(request.args.get("before") or 0) or None
    except (TypeError, ValueError):
        before = None
    try:
        rows = paypay_gateway.review_list(REVIEW_PAGE, before, shop=_review_shop())
    except Exception:  # noqa: BLE001
        _log_orders.exception("review list failed")
        rows = []
    return jsonify(reviews=_public_reviews(rows), more=len(rows) == REVIEW_PAGE)


def _review_shop():
    """実績を部門で絞る指定（?shop=nyanko|tsum|puni）。それ以外は絞らない。"""
    shop = request.args.get("shop", "")
    return shop if shop in SHOP_LABELS else None


REVIEW_PAGE = 20   # 実績を1回に出す件数

# ---- 意見・質問箱（7つ目のメニュー）。書くのはだれでも・読めるのは管理者だけ
BOX_MIN, BOX_MAX, BOX_CONTACT_MAX = 10, 1000, 100
BOX_KINDS = {"opinion": "意見・要望", "question": "質問", "bug": "不具合の報告"}


@app.get("/box")
def box_page():
    return render_template_string(BOX_HTML, kinds=BOX_KINDS, box_min=BOX_MIN, box_max=BOX_MAX, contact_max=BOX_CONTACT_MAX,
                                  maint=_maint(), site=_site(), version=APP_VERSION)


@app.post("/api/box")
def box_post():
    ip = _client_ip()
    if not _rate_ok("box:" + ip, 3, 600) or not _rate_ok("boxday:" + ip, 10, 86400) or not _rate_ok("box:*", 60, 3600):
        return jsonify(error="送信が続いています。少し時間をおいてからお試しください。", code="too_fast"), 429
    data = _json_body()
    if str(data.get("website") or "").strip():          # 人には見えない欄。埋めてくるのは自動投稿だけ → 受け取ったふりをして捨てる
        return jsonify(ok=True)
    if _client_blocked():
        return jsonify(error=BLOCK_MSG, code="blocked"), 403
    if not _human_ok(data.get("human")):
        return _human_required()
    kind = str(data.get("kind") or "")
    if kind not in BOX_KINDS:
        return jsonify(error="種類を選んでください。", code="kind"), 400
    text = re.sub(r"[ \t\u3000]{2,}", " ", str(data.get("text") or "")).strip()
    text = "".join(ch for ch in text.replace("\u3000", " ") if ch in "\n" or ch.isprintable())
    contact = "".join(ch for ch in str(data.get("contact") or "").strip() if ch.isprintable())
    if len(text) < BOX_MIN:
        return jsonify(error=f"{BOX_MIN}文字以上でお書きください（いま {len(text)} 文字）。", code="too_short"), 400
    if len(text) > BOX_MAX or len(contact) > BOX_CONTACT_MAX:
        return jsonify(error="文章が長すぎます。", code="too_long"), 400
    if review_filter.has_bad_words(text, review_extra_words()):   # 連絡先・URL は書いてよい（返事や不具合の説明に要る）。暴言だけ断る
        return jsonify(error=review_filter.MSG_WORDS, code="bad_words"), 400
    try:
        paypay_gateway.feedback_add(kind, text, contact, _uid(_owner()))
    except Exception:  # noqa: BLE001
        _log_orders.exception("feedback save failed")
        return jsonify(error="保存できませんでした。もう一度お試しください。", code="save_failed"), 503
    return jsonify(ok=True)


@app.get("/reviews")
def reviews_page():
    """実績（お客さんの評価と感想）の一覧。"""
    if not site_config.REVIEWS_ENABLED:
        return redirect("/")
    shop = _review_shop()
    try:
        rows = paypay_gateway.review_list(REVIEW_PAGE, shop=shop)
    except Exception:  # noqa: BLE001
        _log_orders.exception("review list failed")
        rows = []
    return render_template_string(REVIEWS_HTML, stats=_review_stats(shop), reviews=_public_reviews(rows),
                                  shop=shop or "", shop_labels=SHOP_LABELS,
                                  more=len(rows) == REVIEW_PAGE, maint=_maint(), version=APP_VERSION, site=_site())


@app.post("/api/qa")
def qa():
    """FAQ Q&A チャット。各Shopの FAQ と、このお客様自身の注文状況だけを根拠に答える。
    鍵（ANTHROPIC_API_KEY）が無ければ固定文で案内する。"""
    data = _json_body()
    shop = str(data.get("shop", "")).strip()
    if shop not in ("nyanko", "tsum", "puni"):
        shop = "nyanko"
    question = str(data.get("q", ""))
    if not question.strip():
        return jsonify(error="質問を入力してください。"), 400
    if not _rate_ok("qa:" + _client_ip(), 12, 600):   # 10分で12問まで
        return _too_fast()
    on = qa_bot.enabled()
    orders = _qa_orders_for(shop) if on else []
    live = _qa_live(shop) if on else None
    history = data.get("history") if isinstance(data.get("history"), list) else []
    try:
        extra = admin_ops.faq_list(shop)
    except Exception:  # noqa: BLE001
        extra = []
    out = qa_bot.answer(shop, question, orders, history[-qa_bot.MAX_HISTORY:], live, extra)
    return jsonify(answer=out["answer"], enabled=out.get("enabled", False))


# ---------------------------------------------------------------- 11. 注文の一覧・支払い・進み具合
@app.get("/api/orders/mine")
def my_orders():
    """このブラウザの未完了の注文（支払い待ち・保留・支払い済み未開始）。"""
    _gc_orders()
    _reconcile_orders()
    me = _owner()
    with _jobs_lock:
        live = set(_jobs)
    with _orders_lock:
        items = [_public_order(k, v) for k, v in _orders.items() if v["owner"] == me and v["state"] in LISTED
                 and not v.get("hidden_by_owner")  # 利用者が自分で消した注文は見せない
                 and not str(v.get("diag", "")).startswith("superseded")]  # 新しい注文に支払いを引き継いだ古い注文は見せない
        for k, v in _orders.items():  # 実行中・完了した注文は、状況・結果を見られるようにする
            if v.get("hidden_by_owner"):
                continue
            if v["owner"] == me and v["state"] == "started" and v.get("job_id") in live:
                items.append(dict(_public_order(k, v), job=v["job_id"]))
            elif v["owner"] == me and v["state"] == "done" and (v.get("job_id") in live or ((v.get("result") or v.get("outcome")) and not v.get("dry"))
                                                               or _review_locked(v) or str(v.get("diag", "")).startswith("結果不明")):
                items.append(dict(_public_order(k, v), job=v.get("job_id") if v.get("job_id") in live else None))
    items.sort(key=lambda x: -x["created"])
    return jsonify(orders=items, maint=_maint())


@app.post("/api/orders/<order_id>/claim")
def claim_order(order_id: str):
    """注文番号で呼び戻す（別のブラウザ・Cookie消去後・一覧から消したあとでも）。
    完了注文なら結果（引き継ぎ情報・アカウント）をもう一度見られる。
    注文番号は推測できない長さ（128bit）の秘密の値で、本人にしか表示されない。"""
    ip = _client_ip()
    if _too_many_fails("claim:" + ip):
        return jsonify(error="失敗が続いたので、10分ほど待ってください。", code="too_many_fails"), 429
    order_id = (order_id or "").strip()
    with _orders_lock:
        o = _orders.get(order_id)
        if not o or o["state"] not in CLAIMABLE:
            _note_fail("claim:" + ip)
            return jsonify(error="見つかりません。番号を確認してください（実行中・確認中・期限切れの注文は呼び戻せません）。", code="order_missing"), 404
        o["owner"] = _owner()
        o["uid"] = _uid(o["owner"])
        if o.get("job"):
            o["job"]["owner"] = o["owner"]
        with _jobs_lock:   # 実行の記録（状況・結果の表示に使う）の持ち主も移す。移さないと、呼び戻した先で「見つかりません」になる
            if o.get("job_id") in _jobs:
                _jobs[o["job_id"]]["owner"] = o["owner"]
        o.pop("hidden_by_owner", None)   # 番号で呼び戻したら表示を復活させる
        out = _public_order(order_id, o)
    _persist_orders()
    return jsonify(out)


@app.post("/api/orders/<order_id>/forget")
def forget_order(order_id: str):
    """利用者が自分の一覧からこの注文を消す（表示だけ）。管理台帳には残る。
    進行中・支払い済み・保留の注文は消せない（お金が動いているため）。"""
    me = _owner()
    order_id = (order_id or "").strip()
    with _orders_lock:
        o = _orders.get(order_id)
        if not o or o["owner"] != me:
            return jsonify(error="注文が見つかりません。", code="order_missing"), 404
        if o["state"] not in FORGETTABLE:
            return jsonify(error="この注文は進行中または支払い済みのため、まだ削除できません。"
                                 "完了してから削除してください。", code="not_forgettable"), 409
        o["hidden_by_owner"] = True
    _persist_orders()
    return jsonify(ok=True)


@app.post("/api/orders/<order_id>/pay")
def pay_order(order_id: str):
    try:
        return _pay_order(order_id)
    finally:
        _persist_orders()


def _adopt_existing_link(order_id: str, order: dict) -> None:
    code = order["link_code"]
    with _orders_lock:
        others = [(k, o) for k, o in _orders.items() if k != order_id and o.get("link_code") == code]
        if any(o["state"] in ("started", "paying", "starting", "done") for _, o in others):
            raise paypay_gateway.PayPayError("duplicate", "このリンクはすでに別の注文で使用されています。新しい送金リンクを作ってください。")
        if any(_partly_delivered(o) for _, o in others):   # 途中まで実行した注文の支払いは、別の注文へ移さない
            raise paypay_gateway.PayPayError("duplicate", "このリンクのお支払いは、途中まで実行したご注文に使われています。"
                                             "「このブラウザの注文」から、その注文の続きを実行してください。")
        # 古い注文は、付け替える前に無効にする（確認と付け替えのあいだに古い注文が開始されると、同じ支払いで2回実行になる）
        before = [(o, o["state"], o.get("diag")) for _, o in others]
        for _, o in others:
            o.update(state="dead", diag=f"superseded {time.strftime('%m/%d %H:%M')}")
    try:
        paypay_gateway.adopt_link(code, order_id, order["amount"])
    except Exception:
        with _orders_lock:   # 付け替えできなかった → 古い注文を元に戻す
            for o, state, diag in before:
                o["state"] = state
                o["diag"] = diag
        raise
    with _orders_lock:
        for _, o in others:
            o["link_code"] = None


RECHECK_WAIT = ("temporary_hold", "accept_uncertain", "accept_unconfirmed", "still_pending", "link_network", "link_check",
                "rate_limited", "paypay_unavailable", "session_rejected", "login_required", "otp_required", "cookie_missing",
                "oauth_rejected", "oauth_network", "oauth_response", "runtime_store", "account_unreadable", "missing_key", "invalid_key",
                "ky_network", "ky_unavailable", "ky_rate_limited", "ky_busy", "ky_login_required", "ky_verify_required", "ky_unconfirmed")
# Kyash: 受け取りの結果を明細で確かめられていない（自動で確認を続ける）
HOLDISH = ("temporary_hold", "accept_uncertain", "accept_unconfirmed", "ky_unconfirmed")


def _release_unused_coupon(order_id: str, order: dict) -> None:
    """入金が無いまま止まった注文が押さえているクーポンを返す（押さえたままだと、新しい注文に7日使えない）。"""
    if order.get("coupon") and not order.get("coupon_used") and order.get("coupon_kind") != "period":
        try:
            paypay_gateway.coupon_release(order["coupon"], order_id)
        except Exception:  # noqa: BLE001
            _log_orders.exception("coupon release failed")


def _recheck_and_start(order_id: str, who: str) -> str:
    """送金リンクがある注文を、サーバー側でPayPayに確認し直す。入金済みなら支払い済みにして実行を開始する。
    利用者が画面を開いていなくても動く（保留の自動確認・管理者の「再確認」で使う）。戻り値は結果の説明。"""
    with _orders_lock:
        order = _orders.get(order_id)
        if not order or not order.get("link_code") or order["state"] not in ("hold", "unpaid", "dead"):
            return "skip"
        previous = order["state"]
        order["state"] = "paying"
    try:
        asyncio.run(payments.confirm_received(order["link_code"], order_id, order["amount"]))
    except paypay_gateway.PayPayError as exc:
        with _orders_lock:
            if exc.code == "not_found":  # このリンクは一度も受け付けていない（検証で弾かれた等）→ 状態は変えない
                order["state"] = previous
            else:
                order["state"] = "hold" if exc.code in RECHECK_WAIT else "dead"
            order["diag"] = f"{who}:{exc.code} {time.strftime('%m/%d %H:%M')}"
            stopped = order["state"] == "dead"
        if stopped:
            _release_unused_coupon(order_id, order)
        _persist_orders()
        return "wait:" + exc.code if exc.code in RECHECK_WAIT else "stop:" + exc.code
    except Exception:
        _log_orders.exception("recheck failed")
        with _orders_lock:
            order["state"] = "hold"
            order["diag"] = f"{who}:internal {time.strftime('%m/%d %H:%M')}"
        _persist_orders()
        return "wait:internal"
    with _orders_lock:
        order["state"] = "paid"
        order["diag"] = f"{who}:入金確認 {time.strftime('%m/%d %H:%M')}"
    with app.app_context():
        resp = _start_paid_order(order)
    rc = resp[1] if isinstance(resp, tuple) else 200
    _persist_orders()
    return "started" if rc == 200 else "paid"


def _hold_watcher() -> None:
    """保留中の注文を90秒ごとにサーバー側で確認する（1回に最大5件・3日以内の注文）。"""
    while True:
        time.sleep(90)
        if not PAYMENTS_ENABLED or _draining():
            continue
        try:
            now = time.time()
            with _orders_lock:
                # 最後に確かめてから時間がたったものを先に（古い5件だけを毎回見て、新しい保留を見ないままにしない）
                targets = [k for k, o in sorted(_orders.items(), key=lambda kv: (kv[1].get("hold_checked") or 0, kv[1]["created"]))
                           if o["state"] == "hold" and o.get("link_code") and now - o["created"] < 3 * 86400][:5]
                for k in targets:
                    _orders[k]["hold_checked"] = now
            for k in targets:
                _recheck_and_start(k, "auto")
        except Exception:
            _log_orders.exception("hold watcher failed")


HUMAN_TTL = 15 * 60     # 人間確認が通ったら、このあいだは同じブラウザに聞き直さない（リンクの入れ直しのたびに出さない）
HUMAN_MSG = ("「人間であることの確認」が済んでいません。チェックが終わるのを待って、もう一度押してください。"
             "チェックの枠が出ないときは、広告ブロックを切るか、別のブラウザでお試しください。")


def _human_ok(token) -> bool:
    """購入・投稿の前の人間確認（Cloudflare Turnstile）。鍵が設定されていなければ、確認しない。"""
    if not human.enabled() or _is_admin():
        return True
    at = session.get("human_at")
    if isinstance(at, (int, float)) and 0 <= time.time() - at <= HUMAN_TTL:
        return True
    result = human.verify(token, _client_ip())
    if result is True:
        session["human_at"] = time.time()
    return result is not False          # None（Cloudflare に聞けない）は通す＝売り上げを止めない


def _human_required():
    return jsonify(error=HUMAN_MSG, code="human_required"), 400


def _pay_order(order_id: str):
    """PayPay送金リンクを受け取り、確認できたら実行を開始する。支払い済みなら再送不要で開始だけ再試行。"""
    data = _json_body()
    if _draining():
        return _updating_response()
    if not _rate_ok("pay:" + _client_ip(), PAY_LIMIT, 600):
        return _too_fast()
    with _orders_lock:
        peek = _orders.get(order_id)
        fresh = bool(peek and peek["owner"] == _owner() and peek["state"] == "unpaid")
    if fresh and not _human_ok(data.get("human")):      # 新しい支払いのときだけ（保留の確認・支払い済みの再開には出さない）
        return _human_required()
    with _orders_lock:
        order = _orders.get(order_id)
        if not order or order["owner"] != _owner():
            return jsonify(error="注文が見つかりません（30分で期限切れになります）。もう一度「実行」から始めてください。", code="order_missing"), 404
        if _maint()["on"] and order["state"] == "unpaid":  # メンテナンス中は新しい支払いを受け付けない（すでに支払い済み・保留の注文は進められる）
            return _maintenance_response()
        if order["state"] == "paying":
            return jsonify(error="決済確認中です。少しお待ちください。", code="paying"), 409
        if order["state"] in ("started", "starting"):
            return jsonify(error="この注文はすでに開始済みです。「状況」タブを確認してください。", code="started"), 409
        if order["state"] == "done":
            return jsonify(error="この注文は完了しています。新しく実行する場合は、もう一度「決済して実行」から始めてください。", code="done"), 409
        if order["state"] == "dead":
            return jsonify(error="この注文は自動では確認できませんでした。お支払い済みの場合は、注文番号を管理者に伝えてください。", code="dead"), 409
        if order["state"] in ("unpaid", "hold"):
            if order["state"] == "unpaid" and _too_many_fails("paypay:" + order_id):
                return jsonify(error="失敗が続いたので、10分ほど待ってください。", code="too_many_fails"), 429
            mode = "link" if order["state"] == "unpaid" else "recheck"
            order["state"] = "paying"
            need_pay = True
        else:
            need_pay = False
    if need_pay:
        try:
            if mode == "link":
                raw = str(data.get("link", ""))
                order["link_code"] = payments.normalize(raw) or order.get("link_code")
                _persist_orders()  # 送金リンクを受け取る前に保存（途中で再起動しても同じリンクを再確認できる）
                try:
                    asyncio.run(payments.receive_payment(raw, order["amount"], order_id))
                except paypay_gateway.PayPayError as dup:
                    if dup.code != "duplicate" or not order.get("link_code"):
                        raise
                    # 以前の注文（保留・停止で実行されなかった）と同じリンク → この注文に付け替えて確認し直す
                    _adopt_existing_link(order_id, order)
                    mode = "recheck"
                    asyncio.run(payments.confirm_received(order["link_code"], order_id, order["amount"]))
            else:
                asyncio.run(payments.confirm_received(order.get("link_code"), order_id, order["amount"]))
        except paypay_gateway.PayPayError as exc:
            # 待てば直るもの（PayPay側の保留・混雑・通信、こちらのPayPay接続切れなど）
            WAITING = RECHECK_WAIT
            if not (mode == "recheck" and exc.code in WAITING):
                _note_fail("paypay:" + order_id)
            hold = bool(order.get("link_code")) and (exc.code in HOLDISH or (mode == "recheck" and exc.code in WAITING))
            # 再確認（同じリンク）で、待てば直る種類以外のエラー → 自動では進められないので管理者確認へ。
            # 「支払い待ち」に戻すと、台帳にこの注文のリンクが残っているため新しいリンクも受け付けられず行き詰まる。
            dead = mode == "recheck" and not hold
            with _orders_lock:
                order["state"] = "dead" if dead else ("hold" if hold else "unpaid")
                if order["state"] == "unpaid" and mode == "link":
                    # 受け付けなかったリンクは注文に残さない（残すと、でたらめなリンク1つで注文が7日残り、クーポンも押さえ続ける）
                    try:
                        if paypay_gateway.link_order_id(order.get("link_code") or "") != order_id:
                            order["link_code"] = None
                    except Exception:  # noqa: BLE001
                        pass
                order["diag"] = f"{exc.code} {time.strftime('%m/%d %H:%M')}"
            if dead:
                _release_unused_coupon(order_id, order)
            return jsonify(error=exc.message, code=exc.code, hold=hold, uncertain=hold), 402
        except Exception:
            _log_orders.exception("unexpected error while confirming payment")
            with _orders_lock:
                order["state"] = "hold" if order.get("link_code") else "unpaid"
                order["diag"] = f"internal {time.strftime('%m/%d %H:%M')}"
            return jsonify(error="決済確認中に予期しないエラーが発生しました。PayPayアプリで送金状況を確認してください。", code="internal",
                           hold=order["state"] == "hold", uncertain=True), 500
        with _orders_lock:
            order["state"] = "paid"
    return _start_paid_order(order)


def _start_paid_order(order: dict):
    """支払い済みの注文の実行を開始する（利用者の操作・管理者の「支払い済みにする」の両方から使う）。"""
    if order.get("product"):   # チケット・支援BOX：実行するものは無く、クーポンを発行して完了にする
        return _deliver_product(order)
    with _orders_lock:
        # 開始できるのは「支払い済み」の注文だけ。開始の権利をロック内で1回だけ取る（同時押しで二重に実行しない）
        if order["state"] != "paid" or order.get("arguments") is None:
            return jsonify(error="この注文は実行を開始できる状態ではありません。画面を開き直してください。", code="started"), 409
        order["state"] = "starting"
        coupon_to_use = order.get("coupon") if not order.get("coupon_used") and order.get("coupon_kind") != "period" else None
    if coupon_to_use:   # 入金が確認できた（支払い済み）ので、クーポンを使用済みにする
        try:
            if paypay_gateway.coupon_consume(coupon_to_use, _order_id_of(order)):
                order["coupon_used"] = True
            elif order.get("free_coupon"):
                # 0円の注文なのに、クーポンはほかの注文が使っていた（同時に2つ送った）→ この注文は始めない
                with _orders_lock:
                    order.update(state="dead", diag=f"無料クーポンは別の注文で使用済み {time.strftime('%m/%d %H:%M')}")
                return jsonify(error="このクーポンは、ほかのご注文で使われました。", code="coupon_invalid"), 409
            else:
                _log_orders.warning("coupon was not reserved for this order; started at the paid price")
        except Exception:
            _log_orders.exception("coupon consume failed")
    if order.get("coupon_kind") == "period":   # 期間チケットは1件ずつ：ほかの注文が動いているあいだは始めない（一覧からのやり直しも同じ）
        with _orders_lock:
            busy = any(o is not order and o.get("coupon") == order.get("coupon") and o["state"] in _PERIOD_BUSY for o in _orders.values())
            if busy:
                order["state"] = "paid"
        if busy:
            return jsonify(error="この期間チケットは、いま別のご注文で使用中です。その注文が終わってから始めてください。", code="coupon_busy", paid=True), 409
    try:
        resp = _start_job(order["job"], order["arguments"], order["transfer_key"])
    except Exception:
        resp = (jsonify(error="処理を開始できませんでした。もう一度お試しください。"), 503)
    body, code = (resp if isinstance(resp, tuple) else (resp, 200))
    if code != 200:
        with _orders_lock:
            order["state"] = "paid"  # 支払いは有効のまま。もう一度押せば再送金なしで開始できる
            if order.get("coupon_used"):
                order["retry"] = True   # クーポンは使用済みにしたあと。入力し直した注文にも、この支払いを充てられるようにする
    if code == 200:
        with _orders_lock:
            order["state"] = "started"
            order["job_id"] = body.get_json().get("job")
            order["retry"] = False
            order["diag"] = f"started {time.strftime('%m/%d %H:%M')}"
        try:
            paypay_gateway.mark_used(order.get("link_code"))
        except Exception:
            _log_orders.exception("mark_used failed")
        return body
    out = body.get_json()
    out["paid"] = True
    out["code"] = "busy" if code == 429 else "start_failed"
    return jsonify(out), code


@app.get("/status/<job_id>")
def status(job_id: str):
    _gc_jobs()
    with _jobs_lock:
        job = _jobs.get(job_id)
        same_lane = [entry[0] for entry in _pending_jobs if _is_long(entry[0]) == _is_long(job_id)]
        position = same_lane.index(job_id) + 1 if job_id in same_lane else None   # 同じ種類の待ちの中で何番目か
        slot_number = next((i for i, value in enumerate(_slots + _long_slots, 1) if value == job_id), None)
        parallel = _parallel_state_locked()
        # Copy the mutable fields used below while holding the same lock as
        # worker updates and slot transitions.
        job = dict(job) if job else None
    if not job or job.get("owner") != _owner():
        return jsonify(error="見つかりません（時間切れ、またはサーバーが再起動した可能性）"), 404
    end = job["finished"] or time.time()
    locked = _job_review_locked(job_id, job)   # 完了したが、評価と感想がまだ → 結果は渡さない
    # 段取りの総数はメニューの種類で変わる（にゃんこ=4・ツムツム=6・ぷにぷに=10）。
    # 画面は自分の total_steps 定数で進捗バーを計算するが、API もモードに合った total を返す（P-16）。
    total_steps = {"tsum": TSUM_TOTAL_STEPS, "puni": PUNI_TOTAL_STEPS}.get(job.get("mode"), TOTAL_STEPS)
    return jsonify(
        status=job["status"], queue_position=position, slot=slot_number,
        active_jobs=parallel["running_jobs"], warmup_slots=parallel["warmup_slots"],
        occupied_slots=parallel["occupied_slots"], available_slots=parallel["available_slots"],
        max_concurrent_jobs=parallel["max_concurrent_jobs"], queued_jobs=parallel["queued_jobs"],
        step=job["step"], total=total_steps,
        mode=job["mode"], count=job["count"],
        elapsed=round(end - job["created"], 1),
        log=job["log"], warn=job["warn"], report=job["report"], dry=job["dry"], clone=job["clone"],
        outcome_unknown=job.get("outcome_unknown", False),
        # 実行の途中（複製は、元アカウントのコードが先に出る）では返さない。終わってから、評価の確認を通して渡す
        result=None if locked or job["status"] in ("running", "queued") else job["result"], result_locked=locked,
        error=job["error"], integrity=job.get("integrity"), integrity_only=job.get("integrity_only", False),
        audit=job.get("audit"),
        repair=job.get("repair"), post_audit=job.get("post_audit"),
        tsum=None if locked else job.get("tsum"), captcha=job.get("captcha"), puni=None if locked else job.get("puni"),
        long=bool(job.get("long")), stopping=bool(job.get("stopping")),
    )


@app.post("/api/jobs/<job_id>/captcha")
def job_captcha(job_id: str):
    """ツムツム代行の画像認証（CAPTCHA）の答えを、実行中のプロセスへ渡す。"""
    data = _json_body()
    text = str(data.get("text", "")).strip()[:32]
    if not text:
        return jsonify(error="画像に出ている文字を入力してください。"), 400
    with _jobs_lock:
        job = _jobs.get(job_id)
        owner = job.get("owner") if job else None
        asked = (job or {}).get("captcha") or {}
    if not job or owner != _owner():
        return jsonify(error="処理が見つかりません。"), 404
    if not asked:
        return jsonify(error="いまは画像認証の入力待ちではありません。"), 409
    if data.get("id") and data.get("id") != asked.get("id"):
        return jsonify(error="画像が新しくなりました。新しい画像の文字を入力してください。", code="captcha_changed"), 409
    with _job_pipes_lock:
        pipe = _job_pipes.get(job_id)
    if pipe is None:
        return jsonify(error="処理とつながっていません。もう一度実行してください。"), 409
    try:
        pipe.send({"captcha": text, "id": asked.get("id")})
    except (BrokenPipeError, EOFError, OSError):
        return jsonify(error="処理とつながっていません。もう一度実行してください。"), 409
    return jsonify(ok=True)


@app.post("/api/jobs/<job_id>/stop")
def job_stop(job_id: str):
    """長い周回を、お客さんが自分で途中で終わらせる。いまの1周が終わったところで正常に終了する。
    順番待ちの実行は /cancel で取り消す（まだ何も始まっていない）。"""
    with _jobs_lock:
        job = _jobs.get(job_id)
        ok = bool(job and job.get("owner") == _owner())
        status = job.get("status") if ok else None
        mode = job.get("mode") if ok else None
    if not ok:
        return jsonify(error="処理が見つかりません。"), 404
    if status != "running" or mode != "puni":
        return jsonify(error="いまは途中で終了できません。", code="not_running"), 409
    with _job_pipes_lock:
        pipe = _job_pipes.get(job_id)
    try:
        if pipe is None:
            raise OSError
        pipe.send({"stop": True})
    except (BrokenPipeError, EOFError, OSError):
        return jsonify(error="処理とつながっていません。少し待ってからもう一度お試しください。"), 409
    return jsonify(ok=True)


@app.post("/cancel/<job_id>")
def cancel_job(job_id: str):
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or job.get("owner") != _owner():
            return jsonify(error="待機中の処理が見つかりません。"), 404
        for entry in _pending_jobs:
            if entry[0] == job_id:
                _pending_jobs.remove(entry)
                if entry[2] is not None:
                    _active_transfers.discard(entry[2])
                _jobs[job_id].update(status="error", error="順番待ちを取り消しました。お支払い済みの場合は、注文からそのまま（追加料金なしで）実行できます。", finished=time.time())
                return jsonify(ok=True)
    return jsonify(error="待機中の処理が見つかりません。"), 409


# ---------------------------------------------------------------- 画面のひな形（templates/）
_COMMENT_BLOCK = re.compile(r"/\*(?:(?!\*/).)*\*/")
_HTML_COMMENT = re.compile(r"<!--(?!\[if)(?:(?!-->).)*-->", re.S)
_NON_ASCII = re.compile(r"[^\x00-\x7f]")
_CODE_BLOCK = re.compile(r"(<(?:script|style)\b[^>]*>)(.*?)(</(?:script|style)>)", re.S)


def strip_comments(text: str, html: bool = False) -> str:
    """配る HTML・JavaScript・CSS から、説明のコメントを取り除く（仕組みの説明書を、見に来た人へ渡さない）。
    動きを変えないよう、確実にコメントだと言えるものだけを消す：
      ・行がまるごとコメント（// … や /* … */、複数行の /* … */）
      ・行の途中・行末の /* … */ と、行末の // … のうち、日本語を含むもの（URL の // や文字列の中身を巻き込まない）
      ・HTML の <!-- … -->
    元のファイル（読む人のためのコメント）はそのまま。消すのは配るときだけ。"""
    if html:        # HTML は、<script> と <style> の中身だけを対象にする（本文の「//」などに触らない）
        text = _HTML_COMMENT.sub("", text)
        return _CODE_BLOCK.sub(lambda m: m.group(1) + strip_comments(m.group(2)) + m.group(3), text)
    out, in_block = [], False
    for line in text.split("\n"):
        bare = line.strip()
        if in_block:
            if "*/" in bare:
                in_block = False
            continue
        if bare.startswith("/*") and "*/" not in bare:
            in_block = True
            continue
        if bare.startswith("//") or (bare.startswith("/*") and bare.endswith("*/") and bare.count("*/") == 1):
            continue
        line = _COMMENT_BLOCK.sub(lambda m: "" if _NON_ASCII.search(m.group(0)) else m.group(0), line)
        cut = re.search(r"(?:^|\s)//\s?[^'\"`]*$", line)
        if cut and _NON_ASCII.search(cut.group(0)) and "}}" not in cut.group(0) and "%}" not in cut.group(0):
            line = line[:cut.start()]
        if line.strip() or not bare:
            out.append(line.rstrip())
    return "\n".join(out)


def _template(name: str, guard: bool = True) -> str:
    """画面の HTML（Jinja のひな形）。templates/ に置いてある（以前は app.py の中に直接書いていた）。
    読み込むときに、コメントを取り除き、<script> に CSP の合い言葉（nonce）を付ける。"""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", name), encoding="utf-8", newline="") as f:
        text = strip_comments(f.read().replace("\r\n", "\n"), html=True)
    if guard and "</body>" in text:    # のぞき見よけ（右クリック・保存・開発者ツールのキー）。管理画面には入れない
        text = text.replace("</body>", '<script src="/static/guard.js?v=1" defer></script>\n'
                            '{% if turnstile_key %}<script src="https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit" async defer></script>'
                            '<script src="/static/human.js?v=1" defer></script>{% endif %}\n</body>', 1)
    return re.sub(r"<script(?=[\s>])", '<script nonce="{{ csp_nonce }}"', text)


_static_cache: dict = {}


def _static(filename: str):
    """/static のファイル。JavaScript と CSS は、コメントを取り除いたものを配る。"""
    filename = filename.rstrip("/")
    if not filename.endswith((".js", ".css")):
        return app.send_static_file(filename)
    from werkzeug.security import safe_join
    path = safe_join(app.static_folder, filename)
    try:
        mtime = os.path.getmtime(path) if path else None
    except OSError:
        mtime = None
    if mtime is None:
        return app.send_static_file(filename)       # 無いファイルは、いつもの 404
    hit = _static_cache.get(filename)
    if not hit or hit[0] != mtime:
        with open(path, encoding="utf-8") as f:
            hit = _static_cache[filename] = (mtime, strip_comments(f.read()).encode("utf-8"))
    resp = Response(hit[1], mimetype="text/javascript" if filename.endswith(".js") else "text/css")
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


app.view_functions["static"] = _static


ADMIN_HTML = _template("admin.html", guard=False)   # 管理画面


MAIN_HTML = _template("main.html")     # 利用者の画面（HTML・CSS・JavaScript）


TSUM_HTML = _template("tsum.html")     # ツムツム代行の画面


PUNI_HTML = _template("puni.html")     # ぷにぷに代行の画面


HOME_HTML = _template("home.html")     # トップ（大メニュー）
EXTRA_HTML = _template("extra.html")   # チケット購入・支援BOX・ソースコード購入
REVIEWS_HTML = _template("reviews.html")   # 実績（星と感想）
BOX_HTML = _template("box.html")           # 意見・質問箱

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
