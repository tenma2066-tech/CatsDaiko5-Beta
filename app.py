"""にゃんこ代行 Web版（個人用・無料）
ブラウザから引継ぎコードを入力して、セーブデータを書き換えます。
課金・PayPay・Discordは一切ありません。
"""
from __future__ import annotations

import hashlib
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

from flask import Flask, jsonify, redirect, render_template_string, request, session

import engine
import item_catalog
import asyncio
import logging
import paypay_gateway
import save_audit
import save_repair

APP_VERSION = "1.15"
GAME_DATA_VERSION = engine.GAME_DATA_VERSION
CAT_NAMES_CACHE_SCHEMA = 2

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
PRICES = {"chara": 50, "crash": 50, "ban_guarantee": 500, "clone": 100, "other": 25, "playtime10": 10, "restart": 50}
PRICE_LABELS = {"chara": "キャラ指定（1体・1項目ごと）", "crash": "クラッシュ解除（1件）", "ban_guarantee": "バン保証",
                "clone": "アカウント複製", "other": "それ以外のメニュー（1件）", "playtime10": "プレイ時間（10分ごと）",
                "restart": "初期垢作成（リスタートパック・基本料金）"}
PT_KEY = "プレイ時間+分"          # qty に入れる「プレイ時間を増やす（分）」の特別なキー
PT_MIN_MINUTES = 60
PT_MAX_MINUTES = 2147483647 // (30 * 60)   # セーブの上限（1/30秒単位の int）を超えない
RESTART_KEY = "初期垢作成（リスタートパック）"   # others に入れる特別な項目
COUPON_MIN = 200                  # 200円以上の購入で発行・利用
COUPON_DAYS = 30
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
CRASH_ITEMS = ("ガマトトのクラッシュ修正", "オトート砲台のクラッシュ修正")
ORDER_TTL = 1800

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("nyanko-web:" + ADMIN_PASSWORD).encode()  # ワーカープロセス間でも同じ値になるよう固定（本番は SECRET_KEY を設定）
).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                  SESSION_COOKIE_SECURE=bool(os.environ.get("FLY_APP_NAME") or os.environ.get("SESSION_COOKIE_SECURE")),
                  PERMANENT_SESSION_LIFETIME=30 * 86400)

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
def _one_legend_tier(subs):
    """同じレジェンドの段階は、いちばん上だけを残す（下の段階は含まれるので、二重に料金を取らない）。"""
    best = {}
    for x in subs:
        t = engine.LEGEND_TIERS.get(x)
        if t and (t[0] not in best or t[1] > engine.LEGEND_TIERS[best[t[0]]][1]):
            best[t[0]] = x
    return [x for x in subs if x not in engine.LEGEND_TIERS or best.get(engine.LEGEND_TIERS[x][0]) == x]


_SUB_PRIORITY = {
    "全キャラ解放&エラーキャラ削除": 0,
    "全キャラ最高形態": 1,
    "全キャラレベルMAX": 2,
    **{k: (10 if v[0] == "old" else 11) for k, v in engine.LEGEND_TIERS.items()},   # 旧レジェンド → 真レジェンド の順
}


def _load_event_maps():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "event_maps.json"), encoding="utf-8") as f:
            return json.load(f)["maps"]
    except (OSError, ValueError, KeyError):
        return []


_ALL_EVENT_MAPS = _load_event_maps()                # イベント・コラボステージの一覧（ゲームデータから作成）
EVENT_BY_ID = {m["id"]: m for m in _ALL_EVENT_MAPS}  # 実行用（支払い済みの古い注文のため、コラボも残す）
EVENT_MAPS = [m for m in _ALL_EVENT_MAPS if m.get("cat") != "コラボ"]   # 選べるのはイベントステージだけ（コラボは外す）
EVENT_SELECTABLE = {m["id"] for m in EVENT_MAPS}
EV_PREFIX = "EV:"                                   # 注文の中では「その他」に EV:マップ番号 として入れる


def _event_ids(others):
    out = []
    for x in others:
        if str(x).startswith(EV_PREFIX) and str(x)[3:].isdigit() and int(str(x)[3:]) in EVENT_BY_ID:
            out.append(int(str(x)[3:]))
    return out


def _legend_kinds(subs):
    return {engine.LEGEND_TIERS[x][0] for x in subs if x in engine.LEGEND_TIERS}

# bcsfe の配布元（PyPI / GitHub）の説明に「BANされることがある」と書かれている項目。
# これ以外が安全という意味ではない（公開情報が無いだけ）。
# プラチナのかけらは10個でプラチケ1枚になるため、プラチケと同じ扱いにする。
HIGH_RISK_ITEMS = ["猫缶", "レアチケ", "プラチケ", "プラチナのかけら", "レジェチケ"]

# ---------------------------------------------------------------- キャラ番号
# にゃんこDBの番号は1始まり（ネコ=1）。セーブ内部のIDは0始まり（ネコ=0）なので1引く。
DB_OFFSET = 1
MAX_CAT_NUMBER = 9999
MAX_CAT_COUNT = 2000


def parse_cat_numbers(text: str):
    """「1, 5, 100-120」のような入力を番号の一覧にする。戻り値は (番号リスト, エラー文 or None)。"""
    t = unicodedata.normalize("NFKC", text or "")
    t = re.sub(r"[~〜～ー−―‐–—]", "-", t)
    nums: set[int] = set()
    for tok in [x for x in re.split(r"[\s,、，;；・]+", t) if x]:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", tok)
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


# ---------------------------------------------------------------- 状態管理
_started_at = time.time()
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_run_lock = threading.Lock()          # 親プロセス内でキャラ名の取得を重複させない
_init_lock = threading.Lock()
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "4"))
if MAX_CONCURRENT_JOBS < 1:
    raise ValueError("MAX_CONCURRENT_JOBS must be at least 1")
_slots: list[str | None] = [None] * MAX_CONCURRENT_JOBS
_active_transfers: set[bytes] = set()
MAX_QUEUED_JOBS = 20
QUEUE_TTL = 15 * 60
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


def _parallel_state_locked() -> dict:
    """Return an atomic, privacy-safe snapshot of the shared execution slots.

    _slots is also used briefly by warmup. Keep warmup reservations separate
    from actual worker processes so the UI never labels preparation as a job.
    The caller must hold _jobs_lock.
    """
    states = []
    running = warmup = occupied = 0
    for index, value in enumerate(_slots, 1):
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
        "available_slots": len(_slots) - occupied,
        "max_concurrent_jobs": len(_slots),
        "slot_states": states,
        "queued_jobs": len(_pending_jobs),
    }

def _gc_jobs() -> None:
    now = time.time()
    with _jobs_lock:
        for entry in list(_pending_jobs):
            job_id, _, transfer_key = entry
            if now - _jobs[job_id]["created"] >= QUEUE_TTL:
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


@app.after_request
def _private_response(response):
    if request.path.startswith("/status/") or request.path in ("/admin",) or request.path.startswith("/api/orders/"):
        response.headers["Cache-Control"] = "no-store"
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
                    if _slots[slot] is not None or _pending_jobs:
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


def _worker_entry(pipe, job, arguments, slot, learned_version, names):
    """Run BCSFE in its own process and its own persistent slot directory."""
    global _worker_pipe, _bcsfe_base, _NAMES_FILE, _VERSION_FILE
    _worker_pipe = pipe
    try:
        root = os.environ.get("BCSFE_PARALLEL_ROOT", "/tmp/bcsfe/parallel")
        _bcsfe_base = os.path.join(root, str(slot))
        os.makedirs(_bcsfe_base, exist_ok=True)
        lock_dir = os.path.join(root, "locks")
        os.makedirs(lock_dir, mode=0o700, exist_ok=True)
        with ExitStack() as locks:
            paths = [os.path.join(lock_dir, f"slot-{slot}")]
            if arguments[0]:
                digest = hmac.new(app.secret_key.encode(), arguments[0].encode(), hashlib.sha256).hexdigest()
                paths.append(os.path.join(lock_dir, f"transfer-{digest}"))
            for path in paths:
                lock_file = locks.enter_context(open(path, "a"))
                try:
                    fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RuntimeError("同じ実行枠または引継ぎコードの処理がまだ実行中です。") from exc

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
        _slots[slot] = None
        if transfer_key is not None:
            _active_transfers.discard(transfer_key)
        _start_queued_jobs_locked()


def _start_queued_jobs_locked() -> None:
    """Called with _jobs_lock held; never persist queued credentials."""
    while _pending_jobs and None in _slots:
        job_id, arguments, transfer_key = _pending_jobs.popleft()
        job = _jobs[job_id]
        if time.time() - job["created"] >= QUEUE_TTL:
            job.update(status="error", error="待機時間が長いため処理を中止しました。", finished=time.time())
            if transfer_key is not None:
                _active_transfers.discard(transfer_key)
            continue
        slot = _slots.index(None)
        _slots[slot] = job_id
        job["status"] = "running"
        try:
            _launch_job(job_id, job, arguments, slot, transfer_key)
        except Exception:
            _slots[slot] = None
            if transfer_key is not None:
                _active_transfers.discard(transfer_key)
            job.update(status="error", error="処理を開始できませんでした。", finished=time.time())


def _monitor_worker(pipe, process, job_id: str, slot: int, transfer_key: bytes | None) -> None:
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
        pipe.close()
        process.join()
        _finish_job(job_id, slot, transfer_key)


def _launch_job(job_id: str, job: dict, arguments: tuple, slot: int, transfer_key: bytes | None) -> None:
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
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


# ---------------------------------------------------------------- 認証
_fail: dict[str, list[float]] = {}


def _too_many_fails(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _fail.get(ip, []) if now - t < 600]
    _fail[ip] = recent
    return len(recent) >= 5


def chara_units(cat_nums, cat_settings) -> int:
    """キャラ1体ごとに、設定した項目（レベル・形態・本能）1つにつき1件。設定なし（解放のみ）は1件。
    レベルは本体レベルと＋値をまとめて1件とする。cat_settings のキーはセーブ内部ID（番号-1）。"""
    total = 0
    for n in cat_nums:
        s = (cat_settings or {}).get(n - DB_OFFSET) or {}
        k = (1 if ("base_level" in s or "plus_level" in s) else 0) + (1 if "form" in s else 0) + (1 if s.get("talents_all") else 0)
        total += max(1, k)
    return total


def compute_amount(items, subs, others, qty, cat_nums, clone, integrity_only, cat_settings=None) -> tuple[int, list]:
    """サーバー側で金額を決める（ブラウザの金額は信用しない）。戻り値は (合計, 内訳)。"""
    if integrity_only:
        return PRICES["ban_guarantee"], [("バン保証（セーブの検査・修復）", 1, PRICES["ban_guarantee"])]
    if clone:
        return PRICES["clone"], [("アカウント複製", 1, PRICES["clone"])]
    crash = [x for x in others if x in CRASH_ITEMS]
    restart = RESTART_KEY in others
    pt_min = int(qty.get(PT_KEY, 0) or 0)
    plain_qty = {k for k in qty if k != PT_KEY}
    events = _event_ids(others)
    plain_others = [x for x in others if x not in CRASH_ITEMS and x != RESTART_KEY and not str(x).startswith(EV_PREFIX)]
    # 種類ごとの数量指定（「マタタビ#3」など）は、元の項目1件としてまとめて数える
    legend = [x for x in subs if x in engine.LEGEND_TIERS]
    legend_units = sum(engine.LEGEND_TIERS[x][1] for x in legend)   # レジェンドは段階が上がるごとに1件分（+25円）ずつ高くなる
    other_n = len(set(items) | {str(k).split("#")[0] for k in plain_qty}) + len(subs) - len(legend) + len(plain_others)
    rows = [("初期垢作成（リスタートパック）", 1 if restart else 0, PRICES["restart"]),
            ("レジェンドストーリー（段階ごとに1件分）", legend_units, PRICES["other"]),
            ("イベントステージ（1マップごと）", len(events), PRICES["other"]),
            ("キャラ指定（レベル・形態・本能など1項目ずつ）", chara_units(cat_nums, cat_settings), PRICES["chara"]),
            ("クラッシュ解除", len(crash), PRICES["crash"]),
            ("プレイ時間（10分ごと）", pt_min // 10, PRICES["playtime10"]),
            ("その他の項目", other_n, PRICES["other"])]
    rows = [r for r in rows if r[1]]
    return sum(n * p for _, n, p in rows), rows


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
             "maint_save": "settings", "price_save": "settings", "order_recheck": "orders", "order_start": "orders",
             "order_paid": "orders", "order_dead": "orders", "order_hold": "orders"}


def _admin_post(token):
    """管理画面の操作。結果は次の画面（GET）で表示する＝再読み込みしても同じ操作が二重に送られない。"""
    msg = ""
    if True:
        provided = request.form.get("csrf", "")
        if not hmac.compare_digest(provided.encode(), token.encode()):
            return "不正なリクエストです。", 403
        action = request.form.get("action")
        if action == "unlock":
            ip = request.remote_addr or ""
            if _too_many_fails("admin:" + ip):
                msg = "失敗が続いたので、10分ほど待ってください。"
            elif ADMIN_PASSWORD and hmac.compare_digest(request.form.get("passcode", "").encode(), ADMIN_PASSWORD.encode()):
                session["admin"] = True
                session["admin_csrf"] = secrets.token_urlsafe(32)
                return redirect("/admin")
            else:
                _fail.setdefault("admin:" + ip, []).append(time.time())
                msg = "管理者パスコードが違うか、設定されていません。"
        elif not session.get("admin"):
            return "管理者モードが必要です。", 403
        elif action == "pp_login":
            res, err = _pp(paypay_gateway.begin_login(request.form.get("phone", "").strip(), request.form.get("password", "")))
            msg = err or ("SMSの認証コードを入力してください。" if res["otp_required"] else "PayPayに接続しました。")
        elif action == "pp_otp":
            res, err = _pp(paypay_gateway.finish_login(request.form.get("otp", "").strip()))
            msg = err or "PayPayに接続しました。"
        elif action == "maint_save":
            try:
                _set_maint(request.form.get("on") == "1", request.form.get("message", ""), request.form.get("url", ""))
                msg = "メンテナンス表示を" + ("オンにしました（新規の注文と新しい支払いを止めています）。" if request.form.get("on") == "1" else "オフにしました。")
            except (ValueError, OSError) as exc:
                msg = str(exc) or "保存できませんでした。"
        elif action == "order_recheck":
            oid = request.form.get("order", "")
            r = _recheck_and_start(oid, "admin")
            msg = {"started": "PayPayで入金を確認できたので、実行を開始しました。",
                   "paid": "PayPayで入金を確認しました（支払い済み）。待機枠がいっぱいのため、利用者の画面に開始ボタンが出ます。",
                   "skip": "この注文は再確認できません（送金リンクがない、または開始済み）。"}.get(r)
            if not msg:
                kind, _, code = r.partition(":")
                msg = (f"まだ入金を確認できません（{code}）。保留中のままです。90秒ごとに自動で確認します。" if kind == "wait"
                       else f"PayPayの状態が受け取り不可でした（{code}）。PayPayアプリで着金を確認できた場合だけ「支払い済みにする」を押してください。")
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
            with _orders_lock:
                o = _orders.get(oid)
                if not o or o["state"] in ("started", "paying", "starting"):
                    msg = "その注文は変更できません（開始済み・確認中・期限切れ）。"
                else:
                    if action == "order_paid":
                        o["state"] = "paid"
                    elif action == "order_dead":
                        o["state"] = "dead"
                    else:
                        o["state"] = "hold" if o.get("link_code") else "unpaid"
                    o["diag"] = f"admin:{action} {time.strftime('%m/%d %H:%M')}"
                    msg = "注文を更新しました。"
                    if action == "order_paid" and o.get("arguments") is None:
                        o["state"] = "done"
                        msg = "この注文は入力内容が残っていない（完了済み）ため、実行できません。"
                    code_for_ledger = o.get("link_code") if action == "order_paid" else None
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
            msg = f"無料クーポン {code} を取り消しました。" if ok else "取り消せませんでした（未使用の無料クーポンだけ取り消せます）。"
        elif action == "lock":
            session.pop("admin", None)
            return redirect("/admin")
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
            msg = ("保留・要確認の注文 {} 件をPayPayに確認しました：".format(len(targets[:20])) + "、".join(f"{k} {v}件" for k, v in results.items())) if targets else "再確認が必要な注文（保留・要確認で送金リンクのあるもの）はありません。"
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
    if request.form.get("action") == "unlock" and not session.get("admin"):
        session["admin_flash"] = msg
        return redirect("/admin")
    session["admin_flash"] = msg
    return redirect("/admin#" + ADMIN_TAB.get(request.form.get("action"), "home"))


def _admin_page(msg):
    unlocked = bool(session.get("admin"))
    pp = {}
    if unlocked:
        try:
            pp = paypay_gateway.status()
        except paypay_gateway.PayPayError as exc:
            msg = msg or exc.message
    orders = []
    if unlocked:
        _gc_orders()
        with _orders_lock:
            snap = [(k, dict(v)) for k, v in _orders.items() if v["state"] != "started"]
        names = {"unpaid": "支払い待ち", "paying": "確認中", "hold": "保留中", "paid": "支払い済み・未開始", "starting": "開始処理中", "dead": "要確認（停止）"}
        for k, v in sorted(snap, key=lambda kv: -kv[1]["created"]):
            if v["state"] == "done":
                continue
            orders.append({"id": k, "uid": v.get("uid") or _uid(v["owner"]), "summary": v.get("summary", ""), "amount": v["amount"], "state": v["state"], "state_ja": names.get(v["state"], v["state"]),
                           "created": time.strftime("%m/%d %H:%M", time.localtime(v["created"])),
                           "diag": v.get("diag", ""), "ledger": paypay_gateway.link_diag(v.get("link_code") or ""),
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
        dash = {"running": par.get("running_jobs", 0), "queued": par.get("queued_jobs", 0), "max_jobs": par.get("max_concurrent_jobs", 0),
                "today": sales(day0), "week": sales(now - 7 * 86400), "uptime": int(now - _started_at), "version": APP_VERSION,
                "attention": [o for o in orders if o["state"] in ("dead", "paid", "hold")],
                "counts": counts, "open_orders": len(orders)}
    coupons = []
    if unlocked:
        try:
            for c in paypay_gateway.coupon_list(100):
                c["when"] = time.strftime("%m/%d %H:%M", time.localtime(c["created"]))
                c["until"] = time.strftime("%m/%d", time.localtime(c["expires"]))
                c["state_ja"] = {"active": "未使用", "reserved": "使用中（支払い待ち）", "used": "使用済み"}.get(c["state"], c["state"])
                c["free"] = c.get("kind") == "free"
                if c["state"] == "used" and c.get("used_order") == "revoked":
                    c["state_ja"] = "取り消し"
                if c["state"] == "active" and time.time() > c["expires"]:
                    c["state_ja"] = "期限切れ"
                coupons.append(c)
        except Exception:
            _log_orders.exception("coupon list failed")
    rp = restart_pack()
    rp_text = "\n".join(f"{k}={v}" for k, v in rp["items"].items())
    return render_template_string(ADMIN_HTML, coupons=coupons, rp=rp, all_subs=SUBS, rp_text=rp_text, rp_cats=_compress(rp["cats"]) if rp["cats"] else "",
                                  maint=_maint(), msg=msg, csrf=session["admin_csrf"], unlocked=unlocked, pp=pp, orders=orders,
                                  history=history, hist_user=hist_user, dash=dash,
                                  prices=PRICES, price_labels=PRICE_LABELS, payments=PAYMENTS_ENABLED)


# ---------------------------------------------------------------- 注文（決済してから実行）
_orders: dict[str, dict] = {}
_orders_lock = threading.Lock()


ORDER_KEEP = {"hold": 7 * 86400, "paid": 7 * 86400, "paying": 7 * 86400, "starting": 7 * 86400, "dead": 7 * 86400, "started": 86400, "done": 3 * 86400}  # 支払い済み・保留は24時間残す（それ以外は ORDER_TTL）
_log_orders = logging.getLogger("orders")


def _order_id_of(order: dict) -> str:
    with _orders_lock:
        return next((k for k, v in _orders.items() if v is order), "")


def _gc_orders() -> None:
    now = time.time()
    with _orders_lock:
        expiring = [(k, v.get("coupon")) for k, v in _orders.items()
                    if v.get("coupon") and not v.get("coupon_used") and v["state"] == "unpaid"
                    and now - v["created"] > (7 * 86400 if v.get("link_code") else ORDER_KEEP.get(v["state"], ORDER_TTL))]
    for k, c in expiring:   # 支払われずに期限切れになった注文のクーポンは、使えるように戻す
        try:
            paypay_gateway.coupon_release(c, k)
        except Exception:
            pass
    with _orders_lock:
        # 保留・支払い済み・要確認は7日、送金リンクを受け付けたことのある注文は（支払い待ちでも）7日残す
        for k in [k for k, v in _orders.items()
                  if now - v["created"] > (7 * 86400 if v.get("link_code") else ORDER_KEEP.get(v["state"], ORDER_TTL))]:
            del _orders[k]


def _order_to_json(o: dict) -> dict:
    out = {k: v for k, v in o.items() if k not in ("arguments", "transfer_key")}
    args = o.get("arguments")
    if args is not None:
        a = list(args)
        a[7] = {str(k): v for k, v in a[7].items()}  # cat_settings のキーを文字列に
        out["arguments"] = a
    tk = o.get("transfer_key")
    out["transfer_key"] = tk.hex() if tk else None
    return out


def _order_from_json(o: dict) -> dict:
    out = dict(o)
    if out.get("arguments") is not None:
        a = list(out["arguments"])
        a[7] = {int(k): v for k, v in a[7].items()}
        out["arguments"] = tuple(a)
    out["transfer_key"] = bytes.fromhex(out["transfer_key"]) if out.get("transfer_key") else None
    if out.get("state") == "paying":  # 決済確認中に再起動した → 同じリンクを再確認できる保留へ
        out["state"] = "hold" if out.get("link_code") else "unpaid"
    if out.get("state") == "starting":  # 開始処理の途中で再起動した → 支払い済みに戻す（再送金なしで再開できる）
        out.update(state="paid", retry=True)
    return out


def _persist_orders() -> None:
    """注文を暗号化して保存する。失敗してもサービスは止めない（ログのみ）。"""
    if not PAYMENTS_ENABLED:
        return
    with _orders_lock:
        snapshot = {k: _order_to_json(v) for k, v in _orders.items()}
    try:
        paypay_gateway.save_orders(snapshot)
    except Exception:
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
    with _jobs_lock:
        jobs = {k: (v.get("status"), v.get("error"), bool(v.get("outcome_unknown")), v.get("result") is not None) for k, v in _jobs.items()}
        results = {k: (v.get("result"), bool(v.get("clone")), bool(v.get("dry"))) for k, v in _jobs.items() if v.get("result") is not None}
    stamp = time.strftime('%m/%d %H:%M')
    with _orders_lock:
        for o in _orders.values():
            if o["state"] != "started":
                continue
            info = jobs.get(o.get("job_id"))
            if info and info[0] in ("running", "queued"):
                continue
            if info and info[0] == "done":
                o.update(state="done", arguments=None, transfer_key=None, diag=f"done {stamp}")
                if not o.get("admin_free") and int(o.get("amount") or 0) >= COUPON_MIN and not o.get("coupon_issued"):
                    issue_for.append(o)
                if o.get("job_id") in results:  # 発行されたコードを注文に残す（暗号化して保存。あとから開いても見られる）
                    o["result"], o["clone"], o["dry"] = results[o["job_id"]]
            elif info and (info[2] or info[3]):
                o.update(state="done", arguments=None, transfer_key=None, diag=f"結果不明: {str(info[1])[:80]} {stamp}")
                if o.get("job_id") in results:
                    o["result"], o["clone"], o["dry"] = results[o["job_id"]]
            else:
                reason = str(info[1])[:80] if info else "中断（サーバー再起動など）"
                o.update(state="paid", retry=True, job_id=None, diag=f"失敗→再実行可: {reason} {stamp}")
            changed = True
    for o in issue_for:   # 200円以上の購入にはクーポンを1枚発行（次回200円以上で10%引き・1回きり）
        try:
            o["coupon_issued"] = paypay_gateway.coupon_issue(_order_id_of(o), COUPON_DAYS)
        except Exception:
            _log_orders.exception("coupon issue failed")
    if changed:
        _persist_orders()


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


def _history_rows() -> list:
    with _orders_lock:
        return [{"order_id": k, "created": v["created"], "user_id": v.get("uid") or _uid(v["owner"]), "amount": v["amount"],
                 "state": v["state"], "summary": v.get("summary", ""), "diag": v.get("diag", "")} for k, v in _orders.items()]


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
    except Exception:
        _log_orders.exception("reconcile at startup failed")


@app.get("/")
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
        MAIN_HTML, ev_names={m["id"]: m["name"] for m in EVENT_MAPS}, legend_levels={k: v[1] for k, v in engine.LEGEND_TIERS.items()}, is_admin=bool(session.get("admin")) and PAYMENTS_ENABLED, restart=restart_pack(), pt_min=PT_MIN_MINUTES, coupon_min=COUPON_MIN, maint=_maint(), uid=_uid(_owner()), payments=PAYMENTS_ENABLED, prices=PRICES, crash_items=CRASH_ITEMS, groups=groups, totals=totals, version=APP_VERSION,
        limits=engine.QUANTITY_ITEM_LIMITS, high=HIGH_RISK_ITEMS,
        type_names={k: v for k, v in engine.ITEM_TYPE_NAMES.items() if k != "本能玉"},
        orb_kinds=item_catalog.ORB_KIND_NAMES, orb_grades=item_catalog.ORB_GRADES,
        error_nums=sorted(i + DB_OFFSET for i in engine.ERROR_CAT_IDS),
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
    token = request.form.get("token", "")
    if not ADMIN_PASSWORD or not hmac.compare_digest(token.encode(), ADMIN_PASSWORD.encode()):
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
    if _names["state"] in ("ready", "error", "none"):
        _names.update(state="none", items=[], method=None, error=None)
        _start_names()
    return jsonify(state=_names["state"])


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


@app.post("/run")
def run():
    _gc_jobs()
    admin_free = bool(session.get("admin")) and PAYMENTS_ENABLED   # 管理者でログイン中のブラウザは全メニュー無料
    if _maint()["on"] and PAYMENTS_ENABLED and not admin_free:
        return _maintenance_response()
    data = _clean_run_body(request.get_json(silent=True))
    if data is None:
        return jsonify(error="注文の内容を読み取れませんでした。画面を再読み込みしてから、もう一度お試しください。"), 400
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
    if cat_err:
        return jsonify(error="キャラ管理：" + cat_err), 400
    raw_cat_settings = data.get("cat_settings") or {}
    if not isinstance(raw_cat_settings, dict):
        return jsonify(error="キャラ個別設定の形式が正しくありません。"), 400
    allowed_cat_fields = {"base_level", "plus_level", "form", "talents_all"}
    cat_settings = {}
    for raw_number, raw_setting in raw_cat_settings.items():
        try:
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
            if isinstance(value, bool) or not str(value).isascii() or not str(value).isdigit():
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
            if not (sv.isascii() and sv.isdigit()):
                return jsonify(error=f"「{engine.type_label(k)}」の数量は0以上の整数で入力してください。"), 400
            n = int(sv)
            lim = engine.QUANTITY_ITEM_LIMITS.get(typed[0] if typed else k, 999)
            if n > lim:
                return jsonify(error=f"「{engine.type_label(k)}」の上限は {lim:,} です。"), 400
            qty[k] = n
    raw_pt = data.get("playtime_add_min")
    if not clone and not integrity_only and raw_pt not in (None, "", 0, "0"):
        sv = str(raw_pt).strip()
        if not (sv.isascii() and sv.isdigit()):
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
    if transfer_key is not None:
        with _jobs_lock:
            if transfer_key in _active_transfers:
                return jsonify(error="この引継ぎコードの処理はすでに実行中です。完了までお待ちください。"), 409
    if not PAYMENTS_ENABLED:
        if _draining():
            return _updating_response()
        return _start_job(job, arguments, transfer_key)
    if admin_free:
        if _draining():
            return _updating_response()
        oid = secrets.token_urlsafe(16)
        me = _owner()
        free = {"owner": me, "uid": _uid(me), "state": "paid", "amount": 0, "created": time.time(), "admin_free": True,
                "job": job, "arguments": arguments, "transfer_key": transfer_key, "breakdown": [], "skipped": skipped,
                "summary": "【管理者・無料】" + _summary(items, subs, others, qty, cat_nums, cat_settings, dry, clone, integrity_only, bool(transfer))}
        with _orders_lock:
            _orders[oid] = free
        resp = _start_paid_order(free)
        _persist_orders()
        return resp
    _gc_orders()
    _reconcile_orders()
    amount, rows = compute_amount(items, subs, others, qty, cat_nums, clone, integrity_only, cat_settings)
    breakdown = [{"label": l, "count": n, "unit": p} for l, n, p in rows]
    coupon = unicodedata.normalize("NFKC", str(data.get("coupon", ""))).strip().upper().replace(" ", "")[:20]
    base_amount = amount
    free_coupon = False
    if coupon:
        # 同じ人が支払う前に注文を作り直したとき：前の（未払いの）注文が押さえているクーポンを、新しい注文へ移す
        me0 = _owner()
        with _orders_lock:
            stale = [k for k, o in _orders.items() if o["owner"] == me0 and o["state"] == "unpaid"
                     and o.get("coupon") == coupon and not o.get("coupon_used")]
            for k in stale:
                _orders[k].update(state="dead", coupon=None, diag="superseded: クーポンを新しい注文へ移しました")
        for k in stale:
            try:
                paypay_gateway.coupon_release(coupon, k)
            except Exception:  # noqa: BLE001
                _log_orders.exception("coupon release failed")
        ok, why = paypay_gateway.coupon_check(coupon)
        if ok and paypay_gateway.coupon_kind(coupon) == "free":
            if _draining():
                return _updating_response()
            free_coupon = True
            amount = 0           # 管理者が発行した無料クーポン：金額に関係なく1回だけ0円
            breakdown.append({"label": f"無料クーポン {coupon}", "count": 1, "unit": -base_amount})
        else:
            if base_amount < COUPON_MIN:
                return jsonify(error=f"クーポンは {COUPON_MIN}円以上のご注文で使えます（いまは {base_amount}円）。", code="coupon_min"), 400
            if not ok:
                return jsonify(error=why, code="coupon_invalid"), 400
            amount = base_amount * 9 // 10   # 10%引き（1円未満切り捨て）
            breakdown.append({"label": f"クーポン（10%引き）{coupon}", "count": 1, "unit": amount - base_amount})
    summary = _summary(items, subs, others, qty, cat_nums, cat_settings, dry, clone, integrity_only, bool(transfer))
    me = _owner()
    with _orders_lock:
        # 支払い済みなのに実行できなかった注文があれば、その支払いを今回の内容に充てる（同額以下のとき・払い直し不要）
        credit = next((k for k, o in sorted(_orders.items(), key=lambda kv: kv[1]["created"])
                       if o["owner"] == me and o["state"] == "paid" and o.get("retry")
                       and (o["amount"] >= amount or o.get("free_coupon"))), None)
        if credit:
            o = _orders[credit]
            breakdown = [b for b in breakdown if not str(b.get("label", "")).startswith("クーポン")]   # 支払い済みを充てるので、クーポンは使わない
            o.update(job=job, arguments=arguments, transfer_key=transfer_key, breakdown=breakdown, skipped=skipped, summary=summary)
            paid_amount = o["amount"]
        else:
            order_id = secrets.token_urlsafe(16)
            _orders[order_id] = {"owner": me, "uid": _uid(me), "state": "unpaid", "amount": amount, "created": time.time(),
                                 "job": job, "arguments": arguments, "transfer_key": transfer_key,
                                 "breakdown": breakdown, "skipped": skipped, "summary": summary,
                                 "coupon": coupon or None, "base_amount": base_amount}
    if coupon and not credit:
        if not paypay_gateway.coupon_reserve(coupon, order_id):   # 同時に2つの注文で使われないよう押さえる
            with _orders_lock:
                _orders.pop(order_id, None)
            return jsonify(error="このクーポンはすでに使われたか、ほかの注文で使用中です。", code="coupon_invalid"), 400
    if free_coupon and not credit:
        # 無料クーポン：支払いは無いので、そのまま「支払い済み」にして開始する（クーポンは開始時に使用済みになる）
        with _orders_lock:
            o = _orders[order_id]
            o.update(state="paid", free_coupon=True, summary="【無料クーポン】" + summary)
        resp = _start_paid_order(o)
        _persist_orders()
        return resp
    _persist_orders()
    if credit:
        return jsonify(order=credit, amount=paid_amount, skipped=skipped, breakdown=breakdown, state="paid", prepaid=True)
    return jsonify(order=order_id, amount=amount, skipped=skipped, breakdown=breakdown)


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
        try:
            slot = _slots.index(None)
        except ValueError:
            if len(_pending_jobs) >= MAX_QUEUED_JOBS:
                return jsonify(error="待機枠がいっぱいです。しばらくしてからもう一度お試しください。"), 429
            job["status"] = "queued"
            _jobs[job_id] = job
            if transfer_key is not None:
                _active_transfers.add(transfer_key)
            _pending_jobs.append((job_id, arguments, transfer_key))
            return jsonify(job=job_id)
        _slots[slot] = job_id
        if transfer_key is not None:
            _active_transfers.add(transfer_key)
        _jobs[job_id] = job
        try:
            _launch_job(job_id, job, arguments, slot, transfer_key)
        except Exception:
            _slots[slot] = None
            _active_transfers.discard(transfer_key)
            del _jobs[job_id]
            return jsonify(error="処理を開始できませんでした。もう一度お試しください。"), 503
    return jsonify(job=job_id)


RESUMABLE = ("unpaid", "hold", "paid")
VISIBLE = RESUMABLE + ("dead",)  # dead は再開できないが、注文番号を管理者に伝えられるよう表示する


def _public_order(order_id: str, o: dict) -> dict:
    return {"order": order_id, "amount": o["amount"], "state": o["state"], "created": int(o["created"]), "retry": bool(o.get("retry")),
            "result": o.get("result") if o["state"] == "done" and not o.get("dry") else None, "clone": bool(o.get("clone")),
            "coupon_issued": o.get("coupon_issued"),
            "breakdown": o.get("breakdown") or [], "skipped": o.get("skipped") or []}


@app.get("/api/orders/mine")
def my_orders():
    """このブラウザの未完了の注文（支払い待ち・保留・支払い済み未開始）。"""
    _gc_orders()
    _reconcile_orders()
    me = _owner()
    with _jobs_lock:
        live = set(_jobs)
    with _orders_lock:
        items = [_public_order(k, v) for k, v in _orders.items() if v["owner"] == me and v["state"] in VISIBLE
                 and not str(v.get("diag", "")).startswith("superseded")]  # 新しい注文に支払いを引き継いだ古い注文は見せない
        for k, v in _orders.items():  # 実行中・完了した注文は、状況・結果を見られるようにする
            if v["owner"] == me and v["state"] == "started" and v.get("job_id") in live:
                items.append(dict(_public_order(k, v), job=v["job_id"]))
            elif v["owner"] == me and v["state"] == "done" and (v.get("job_id") in live or (v.get("result") and not v.get("dry"))):
                items.append(dict(_public_order(k, v), job=v.get("job_id") if v.get("job_id") in live else None))
    items.sort(key=lambda x: -x["created"])
    return jsonify(orders=items, maint=_maint())


@app.post("/api/orders/<order_id>/claim")
def claim_order(order_id: str):
    """注文番号で再開（別のブラウザ・Cookie消去後でも）。注文番号は推測できない長さの秘密の値。"""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
    if _too_many_fails("claim:" + ip):
        return jsonify(error="失敗が続いたので、10分ほど待ってください。", code="too_many_fails"), 429
    order_id = (order_id or "").strip()
    with _orders_lock:
        o = _orders.get(order_id)
        if not o or o["state"] not in VISIBLE:
            _fail.setdefault("claim:" + ip, []).append(time.time())
            return jsonify(error="再開できる注文が見つかりません。番号を確認してください（実行開始済み・期限切れの注文は再開できません）。", code="order_missing"), 404
        o["owner"] = _owner()
        o["uid"] = _uid(o["owner"])
        o["job"]["owner"] = o["owner"]
        out = _public_order(order_id, o)
    _persist_orders()
    return jsonify(out)


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
    paypay_gateway.adopt_link(code, order_id, order["amount"])
    with _orders_lock:  # 付け替えに成功したら、古い注文は無効にする（二重に実行させない）
        for k, o in others:
            o["state"] = "dead"
            o["link_code"] = None
            o["diag"] = f"superseded {time.strftime('%m/%d %H:%M')}"


RECHECK_WAIT = ("temporary_hold", "accept_uncertain", "accept_unconfirmed", "still_pending", "link_network", "link_check",
                "rate_limited", "paypay_unavailable", "session_rejected", "login_required", "otp_required", "cookie_missing",
                "oauth_rejected", "oauth_network", "oauth_response", "runtime_store", "account_unreadable", "missing_key", "invalid_key")


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
        asyncio.run(paypay_gateway.confirm_received(order["link_code"], order_id, order["amount"]))
    except paypay_gateway.PayPayError as exc:
        with _orders_lock:
            if exc.code == "not_found":  # このリンクは一度も受け付けていない（検証で弾かれた等）→ 状態は変えない
                order["state"] = previous
            else:
                order["state"] = "hold" if exc.code in RECHECK_WAIT else "dead"
            order["diag"] = f"{who}:{exc.code} {time.strftime('%m/%d %H:%M')}"
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
                targets = [k for k, o in sorted(_orders.items(), key=lambda kv: kv[1]["created"])
                           if o["state"] == "hold" and o.get("link_code") and now - o["created"] < 3 * 86400][:5]
            for k in targets:
                _recheck_and_start(k, "auto")
        except Exception:
            _log_orders.exception("hold watcher failed")


def _pay_order(order_id: str):
    """PayPay送金リンクを受け取り、確認できたら実行を開始する。支払い済みなら再送不要で開始だけ再試行。"""
    data = request.get_json(silent=True) or {}
    if _draining():
        return _updating_response()
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
        HOLDISH = ("temporary_hold", "accept_uncertain", "accept_unconfirmed")
        try:
            if mode == "link":
                raw = str(data.get("link", ""))
                order["link_code"] = paypay_gateway._normalize_link(raw) or order.get("link_code")
                _persist_orders()  # 送金リンクを受け取る前に保存（途中で再起動しても同じリンクを再確認できる）
                try:
                    asyncio.run(paypay_gateway.receive_payment(raw, order["amount"], order_id))
                except paypay_gateway.PayPayError as dup:
                    if dup.code != "duplicate" or not order.get("link_code"):
                        raise
                    # 以前の注文（保留・停止で実行されなかった）と同じリンク → この注文に付け替えて確認し直す
                    _adopt_existing_link(order_id, order)
                    mode = "recheck"
                    asyncio.run(paypay_gateway.confirm_received(order["link_code"], order_id, order["amount"]))
            else:
                asyncio.run(paypay_gateway.confirm_received(order.get("link_code"), order_id, order["amount"]))
        except paypay_gateway.PayPayError as exc:
            # 待てば直るもの（PayPay側の保留・混雑・通信、こちらのPayPay接続切れなど）
            WAITING = ("still_pending", "temporary_hold", "link_network", "link_check", "rate_limited", "paypay_unavailable",
                       "session_rejected", "login_required", "otp_required", "cookie_missing", "oauth_rejected",
                       "oauth_network", "oauth_response", "runtime_store", "account_unreadable", "missing_key", "invalid_key")
            if not (mode == "recheck" and exc.code in WAITING):
                _fail.setdefault("paypay:" + order_id, []).append(time.time())
            hold = bool(order.get("link_code")) and (exc.code in HOLDISH or (mode == "recheck" and exc.code in WAITING))
            # 再確認（同じリンク）で、待てば直る種類以外のエラー → 自動では進められないので管理者確認へ。
            # 「支払い待ち」に戻すと、台帳にこの注文のリンクが残っているため新しいリンクも受け付けられず行き詰まる。
            dead = mode == "recheck" and not hold
            with _orders_lock:
                order["state"] = "dead" if dead else ("hold" if hold else "unpaid")
                order["diag"] = f"{exc.code} {time.strftime('%m/%d %H:%M')}"
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
    with _orders_lock:
        # 開始できるのは「支払い済み」の注文だけ。開始の権利をロック内で1回だけ取る（同時押しで二重に実行しない）
        if order["state"] != "paid" or order.get("arguments") is None:
            return jsonify(error="この注文は実行を開始できる状態ではありません。画面を開き直してください。", code="started"), 409
        order["state"] = "starting"
        coupon_to_use = order.get("coupon") if not order.get("coupon_used") else None
    if coupon_to_use:   # 入金が確認できた（支払い済み）ので、クーポンを使用済みにする
        try:
            paypay_gateway.coupon_consume(coupon_to_use, _order_id_of(order))
            order["coupon_used"] = True
        except Exception:
            _log_orders.exception("coupon consume failed")
    try:
        resp = _start_job(order["job"], order["arguments"], order["transfer_key"])
    except Exception:
        resp = (jsonify(error="処理を開始できませんでした。もう一度お試しください。"), 503)
    body, code = (resp if isinstance(resp, tuple) else (resp, 200))
    if code != 200:
        with _orders_lock:
            order["state"] = "paid"  # 支払いは有効のまま。もう一度押せば再送金なしで開始できる
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
        position = next((i for i, entry in enumerate(_pending_jobs, 1) if entry[0] == job_id), None)
        slot_number = next((i for i, value in enumerate(_slots, 1) if value == job_id), None)
        parallel = _parallel_state_locked()
        # Copy the mutable fields used below while holding the same lock as
        # worker updates and slot transitions.
        job = dict(job) if job else None
    if not job or job.get("owner") != _owner():
        return jsonify(error="見つかりません（時間切れ、またはサーバーが再起動した可能性）"), 404
    end = job["finished"] or time.time()
    return jsonify(
        status=job["status"], queue_position=position, slot=slot_number,
        active_jobs=parallel["running_jobs"], warmup_slots=parallel["warmup_slots"],
        occupied_slots=parallel["occupied_slots"], available_slots=parallel["available_slots"],
        max_concurrent_jobs=parallel["max_concurrent_jobs"], queued_jobs=parallel["queued_jobs"],
        step=job["step"], total=TOTAL_STEPS,
        mode=job["mode"], count=job["count"],
        elapsed=round(end - job["created"], 1),
        log=job["log"], warn=job["warn"], report=job["report"], dry=job["dry"], clone=job["clone"],
        outcome_unknown=job.get("outcome_unknown", False),
        result=job["result"], error=job["error"], integrity=job.get("integrity"), integrity_only=job.get("integrity_only", False),
        audit=job.get("audit"),
        repair=job.get("repair"), post_audit=job.get("post_audit"),
    )


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
                _jobs[job_id].update(status="error", error="待機中の処理を取り消しました。", finished=time.time())
                return jsonify(ok=True)
    return jsonify(error="待機中の処理が見つかりません。"), 409


# ---------------------------------------------------------------- 画面
ADMIN_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark"><meta name="robots" content="noindex,nofollow">
<title>管理者 | にゃんこ代行</title>
<style>
:root{--bg:#e8edf2;--panel:#fff;--ink:#17181c;--muted:#5b6472;--line:#17181c;--soft:#f1f4f8;--accent:#ffc42e;--ok:#12a05c;--warn:#e08a00;--bad:#d93636;--okbg:#e6f6ee;--warnbg:#fff3dc;--badbg:#fdebeb}
@media (prefers-color-scheme:dark){:root{--bg:#121418;--panel:#1d2026;--ink:#f1f3f6;--muted:#9aa4b2;--line:#444b57;--soft:#262a32;--okbg:#14291f;--warnbg:#33290f;--badbg:#321a1a}}
*{box-sizing:border-box}html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Hiragino Sans","Noto Sans JP",system-ui,sans-serif;line-height:1.5;padding-bottom:40px}
.wrap{max-width:760px;margin:0 auto;padding:0 14px}
header{padding:calc(14px + env(safe-area-inset-top,0px)) 0 8px;display:flex;align-items:center;gap:10px}
header h1{margin:0;font-size:20px;flex:1}header a{font-size:13px;color:var(--muted)}
.nav{position:sticky;top:0;z-index:5;background:var(--bg);padding:6px 0 8px}
.tabs{display:flex;gap:6px;overflow-x:auto;scrollbar-width:none;padding:8px 2px 4px}.tabs::-webkit-scrollbar{display:none}
.tab{position:relative;flex:1 0 auto;min-height:42px;padding:0 12px;border:2px solid var(--line);border-radius:12px;background:var(--panel);color:var(--ink);font:inherit;font-size:13.5px;font-weight:700;cursor:pointer;box-shadow:0 3px 0 var(--line);white-space:nowrap}
.tab[aria-selected=true]{background:var(--accent);color:#17181c}.tab:active{transform:translateY(2px);box-shadow:0 1px 0 var(--line)}
.badge{position:absolute;top:-8px;right:-4px;min-width:20px;padding:0 5px;border:2px solid var(--line);border-radius:999px;background:var(--bad);color:#fff;font-size:11px;line-height:16px;text-align:center}
.badge.warn{background:var(--warn)}
section.view{display:none}section.view.on{display:block}
.card{background:var(--panel);border:2px solid var(--line);border-radius:16px;box-shadow:0 3px 0 var(--line);padding:14px;margin-top:12px}
.card h2{margin:0 0 4px;font-size:17px}.card h3{margin:14px 0 6px;font-size:14px}.sub{margin:0 0 8px;font-size:13px;color:var(--muted)}
.msgbar{margin-top:12px;padding:10px 12px;border:2px solid var(--line);border-radius:12px;background:var(--accent);color:#17181c;font-weight:700;font-size:14px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-top:10px}
.stat{background:var(--soft);border-radius:12px;padding:10px 12px}.stat small{display:block;color:var(--muted);font-size:12px}.stat b{display:block;font-size:20px;line-height:1.3}.stat span{font-size:12px;color:var(--muted)}
.stat.ok{background:var(--okbg)}.stat.warn{background:var(--warnbg)}.stat.bad{background:var(--badbg)}
.chip{display:inline-block;padding:1px 9px;border-radius:999px;border:1.5px solid var(--line);font-size:12px;font-weight:700;background:var(--soft);white-space:nowrap}
.chip.ok{background:var(--okbg);border-color:var(--ok);color:var(--ok)}.chip.warn{background:var(--warnbg);border-color:var(--warn);color:var(--warn)}.chip.bad{background:var(--badbg);border-color:var(--bad);color:var(--bad)}
label{display:block;font-size:13px;font-weight:700;margin:10px 0 4px}
input[type=text],input[type=password],input[type=search],input:not([type]),textarea,input[type=tel],input[type=url]{width:100%;font:inherit;font-size:16px;padding:11px 12px;border:2px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}
textarea{min-height:76px;resize:vertical}
input:focus-visible,textarea:focus-visible,button:focus-visible,.tab:focus-visible{outline:3px solid #2f7df6;outline-offset:2px}
.btn{min-height:42px;padding:0 14px;border:2px solid var(--line);border-radius:10px;background:var(--panel);color:var(--ink);font:inherit;font-size:14px;font-weight:700;cursor:pointer;box-shadow:0 2px 0 var(--line)}
.btn:active{transform:translateY(1px);box-shadow:0 1px 0 var(--line)}
.btn.pri{background:var(--accent);color:#17181c}.btn.danger{color:var(--bad);border-color:var(--bad);box-shadow:0 2px 0 var(--bad)}.btn.full{width:100%;margin-top:10px}
.row{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.row .btn{flex:1 1 auto}
.order{border:2px solid var(--line);border-radius:14px;padding:12px;margin-top:10px;background:var(--panel)}
.order.bad{border-color:var(--bad);background:var(--badbg)}.order.warn{border-color:var(--warn);background:var(--warnbg)}.order.ok{border-color:var(--ok);background:var(--okbg)}
.order .top{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.order .amt{margin-left:auto;font-size:22px;font-weight:800}
.order .meta{font-size:12.5px;color:var(--muted);margin-top:4px;word-break:break-all}
.order .sum{font-size:13.5px;margin-top:6px}
code{font-size:12px;background:var(--soft);padding:1px 5px;border-radius:5px;word-break:break-all}
a{color:#2f7df6}@media (prefers-color-scheme:dark){a{color:#7fb0ff}}
header a{color:var(--muted)}
.note{margin:8px 0 0;padding:8px 10px;border-radius:10px;font-size:13px;background:var(--warnbg);border:1.5px solid var(--warn)}
.note.bad{background:var(--badbg);border-color:var(--bad)}
.h{border-top:1px solid var(--soft);padding:10px 0}.h:first-child{border-top:0}
.h .l1{display:flex;align-items:center;gap:6px;flex-wrap:wrap}.h .l1 .amt{margin-left:auto;font-weight:800}
.h .l2{font-size:13px;color:var(--muted);margin-top:2px}.h details{margin-top:4px;font-size:12.5px;color:var(--muted)}.h summary{cursor:pointer}
.sw{display:flex;gap:10px;margin:10px 0}.sw label{flex:1;margin:0;padding:12px;border:2px solid var(--line);border-radius:12px;text-align:center;cursor:pointer;font-size:14px}
.sw input{position:absolute;opacity:0}.sw label:has(input:checked){background:var(--accent);color:#17181c}
.empty{padding:16px 0;text-align:center;color:var(--muted);font-size:14px}
.gate{max-width:380px;margin:14vh auto 0}
table.price{width:100%;border-collapse:collapse;font-size:14px}.price td{padding:6px 0;border-top:1px solid var(--soft)}.price td:last-child{text-align:right;font-weight:800}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
</style></head><body>
<div class="wrap">
<header><h1>管理者</h1><a href="/">← サイトへ戻る</a></header>
{% if msg %}<div class="msgbar" role="status">{{ msg }}</div>{% endif %}

{% if not unlocked %}
<div class="card gate"><h2>管理者パスコード</h2><p class="sub">入るにはパスコードが必要です。</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="unlock">
<label for="pc">パスコード</label><input id="pc" name="passcode" type="password" autocomplete="current-password" required>
<button class="btn pri full">入る</button></form></div>
{% else %}

<div class="nav"><div class="tabs" role="tablist">
<button class="tab" role="tab" data-sec="home">ホーム</button>
<button class="tab" role="tab" data-sec="orders">注文{% if dash.attention %}<span class="badge">{{ dash.attention|length }}</span>{% endif %}</button>
<button class="tab" role="tab" data-sec="history">履歴</button>
<button class="tab" role="tab" data-sec="paypay">PayPay{% if not pp.ready %}<span class="badge">!</span>{% endif %}</button>
<button class="tab" role="tab" data-sec="settings">設定{% if maint.on %}<span class="badge warn">ON</span>{% endif %}</button>
</div></div>

<!-- ===== ホーム ===== -->
<section class="view" id="sec-home">
<div class="card"><h2>いまの状態</h2>
<div class="grid">
<div class="stat {{ 'ok' if pp.ready else 'bad' }}"><small>PayPay 接続</small><b>{% if pp.ready %}接続済み{% elif pp.connected %}再認証が必要{% else %}未接続{% endif %}</b></div>
<div class="stat {{ 'warn' if maint.on else '' }}"><small>メンテナンス表示</small><b>{{ 'オン' if maint.on else 'オフ' }}</b></div>
<div class="stat"><small>実行中 / 待機</small><b>{{ dash.running }} / {{ dash.queued }}</b><span>同時実行の上限 {{ dash.max_jobs }}</span></div>
<div class="stat {{ 'bad' if dash.attention else 'ok' }}"><small>要対応の注文</small><b>{{ dash.attention|length }} 件</b><span>未完了 全{{ dash.open_orders }}件</span></div>
</div></div>
<div class="card"><h2>売上（入金確認済み）</h2>
<div class="grid"><div class="stat"><small>今日</small><b>{{ '{:,}'.format(dash.today.yen) }} 円</b><span>{{ dash.today.count }} 件</span></div>
<div class="stat"><small>直近7日</small><b>{{ '{:,}'.format(dash.week.yen) }} 円</b><span>{{ dash.week.count }} 件</span></div></div>
<p class="sub" style="margin-top:8px">履歴に残っている範囲の集計です。</p></div>
{% if dash.attention %}
<div class="card"><h2>要対応</h2><p class="sub">「注文」タブで対応できます。</p>
{% for o in dash.attention[:5] %}<div class="h"><div class="l1"><span class="chip {{ 'bad' if o.state=='dead' else 'warn' }}">{{ o.state_ja }}</span><span class="chip">{{ o.created }}</span><span class="amt">{{ o.amount }}円</span></div>
<div class="l2">{{ o.summary }}</div></div>{% endfor %}
<button class="btn pri full" data-go="orders">注文を見る</button></div>
{% endif %}
<div class="card"><h2>サーバー</h2><p class="sub">バージョン {{ dash.version }} ・ 稼働 {{ (dash.uptime // 3600) }}時間{{ ((dash.uptime % 3600) // 60) }}分</p></div>
</section>

<!-- ===== 注文 ===== -->
<section class="view" id="sec-orders">
<div class="card"><h2>未完了の注文</h2>
<p class="sub">保留などで止まった注文です。<b>PayPayアプリで着金を確認できたら「支払い済みにする」</b>を押してください。押すとすぐ実行が始まり、利用者の画面は自動で切り替わります。</p>
{% for o in orders %}
<div class="order {{ 'bad' if o.state=='dead' else ('warn' if o.state in ('hold','paid') else '') }}">
<div class="top"><span class="chip {{ 'bad' if o.state=='dead' else ('warn' if o.state in ('hold','paid') else '') }}">{{ o.state_ja }}</span><span class="chip">{{ o.created }}</span><span class="amt">{{ o.amount }}円</span></div>
<div class="sum">{{ o.summary }}</div>
<div class="meta">USERID <a href="/admin?user={{ o.uid }}#history"><code>{{ o.uid }}</code></a>　注文番号 <code>{{ o.id }}</code></div>
<div class="meta">最後の結果: {{ o.diag or "-" }}{% if o.ledger %}　／　PayPay: {{ o.ledger }}{% endif %}{% if not o.has_link %}　／　リンク未送信{% endif %}</div>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="order" value="{{ o.id }}">
{% if o.superseded %}<div class="note bad">この注文の支払いは、同じ利用者の新しい注文に引き継ぎ済みです。支払い済みにしないでください（二重に実行されます）。</div>
{% elif o.state != "paid" %}{% if not o.has_link %}<div class="note">この注文には送金リンクの記録がありません（未払いの可能性が高いです）。</div>{% endif %}{% endif %}
{% if o.state == "paid" and not o.has_link %}<div class="note">この注文には送金リンク（入金）の記録がありません。同じ人の別の注文と重複していないか確認してから開始してください。</div>{% endif %}
<div class="row">
{% if o.has_link and o.state in ("hold", "unpaid", "dead") %}<button class="btn" name="action" value="order_recheck">PayPayに再確認する</button>{% endif %}
{% if o.state == "paid" %}<button class="btn pri" name="action" value="order_start" onclick="return confirm('この注文の実行を開始しますか？')">実行を開始する</button>{% endif %}
{% if not o.superseded and o.state != "paid" %}<button class="btn {{ 'pri' if o.has_link else '' }}" name="action" value="order_paid" onclick="return confirm('{% if not o.has_link %}この注文には入金の記録がありません。{% endif %}PayPayアプリで {{ o.amount }}円 の着金を確認しましたか？（同じ人の別の注文を、すでに支払い済みにしていませんか？）')">{{ "支払い済みにする" if o.has_link else "入金記録なし・それでも支払い済みにする" }}</button>{% endif %}
{% if o.state != "dead" %}<button class="btn danger" name="action" value="order_dead" onclick="return confirm('この注文を停止しますか？')">停止する</button>{% endif %}
</div></form></div>
{% else %}<div class="empty">未完了の注文はありません 🎉</div>{% endfor %}
</div></section>

<!-- ===== 履歴 ===== -->
<section class="view" id="sec-history">
<div class="card"><h2>取引履歴{% if hist_user %}<span class="chip" style="margin-left:6px">{{ hist_user }}</span>{% endif %}</h2>
<p class="sub">新しい順に最大200件。USERID はブラウザごとの番号（Cookieを消すと変わります）。送金者は、送金リンクを確認できた注文だけ表示されます。{% if hist_user %}<a href="/admin#history">すべて表示</a>{% endif %}</p>
<input type="search" id="hq" placeholder="絞り込み（金額・内容・ID・状態）" autocomplete="off" aria-label="履歴を絞り込み">
<div id="hlist" style="margin-top:6px">
{% for h in history %}<div class="h" data-q="{{ h.when }} {{ h.user_id }} {{ h.sender_id or '' }} {{ h.sender_name or '' }} {{ h.amount }} {{ h.state_ja }} {{ h.summary }} {{ h.order_id }}">
<div class="l1"><span class="chip {{ 'ok' if h.state=='done' else ('bad' if h.state=='dead' else ('warn' if h.state in ('hold','paid','started','starting') else '')) }}">{{ h.state_ja }}</span>
{% if h.received %}<span class="chip ok">入金済</span>{% endif %}<span class="chip">{{ h.when }}</span><span class="amt">{{ h.amount }}円</span></div>
<div class="l2">{{ h.summary }}</div>
<div class="l2">USERID <a href="/admin?user={{ h.user_id }}#history"><code>{{ h.user_id }}</code></a>{% if h.sender_id %}　送金者 {{ h.sender_name or "(名前なし)" }} <a href="/admin?user={{ h.sender_id }}#history"><code>{{ h.sender_id[:16] }}</code></a>{% endif %}</div>
<details><summary>くわしく</summary>メモ: {{ h.diag or "-" }}{% if h.last_status %}<br>PayPay: {{ h.last_status }}{% endif %}<br>注文番号: <code>{{ h.order_id }}</code></details></div>
{% else %}<div class="empty">まだありません</div>{% endfor %}
</div></div></section>

<!-- ===== PayPay ===== -->
<section class="view" id="sec-paypay">
<div class="card"><h2>PayPay 受取アカウント</h2>
<p><span class="chip {{ 'ok' if pp.ready else ('warn' if pp.connected else 'bad') }}">{% if pp.ready %}接続済み（有効）{% elif pp.connected %}接続済み（セッション期限切れ。次の決済時に再認証）{% else %}未接続{% endif %}</span></p>
<h3>手動での更新</h3>
<p class="sub">それぞれ別々に使えます。押したあと画面を再読み込みしても、同じ操作がもう一度送られることはありません。</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}">
<button class="btn full" name="action" value="pp_refresh" onclick="return confirm('保存済みの電話番号・パスワードでPayPayにログインし直します。SMS認証を求められることがあります。よろしいですか？')">🔄 PayPayの接続を更新（ログインし直す）</button>
<button class="btn full" name="action" value="pp_recheck_all" onclick="return confirm('保留・要確認の注文を、いますぐ全部PayPayに確認します。入金済みのものは実行まで進みます。よろしいですか？')">🔍 保留中の注文をまとめて再確認</button>
</form>
<a class="btn full" href="/admin#paypay" style="display:flex;align-items:center;justify-content:center;text-decoration:none;color:var(--ink)">↻ 状態を表示し直す（何も送らない）</a>
<p class="sub" style="margin-top:12px">初めて接続するとき・接続が切れたときは、① 電話番号とパスワードでログイン → ② SMS で届いた4桁のコードを入力、の順です。</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="pp_login">
<h3>① ログイン</h3>
<label for="ph">電話番号</label><input id="ph" name="phone" type="tel" inputmode="tel" autocomplete="off" required>
<label for="pw">パスワード</label><input id="pw" name="password" type="password" autocomplete="off" required>
<button class="btn pri full">PayPayにログイン</button></form>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="pp_otp">
<h3>② SMS 認証</h3>
<label for="otp">認証コード（4桁）</label><input id="otp" name="otp" inputmode="numeric" maxlength="4" autocomplete="off">
<button class="btn full">認証</button></form></div></section>

<!-- ===== 設定 ===== -->
<section class="view" id="sec-settings">
<div class="card"><h2>メンテナンス表示</h2>
<p class="sub">オンの間は新規の注文・新しい支払いを止めます。すでに支払い済み・保留中の注文は最後まで進みます。</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="maint_save">
<div class="sw"><label><input type="radio" name="on" value="1" {{ "checked" if maint.on }}>オン</label><label><input type="radio" name="on" value="0" {{ "checked" if not maint.on }}>オフ</label></div>
<label for="mm">表示する文章（空なら標準の文章）</label><textarea id="mm" name="message" maxlength="300">{{ maint.message }}</textarea>
<label for="mu">新しいサイトのURL（移行後に入れると、押せるリンクが出ます）</label><input id="mu" name="url" type="url" value="{{ maint.url }}" placeholder="https://..." maxlength="300">
<button class="btn pri full">保存</button></form></div>
<div class="card"><h2>料金（決済{{ "あり" if payments else "なし" }}）</h2>
<p class="sub">変更は、保存したあとの<b>新しい注文から</b>反映されます。作成済み・支払い中の注文の金額は変わりません。</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="price_save">
{% for k, label in price_labels.items() %}
<label for="pr-{{ k }}">{{ label }}（円）</label><input id="pr-{{ k }}" name="{{ k }}" type="text" inputmode="numeric" pattern="[0-9]*" value="{{ prices[k] }}" required maxlength="6">
{% endfor %}
<button class="btn pri full" onclick="return confirm('料金を変更します。これからの新しい注文から反映されます。よろしいですか？')">料金を保存</button></form></div>
<div class="card"><h2>初期垢作成（リスタートパック）</h2>
<p class="sub">新しいアカウントに入れる中身です。1行に「項目名=数量」（例：<code>猫缶=1500</code>、<code>レアチケ=10</code>、種類ごとは <code>マタタビ#1=5</code>）。キャラは番号で（例：<code>1-9, 25</code>）。料金は「設定」の料金欄の「初期垢作成」で変えられます。</p>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="restart_save">
<div class="sw"><label><input type="radio" name="enabled" value="1" {{ "checked" if rp.enabled }}>選べるようにする</label><label><input type="radio" name="enabled" value="0" {{ "checked" if not rp.enabled }}>準備中</label></div>
<label for="rpi">アイテム（1行に1つ）</label><textarea id="rpi" name="items" rows="6">{{ rp_text }}</textarea>
<label for="rpc">キャラ番号（任意）</label><input id="rpc" name="cats" value="{{ rp_cats }}" placeholder="例：1-9, 25">
<label for="rpl">キャラのレベル（任意・空欄なら解放だけ）</label><input id="rpl" name="cat_level" inputmode="numeric" maxlength="2" value="{{ rp.cat_level or '' }}" placeholder="例：20">
<label for="rpf">施設のレベル（任意・にゃんこ砲の射程などは上限まで）</label><input id="rpf" name="facility_level" inputmode="numeric" maxlength="2" value="{{ rp.facility_level or '' }}" placeholder="例：20">
<label>一緒にクリアするステージ・サブ（任意）</label>
<div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 10px;font-size:13.5px">{% for x in all_subs %}<label style="font-weight:400;margin:2px 0"><input type="checkbox" name="subs" value="{{ x }}" {{ "checked" if x in rp.subs }}> {{ x }}</label>{% endfor %}</div>
<label for="rpn">利用者に見せる説明（任意）</label><input id="rpn" name="note" value="{{ rp.note }}" maxlength="300" placeholder="例：公式リスタートパックと同じ内容">
<button class="btn pri full">保存</button></form>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="restart_official">
<button class="btn full" onclick="return confirm('公式リスタートパックと同じ中身（日本編1〜3章クリア・お宝金・基本キャラ9種＋もねこ・ネコヴァルキリー・ネコムートLv20・施設Lv20・レアチケ1・にゃんチケ10）を入れて、利用者が選べるようにします。よろしいですか？')">⭐ 公式と同じ中身を入れて公開する</button></form></div>
<div class="card"><h2>クーポン</h2><p class="sub">支払額200円以上の注文の完了時に自動で発行（次回200円以上で10%引き・1回きり・30日）。新しい順に最大100件。</p>
<form method="post" style="margin:8px 0 14px;padding:10px;border:1px dashed var(--line);border-radius:10px"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="coupon_free_issue">
<b>🎁 無料クーポンを発行</b><p class="sub" style="margin:4px 0">1回だけ、どのメニューでも0円で実行できるコード（FR-XXXX-XXXX）を作ります。利用者は注文確認の画面でクーポン欄に入れます。</p>
<label for="fcd">有効期限（日）</label><input id="fcd" name="days" inputmode="numeric" maxlength="3" value="30">
<label for="fcn">メモ（任意・自分用。例：誰に渡したか）</label><input id="fcn" name="note" maxlength="60" placeholder="例：お詫び用">
<button class="btn pri full" onclick="return confirm('1回だけ無料で使えるクーポンを発行します。よろしいですか？')">発行する</button></form>
{% for c in coupons %}<div class="h"><div class="l1"><code>{{ c.code }}</code>{% if c.free %}<span class="chip ok">無料</span>{% endif %}<span class="chip {{ 'ok' if c.state_ja=='未使用' else ('warn' if c.state=='reserved' else '') }}">{{ c.state_ja }}</span><span class="chip">{{ c.when }}発行</span><span class="amt" style="font-size:12px">〜{{ c.until }}</span></div>
{% if c.free and c.note %}<div class="sub" style="font-size:12px">メモ：{{ c.note }}</div>{% endif %}
{% if c.free and c.state_ja=='未使用' %}<form method="post" style="margin-top:4px"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="coupon_revoke"><input type="hidden" name="code" value="{{ c.code }}"><button class="btn" style="padding:4px 10px;font-size:12px" onclick="return confirm('このクーポンを取り消します。よろしいですか？')">取り消す</button></form>{% endif %}</div>
{% else %}<div class="empty">まだ発行していません</div>{% endfor %}</div>
<div class="card"><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="lock"><button class="btn full">管理者モードを終了</button></form></div>
</section>

<script>
(function(){
  var secs=['home','orders','history','paypay','settings'];
  function show(id,push){
    if(secs.indexOf(id)<0) id='home';
    secs.forEach(function(s){ var e=document.getElementById('sec-'+s); if(e) e.classList.toggle('on',s===id); });
    document.querySelectorAll('.tab').forEach(function(b){ b.setAttribute('aria-selected',b.dataset.sec===id?'true':'false'); });
    try{ sessionStorage.setItem('adm-tab',id); }catch(e){}
    if(push) history.replaceState(null,'','#'+id);
    window.scrollTo(0,0);
  }
  document.querySelectorAll('.tab').forEach(function(b){ b.addEventListener('click',function(){ show(b.dataset.sec,true); }); });
  document.querySelectorAll('[data-go]').forEach(function(b){ b.addEventListener('click',function(){ show(b.dataset.go,true); }); });
  var start=(location.hash||'').slice(1);
  if(!start){ try{ start=sessionStorage.getItem('adm-tab')||''; }catch(e){} }
  /* 操作の直後（メッセージが出ているとき）は、その操作をしたタブに戻す */
  show(start||'home',false);
  var hq=document.getElementById('hq');
  if(hq) hq.addEventListener('input',function(){
    var q=hq.value.trim().toLowerCase();
    document.querySelectorAll('#hlist .h').forEach(function(r){ r.style.display=(!q||r.dataset.q.toLowerCase().indexOf(q)>=0)?'':'none'; });
  });
})();
</script>
{% endif %}
</div></body></html>"""


MAIN_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>にゃんこ代行</title>
<style>
:root{
  --bg:#e8edf2;--panel:#fff;--ink:#17181c;--muted:#5b6472;--line:#17181c;--soft:#f1f4f8;
  --accent:#ffc42e;--ok:#12a05c;--warn:#e08a00;--bad:#d93636;--okbg:#e6f6ee;--badbg:#fdebeb;
  --hit:rgba(255,196,46,.24);--idle:#9aa4b2;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#121418;--panel:#1d2026;--ink:#f1f3f6;--muted:#9aa4b2;--line:#444b57;--soft:#262a32;
  --okbg:#14291f;--badbg:#321a1a;--hit:rgba(255,196,46,.16);--idle:#6c7684;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);line-height:1.5;
  font-family:"Hiragino Maru Gothic ProN","Hiragino Sans","Noto Sans JP",system-ui,sans-serif;
  padding-bottom:calc(112px + env(safe-area-inset-bottom,0px))}
.wrap{max-width:640px;margin:0 auto;padding:0 14px}
button,input{font:inherit;color:inherit}
button:focus-visible,input:focus-visible,a:focus-visible{outline:3px solid #2f7df6;outline-offset:2px}

/* ヘッダー */
header{padding:calc(14px + env(safe-area-inset-top,0px)) 0 10px}
.top{display:flex;align-items:center;justify-content:space-between;gap:8px}
h1{margin:0;font-size:22px;font-weight:800;letter-spacing:.02em}
.top a{font-size:13px;color:var(--muted)}
.pills{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
#p-data,#p-job[data-s=idle]{display:none}
.brand{display:flex;flex-direction:column;line-height:1.15}.brand small{font-size:12px;color:var(--muted);font-weight:600;letter-spacing:.04em}.brand small b{color:var(--ink)}
.lead{font-size:14.5px;line-height:1.7;margin:4px 0 12px}
.promise{display:grid;gap:8px;margin:0 0 14px}
.promise>div{display:flex;gap:10px;align-items:flex-start;padding:10px 12px;border:2px solid var(--line);border-radius:14px;background:var(--soft)}
.promise i{font-style:normal;font-size:20px;line-height:1.2}.promise p{margin:0}.promise b{display:block;font-size:14.5px}.promise span{display:block;font-size:12.5px;color:var(--muted);line-height:1.55}
.steps4{list-style:none;counter-reset:s;padding:0;margin:6px 0 14px;display:grid;gap:6px}
.steps4 li{counter-increment:s;display:flex;gap:10px;align-items:baseline;font-size:14px}
.steps4 li::before{content:counter(s);flex:none;width:24px;height:24px;border-radius:50%;background:var(--accent);color:#17181c;border:2px solid var(--line);font-weight:800;font-size:13px;display:inline-flex;align-items:center;justify-content:center}
.steps4 b{flex:none}.steps4 span{color:var(--muted);font-size:13px}
.notice small{display:block;margin-top:8px;font-size:12px;color:var(--muted);line-height:1.6}
.foot{margin:28px 0 8px;text-align:center;font-size:12.5px;color:var(--muted);line-height:1.9}.foot p{margin:0}
.safe{margin:8px 0 0;padding:8px 10px;border-radius:10px;background:var(--soft);font-size:13px}
.nowmsg{margin:4px 0 8px;font-size:15.5px;font-weight:700;line-height:1.6}
.nowmsg[data-s=error]{color:var(--bad)}.nowmsg[data-s=done]{color:var(--ok)}
.pbar{height:10px;border:2px solid var(--line);border-radius:999px;background:var(--soft);overflow:hidden;margin-bottom:12px}
.pbar i{display:block;height:100%;width:0;background:var(--accent);transition:width .6s ease}
.pbar i[data-s=done]{background:var(--ok)}.pbar i[data-s=error]{background:var(--bad)}
.logbox{margin-top:10px;font-size:13px}.logbox summary{cursor:pointer;color:var(--muted);font-weight:700}
.paysteps{margin:10px 0 4px;padding:10px 12px 10px 30px;border-radius:12px;background:var(--soft);font-size:13.5px;line-height:1.7}
.pill{display:inline-flex;align-items:center;gap:7px;padding:4px 11px 4px 9px;border:2px solid var(--line);
  border-radius:999px;background:var(--panel);font-size:13px;font-weight:700;white-space:nowrap}
.dot{width:10px;height:10px;border-radius:50%;background:var(--idle);flex:none}
.pill[data-s=ok] .dot{background:var(--ok)}
.pill[data-s=wait] .dot{background:var(--warn);animation:pulse 1.1s ease-in-out infinite}
.pill[data-s=bad] .dot{background:var(--bad)}
.pill[data-s=busy] .dot{background:var(--accent);animation:pulse 1.1s ease-in-out infinite}
@keyframes pulse{50%{opacity:.3}}
.hint{margin:8px 0 0;font-size:13px;color:var(--muted)}
.hint b{color:var(--ink)}

/* タブ */
.tabs{position:sticky;top:0;z-index:5;background:var(--bg);padding:6px 0 8px;
  padding-top:calc(6px + env(safe-area-inset-top,0px))}
.tablist{display:flex;gap:4px;padding:9px 6px 5px 2px;overflow-x:auto;scrollbar-width:none}
.tablist::-webkit-scrollbar{display:none}
.tabs.more .tablist{-webkit-mask-image:linear-gradient(to right,#000 90%,transparent);mask-image:linear-gradient(to right,#000 90%,transparent)}
.tab{position:relative;flex:1 0 auto;display:flex;align-items:center;justify-content:center;min-height:44px;padding:0 5px;
  border:2px solid var(--line);border-radius:12px;background:var(--panel);font-size:12.5px;font-weight:700;
  white-space:nowrap;cursor:pointer;box-shadow:0 3px 0 var(--line)}
.tab[aria-selected=true]{background:var(--accent);color:#17181c}
.tab:active{transform:translateY(2px);box-shadow:0 1px 0 var(--line)}
.badge{position:absolute;top:-9px;right:-5px;min-width:22px;padding:0 5px;border:2px solid var(--line);border-radius:999px;
  background:var(--soft);font-size:11px;line-height:17px;text-align:center;color:var(--muted)}
.badge.on{background:var(--ink);color:var(--bg)}
.tab .live{position:absolute;top:-6px;right:-3px;width:12px;height:12px;border-radius:50%;background:var(--accent);
  border:2px solid var(--line);display:none;animation:pulse 1.1s ease-in-out infinite}
.tab.running .live{display:block}

/* パネル */
.panel{background:var(--panel);border:2px solid var(--line);border-radius:16px;box-shadow:0 3px 0 var(--line);padding:14px;margin-top:10px}
.panel h2{margin:0;font-size:17px;font-weight:800}
.panel .sub{margin:2px 0 0;font-size:13px;color:var(--muted)}
[hidden]{display:none!important}

/* 入力 */
.field{display:block;margin-top:14px}
.field span{display:block;font-size:14px;font-weight:700;margin-bottom:5px}
input[type=text]{width:100%;font-size:18px;padding:12px 13px;border:2px solid var(--line);border-radius:12px;background:var(--soft)}
.mode{margin-top:14px;padding:10px 12px;border-radius:12px;background:var(--soft);font-size:14px}
.mode b{font-weight:800}
.err{margin-top:10px;color:var(--bad);font-size:14px;font-weight:700}
.err:empty{display:none}
.sum{display:grid;grid-template-columns:repeat(5,1fr);gap:5px;margin-top:12px}
.sum div{background:var(--soft);border-radius:12px;padding:8px 2px;text-align:center;font-size:11px;color:var(--muted);white-space:nowrap}
.sum b{display:block;font-size:14px;color:var(--ink);white-space:nowrap}

/* 項目リスト */
.tools{display:flex;gap:8px;margin-top:12px}
.btn{min-height:40px;padding:0 14px;border:2px solid var(--line);border-radius:10px;background:var(--panel);font-size:14px;font-weight:700;cursor:pointer;box-shadow:0 2px 0 var(--line)}
.btn:active{transform:translateY(1px);box-shadow:0 1px 0 var(--line)}
.btn:disabled{opacity:.45;cursor:default}
.count{margin-left:auto;align-self:center;font-size:14px;color:var(--muted);font-weight:700}
.list{margin-top:8px}
.opt{display:flex;align-items:center;gap:12px;min-height:50px;padding:6px 8px;border-top:1px solid var(--soft);border-radius:8px;cursor:pointer}
.opt input{width:22px;height:22px;flex:none;accent-color:var(--ink)}
.opt:has(input:checked){background:var(--hit);font-weight:700}

/* キャラ個別設定 */
.cat-tune-list{display:grid;gap:10px;margin-top:12px}
.cat-tune{padding:12px;border:1px solid var(--line);border-radius:14px;background:var(--soft)}
.cat-tune-head{display:flex;align-items:center;justify-content:space-between;gap:8px;font-weight:800;font-size:14px}
.cat-remove{min-height:32px;padding:0 9px;font-size:12px}
.cat-levels{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:8px;margin-top:10px}
.cat-setting{display:block;min-width:0;font-size:12px;font-weight:700}
.cat-setting span{display:block;margin-bottom:4px}
.cat-setting input,.cat-setting select{width:100%;min-width:0;min-height:40px;padding:7px;border:1px solid var(--line);border-radius:9px;background:var(--panel);color:var(--ink);font:inherit;font-size:14px}
.cat-talent{display:flex;align-items:center;gap:9px;margin-top:10px;font-size:13px;font-weight:700}
.cat-talent input{width:20px;height:20px;accent-color:var(--ink)}
@media(max-width:380px){.cat-levels{grid-template-columns:1fr 1fr}.cat-setting:last-child{grid-column:1/-1}}

/* キャラ解放 */
textarea.nums{width:100%;margin-top:12px;font:inherit;font-size:18px;line-height:1.5;padding:12px 13px;min-height:116px;resize:vertical;
  border:2px solid var(--line);border-radius:12px;background:var(--soft);color:var(--ink)}
.numinfo{margin-top:10px;padding:10px 12px;border-radius:12px;background:var(--soft);font-size:14px;word-break:break-all}
.numinfo.bad{background:var(--badbg);color:var(--bad);font-weight:700}
.numinfo b{font-weight:800}
.warnbox{margin-top:12px;border:2px solid var(--warn);border-radius:14px;padding:10px 12px;font-size:13px;line-height:1.6;background:var(--soft)}
.warnbox b{color:var(--warn)}

/* 実行モード・実行前チェック */
.sw{display:flex;align-items:flex-start;gap:12px;padding:12px 4px;border-top:1px solid var(--soft);cursor:pointer}
.sw:first-of-type{border-top:0}
.sw input{width:22px;height:22px;flex:none;margin-top:2px;accent-color:var(--ink)}
.sw b{display:block;font-size:15px}
.sw small{display:block;font-size:12px;color:var(--muted);line-height:1.45}
.checks{margin:10px 0 0;padding:0;list-style:none;font-size:14px}
.checks li{display:flex;gap:9px;padding:7px 0;border-top:1px solid var(--soft);line-height:1.45}
.checks li:first-child{border-top:0}
.checks i{flex:none;width:22px;height:22px;border-radius:50%;display:grid;place-items:center;font-style:normal;font-size:13px;font-weight:800;color:#fff;background:var(--ok)}
.checks li[data-lv=warn] i{background:var(--warn)}
.checks li[data-lv=bad] i{background:var(--bad)}
.checks li[data-lv=bad]{color:var(--bad);font-weight:700}

/* キャラ検索 */
input.search{width:100%;margin-top:12px;font-size:17px;padding:12px 13px;border:2px solid var(--line);border-radius:12px;background:var(--soft);color:inherit}
.nstat{margin-top:8px;font-size:12px;color:var(--muted);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.cres{margin-top:8px;max-height:340px;overflow:auto;border-radius:12px}
.cr{display:flex;align-items:center;gap:10px;width:100%;min-height:48px;padding:6px 10px;border:0;border-top:1px solid var(--soft);
  background:transparent;text-align:left;cursor:pointer;border-radius:8px}
.cr .no{flex:none;min-width:44px;padding:2px 6px;border-radius:8px;background:var(--soft);font-size:13px;font-weight:800;text-align:center;font-variant-numeric:tabular-nums}
.cr .nm{flex:1;min-width:0;font-size:15px;line-height:1.3}
.cr .nm small{display:block;font-size:11px;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cr .st{flex:none;font-size:12px;font-weight:800;color:var(--muted)}
.cr[aria-pressed=true]{background:var(--hit)}
.cr[aria-pressed=true] .st{color:var(--ok)}
.cmore{padding:8px 10px;font-size:12px;color:var(--muted)}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:10px}
.chip{display:inline-flex;align-items:center;gap:6px;min-height:34px;padding:0 6px 0 10px;border:2px solid var(--line);border-radius:999px;
  background:var(--panel);font-size:13px;font-weight:700;cursor:pointer}
.chip u{text-decoration:none;display:grid;place-items:center;width:20px;height:20px;border-radius:50%;background:var(--soft);font-size:12px}
.sec{margin:14px 0 0;font-size:13px;font-weight:800;color:var(--muted)}

/* 変更内容 */
.rep{margin-top:12px;border:2px solid var(--line);border-radius:14px;padding:10px 12px;background:var(--panel)}
.rep h3{margin:0 0 6px;font-size:15px;font-weight:800}
.rep .row{display:flex;justify-content:space-between;gap:10px;padding:6px 0;border-top:1px solid var(--soft);font-size:14px;font-variant-numeric:tabular-nums}
.rep .row:first-of-type{border-top:0}
.rep .row b{font-weight:700;text-align:right}
.rep .dry{margin:8px 0 0;font-size:13px;color:var(--warn);font-weight:700}

/* 数量指定 */
.qrow{display:flex;align-items:center;gap:8px;padding:8px;border-top:1px solid var(--soft);border-radius:8px}
.qtypes{margin-top:10px}.qtypes summary{cursor:pointer;padding:8px;font-weight:700}
.qrow.has{background:var(--hit)}
.qname{flex:1;min-width:0;font-size:15px;line-height:1.35}
.qname small{display:block;font-size:12px;color:var(--muted)}
.qname .dupnote{display:none;font-style:normal;font-size:11px;color:var(--warn);font-weight:700}
.qrow.dup .dupnote{display:block}
.qrow input.q{flex:none;width:118px;font-size:18px;padding:9px 10px;text-align:right;border:2px solid var(--line);
  border-radius:10px;background:var(--soft);font-variant-numeric:tabular-nums}
.qrow .qmax{flex:none;min-height:40px;padding:0 10px}
.qrow.over input.q{border-color:var(--bad);background:var(--badbg)}
.qrow.over .qname small{color:var(--bad);font-weight:700}
.qtcount{margin-left:6px;font-size:12px;color:var(--ok)}
input.q.over{border-color:var(--bad)!important;background:var(--badbg)!important}
.orbrow{flex-wrap:wrap}
.orbrow .qname{flex:1 1 100%}
.orbgrid{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:4px;flex:1;min-width:0}
.orbgrid label{display:block;text-align:center;font-size:11px;font-weight:800;color:var(--muted)}
.orbrow .orbgrid input{width:100%;min-width:0;font-size:15px;padding:7px 4px;text-align:right;border:2px solid var(--line);border-radius:8px;background:var(--soft);font-variant-numeric:tabular-nums}
.orbbulk{margin-top:8px;background:var(--soft)}
.orbrow.orbbulk .orbgrid input{background:var(--panel)}

/* 状況 */
.steps{display:grid;grid-template-columns:repeat(4,1fr);gap:6px;margin:14px 0 4px;padding:0;list-style:none}
.steps li{text-align:center;font-size:12px;color:var(--muted);position:relative}
.steps li::before{content:"";position:absolute;top:14px;left:-50%;width:100%;height:3px;background:var(--soft);z-index:0}
.steps li:first-child::before{display:none}
.steps li[data-s=done]::before,.steps li[data-s=active]::before,.steps li[data-s=bad]::before{background:var(--ink)}
.steps i{position:relative;z-index:1;display:grid;place-items:center;width:30px;height:30px;margin:0 auto 5px;border-radius:50%;
  border:2px solid var(--line);background:var(--soft);font-style:normal;font-weight:800;font-size:14px;color:var(--ink)}
.steps li[data-s=done] i{background:var(--ok);color:#fff;border-color:var(--ok)}
.steps li[data-s=active] i{background:var(--accent);color:#17181c;animation:pulse 1.1s ease-in-out infinite}
.steps li[data-s=bad] i{background:var(--bad);color:#fff;border-color:var(--bad)}
.steps li[data-s=active],.steps li[data-s=done]{color:var(--ink);font-weight:700}
.meta{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:12px}
.meta div{background:var(--soft);border-radius:12px;padding:8px 10px;font-size:12px;color:var(--muted)}
.meta b{display:block;font-size:16px;color:var(--ink);font-variant-numeric:tabular-nums}
#log{white-space:pre-wrap;word-break:break-word;background:var(--soft);border-radius:12px;padding:10px 12px;margin-top:12px;
  font-size:13px;line-height:1.6;min-height:3.2em;max-height:240px;overflow:auto}
#log:empty::before{content:"まだ実行していません。";color:var(--muted)}
.result{margin-top:12px;border:2px solid var(--ok);background:var(--okbg);border-radius:14px;padding:12px}
.result h3{margin:0 0 8px;font-size:15px;font-weight:800;color:var(--ok)}
.code{display:flex;align-items:center;gap:8px;margin-top:8px}
.code div{flex:1;min-width:0}
.code small{display:block;font-size:12px;color:var(--muted)}
.code strong{display:block;font-size:22px;letter-spacing:.04em;word-break:break-all;font-variant-numeric:tabular-nums}
.rvlock .code strong,.rvlock .hn b{filter:blur(7px);-webkit-user-select:none;user-select:none;pointer-events:none}
.rvhint{display:none;margin:8px 0 0;padding:8px 10px;border-radius:10px;background:var(--soft);font-size:13px}
.rvlock>.rvhint,.rvlock .rvhint{display:block}
#reviewPanel{z-index:40}
#reviewPanel .rvmsg p{margin:8px 0;line-height:1.7}
#reviewLink{display:block;text-align:center;padding:14px 16px;text-decoration:none;font-size:16px;margin:14px 0 6px}
.note{margin:10px 0 0;font-size:12px;color:var(--muted)}
.fail{margin-top:12px;border:2px solid var(--bad);background:var(--badbg);border-radius:14px;padding:12px;font-size:14px;font-weight:700}
.srv{margin-top:12px;font-size:13px;color:var(--muted)}
.srv .row{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-top:1px solid var(--soft)}
.srv .row b{color:var(--ink);font-weight:700;text-align:right}

/* 組み合わせ・保存した結果 */
.preset-save input{flex:1;min-width:0;font-size:16px;padding:9px 11px}
.presets{margin-top:8px}
.preset,.hrow{display:flex;align-items:center;gap:8px;padding:8px;border-top:1px solid var(--soft)}
.preset .pn,.hrow .hn{flex:1;min-width:0;font-size:14px;line-height:1.4;word-break:break-all}
.preset small,.hrow small{display:block;font-size:12px;color:var(--muted)}
.hrow .hn b{font-size:17px;letter-spacing:.03em;font-variant-numeric:tabular-nums}
.keepnote{margin:10px 0 0;font-size:13px;font-weight:700;color:var(--ok)}
.keepnote:empty{display:none}
.keepnote.warn{color:var(--warn)}

/* 下部バー */


.tab.locked{opacity:.4;cursor:not-allowed}
.maintbar{position:sticky;top:0;z-index:50;background:var(--accent);color:#17181c;border-bottom:2px solid #17181c;padding:8px 14px;font-size:13.5px;line-height:1.5;text-align:center}
.maintbar b{margin-right:8px}.maintbar a{color:#17181c;font-weight:800;margin-left:6px}
.maintwall{position:fixed;inset:0;z-index:60;background:var(--bg);display:flex;align-items:center;justify-content:center;padding:20px}
.maintwall[hidden],.maintbar[hidden]{display:none}
.maintwall .mbox{max-width:420px;text-align:center;background:var(--panel);color:var(--ink);border:2px solid var(--line);border-radius:16px;box-shadow:0 4px 0 var(--line);padding:22px 18px}
.maintwall .btn.pri{background:var(--accent);color:#17181c;border:2px solid var(--line);border-radius:12px;font-weight:800;box-shadow:0 3px 0 var(--line)}.maintwall p{line-height:1.7}
.updbanner{position:fixed;left:12px;right:12px;top:calc(10px + env(safe-area-inset-top,0px));z-index:40;max-width:616px;margin:0 auto;background:var(--accent);color:#17181c;border:2px solid #17181c;border-radius:14px;box-shadow:0 4px 0 #17181c;padding:10px 14px;font-size:13.5px;line-height:1.5}
.updbanner b{display:block;font-size:15px}
.updbanner[hidden]{display:none}
.locknote{border:2px solid var(--accent,#ffc42e);border-radius:10px;padding:10px 12px;margin:0 0 10px;font-size:14px;font-weight:700;line-height:1.5}
#doneCodes .code{display:flex;align-items:center;gap:8px;padding:8px 0;border-top:1px solid var(--line)}
#doneCodes .code div{flex:1;min-width:0}#doneCodes small{display:block;color:var(--muted);font-size:12px}
#doneCodes strong{font-size:22px;letter-spacing:.04em;word-break:break-all;user-select:all;-webkit-user-select:all}
#doneCodes h4{margin:10px 0 0;font-size:14px}
.ordno{font-size:13px;color:var(--muted);margin:0 0 6px;line-height:1.5}.ordno code{font-size:12px;word-break:break-all;color:var(--ink)}
.btn.mini{padding:2px 8px;font-size:12px}
.resume{border-color:var(--accent,#ffc42e)}.resume .rrow{display:flex;align-items:center;gap:8px;padding:8px 0;border-top:1px solid var(--line)}.resume .rrow span{flex:1;font-size:14px}
#claimNo{flex:1;min-width:0}
.modal{position:fixed;inset:0;z-index:20;background:rgba(0,0,0,.55);display:flex;align-items:flex-start;justify-content:center;overflow-y:auto;padding:max(16px,env(safe-area-inset-top,0px)) 14px 24px;-webkit-overflow-scrolling:touch}
.modal[hidden]{display:none}
.mbox{background:var(--panel);color:var(--ink);border:2px solid var(--line);border-radius:16px;box-shadow:0 4px 0 var(--line);width:100%;max-width:460px;margin:auto;padding:18px 16px}
.mbox h2{margin:0 0 8px;font-size:19px}.mbox h3{margin:16px 0 6px;font-size:15px}
.payamt{display:flex;align-items:baseline;gap:4px;justify-content:center;margin:6px 0}
.payamt b{font-size:40px;font-weight:800;letter-spacing:.02em}.payamt span{font-weight:700}
.paylist{margin:0 0 10px;padding-left:1.2em;font-size:14px;color:var(--muted);line-height:1.6}
.mbox .field{display:block;margin:10px 0}.mbox .field span{display:block;font-size:13px;color:var(--muted);margin-bottom:4px}
.mbox input:not([type=checkbox]){width:100%;box-sizing:border-box;font:inherit;font-size:16px;padding:12px;border:2px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}
.payerr{margin:8px 0;padding:10px 12px;border:2px solid var(--bad,#d93636);border-radius:10px;font-size:14px;line-height:1.5}
.payerr b{display:block;color:var(--bad,#d93636)}
.payerr[data-lv="wait"]{border-color:var(--line)}.payerr[data-lv="wait"] b{color:var(--ink)}
.mbtns{display:flex;gap:8px;margin-top:12px}.mbtns .btn{flex:1;padding:12px 8px;font-size:15px}
.mbtns .pri{background:var(--accent,#ffc42e);color:#17181c;font-weight:800;flex:2}
.notice{border:2px solid var(--accent,#ffc42e);border-radius:10px;padding:10px 12px;margin:0 0 12px;font-size:14px;line-height:1.6}.notice ul{margin:6px 0 0;padding-left:1.2em}
.howto{margin:0 0 8px;padding-left:1.3em;font-size:14px;line-height:1.7}
.mbox details{border-top:1px solid var(--line);padding:8px 0;font-size:14px}.mbox summary{cursor:pointer;font-weight:700}.mbox details p{margin:6px 0 0;color:var(--muted)}

/* ---------- 手順ナビ ---------- */
.stepper{display:grid;grid-template-columns:repeat(4,1fr);gap:0;padding:6px 0 2px;position:relative}
.step{all:unset;position:relative;display:flex;flex-direction:column;align-items:center;gap:4px;cursor:pointer;
  font-size:11.5px;font-weight:700;color:var(--muted);text-align:center;padding:2px 0;-webkit-tap-highlight-color:transparent}
.step::before{content:"";position:absolute;top:17px;left:-50%;width:100%;height:3px;background:var(--soft);z-index:0}
.step:first-child::before{display:none}
.step i{position:relative;z-index:1;display:grid;place-items:center;width:34px;height:34px;border-radius:50%;
  border:2px solid var(--line);background:var(--panel);color:var(--ink);font-style:normal;font-size:15px;font-weight:800}
.step[data-state=done]{color:var(--ink)}
.step[data-state=done] i{background:var(--ink);color:var(--bg)}
.step[data-state=done]::before,.step[data-state=current]::before{background:var(--ink)}
.step[data-state=current]{color:var(--ink)}
.step[data-state=current] i{background:var(--accent);color:#17181c;box-shadow:0 3px 0 var(--line);transform:translateY(-1px)}
.step[data-state=skip] i{border-style:dashed;color:var(--muted)}
.stepper .step[aria-selected]{background:none;box-shadow:none;border:0}
.stepper .step:active{transform:none;box-shadow:none}
.step:focus-visible i{outline:3px solid #2f7df6;outline-offset:2px}
.stepper .step.locked{opacity:1;cursor:not-allowed}
.stepper .step.locked i{background:var(--soft);color:var(--idle);border-color:var(--idle)}
.stepper .step.locked span{opacity:.55}
.step .live{top:-2px;right:calc(50% - 22px)}
.subnav{flex-wrap:wrap;overflow:visible;gap:8px 6px;padding:12px 2px 2px}
.subnav .tab{flex:1 1 28%;min-height:40px;font-size:13px}
.tabs.more .tablist.subnav{-webkit-mask-image:none;mask-image:none}

/* ---------- 何をしますか ---------- */
.modes{display:grid;gap:10px;margin-top:12px}
.modecard{all:unset;box-sizing:border-box;display:block;padding:14px 16px;border:2px solid var(--line);border-radius:14px;
  background:var(--panel);box-shadow:0 3px 0 var(--line);cursor:pointer;-webkit-tap-highlight-color:transparent;position:relative}
.modecard b{display:block;font-size:16px;padding-right:30px}
.modecard small{display:block;margin-top:3px;font-size:12.5px;color:var(--muted);line-height:1.5}
.modecard em{font-style:normal;font-weight:800;color:var(--ink)}
.modecard::after{content:"";position:absolute;top:15px;right:14px;width:20px;height:20px;border-radius:50%;border:2px solid var(--line);background:var(--panel)}
.modecard[aria-checked=true]{background:var(--hit);}
.modecard[aria-checked=true]::after{background:var(--ink);box-shadow:inset 0 0 0 4px var(--panel)}
.modecard:focus-visible{outline:3px solid #2f7df6;outline-offset:2px}
.modecard:active{transform:translateY(2px);box-shadow:0 1px 0 var(--line)}
.specialsw input{display:none}
.ptbox{margin:12px 0;padding:12px;border:2px solid var(--line);border-radius:14px;background:var(--soft)}
.ptbox>b{display:block;font-size:15px}.ptbox small{display:block;font-size:12.5px;color:var(--muted);margin-top:2px}
.ptrow{display:flex;align-items:center;gap:8px;margin-top:8px;flex-wrap:wrap}
.ptrow input{width:110px;font:inherit;font-size:16px;padding:9px 10px;border:2px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}
.ptrow select{font:inherit;font-size:16px;padding:9px 8px;border:2px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)}
label.opt.locked{opacity:.45}.evreq{display:block;font-size:11.5px;color:var(--muted)}
.lp{margin-left:8px;padding:0 7px;border-radius:999px;background:var(--soft);font-size:12px;font-style:normal;font-weight:700;color:var(--muted);white-space:nowrap}
.soon{margin-left:6px;padding:0 8px;border-radius:999px;background:var(--soft);font-size:12px;font-style:normal;color:var(--muted)}
.modecard:disabled{opacity:.55;cursor:not-allowed}
.couponbox{margin:12px 0;padding:12px;border:2px dashed var(--line);border-radius:14px;background:var(--soft)}
.couponbox strong{font-size:20px;letter-spacing:.06em}
details.more{margin-top:12px}
details.more > summary{cursor:pointer;list-style:none;display:flex;flex-direction:column;gap:1px}
details.more > summary::-webkit-details-marker{display:none}
details.more > summary b{font-size:16px}
details.more > summary b::after{content:"　＋";color:var(--muted);font-weight:700}
details.more[open] > summary b::after{content:"　−"}
details.more > summary small{font-size:12.5px;color:var(--muted)}
.panel > details.more{margin-top:10px;border-top:1px solid var(--soft);padding-top:8px}
.panel > details.more > summary{font-size:13px;color:var(--muted);font-weight:700}

/* ---------- 注文内容 ---------- */
#cart{margin-top:10px}
.cline{display:flex;align-items:flex-start;gap:10px;padding:11px 0;border-top:1px solid var(--soft)}
.cline:first-child{border-top:0}
.cline .cl{flex:1;min-width:0}
.cline .cl b{display:block;font-size:15px}
.cline .cl small{display:block;font-size:12.5px;color:var(--muted);line-height:1.5;word-break:break-all}
.cline .cp{flex:none;font-weight:800;font-size:15px;text-align:right;white-space:nowrap}
.cline .cp small{display:block;font-size:11.5px;color:var(--muted);font-weight:600}
.cline .edit{flex:none;align-self:center;min-height:32px;padding:0 10px;font-size:12px}
.ctotal{display:flex;align-items:baseline;justify-content:space-between;margin-top:6px;padding-top:12px;border-top:2px solid var(--line)}
.ctotal span{font-weight:800}
.ctotal b{font-size:30px;font-weight:800;letter-spacing:.01em}
.ctotal b small{font-size:15px;margin-left:2px}
.cnote{margin:8px 0 0;font-size:12.5px;color:var(--muted)}
.cnote.warn{color:var(--warn);font-weight:700}
.cempty{padding:18px 0;text-align:center;color:var(--muted);font-size:14px}

/* ---------- 下のバー ---------- */
.bar #back{flex:none;min-height:52px;padding:0 14px;font-size:15px}
.bar .info{min-width:0}
#p-data,#p-api{display:none!important}
.bar{position:fixed;left:0;right:0;bottom:0;z-index:6;background:var(--bg);border-top:2px solid var(--line);
  padding:10px 14px calc(10px + env(safe-area-inset-bottom,0px))}
.bar .in{max-width:640px;margin:0 auto;display:flex;align-items:center;gap:12px}
.bar .info{flex:1;min-width:0;font-size:13px;color:var(--muted);line-height:1.35}
.bar .info b{display:block;color:var(--ink);font-size:20px;font-weight:800;line-height:1.2}
.bar .info span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#go,#next{flex:none;min-width:130px;min-height:52px;padding:0 20px;border:2px solid var(--line);border-radius:14px;
  background:var(--accent);color:#17181c;font-size:18px;font-weight:800;cursor:pointer;box-shadow:0 4px 0 var(--line)}
#go:active,#next:active{transform:translateY(3px);box-shadow:0 1px 0 var(--line)}
#go:disabled,#next:disabled{background:var(--soft);color:var(--muted);cursor:default;box-shadow:0 2px 0 var(--line)}
.toast{position:fixed;left:50%;bottom:calc(100px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);z-index:30;
  background:var(--ink);color:var(--bg);padding:9px 16px;border-radius:999px;font-size:14px;font-weight:700;max-width:90vw}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body>

<div class="wrap"><header>
  <div class="top"><div class="brand"><h1>にゃんこ代行</h1><small>by <b>TN SHOP</b></small></div><button type="button" class="btn" id="helpBtn">使い方・よくある質問</button></div>
  <div class="pills" aria-live="polite">
    <span class="pill" id="p-conn" data-s="wait"><i class="dot"></i><span>接続を確認中</span></span>
    <span class="pill" id="p-data" data-s="idle"><i class="dot"></i><span>ゲームデータ</span></span>
    <span class="pill" id="p-job" data-s="idle"><i class="dot"></i><span>待機中</span></span>
    <span class="pill" id="p-api" data-s="bad" hidden><i class="dot"></i><span>bcsfe 要確認</span></span>
  </div>
</header></div>

<div class="tabs"><div class="wrap">
  <nav class="stepper" aria-label="手順">
    <button class="step tab" type="button" id="t-menu" data-tab="start" data-n="1"><i>1</i><span>メニュー</span></button>
    <button class="step tab" type="button" id="t-input" data-tab="input" data-n="2"><i>2</i><span>アカウント</span></button>
    <button class="step tab" type="button" id="t-confirm" data-tab="confirm" data-n="3"><i>3</i><span>確認・支払い</span></button>
    <button class="step tab" type="button" id="t-status" data-tab="status" data-n="4"><i>4</i><span>実行・受取</span><span class="live"></span></button>
  </nav>
  <div class="tablist subnav" role="tablist" aria-label="メニューの種類" id="subnav" hidden>
    {% for key, title, desc, lst in groups %}
    <button class="tab sub" type="button" role="tab" id="t-{{ key }}" aria-controls="v-{{ key }}" aria-selected="false" data-tab="{{ key }}">{{ title }}<span class="badge" id="cnt-{{ key }}">0</span></button>
    {% endfor %}
  </div>
</div></div>

<main class="wrap">

  <section class="view" id="v-start" role="tabpanel" aria-labelledby="t-menu">
    <div class="panel resume" id="resumePanel" hidden>
      <h2>未完了の注文があります</h2>
      <div id="resumeList"></div>
    </div>
    <div class="panel">
      <h2>何をしますか？</h2>
      <p class="sub">いずれか1つを選んでください。引継ぎコードは、このあとの「アカウント」で入力します。</p>
      <div class="modes" role="radiogroup" aria-label="内容">
        <button type="button" class="modecard" role="radio" data-mode="normal"><b>メニューから選ぶ</b><small>アイテム・キャラ・ステージなどを選んで付与・強化します。1項目 {{ prices.other }}円から</small></button>
        <button type="button" class="modecard" role="radio" data-mode="integrity"><b>バン保証</b><small>セーブを検査し、既知のエラーキャラなどを修復します。<em>{{ prices.ban_guarantee }}円</em>・引継ぎコード必須</small></button>
        {% if payments %}<button type="button" class="modecard" role="radio" data-mode="restart" {{ "" if restart.enabled else "disabled aria-disabled=true" }}><b>初期垢作成（リスタートパック）{% if not restart.enabled %}<em class="soon">準備中</em>{% endif %}</b><small>新しいアカウントを、公式リスタートパックと同じ中身で作ります。<em>{{ prices.restart }}円</em>〜・ほかのメニューも追加できます・引継ぎコード不要</small></button>{% endif %}
        <button type="button" class="modecard" role="radio" data-mode="clone"><b>アカウント複製</b><small>試験提供。元と複製の2アカウントのコードを出します。<em>{{ prices.clone }}円</em>・引継ぎコード必須</small></button>
      </div>
      {% if payments %}<input type="checkbox" id="restart" hidden {{ "" if restart.enabled else "disabled" }}>{% if restart.note %}<p class="sub" id="restartNote" hidden>{{ restart.note }}</p>{% endif %}{% endif %}
      <details class="more"><summary>くわしい説明</summary>
      <label class="sw specialsw"><input type="checkbox" id="integrity">
        <span><b>バン保証（セーブの検査・既知異常の自動修復）　{{ prices.ban_guarantee }}円</b><small>既存セーブを検査し、確認済みのエラーキャラと直接参照だけを修復します。修復不要なら再登録しません。修復が必要な場合は新しい引継ぎコードを発行します。既存セーブの引継ぎコードが必要です。</small></span></label>
      <label class="sw specialsw"><input type="checkbox" id="clone">
        <span><b>アカウント複製（試験提供）　{{ prices.clone }}円</b><small>入力セーブを別データとして読み込み、複製側に新しい問い合わせコードを発行して、元・複製それぞれの引継ぎコードを出します。公式の機能ではないため、成功・継続利用は保証されません。</small></span></label>
      </details>
    </div>
  </section>

  <section class="view" id="v-input" role="tabpanel" aria-labelledby="t-input" hidden>
    <div class="panel">
      <h2>アカウント（引継ぎ情報）</h2>
      <p class="sub">いま遊んでいるデータを使う場合は、ゲームで発行した<b>引継ぎコード</b>と<b>暗証番号</b>を入れてください。<b>新しいアカウントで始めるなら、両方空欄のまま</b>「次へ」でOKです。</p>
      <p class="safe">🔒 入力したコードは処理にだけ使い、完了後に削除します。</p>
      <label class="field"><span>引継ぎコード</span>
        <input type="text" id="transfer" placeholder="例：aBcD1234e" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false"></label>
      <label class="field"><span>暗証番号</span>
        <input type="text" id="confirm" placeholder="例：1234" autocomplete="off" inputmode="numeric"></label>
      <div class="mode" id="mode"></div>
      <div class="err" id="inputErr" role="alert"></div>
    </div>
  </section>

  {% for key, title, desc, lst in groups %}
  <section class="view" id="v-{{ key }}" role="tabpanel" aria-labelledby="t-{{ key }}" hidden>
    <div class="panel">
      <h2>{{ title }}</h2>
      <p class="sub">{{ desc }}</p>
      {% if key == 'subs' %}
      <label class="sw" id="oldDoneWrap" style="margin:10px 0" hidden><input type="checkbox" id="oldDone"><span><b>旧レジェンドの冠1はクリア済み</b><small>真レジェンドは、旧レジェンドの冠1まで終わっていないと遊べません。旧レジェンドの段階を選ばずに真レジェンドだけ頼むときは、ここにチェックしてください（引継ぎコードのデータのみ）。終わっていなかった場合は、こちらで旧レジェンドの冠1も一緒にクリアします。</small></span></label>
      {% endif %}
      {% if key == 'others' and payments %}
      <div class="ptbox"><b>プレイ時間を増やす</b><small>いまのプレイ時間に<b>足します</b>。{{ pt_min // 60 }}時間以上・10分単位で指定できます（10分ごとに {{ prices.playtime10 }}円）。空欄なら変更しません。</small>
        <div class="ptrow"><input id="ptH" type="text" inputmode="numeric" maxlength="6" placeholder="0" aria-label="時間"><span>時間</span>
          <select id="ptM" aria-label="分">{% for m in range(0, 60, 10) %}<option value="{{ m }}">{{ m }}</option>{% endfor %}</select><span>分</span></div>
        <div class="err" id="ptErr" role="alert"></div></div>
      {% endif %}
      {% if key == 'qty' %}
      <div class="tools">
        <button type="button" class="btn" id="qfill">すべて最大値</button>
        <button type="button" class="btn" id="qclear">クリア</button>
        <span class="count" id="n-qty">0 / {{ totals[key] }}</span>
      </div>
      <p class="note">「すべて最大値」は上の一覧（項目全体）だけに入ります。種類ごとの欄は、開いて個別に入力してください。種類ごとの指定は、同じ項目全体の指定より優先します。</p>
      <div class="list">
        {% for x in lst %}{% set mx = limits.get(x, 999) %}
        <div class="qrow" data-item="{{ x }}">
          <div class="qname">{{ x }}<small>上限 {{ "{:,}".format(mx) }}</small><em class="dupnote">MAX選択より優先</em></div>
          <input class="q top" type="text" inputmode="numeric" autocomplete="off" placeholder="未指定" data-item="{{ x }}" data-label="{{ x }}" data-max="{{ mx }}" aria-label="{{ x }}の数量">
          <button type="button" class="btn qmax" data-item="{{ x }}">最大</button>
        </div>
        {% endfor %}
        {% for base, names in type_names.items() %}{% set mx = limits.get(base, 999) %}
        <details class="qtypes">
          <summary>{{ 'マタタビ・獣石' if base == 'マタタビ' else base }}を種類ごとに指定（{{ names|length }}種）<span class="qtcount" data-base="{{ base }}"></span></summary>
          {% for name in names %}{% set k = base ~ '#' ~ loop.index %}
          <div class="qrow" data-item="{{ k }}">
            <div class="qname">{{ name }}<small>上限 {{ "{:,}".format(mx) }}</small></div>
            <input class="q" type="text" inputmode="numeric" autocomplete="off" placeholder="未指定" data-item="{{ k }}" data-label="{{ name }}" data-max="{{ mx }}" aria-label="{{ name }}の数量">
            <button type="button" class="btn qmax" data-item="{{ k }}">最大</button>
          </div>
          {% endfor %}
        </details>
        {% endfor %}
        {% set mx = limits.get('本能玉', 998) %}
        <details class="qtypes" id="orbs">
          <summary>本能玉を種類・等級ごとに指定（{{ orb_kinds|length }}種 × {{ orb_grades|length }}等級）<span class="qtcount" data-base="本能玉"></span></summary>
          <p class="note">上限は各 {{ "{:,}".format(mx) }} 個です。下の「まとめて入力」に数を入れて「全種類に入れる」を押すと、その等級の欄にまとめて入ります（空欄の等級は変えません）。</p>
          <div class="orbrow orbbulk">
            <div class="qname">まとめて入力</div>
            <div class="orbgrid">
              {% for g in orb_grades %}<label><span>{{ g }}</span><input class="ob" type="text" inputmode="numeric" autocomplete="off" data-grade="{{ loop.index0 }}" aria-label="全種類の{{ g }}等級"></label>{% endfor %}
            </div>
            <button type="button" class="btn" id="orbApply">全種類に入れる</button>
          </div>
          <input type="search" class="search" id="orbSearch" placeholder="効果や属性で絞り込み（例：赤、烈波）" autocomplete="off" aria-label="本能玉の絞り込み">
          {% for kind in orb_kinds %}{% set kind_index = loop.index0 %}
          <div class="qrow orbrow" data-kind="{{ kind }}">
            <div class="qname">{{ kind }}</div>
            <div class="orbgrid">
              {% for g in orb_grades %}{% set k = '本能玉#' ~ (kind_index * 5 + loop.index) %}
              <label><span>{{ g }}</span><input class="q" type="text" inputmode="numeric" autocomplete="off" placeholder="-" data-item="{{ k }}" data-label="{{ kind }} {{ g }}" data-max="{{ mx }}" aria-label="{{ kind }} {{ g }}の数量"></label>
              {% endfor %}
            </div>
            <button type="button" class="btn qrowmax">最大</button>
          </div>
          {% endfor %}
        </details>
      </div>
      {% elif key == 'chars' %}
      <input type="search" class="search" id="cs" placeholder="名前か番号で検索（例：ネコ、155）" autocomplete="off" autocorrect="off" spellcheck="false" aria-label="キャラ検索">
      <div class="nstat"><span id="nstat">キャラ名データを確認中…</span><button type="button" class="btn" id="cRetry" hidden>再取得</button><button type="button" class="btn" id="latestCats">最新キャラに対応</button></div>
      <div class="cres" id="cres" role="list"></div>
      <div class="tools"><button type="button" class="btn" id="cAddAll" hidden>表示中をすべて追加</button></div>
      <p class="sec">選んだキャラ・個別強化設定</p>
      <p class="note">各キャラごとに設定できます。空欄の項目は変更しません。レベルは「基本レベル＋プラス値」（例：60＋70）で指定します。個別設定カードは先頭60体まで表示します。</p>
      <div class="chips" id="chips"></div>
      <div class="cat-tune-list" id="catTuneList"></div>
      <p class="sec">番号で直接入力</p>
      <textarea class="nums" id="chars" rows="3" inputmode="text" autocomplete="off" autocorrect="off" spellcheck="false"
        placeholder="例：1, 5, 100-120" aria-label="キャラ番号"></textarea>
      <div class="numinfo" id="charInfo">番号を入力してください。</div>
      <div class="tools"><button type="button" class="btn" id="charClear">すべてクリア</button></div>
      <p class="note">カンマ・スペース・改行で区切ります。ハイフンで範囲指定できます（例：100-120）。<br>
        番号はDBの表記どおり（1始まり）で入力してください。すでに持っているキャラは変更しません。
        図鑑に無い番号と、エラーキャラになる番号は自動で除外します。</p>
      {% elif key == 'events' %}
      <div class="tools">
        <input type="search" id="evq" placeholder="マップ名で絞り込み（例：狂乱、降臨、絶）" autocomplete="off" aria-label="イベステを絞り込み" style="flex:1;min-width:0;font:inherit;font-size:16px;padding:9px 11px;border:2px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink)">
        <button type="button" class="btn" data-all="events" data-v="0">解除</button>
        <span class="count" id="n-events">0 / {{ totals[key] }}</span>
      </div>
      <label class="sw" style="margin:8px 0"><input type="checkbox" id="evPre"><span><b>前提のステージはクリア済み</b><small>大狂乱・絶など前提があるマップを、前提を選ばずに頼むときにチェック（引継ぎコードのデータのみ）。前提が終わっていなかった場合は、こちらで一緒にクリアします。</small></span></label>
      <div class="list" id="evlist">
        {% for m in lst %}<label class="opt" data-q="{{ m.name }} {{ m.cat }}"><input type="checkbox" name="events" value="{{ m.id }}"{% if m.reqs %} data-req="{{ m.reqs | join(',') }}"{% endif %}><span>{{ m.name }}{% if m.stars > 1 %}<em class="lp">冠{{ m.stars }}</em>{% endif %}{% if m.drops %}<em class="lp" title="クリアするとドロップキャラも獲得">キャラ付き</em>{% endif %}{% if m.reqs %}<small class="evreq">前提：{% if m.reqs | length > 3 %}狂乱9種すべて{% else %}{% for r in m.reqs %}{{ ev_names[r] }}{{ '・' if not loop.last }}{% endfor %}{% endif %}</small>{% endif %}</span></label>{% endfor %}
      </div>
      {% else %}
      <div class="tools">
        <button type="button" class="btn" data-all="{{ key }}" data-v="1">全選択</button>
        <button type="button" class="btn" data-all="{{ key }}" data-v="0">解除</button>
        <span class="count" id="n-{{ key }}">0 / {{ totals[key] }}</span>
      </div>
      <div class="list">
        {% for x in lst %}<label class="opt"><input type="checkbox" name="{{ key }}" value="{{ x }}"><span>{{ x }}{% if key == 'subs' and x in legend_levels %}<em class="lp">{{ prices.other * legend_levels[x] }}円</em>{% endif %}</span></label>{% endfor %}
      </div>
      {% endif %}
    </div>
  </section>
  {% endfor %}

  <section class="view" id="v-confirm" role="tabpanel" aria-labelledby="t-confirm" hidden>
    <div class="panel cartpanel">
      <h2>ご注文内容</h2>
      <p class="sub">内容と金額を確認して、下の「お支払いへ進む」を押してください。</p>
      <div id="cart"></div>
      {% if payments and not is_admin %}<label class="field"><span>クーポンコード（お持ちの方）</span>
        <input type="text" id="coupon" style="text-transform:uppercase" placeholder="例：NK-ABCD-EFGH" autocapitalize="characters" autocomplete="off" autocorrect="off" spellcheck="false" maxlength="20"></label>
      <p class="sub" style="margin-top:4px">{{ coupon_min }}円以上のご注文で10%引き（1円未満切り捨て）。無料クーポン（FR-で始まるコード）は金額に関係なく0円になります。1つのコードは1回だけ使えます。</p>{% endif %}
      <div class="err" id="orderErr" role="alert"></div>
    </div>
    <div class="panel">
      <h2>実行前チェック</h2>
      <ul class="checks" id="checks" aria-live="polite"></ul>
    </div>
    <details class="panel more">
      <summary><b>オプション</b><small>結果の保存</small></summary>
      <input type="checkbox" id="safe" hidden disabled><input type="checkbox" id="dry" hidden disabled>
      <label class="sw"><input type="checkbox" id="keepResults" checked>
        <span><b>発行したコードをこの端末に残す</b><small>完了時の引継ぎコードと暗証番号を、このブラウザに最大10件まで保存します。画面を閉じたり、サーバーの表示期限（30分）が過ぎたりしても「状況」タブで確認できます。共用の端末ではオフにしてください。</small></span></label>
    </details>
    <details class="panel more">
      <summary><b>よく使う組み合わせ</b><small>選んだ内容を保存・呼び出し</small></summary>
      <p class="sub">いま選んでいる項目・数量・キャラ設定・実行モードを名前を付けて保存し、あとで呼び出せます。引継ぎコードと暗証番号は保存しません。保存先はこの端末のブラウザです。</p>
      <div class="tools preset-save">
        <input type="text" id="presetName" maxlength="30" placeholder="名前（例：いつもの）" autocomplete="off" aria-label="組み合わせの名前">
        <button type="button" class="btn" id="presetSave">保存</button>
      </div>
      <div class="presets" id="presets"></div>
    </details>
    <div class="panel" hidden>
      <h2>選んだ項目</h2>
      <div class="sum">
        {% for key, title, desc, lst in groups %}
        <div>{{ 'キャラ' if key == 'chars' else title }}<b id="sel-{{ key }}">{{ '0体' if key == 'chars' else '0 / ' ~ totals[key] }}</b></div>
        {% endfor %}
      </div>
    </div>
  </section>

  <section class="view" id="v-status" role="tabpanel" aria-labelledby="t-status" hidden>
    <div class="locknote" id="lockNote" hidden>処理中です。終わるまで、この画面のまま（ほかのタブやページを開かずに）お待ちください。完了すると引継ぎコードが表示されます。</div>
    <div class="panel">
      <h2>実行の進み具合</h2>
      <p class="nowmsg" id="nowMsg">ご注文を受け付けると、ここに進み具合が表示されます。</p>
      <div class="pbar" aria-hidden="true"><i id="pbar"></i></div>
      <ol class="steps" id="steps">
        <li data-s="idle"><i>1</i>準備</li>
        <li data-s="idle"><i>2</i>取得</li>
        <li data-s="idle"><i>3</i>書き換え</li>
        <li data-s="idle"><i>4</i>発行</li>
      </ol>
      <div class="meta">
        <div hidden>実行枠<b id="job-slot">割り当て確認中</b></div>
        <div>経過時間<b id="elapsed">--:--</b></div>
        <div>最終更新<b id="updated">--:--:--</b></div>
      </div>
      <details class="logbox"><summary>くわしい記録</summary><div id="log" aria-live="polite"></div></details>
      <button class="btn" id="cancel-queue" type="button" hidden>待機を取り消す</button>
      <div class="warnbox" id="warn" hidden></div>
      <div class="rep" id="rep" hidden></div>
      <div class="fail" id="fail" hidden></div>
      <div class="result" id="res" hidden>
        <h3 id="res-title">完了しました</h3>
        <p class="rvhint">🔒 コードは、お知らせのリンクを開くと見られるようになります。<button class="btn" type="button" data-rvopen style="margin-left:6px;padding:4px 10px;font-size:12px">お知らせを開く</button></p>
        <div id="res-single">
          <div class="code"><div><small>引継ぎコード</small><strong id="r-transfer"></strong></div><button class="btn" type="button" data-copy="r-transfer">コピー</button></div>
          <div class="code"><div><small>暗証番号</small><strong id="r-confirm"></strong></div><button class="btn" type="button" data-copy="r-confirm">コピー</button></div>
        </div>
        <div id="res-clone" hidden>
          <h4>元アカウント</h4>
          <div class="code"><div><small>引継ぎコード</small><strong id="r-original-transfer"></strong></div><button class="btn" type="button" data-copy="r-original-transfer">コピー</button></div>
          <div class="code"><div><small>暗証番号</small><strong id="r-original-confirm"></strong></div><button class="btn" type="button" data-copy="r-original-confirm">コピー</button></div>
          <h4>複製アカウント</h4>
          <div class="code"><div><small>引継ぎコード</small><strong id="r-copy-transfer"></strong></div><button class="btn" type="button" data-copy="r-copy-transfer">コピー</button></div>
          <div class="code"><div><small>暗証番号</small><strong id="r-copy-confirm"></strong></div><button class="btn" type="button" data-copy="r-copy-confirm">コピー</button></div>
        </div>
        <p class="keepnote" id="keepNote"></p>
        <p class="note">サーバー側の表示は30分で消えます。引継ぎコードは今すぐコピーするか、スクリーンショットで控えてください。保存するファイルには引継ぎコードが含まれます。厳重に管理してください。</p>
        <button class="btn" id="download-result" type="button">結果を保存</button>
      </div>
    </div>

    <div class="panel" id="histPanel" hidden>
      <h2>この端末に残した結果</h2>
      <p class="sub">新しい順に最大10件です。ゲームで引継ぎを済ませたものは削除してください。</p>
      <div id="hist"></div>
      <div class="tools"><button type="button" class="btn" id="histClear">すべて削除</button></div>
    </div>

    <details class="panel more">
      <summary><b>サーバーの状態</b><small>くわしい情報（通常は見なくて大丈夫です）</small></summary>
      <div class="srv">
        <div class="row"><span>接続</span><b id="s-conn">確認中</b></div>
        <div class="row"><span>通常処理・待機</span><b id="s-queue">-</b></div>
        <div class="row"><span>実行枠の内訳</span><b id="s-slots">-</b></div>
        <div class="row"><span>ゲームデータ</span><b id="s-data">-</b></div>
        <div class="row"><span>キャラ名データ</span><b id="s-names">-</b></div>
        <div class="row"><span>要求ゲームデータ版</span><b id="s-gv">-</b></div>
        <div class="row"><span>図鑑データ版</span><b id="s-catalog-version">-</b></div>
        <div class="row"><span>最新キャラ更新</span><b id="s-catalog-update" role="status">未確認</b></div>
        <div class="row"><span>実行枠の準備</span><b id="s-warm-cache">-</b></div>
        <div class="row"><span>bcsfe</span><b id="s-bv">-</b></div>
        <div class="row"><span>互換チェック</span><b id="s-api">-</b></div>
        <div class="row"><span>稼働時間</span><b id="s-up">-</b></div>
        <div class="row"><span>最後に確認した時刻</span><b id="s-last">-</b></div>
        <div class="row"><span>バージョン</span><b>{{ version }}</b></div>
      </div>
      <div class="tools"><button type="button" class="btn" id="warm">ゲームデータを先に準備する</button></div>
      <p class="note">先に準備しておくと、実行ボタンを押したあとの待ち時間が短くなります。この画面を開いている間は、サーバーが休止しないよう定期的に接続を確認します。</p>
    </details>
  </section>

<footer class="foot">
  <p><b>にゃんこ代行</b> ・ © TN SHOP 作成</p>
  <p>お問い合わせ：<a href="https://x.com/paysms1212" target="_blank" rel="noopener">X（@paysms1212）</a></p>
  <p>ユーザーID <code>{{ uid }}</code>（お問い合わせのときにお伝えください）</p>
</footer>
</main>

<div class="bar"><div class="in">
  <button id="back" class="btn" type="button" hidden>戻る</button>
  <div class="info"><b id="total">選択 0件</b><span id="barmode">初期アカウントから作成</span></div>
  <button id="next" class="primary" type="button">次へ</button>
  <button id="go" class="primary" type="button" hidden>{% if payments %}お支払いへ進む{% else %}実行{% endif %}</button>
</div></div>

<div class="modal" id="payPanel" role="dialog" aria-modal="true" aria-labelledby="payTitle" hidden>
  <div class="mbox">
    <h2 id="payTitle">PayPayでお支払い</h2>
    <p class="ordno">受付番号 <code id="payOrderShort"></code> <button type="button" class="btn mini" id="payCopy">番号をコピー</button><code id="payOrderNo" hidden></code><br><small>画面を閉じても、コピーした番号で再開できます。お問い合わせのときもこの番号をお伝えください。</small></p>
    <div class="payamt"><b id="payAmount"></b><span>円</span></div>
    <ul class="paylist" id="payList"></ul>
    <div class="payerr" id="paySkip" data-lv="wait" hidden><b>実行されない項目</b><span id="paySkipList"></span><span>これらは料金に含まれません。</span></div>
    <ol class="paysteps" id="paySteps">
      <li>PayPay アプリの<b>「送る」</b>を開く</li>
      <li><b>送金リンク</b>を作る（金額は<b>上の金額ちょうど</b>・<b>パスコードなし</b>）</li>
      <li>できたリンクを<b>コピー</b>して、下に貼り付ける</li>
    </ol>
    <p class="sub" id="payInfo"></p>
    <label class="field" id="payLinkField"><span>送金リンクを貼り付け</span>
      <input id="payLink" type="url" inputmode="url" placeholder="https://pay.paypay.ne.jp/..." autocomplete="off" autocapitalize="off" spellcheck="false" maxlength="2048"></label>
    <div class="payerr" id="payErr" role="alert" hidden><b id="payErrTitle"></b><span id="payErrHint"></span></div>
    <div class="mbtns">
      <button class="btn" id="payCancel" type="button">やめる</button>
      <button class="btn pri" id="payGo" type="button">支払って実行する</button>
    </div>
  </div>
</div>

<div class="modal" id="helpPanel" role="dialog" aria-modal="true" aria-labelledby="helpTitle" hidden>
  <div class="mbox">
    <h2 id="helpTitle">にゃんこ代行へようこそ</h2>
    <p class="lead"><b>TN SHOP</b> のにゃんこ大戦争データ代行です。メニューを選んで PayPay で払うと、自動で処理して<b>数分で引継ぎコード</b>をお渡しします。</p>
    <div class="promise">
      <div><i>✅</i><p><b>入金を確認してから実行</b><span>お支払いが確認できるまで、データには一切さわりません。</span></p></div>
      <div><i>🔁</i><p><b>失敗しても再送金なし</b><span>うまくいかなかったときは、同じお支払いのまま無料でやり直せます。</span></p></div>
      <div><i>🔒</i><p><b>引継ぎコードは処理にだけ使用</b><span>入力したコードは処理のためだけに使い、完了後に削除します。</span></p></div>
      <div><i>💬</i><p><b>困ったらすぐ相談</b><span><a href="https://x.com/paysms1212" target="_blank" rel="noopener">X（@paysms1212）</a>のDMで受け付けています。</span></p></div>
    </div>
    <h3>かんたん4ステップ</h3>
    <ol class="steps4">
      <li><b>メニュー</b><span>欲しいものを選ぶ。下のバーに金額が出ます</span></li>
      <li><b>アカウント</b><span>引継ぎコードと暗証番号を入力（新しく作るなら空欄）</span></li>
      <li><b>確認・支払い</b><span>PayPay の送金リンクを貼り付け</span></li>
      <li><b>受け取り</b><span>引継ぎコードと暗証番号が表示されます</span></li>
    </ol>
    <div class="notice" id="helpNotice">
      <b>お支払いの前に、3つだけ</b>
      <ul>
        <li>送金リンクは<b>表示金額ちょうど・パスコードなし</b>で作ってください。</li>
        <li>PayPay で<b>「保留」</b>になっても<b>再送金は不要</b>です。アプリで保留を解除すると、自動で続きます。</li>
        <li>完了後の<b>引継ぎコードと暗証番号</b>は、スクショなどで必ず控えてください。</li>
      </ul>
      <small>猫缶・レアチケなど一部の項目は、ゲーム側に見つかるとBANされる可能性があります。気になる項目は選ばないでください。最終的な判断はご自身でお願いします。</small>
    </div>
    <h3>料金</h3>
    <ul class="paylist">
      <li>キャラ指定：1体ごとに、レベル・形態・本能を1項目 {{ prices.chara }}円（設定なしの解放のみは1体 {{ prices.chara }}円）</li>
      <li>クラッシュ解除（ガマトト・オトート砲台）：1件 {{ prices.crash }}円</li>
      <li>バン保証（サブ→特別メニュー）：{{ prices.ban_guarantee }}円</li>
      <li>アカウント複製（サブ→特別メニュー）：{{ prices.clone }}円</li>
      <li>それ以外の項目：1件 {{ prices.other }}円</li>
      <li>旧レジェンド・真レジェンド（サブ）：冠1クリア {{ prices.other }}円から、段階が上がるごとに +{{ prices.other }}円（冠4まで {{ prices.other * 4 }}円・イディ／ノーヴァまで {{ prices.other * 5 }}円）</li>
    </ul>
    <h3>よくある質問</h3>
    <details><summary>BANされませんか？</summary><p>アイテムの多くは問題が起きにくいものですが、絶対ではありません。猫缶・レアチケなどは特に注意が必要な項目なので、気になる方は選ばないでください。心配な場合は、検査と修復を行う「バン保証」もご利用ください。</p></details>
    <details><summary>失敗したらどうなりますか？</summary><p>お支払いはそのまま有効です。再送金なしで、もう一度実行できます。それでも解決しないときは、注文番号を添えて <a href="https://x.com/paysms1212" target="_blank" rel="noopener">X（@paysms1212）</a> にご連絡ください。</p></details>
    <details><summary>リンクがエラーになる</summary><p>金額が違う・パスコード付き・期限切れ・受け取り済みのリンクは使えません。金額を確認して、新しいリンクを作り直してください。</p></details>
    <details><summary>「保留」になった</summary><p>PayPay側の安全確認です。送金した方のPayPayアプリに届く確認（承認）の通知、または取引履歴のその送金から、保留を解除してください。解除されると、サーバーが自動で確認して実行します（画面を閉じていても大丈夫です）。あとで同じブラウザで開くと、結果の引継ぎコードが表示されます。同じリンクを送り直したり、再送金したりしないでください。</p></details>
    <details><summary>支払ったあとに「待機枠がいっぱい」と出た</summary><p>支払いは完了しています。もう一度ボタンを押せば、再送金なしで開始できます（注文は30分で期限切れ）。</p></details>
    <details><summary>支払い画面を閉じてしまった・別の端末で続けたい</summary><p>同じブラウザなら、「① メニュー」画面の上に「未完了の注文」が出るので「支払い画面を開く」を押してください。別の端末やブラウザでは、下の欄に注文番号を入れて再開できます。</p>
      <div class="tools"><input type="text" id="claimNo" placeholder="注文番号" autocomplete="off" autocapitalize="off" spellcheck="false"><button type="button" class="btn" id="claimGo">再開</button></div></details>
    <p class="ordno">お問い合わせ：<a href="https://x.com/paysms1212" target="_blank" rel="noopener">X（@paysms1212）</a>　あなたのユーザーID：<code>{{ uid }}</code></p>
    <div class="mbtns"><button class="btn pri" id="helpClose" type="button">はじめる</button></div>
  </div>
</div>
<div class="modal" id="donePanel" role="dialog" aria-modal="true" aria-labelledby="doneTitle" hidden>
  <div class="mbox">
    <h2 id="doneTitle">完了しました</h2>
    <p class="sub">下の<b>引継ぎコード</b>と<b>暗証番号</b>を、必ず今すぐ控えてください（スクリーンショットでもOK）。この表示は30分で消えます。</p>
    <div id="doneCodes"></div>
    <p class="rvhint">🔒 コードは、表示されたお知らせのリンクを開くと見られるようになります。<button class="btn" type="button" data-rvopen style="margin-left:6px;padding:4px 10px;font-size:12px">お知らせを開く</button></p>
    <div id="doneCoupon" class="couponbox" hidden></div>
    <h3>ゲームでの引き継ぎ手順</h3>
    <ol class="howto">
      <li>にゃんこ大戦争を開きます。いま遊んでいるデータがある端末では、先にそのデータが不要か確認してください（引き継ぐと<b>上書き</b>されます）。</li>
      <li>タイトル画面、または「設定（オプション）」から<b>「データ引き継ぎ」</b>を開きます。</li>
      <li><b>「引き継ぎを行う」</b>（データを受け取る側）を選びます。</li>
      <li>上の<b>引継ぎコード</b>と<b>暗証番号</b>を入力して決定します。</li>
      <li>データが読み込まれたら完了です。</li>
    </ol>
    <ul class="paylist">
      <li>コードは<b>1回だけ</b>使えます。入力を間違えないよう、コピーして貼り付けるのがおすすめです。</li>
      <li>コードには有効期限があります。<b>できるだけ早く</b>引き継いでください。</li>
      <li>元の引継ぎコード（入力したもの）は、もう使えません。</li>
      <li id="doneCloneNote" hidden>複製の場合は、元アカウント用と複製アカウント用のコードを、<b>別々の端末</b>で引き継いでください。</li>
    </ul>
    <label class="sw"><input type="checkbox" id="doneAck"><span><b>コードと暗証番号を控えました</b></span></label>
    <div class="mbtns"><button class="btn pri" id="doneClose" type="button" disabled>閉じる</button></div>
  </div>
</div>
<div class="modal" id="reviewPanel" role="dialog" aria-modal="true" aria-labelledby="reviewTitle" hidden>
  <div class="mbox">
    <h2 id="reviewTitle">ご利用ありがとうございました！✨</h2>
    <div class="rvmsg">
      <p>この度はにゃんこ代行をご利用いただき、ありがとうございました！</p>
      <p>代行内容に問題がなければ、よろしければ下のポストに、ご利用後の感想や評価をリプライしていただけると嬉しいです！🙇‍♂️<br>今後のサービス改善の参考にもなりますので、率直なご感想をお待ちしております。</p>
    </div>
    <a id="reviewLink" class="btn pri" href="https://x.com/paysms1212/status/2119246650996932810" target="_blank" rel="noopener">🔗 ポストを開いて、コードを表示する</a>
    <p class="sub" style="text-align:center">ポストを開くと、<b>引継ぎコードと暗証番号</b>が表示されます。<br>リプライは任意です。開いたらこの画面に戻ってきてください。</p>
    <p class="sub" style="text-align:center">またのご利用をお待ちしております！🐱✨</p>
  </div>
</div>
<div class="maintbar" id="maintBar" hidden><b>メンテナンス中</b><span id="maintMsg"></span> <a id="maintLink" href="#" hidden rel="noopener">新しいサイトを開く</a></div>
<div class="maintwall" id="maintWall" hidden><div class="mbox"><h2>メンテナンス中</h2><p id="maintWallMsg"></p><p><a id="maintWallLink" class="btn pri" href="#" hidden rel="noopener" style="display:inline-block;padding:12px 18px;text-decoration:none">新しいサイトを開く</a></p><p class="sub">すでに支払い済み・保留中の注文がある方は、同じブラウザでこのページを開き直すと続きを確認できます。</p></div></div>
<div class="updbanner" id="updBanner" role="status" hidden><b>アップデート中です</b><span>入力した内容はそのまま残ります。自動で再接続して続きを行うので、このままお待ちください。</span></div>
<div class="toast" id="toast" hidden></div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const MAINT={{ maint|tojson }};
const TOTALS={{ totals|tojson }};
const PRICES={{ prices|tojson }}, CRASH={{ crash_items|tojson }}, PAYMENTS={{ payments|tojson }};
const IS_ADMIN={{ is_admin|tojson }}, PT_MIN={{ pt_min|tojson }}, COUPON_MIN={{ coupon_min|tojson }};
function ptMin(){ const h=$('#ptH'), m=$('#ptM'); if(!h) return {min:0,err:''};
  const hv=String(h.value).replace(/[０-９]/g,c=>String.fromCharCode(c.charCodeAt(0)-0xFEE0)).trim();
  if(hv&&!/^\d+$/.test(hv)) return {min:0,err:'時間は半角の数字で入力してください。'};
  const min=(hv?parseInt(hv,10):0)*60+parseInt(m.value||'0',10);
  if(min>0&&min<PT_MIN) return {min:min,err:'プレイ時間は'+(PT_MIN/60)+'時間以上で指定してください。'};
  return {min:min,err:''}; }
function restartOn(){ const r=$('#restart'); return !!(r&&r.checked&&!r.disabled&&!$('#integrity').checked&&!$('#clone').checked); }
function couponCode(){ const c=$('#coupon'); return c?c.value.normalize('NFKC').toUpperCase().replace(/\s/g,''):''; }
const HIGH={{ high|tojson }};
const ERRNUMS={{ error_nums|tojson }};
const GROUPS=['events','subs','others'];
const TABS=['start','input','qty','events','chars','subs','others','confirm','status'];
const MENU=['qty','events','chars','subs','others'];
let curView='input', lastMenu='qty';
const STEP_TEXT=['準備','取得','書き換え','発行'];
let curJob=null, pollTimer=null, tickTimer=null, jobStart=0, jobRunning=false;
let lastOk=0, fails=0, state=null;
let CATS=[], CATSTATE='none', catsTries=0, catsAsked=false;
const NAMEOF=new Map();

/* ---------- タブ ---------- */
function special(){ return $('#integrity').checked||$('#clone').checked; }
function stepOf(v){ return (v==='start'||MENU.includes(v))?1:v==='input'?2:v==='confirm'?3:4; }
function show(t){
  if(jobRunning&&t!=='status'){ toast('処理が終わるまで、この画面のままお待ちください'); t='status'; }
  if(MENU.includes(t)&&special()){ t='start'; }
  TABS.forEach(k=>{
    const on=k===t;
    $('#v-'+k).hidden=!on;
    const tb=$('#t-'+(k==='start'?'menu':k)); if(tb) tb.setAttribute('aria-selected',on?'true':'false');
  });
  curView=t; if(MENU.includes(t)) lastMenu=t;
  if(typeof saveDraft==='function') saveDraft();
  const cur=stepOf(t);
  $$('.step').forEach(b=>{
    const n=+b.dataset.n;
    b.dataset.state=n===cur?'current':(n<cur?'done':'todo');
    b.setAttribute('aria-current',n===cur?'step':'false');
  });
  $('#subnav').hidden=!MENU.includes(t);
  if(MENU.includes(t)) $('#t-'+t).scrollIntoView({block:'nearest',inline:'center'});
  if(t==='chars'&&!catsAsked) loadCats();
  updateBar();
  window.scrollTo({top:0}); setTimeout(tabMore,350);
}
function updateBar(){
  const v=curView, st=stepOf(v);
  const back=$('#back'), next=$('#next'), go=$('#go');
  back.hidden=!((st===1&&v!=='start')||st===2||st===3)||jobRunning;
  go.hidden=!(st===3||jobRunning);
  next.hidden=st===3||jobRunning||(st===4);
  if(v==='start') next.textContent=special()?'次へ：アカウント':'次へ：メニューを選ぶ';
  else if(st===1) next.textContent='次へ：アカウント';
  else if(st===2) next.textContent='次へ：確認';
  if(st===4&&!jobRunning){ next.hidden=false; next.textContent='新しく注文する'; }
  if(st===4){
    $('#total').textContent=jobRunning?'実行中です':'実行・受け取り';
    $('#barmode').textContent=jobRunning?'終わるまで、この画面のままお待ちください':'コードを控えたら完了です';
  }
}
function goNext(){
  const st=stepOf(curView);
  if(st===4){ show('start'); return; }
  if(curView==='start'){ show(special()?'input':lastMenu); return; }
  if(st===1){
    if(parseNums($('#chars').value).err){ show('chars'); toast('キャラ番号を確認してください'); return; }
    if(refresh()===0){ toast('メニューを1つ以上選んでください'); return; }
    const bad=overInputs()[0];
    if(bad){ show('qty'); const d=bad.closest('details'); if(d) d.open=true; toast('「'+(bad.dataset.label||bad.dataset.item)+'」が上限を超えています'); bad.scrollIntoView({block:'center'}); return; }
    const ptv=ptMin(); if(ptv.err){ show('others'); toast(ptv.err); return; }
    show('input'); return;
  }
  if(st===2){
    const t=$('#transfer').value.trim(), c=$('#confirm').value.trim();
    if(!t!==!c){ $('#inputErr').textContent='引継ぎコードと暗証番号は「両方入力」か「両方空欄」にしてください。'; $('#transfer').focus(); return; }
    if(special()&&!(t&&c)){ $('#inputErr').textContent=($('#integrity').checked?'バン保証':'アカウント複製')+'には、引継ぎコードと暗証番号の両方が必要です。'; $('#transfer').focus(); return; }
    if(restartOn()&&(t||c)){ $('#inputErr').textContent='初期垢作成は新しいアカウントで行います。引継ぎコードと暗証番号を空欄にしてください。'; return; }
    show('confirm');
  }
}
function goBack(){ const st=stepOf(curView); if(st===3) show('input'); else if(st===2) show(special()?'start':lastMenu); else if(st===1&&curView!=='start') show('start'); }
$('#next').addEventListener('click',goNext);
$('#back').addEventListener('click',goBack);
$('#t-menu').addEventListener('click',e=>{ e.stopImmediatePropagation(); show('start'); },true);
/* 何をしますか */
function syncModes(){
  const m=$('#integrity').checked?'integrity':$('#clone').checked?'clone':restartOn()?'restart':'normal';
  $$('.modecard').forEach(b=>b.setAttribute('aria-checked',b.dataset.mode===m?'true':'false'));
  const rn=$('#restartNote'); if(rn) rn.hidden=m!=='restart';
}
$$('.modecard').forEach(b=>b.addEventListener('click',()=>{
  const m=b.dataset.mode; if(b.disabled) return;
  $('#integrity').checked=m==='integrity'; $('#clone').checked=m==='clone';
  const r=$('#restart'); if(r) r.checked=m==='restart'&&!r.disabled;
  syncModes(); refresh(); updateBar();
}));
function tabMore(){const t=$('.tablist'); $('.tabs').classList.toggle('more',t.scrollLeft+t.clientWidth<t.scrollWidth-6);}
$('.tablist').addEventListener('scroll',tabMore); window.addEventListener('resize',tabMore);
$$('.tab').forEach(b=>b.addEventListener('click',()=>show(b.dataset.tab)));
$$('.tab').forEach((b,i,all)=>b.addEventListener('keydown',e=>{
  const d=e.key==='ArrowRight'?1:e.key==='ArrowLeft'?-1:0; if(!d)return;
  const n=all[(i+d+all.length)%all.length]; n.focus(); show(n.dataset.tab);
}));

/* ---------- 選択数 ---------- */
function picked(n){return $$('input[name='+n+']:checked').map(e=>e.value)}
const MAXNUM=9999, MAXCNT=2000;
const catSettings = Object.create(null);
function catSettingsPayload(){
  const out={};
  const selected=parseNums($('#chars').value).nums;
  selected.forEach(n=>{
    const s=catSettings[n]||{};
    const clean={};
    ['base_level','plus_level','form'].forEach(k=>{if(s[k]!==''&&s[k]!=null)clean[k]=Number(s[k]);});
    if(s.talents_all)clean.talents_all=true;
    out[n]=clean; // 空設定も含めて、選択した全員を解放対象にする
  });
  return out;
}
function parseNums(text){ /* サーバーの parse_cat_numbers と同じ規則 */
  const t=(text||'').normalize('NFKC').replace(/[~〜～ー−―‐–—]/g,'-');
  const set=new Set();
  for(const k of t.split(/[\s,、，;；・]+/).filter(Boolean)){
    const m=/^(\d+)(?:-(\d+))?$/.exec(k);
    if(!m) return {nums:[],err:'「'+k+'」は読み取れません。'};
    let a=Number(m[1]), b=m[2]?Number(m[2]):a; if(a>b)[a,b]=[b,a];
    if(a<1||b>MAXNUM) return {nums:[],err:'番号は1〜'+MAXNUM+'の範囲で入力してください（「'+k+'」）。'};
    for(let i=a;i<=b;i++){ set.add(i); if(set.size>MAXCNT) return {nums:[],err:'一度に指定できるのは'+MAXCNT+'体までです。'}; }
  }
  return {nums:[...set].sort((x,y)=>x-y),err:null};
}
function compress(n){
  const out=[]; for(let i=0;i<n.length;){ let j=i; while(j+1<n.length&&n[j+1]===n[j]+1) j++; out.push(i===j?String(n[i]):n[i]+'-'+n[j]); i=j+1; }
  return out.join(', ');
}
function isOver(e){ return e.value!==''&&Number(e.value)>Number(e.dataset.max); }
function overInputs(){ return $$('input.q').filter(isOver); }
function qtyMap(){
  const m={}; $$('input.q').forEach(e=>{ if(e.value!=='') m[e.dataset.item]=e.value; }); return m;
}
/* 旧レジェンド・真レジェンドの段階は、それぞれ1つだけ（上の段階が下を含む） */
const LEG=/^(旧|真)レジェンド (?:冠([1-4])|イディ|ノーヴァ)/;
const legLv=k=>k[2]?+k[2]:5;
/* サブの件数（レジェンドは段階の数だけ数える＝段階ごとに +1件分） */
function legKinds(){ const k=new Set(); picked('subs').forEach(x=>{ const m=LEG.exec(x); if(m) k.add(m[1]); }); return k; }
function needOld(){ const k=legKinds(); return k.has('真')&&!k.has('旧'); }
function subUnits(list){ return (list||picked('subs')).reduce((n,x)=>{ const k=LEG.exec(x); return n+(k?legLv(k):1); },0); }
document.addEventListener('change',e=>{
  const t=e.target; if(!(t&&t.name==='subs'&&t.checked)) return; const m=LEG.exec(t.value); if(!m) return;
  document.querySelectorAll('input[name=subs]').forEach(x=>{ const k=LEG.exec(x.value); if(x!==t&&k&&k[1]===m[1]) x.checked=false; });
},true);
function normLegend(){
  const best={};
  document.querySelectorAll('input[name=subs]:checked').forEach(x=>{ const k=LEG.exec(x.value); if(k&&(!best[k[1]]||legLv(k)>best[k[1]])) best[k[1]]=legLv(k); });
  document.querySelectorAll('input[name=subs]:checked').forEach(x=>{ const k=LEG.exec(x.value); if(k&&legLv(k)!==best[k[1]]) x.checked=false; });
}
/* イベステ：前提（大狂乱←狂乱、絶・○○←○○）を満たさないものは押せない */
function evDeps(){
  const pre=$('#evPre')&&$('#evPre').checked;
  const on=new Set(picked('events'));
  $$('input[name=events][data-req]').forEach(x=>{
    const ok=pre||x.dataset.req.split(',').every(r=>on.has(r));
    if(!ok&&x.checked){ x.checked=false; on.delete(x.value); }
    x.disabled=!ok; x.closest('label').classList.toggle('locked',!ok);
  });
}
{ const q=$('#evq'); if(q) q.addEventListener('input',()=>{ const v=q.value.trim().toLowerCase();
    $$('#evlist label').forEach(l=>{ l.hidden=!!v&&!(l.dataset.q||'').toLowerCase().includes(v); }); }); }
function refresh(){
  normLegend(); evDeps();
  { const w=$('#oldDoneWrap'); if(w) w.hidden=!needOld(); }
  let total=0;
  const pc=parseNums($('#chars').value), cn=pc.err?0:pc.nums.length; total+=cn;
  $('#cnt-chars').textContent=cn; $('#cnt-chars').classList.toggle('on',cn>0);
  $('#sel-chars').textContent=cn+'体';
  const ci=$('#charInfo'); ci.classList.toggle('bad',!!pc.err);
  if(pc.err) ci.textContent=pc.err;
  else if(!cn) ci.textContent='番号を入力してください。';
  else{ ci.innerHTML=''; const b=document.createElement('b'); b.textContent=cn+'体'; ci.append(b,'を解放します：'+compress(pc.nums)); }
  const qm=qtyMap(), qn=Object.keys(qm).length; total+=qn;
  $('#cnt-qty').textContent=qn; $('#cnt-qty').classList.toggle('on',qn>0);
  $('#sel-qty').textContent=qn+'件'; $('#n-qty').textContent=qn+'件 指定中';
  const maxPicked=new Set(picked('items'));
  $$('input.q').forEach(e=>e.classList.toggle('over',isOver(e)));
  $$('.qrow').forEach(r=>{
    const ins=[...r.querySelectorAll('input.q')];
    r.classList.toggle('has',ins.some(e=>e.value!==''));
    r.classList.toggle('over',ins.some(isOver));
    r.classList.toggle('dup',ins.some(e=>e.value!=='')&&maxPicked.has(r.dataset.item));
  });
  $$('.qtcount').forEach(c=>{
    const n=Object.keys(qm).filter(k=>k.startsWith(c.dataset.base+'#')).length;
    c.textContent=n?'（'+n+'種 指定中）':'';
  });
  GROUPS.forEach(g=>{
    const n=picked(g).length; total+=n;
    const b=$('#cnt-'+g); b.textContent=n; b.classList.toggle('on',n>0);
    $('#sel-'+g).textContent=n+' / '+TOTALS[g];
    $('#n-'+g).textContent=n+' / '+TOTALS[g];
  });
  const ptv=ptMin(); if(ptv.min>0) total+=1; if(restartOn()) total+=1;
  if($('#ptErr')) $('#ptErr').textContent=ptv.err;
  const t=$('#transfer').value.trim(), c=$('#confirm').value.trim();
  let m, bm;
  if(!t&&!c){m=restartOn()?'<b>初期垢作成</b>：新しいアカウントにリスタートパックの内容を入れて作ります。':'<b>初期アカウント</b>から新しく作ります。'; bm=restartOn()?'初期垢作成（リスタートパック）':'初期アカウントから作成';}
  else if(t&&c){m=$('#integrity').checked?'<b>引継ぎコード</b>のセーブを検査・修復します（バン保証）。':$('#clone').checked?'<b>引継ぎコード</b>のデータから、元・複製の2アカウントを別々に作成します。':'<b>引継ぎコード</b>のデータを取得して書き換えます。'; bm=$('#integrity').checked?'既存セーブの検査（バン保証）':'引継ぎコードから取得';}
  else{m='<b>片方だけ入力されています。</b>両方入力するか、両方空欄にしてください。'; bm='入力を確認してください';}
  $('#mode').innerHTML=m; $('#barmode').textContent=bm;
  $('#total').textContent='選択 '+total+'件';
  if(!t===!c) $('#inputErr').textContent='';
  renderExtras(pc,qm,total,bm);
  return total;
}
document.addEventListener('change',refresh);
function normDigits(v){
  v=v.replace(/[０-９]/g,c=>String.fromCharCode(c.charCodeAt(0)-0xFEE0)).replace(/\D/g,'');
  return v.replace(/^0+(?=\d)/,'');
}
$$('input.q,input.ob').forEach(e=>e.addEventListener('input',()=>{const n=normDigits(e.value); if(n!==e.value)e.value=n; refresh();}));
$$('.qrowmax').forEach(b=>b.addEventListener('click',()=>{
  b.closest('.qrow').querySelectorAll('input.q').forEach(e=>e.value=e.dataset.max); refresh();
}));
$('#orbApply').addEventListener('click',()=>{
  let n=0;
  $$('.orbbulk input.ob').forEach(b=>{
    if(b.value==='') return;
    const g=Number(b.dataset.grade);
    $$('.orbrow[data-kind]').forEach(r=>{ const e=r.querySelectorAll('input.q')[g]; if(e){ e.value=b.value; n++; } });
  });
  toast(n?'本能玉 '+n+'個の欄に入れました':'まとめて入力の欄に数を入れてください'); refresh();
});
$('#orbSearch').addEventListener('input',()=>{
  const q=kana($('#orbSearch').value.trim());
  $$('.orbrow[data-kind]').forEach(r=>r.hidden=!!q&&!kana(r.dataset.kind).includes(q));
});
$('#orbSearch').addEventListener('keydown',e=>{ if(e.key==='Enter') e.preventDefault(); });
$$('.qmax').forEach(b=>b.addEventListener('click',()=>{
  const e=document.querySelector('input.q[data-item="'+b.dataset.item+'"]'); e.value=e.dataset.max; refresh();
}));
$('#chars').addEventListener('input',refresh);
$('#charClear').addEventListener('click',()=>{$('#chars').value=''; refresh();});
$('#qfill').addEventListener('click',()=>{$$('input.q.top').forEach(e=>e.value=e.dataset.max); refresh();});
$('#qclear').addEventListener('click',()=>{$$('input.q,input.ob').forEach(e=>e.value=''); refresh();});
$('#transfer').addEventListener('input',refresh);
$('#confirm').addEventListener('input',refresh);
$$('[data-all]').forEach(b=>b.addEventListener('click',()=>{
  $$('input[name='+b.dataset.all+']').forEach(e=>e.checked=b.dataset.v==='1'); refresh();
}));

/* ---------- キャラ名の検索 ---------- */
const kana=s=>(s||'').normalize('NFKC').toLowerCase().replace(/[ァ-ヶ]/g,c=>String.fromCharCode(c.charCodeAt(0)-0x60));
function el(tag,cls,text){const e=document.createElement(tag); if(cls)e.className=cls; if(text!=null)e.textContent=text; return e;}
function setNums(nums){ $('#chars').value=compress(nums); refresh(); }
function toggleNum(n){
  const pc=parseNums($('#chars').value);
  if(pc.err){ toast('番号欄の入力を先に直してください'); return; }
  const set=new Set(pc.nums); if(set.has(n)) set.delete(n); else set.add(n);
  setNums([...set].sort((a,b)=>a-b));
}
let shown=[], resSig=null;
function syncResults(){
  const sel=new Set(parseNums($('#chars').value).nums);
  $$('#cres .cr').forEach(b=>{
    const on=sel.has(Number(b.dataset.n));
    b.setAttribute('aria-pressed',on?'true':'false'); b.lastElementChild.textContent=on?'選択中 ✓':'追加';
  });
}
function renderResults(){
  const box=$('#cres'), q=kana($('#cs').value.trim());
  const sig=q+'|'+CATS.length;
  if(sig===resSig){ syncResults(); return; }   /* 検索語が同じなら作り直さない（タップ中に行が消えるのを防ぐ） */
  resSig=sig;
  box.textContent=''; shown=[]; $('#cAddAll').hidden=true;
  if(!CATS.length){ return; }
  if(!q){ box.append(el('div','cmore','名前か番号を入力すると、ここに候補が出ます。')); return; }
  const isNum=/^\d+$/.test(q);
  let hit=CATS.filter(c=>isNum?String(c.n).startsWith(q):c.key.includes(q));
  if(isNum) hit.sort((a,b)=>(String(a.n)===q?0:1)-(String(b.n)===q?0:1)||a.n-b.n);
  if(!hit.length){ box.append(el('div','cmore','見つかりませんでした。')); return; }
  const sel=new Set(parseNums($('#chars').value).nums);
  shown=hit.slice(0,40);
  shown.forEach(c=>{
    const b=el('button','cr'); b.type='button'; b.dataset.n=String(c.n); b.setAttribute('role','listitem'); b.setAttribute('aria-pressed',sel.has(c.n)?'true':'false');
    const nm=el('span','nm',c.name); if(c.alt) nm.append(el('small',null,c.alt));
    b.append(el('span','no',String(c.n)),nm,el('span','st',sel.has(c.n)?'選択中 ✓':'追加'));
    b.addEventListener('click',()=>toggleNum(c.n));
    box.append(b);
  });
  if(hit.length>shown.length) box.append(el('div','cmore','ほか '+(hit.length-shown.length)+' 件（絞り込んでください）'));
  $('#cAddAll').hidden=false; $('#cAddAll').textContent='表示中の'+shown.length+'体をすべて追加';
}
let chipSig=null;
function renderChips(nums){
  const sig=nums.join(',')+'|'+CATS.length;
  if(sig===chipSig) return;
  chipSig=sig;
  const box=$('#chips'), tune=$('#catTuneList'); box.textContent=''; tune.textContent='';
  if(!nums.length){ box.append(el('span','cmore','まだ選んでいません。')); return; }
  nums.slice(0,60).forEach(n=>{
    const b=el('button','chip'); b.type='button'; b.setAttribute('aria-label',n+'番を外す');
    b.append(el('span',null,NAMEOF.has(n)?n+' '+NAMEOF.get(n):String(n)),el('u',null,'×'));
    b.addEventListener('click',()=>toggleNum(n)); box.append(b);
    const cfg=catSettings[n]||(catSettings[n]={base_level:'',plus_level:'',form:'',talents_all:false});
    const card=el('div','cat-tune');
    const head=el('div','cat-tune-head',NAMEOF.has(n)?NAMEOF.get(n)+'（'+n+'番）':n+'番');
    const remove=el('button','btn cat-remove','選択解除'); remove.type='button'; remove.addEventListener('click',()=>toggleNum(n));
    head.append(remove); card.append(head);
    const levels=el('div','cat-levels');
    const base=el('label','cat-setting'); base.append(el('span',null,'基本 Lv'));
    const bi=el('input'); bi.type='number'; bi.min='1'; bi.max='100'; bi.inputMode='numeric'; bi.placeholder='例 60'; bi.value=cfg.base_level;
    bi.addEventListener('input',()=>{cfg.base_level=bi.value; refresh();}); base.append(bi);
    const plus=el('label','cat-setting'); plus.append(el('span',null,'プラス値'));
    const pi=el('input'); pi.type='number'; pi.min='0'; pi.max='999'; pi.inputMode='numeric'; pi.placeholder='例 70'; pi.value=cfg.plus_level;
    pi.addEventListener('input',()=>{cfg.plus_level=pi.value; refresh();}); plus.append(pi);
    const form=el('label','cat-setting'); form.append(el('span',null,'形態'));
    const fs=el('select'); [['','変更なし'],['1','第1形態'],['2','第2形態'],['3','第3形態'],['4','第4形態']].forEach(([v,t])=>{const o=el('option',null,t);o.value=v;fs.append(o);}); fs.value=cfg.form;
    fs.addEventListener('change',()=>{cfg.form=fs.value; refresh();}); form.append(fs);
    levels.append(base,plus,form); card.append(levels);
    const talent=el('label','cat-talent'); const ti=el('input'); ti.type='checkbox'; ti.checked=!!cfg.talents_all;
    ti.addEventListener('change',()=>{cfg.talents_all=ti.checked; refresh();});
    talent.append(ti,el('span',null,'本能・超本能を全解放（対応キャラのみ）')); card.append(talent);
    tune.append(card);
  });
  if(nums.length>60) box.append(el('span','cmore','ほか '+(nums.length-60)+'体'));
}
function renderNstat(){
  const s=$('#nstat'), r=$('#cRetry'); r.hidden=true;
  if(CATSTATE==='ready'){ s.textContent='キャラ名データ '+CATS.length+'体'; $('#s-names').textContent=CATS.length+'体'; }
  else if(CATSTATE==='loading'||CATSTATE==='none'){ s.textContent='キャラ名データを準備中…（初回は1分ほどかかります。番号の直接入力は今すぐ使えます）'; $('#s-names').textContent='準備中'; }
  else{ s.textContent=CATSERR||'キャラ名を取得できませんでした。番号で入力してください。'; r.hidden=false; $('#s-names').textContent='未取得'; }
}
let CATSERR='', catalogSignature=null;
async function loadCats(){
  catsAsked=true;
  try{
    const r=await fetch('/api/cats',{cache:'no-store'});
    const j=await r.json(); CATSTATE=j.state; CATSERR=j.error||'';
    if(j.state==='ready'&&catalogSignature!==JSON.stringify([j.version,j.updated,j.items])){
      catalogSignature=JSON.stringify([j.version,j.updated,j.items]);
      CATS=j.items.map(c=>({n:c.n,name:c.name,alt:c.alt,key:kana(c.name+' '+c.alt)}));
      NAMEOF.clear(); CATS.forEach(c=>NAMEOF.set(c.n,c.name));
      chipSig=null;
    }
  }catch(e){ CATSTATE='error'; CATSERR='サーバーに接続できませんでした。'; }
  renderNstat(); renderResults(); refresh();
  if((CATSTATE==='loading'||CATSTATE==='none')&&++catsTries<45) setTimeout(loadCats,4000);
}
$('#cs').addEventListener('input',renderResults);
$('#cs').addEventListener('keydown',e=>{ if(e.key==='Enter') e.preventDefault(); });
$('#cAddAll').addEventListener('click',()=>{
  const pc=parseNums($('#chars').value); if(pc.err){ toast('番号欄の入力を先に直してください'); return; }
  const set=new Set(pc.nums); shown.forEach(c=>set.add(c.n)); setNums([...set].sort((a,b)=>a-b));
});
$('#cRetry').addEventListener('click',async()=>{
  catsTries=0; CATSTATE='loading'; renderNstat();
  try{ await fetch('/api/cats/refresh',{method:'POST'}); }catch(e){}
  setTimeout(loadCats,800);
});

/* ---------- 実行前チェック・モード ---------- */
function goLabel(){ if(PAYMENTS&&IS_ADMIN&&!jobRunning) return '無料で実行（管理者）'; if(PAYMENTS&&freeCoupon()&&!jobRunning) return '無料クーポンで実行'; if(PAYMENTS&&!jobRunning) return 'お支払いへ進む'; return $('#integrity').checked?($('#dry').checked?'バン保証（試運転）':'バン保証'):$('#dry').checked?'試運転':$('#clone').checked?'複製':'実行'; }
function renderChecks(pc,qm,total){
  const L=[]; const add=(lv,t)=>L.push([lv,t]);
  if($('#clone').checked){
    const t0=$('#transfer').value.trim(), c0=$('#confirm').value.trim();
    if(!t0||!c0) add('bad','複製には、引継ぎコードと暗証番号の両方が必要です。');
    add('ok','複製モード：元セーブを別インスタンスに読み込み、複製側に新しい問い合わせコードを割り当てます。');
    if($('#dry').checked) add('ok','試運転：取得できるかだけ確認します。コードは発行しません。');
    const ul0=$('#checks'); ul0.textContent='';
    L.forEach(([lv,tx])=>{ const li=document.createElement('li'); li.dataset.lv=lv;
      li.append(el('i',null,lv==='ok'?'✓':'✕'),el('span',null,tx)); ul0.append(li); });
    return;
  }
  if($('#integrity').checked){
    const t0=$('#transfer').value.trim(), c0=$('#confirm').value.trim();
    if(!t0||!c0) add('bad','バン保証には、既存セーブの引継ぎコードと暗証番号が必要です。');
    add('warn','選択した編集項目は実行せず、セーブ構造検査と既知のエラーキャラ・参照の確認だけを行います。');
    add('warn','修復対象が見つかった場合だけ保存データを修復・再検査して再登録し、新しい引継ぎコードを発行します。');
    if($('#dry').checked) add('ok','試運転：修復と再検査まで行いますが、アップロード・コード発行はしません。');
    const ul0=$('#checks'); ul0.textContent='';
    L.forEach(([lv,tx])=>{ const li=document.createElement('li'); li.dataset.lv=lv;
      li.append(el('i',null,lv==='ok'?'✓':lv==='warn'?'!':'✕'),el('span',null,tx)); ul0.append(li); });
    return;
  }
  const safe=$('#safe').checked;
  const maxed=picked('items'), qkeys=Object.keys(qm);
  const risky=[...new Set([...maxed,...qkeys])].filter(x=>HIGH.includes(x));
  if(risky.length){
    if(safe) add('warn','安全モードのため、'+risky.join('・')+' は今回スキップします。');
    else add('warn',risky.join('・')+' は、ゲーム側に見つかるとBANされる可能性がある項目です。気になる場合は外してください。');
  }
  const over=overInputs().map(e=>e.dataset.label||e.dataset.item);
  if(over.length) add('bad','数量が上限を超えています：'+over.join('・'));
  if(pc.err) add('bad','キャラ番号：'+pc.err);
  else if(pc.nums.length){
    const custom=Object.values(catSettingsPayload());
    const configured=custom.filter(x=>Object.keys(x).length);
    const levels=custom.filter(x=>x.base_level!=null||x.plus_level!=null).length;
    const forms=custom.filter(x=>x.form!=null).length;
    const talents=custom.filter(x=>x.talents_all).length;
    add('ok','キャラ管理：'+pc.nums.length+'体を対象（未設定のキャラは解放のみ）');
    if(configured.length) add('ok','個別強化：レベル '+levels+'体・形態 '+forms+'体・本能 '+talents+'体');
    const ex=pc.nums.filter(n=>ERRNUMS.includes(n));
    if(ex.length) add('warn','エラーキャラになる番号は自動で除外します：'+compress(ex));
  }
  const t=$('#transfer').value.trim(), c=$('#confirm').value.trim();
  if(!t!==!c) add('bad','引継ぎコードと暗証番号が片方だけです。');
  const ptc=ptMin();
  if(ptc.err) add('bad',ptc.err);
  if(ptc.min>0&&picked('others').includes('プレイ時間カンスト')) add('bad','「プレイ時間カンスト」と「プレイ時間を増やす」は同時に選べません。');
  if(restartOn()&&(t||c)) add('bad','初期垢作成は新しいアカウントで行います。引継ぎコードと暗証番号を空欄にしてください。');
  if(needOld()&&!special()){
    if(!t) add('bad','真レジェンドは、旧レジェンドの冠1まで終わっていないと遊べません。新しいアカウントでは、旧レジェンドの段階も一緒に選んでください。');
    else if(!$('#oldDone').checked) add('bad','真レジェンドだけを頼むときは、「サブ」で「旧レジェンドの冠1はクリア済み」にチェックするか、旧レジェンドの段階も選んでください。');
  }
  if(total===0&&!pc.err) add('warn','まだ項目が選ばれていません。');
  if($('#dry').checked) add('ok','試運転：コードは発行せず、変更内容だけ確認します。');
  if(!L.some(x=>x[0]!=='ok')) add('ok','問題は見つかりませんでした。');
  const ul=$('#checks'); ul.textContent='';
  L.forEach(([lv,tx])=>{ const li=document.createElement('li'); li.dataset.lv=lv;
    li.append(el('i',null,lv==='ok'?'✓':lv==='warn'?'!':'✕'),el('span',null,tx)); ul.append(li); });
}
/* 安全モードは廃止：すべての項目を料金に含める（サーバーと同じ数え方） */
function chargeable(name){ return true; }
function estimate(pc,qm){
  if($('#integrity').checked) return PRICES.ban_guarantee;
  if($('#clone').checked) return PRICES.clone;
  const oth=picked('others');
  const crash=oth.filter(x=>CRASH.includes(x)).length;
  const other=new Set([...picked('items'),...Object.keys(qm).map(k=>k.split('#')[0])].filter(chargeable)).size+subUnits()+oth.length-crash+picked('events').length;
  let units=0;
  if(!pc.err){ const cs=catSettingsPayload(); pc.nums.forEach(n=>{ const c=cs[n]||{}; const k=((c.base_level!=null||c.plus_level!=null)?1:0)+(c.form!=null?1:0)+(c.talents_all?1:0); units+=Math.max(1,k); }); }
  return units*PRICES.chara+crash*PRICES.crash+other*PRICES.other+(restartOn()?PRICES.restart:0)+Math.floor(ptMin().min/10)*PRICES.playtime10;
}
function freeCoupon(){ return !IS_ADMIN&&/^FR-/.test(couponCode()); }
function payable(base){ if(IS_ADMIN||freeCoupon()) return 0; return (couponCode()&&base>=COUPON_MIN)?Math.floor(base*9/10):base; }
function renderCart(pc,qm){
  const box=$('#cart'); if(!box) return; box.textContent='';
  const t=$('#transfer').value.trim();
  const line=(title,detail,price,view)=>{
    const r=el('div','cline'), l=el('div','cl'); l.append(el('b',null,title)); if(detail) l.append(el('small',null,detail));
    r.append(l);
    if(price!=null){ const p=el('div','cp'); p.append(price); r.append(p); }
    if(view){ const e=el('button','btn edit','変更'); e.type='button'; e.addEventListener('click',()=>show(view)); r.append(e); }
    box.append(r);
  };
  const yen=(n,unit,cnt)=>{ const f=document.createDocumentFragment(); f.append((n).toLocaleString('ja-JP')+'円'); if(cnt>1||unit){ f.append(el('small',null,cnt+'件 × '+unit+'円')); } return f; };
  line(t?'引継ぎコードのデータ':'新しいアカウント', t?('引継ぎコード '+t.slice(0,3)+'••••'):'初期状態のアカウントから作ります', null, 'input');
  let total=0;
  if($('#integrity').checked){ total=PRICES.ban_guarantee; line('バン保証','セーブを検査し、既知の異常を修復します',yen(total,0,1),'input'); }
  else if($('#clone').checked){ total=PRICES.clone; line('アカウント複製','元と複製の2アカウントのコードを出します',yen(total,0,1),'input'); }
  else{
    if(restartOn()){ total+=PRICES.restart; line('初期垢作成（リスタートパック）','新しいアカウントにリスタートパックの内容を入れます',yen(PRICES.restart,0,1),'input'); }
    const ptc=ptMin(); if(ptc.min>0&&!ptc.err){ const u=Math.floor(ptc.min/10); total+=u*PRICES.playtime10; line('プレイ時間 +'+Math.floor(ptc.min/60)+'時間'+(ptc.min%60?(ptc.min%60)+'分':''),'いまのプレイ時間に足します（上限を超える分は上限まで）',yen(u*PRICES.playtime10,PRICES.playtime10,u),'others'); }
    if(!pc.err&&pc.nums.length){
      const cs=catSettingsPayload(); let units=0;
      pc.nums.forEach(n=>{ const c=cs[n]||{}; units+=Math.max(1,((c.base_level!=null||c.plus_level!=null)?1:0)+(c.form!=null?1:0)+(c.talents_all?1:0)); });
      total+=units*PRICES.chara;
      line('キャラ '+pc.nums.length+'体','番号 '+compress(pc.nums)+'（レベル・形態・本能は1項目ずつ）',yen(units*PRICES.chara,PRICES.chara,units),'chars');
    }
    const items=picked('items').filter(chargeable), qkeys=Object.keys(qm).filter(chargeable);
    const qbase=[...new Set(qkeys.map(k=>k.split('#')[0]))].filter(x=>!items.includes(x));
    if(items.length) { total+=items.length*PRICES.other; line('アイテム（最大まで）',items.join('・'),yen(items.length*PRICES.other,PRICES.other,items.length),'items'); }
    if(qbase.length){ total+=qbase.length*PRICES.other; line('アイテム（数量指定）',qkeys.map(k=>{ const e=document.querySelector('input.q[data-item="'+CSS.escape(k)+'"]'); return (e&&e.dataset.label||k)+' '+Number(qm[k]).toLocaleString('ja-JP'); }).join('・'),yen(qbase.length*PRICES.other,PRICES.other,qbase.length),'qty'); }
    const evs=picked('events'); if(evs.length){ total+=evs.length*PRICES.other; const nm=evs.map(v=>{ const e=document.querySelector('input[name=events][value="'+v+'"]'); return e?e.closest('label').querySelector('span').firstChild.textContent:v; }); line('イベントステージ',nm.slice(0,8).join('・')+(nm.length>8?' ほか'+(nm.length-8)+'件':''),yen(evs.length*PRICES.other,PRICES.other,evs.length),'events'); }
    const subs=picked('subs'); if(subs.length){ const su=subUnits(subs); total+=su*PRICES.other; line('サブ',subs.join('・'),yen(su*PRICES.other,PRICES.other,su),'subs'); }
    const oth=picked('others'), crash=oth.filter(x=>CRASH.includes(x)), rest=oth.filter(x=>!CRASH.includes(x));
    if(crash.length){ total+=crash.length*PRICES.crash; line('クラッシュ修正',crash.join('・'),yen(crash.length*PRICES.crash,PRICES.crash,crash.length),'others'); }
    if(rest.length){ total+=rest.length*PRICES.other; line('その他',rest.join('・'),yen(rest.length*PRICES.other,PRICES.other,rest.length),'others'); }
    if(total===0){ box.append(el('div','cempty','まだメニューが選ばれていません。「戻る」から選んでください。')); }
  }
  if(PAYMENTS){
    const cc=couponCode(), pay=payable(total);
    if(freeCoupon()&&total>0) line('無料クーポン',cc+'（使えるかは次へ進むときに確認します）',el('span',null,'-'+total.toLocaleString('ja-JP')+'円'),null);
    else if(!IS_ADMIN&&cc&&!freeCoupon()){
      if(total>=COUPON_MIN) line('クーポン（10%引き）',cc+'（使えるかは次の画面へ進むときに確認します）',el('span',null,'-'+(total-pay).toLocaleString('ja-JP')+'円'),null);
      else box.append(el('p','cnote warn','クーポンは '+COUPON_MIN+'円以上のご注文で使えます（いまは '+total.toLocaleString('ja-JP')+'円）。'));
    }
    const tot=el('div','ctotal'); const b=el('b'); b.append(pay.toLocaleString('ja-JP')); b.append(el('small',null,'円'));
    tot.append(el('span',null,IS_ADMIN?'お支払い金額（管理者は無料）':'お支払い金額'),b); box.append(tot);
  }
  if($('#dry').checked) box.append(el('p','cnote warn','試運転がオンです：コードは発行されません（料金は同じです）。'));
  if($('#safe').checked){ const sk=[...new Set([...picked('items'),...Object.keys(qm)])].filter(x=>HIGH.includes(x)); if(sk.length) box.append(el('p','cnote warn','安全モードのため実行されません（料金に含まれません）：'+sk.join('・'))); }
}
function renderExtras(pc,qm,total,bm){
  renderChecks(pc,qm,total);
  try{ renderCart(pc,qm); }catch(e){}
  saveDraft();
  syncModes(); updateBar();
  if(!pc.err) renderChips(pc.nums);
  renderResults();
  const what=$('#integrity').checked?'バン保証':$('#clone').checked?'アカウント複製':('選択 '+total+'件');
  const flags=($('#dry').checked?'・試運転':'')+($('#safe').checked&&!special()?'・安全モード':'');
  if(PAYMENTS){ $('#total').textContent=payable(estimate(pc,qm)).toLocaleString('ja-JP')+'円'; $('#barmode').textContent=what+'・'+bm+flags; }
  else{ $('#total').textContent=what; $('#barmode').textContent=bm+flags; }
  if(stepOf(curView)===4) updateBar();
  if(!jobRunning) $('#go').textContent=goLabel();
}
try{ localStorage.removeItem('nyanko-safe2'); }catch(e){}   /* 安全モードは廃止 */
$('#safe').addEventListener('change',()=>{ try{localStorage.setItem('nyanko-safe2',$('#safe').checked?'1':'0')}catch(e){} refresh(); });
$('#dry').addEventListener('change',refresh);
['#ptH','#coupon','#oldDone','#evPre'].forEach(q=>{ const e=$(q); if(e) e.addEventListener('input',refresh); });
$('#clone').addEventListener('change',()=>{ if($('#clone').checked){ $('#integrity').checked=false; if($('#restart')) $('#restart').checked=false; } refresh(); });
$('#integrity').addEventListener('change',()=>{ if($('#integrity').checked){ $('#clone').checked=false; if($('#restart')) $('#restart').checked=false; } refresh(); });

/* ---------- 小物 ---------- */
let toastTimer=null;
function toast(t){const e=$('#toast'); e.textContent=t; e.hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>e.hidden=true,2200);}
function pad(n){return String(n).padStart(2,'0')}
function mmss(s){s=Math.max(0,Math.floor(s)); return pad(Math.floor(s/60))+':'+pad(s%60)}
function clock(d){return pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds())}
function uptime(s){const h=Math.floor(s/3600), m=Math.floor(s%3600/60); return h>0?h+'時間'+m+'分':m+'分'+pad(s%60)+'秒'}
function pill(id,s,text){const e=$(id); e.dataset.s=s; e.lastElementChild.textContent=text;}
/* ---------- コードを見る前のお知らせ（感想ポストのリンクを開くと、コードが見られるようになる） ---------- */
const RVKEY='nyanko-review-ok';
function rvHash(t){ let h=5381; t=String(t||''); for(let i=0;i<t.length;i++) h=((h*33)^t.charCodeAt(i))>>>0; return h.toString(36); }   /* コードそのものは保存しない */
function rvLoad(){ try{ return JSON.parse(localStorage.getItem(RVKEY)||'[]'); }catch(e){ return []; } }
let rvOk=new Set(rvLoad()), rvWait=[];
function rvKeys(result){ return resultPairs(result).map(p=>rvHash(p[1].transfer)); }
function needReview(result){ return !!result&&rvKeys(result).some(k=>!rvOk.has(k)); }
function rvOpen(result,then){ if(result) rvWait.push(result); if(then) rvThen.push(then); $('#reviewPanel').hidden=false; document.body.style.overflow='hidden'; }
let rvThen=[];
function rvApply(){
  [['#donePanel',window.__doneResult],['#res',window.__resResult]].forEach(([q,r])=>{ const e=$(q); if(e) e.classList.toggle('rvlock',needReview(r)); });
  try{ renderHist(); }catch(e){}
}
$('#reviewLink').addEventListener('click',()=>{
  /* リンクを開いた時点で、待っていた結果を見られるようにする（X のアプリに移っても、戻ってきたら表示されている） */
  rvWait.forEach(r=>rvKeys(r).forEach(k=>rvOk.add(k)));
  [window.__doneResult,window.__resResult].forEach(r=>{ if(r) rvKeys(r).forEach(k=>rvOk.add(k)); });
  rvWait=[];
  try{ localStorage.setItem(RVKEY,JSON.stringify([...rvOk].slice(-200))); }catch(e){}
  $('#reviewPanel').hidden=true; if($('#donePanel').hidden&&$('#payPanel').hidden&&$('#helpPanel').hidden) document.body.style.overflow='';
  rvApply();
  const t=rvThen; rvThen=[]; setTimeout(()=>t.forEach(f=>{ try{ f(); }catch(e){} }),0);
});
document.addEventListener('click',e=>{ const b=e.target.closest&&e.target.closest('[data-rvopen]'); if(b) rvOpen(); });
/* コピーの前に確認：まだリンクを開いていなければ、お知らせを出す */
function rvGuard(result,fn){ if(needReview(result)) rvOpen(result); else fn(); }
async function copyText(text){
  try{await navigator.clipboard.writeText(text); return true}catch(e){
    const a=document.createElement('textarea'); a.value=text; a.style.position='fixed'; a.style.opacity='0';
    document.body.appendChild(a); a.select(); let ok=false; try{ok=document.execCommand('copy')}catch(_){}
    a.remove(); return ok;
  }
}
$$('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{
  if(/^r-/.test(b.dataset.copy)&&needReview(window.__resResult)){ rvOpen(window.__resResult); return; }
  const ok=await copyText($('#'+b.dataset.copy).textContent);
  toast(ok?'コピーしました':'コピーできませんでした。長押しで選択してください');
}));

/* ---------- 接続状況 ---------- */
function renderState(){
  const age=Date.now()-lastOk;
  if(!lastOk||fails>=2){
    pill('#p-conn',fails>=2?'bad':'wait',fails>=2?'ただいまつながりません':'つながりを確認中');
    $('#s-conn').textContent=fails>=2?'切断（休止中なら1分ほどで復帰します）':'確認中';
  }else{
    pill('#p-conn','ok','営業中'); $('#s-conn').textContent='接続OK';
  }
  if(lastOk) $('#s-last').textContent=clock(new Date(lastOk));
  if(!state) return;
  const d=state.bcsfe;
  const dm={idle:['idle','データ未準備','未準備'],loading:['wait','データ準備中','準備中'+(d.elapsed!=null?'（'+d.elapsed+'秒）':'')],
            ready:['ok','データ準備OK','準備OK'+(d.took?'（'+d.took+'秒）':'')],error:['bad','データ準備失敗','失敗：'+(d.error||'不明')]}[d.status];
  pill('#p-data',dm[0],dm[1]); $('#s-data').textContent=dm[2];
  const wc=state.warm_cache||{};
  $('#warm').disabled=wc.status==='loading';
  $('#s-warm-cache').textContent=wc.status==='loading'?'準備中':wc.status==='error'?'準備失敗':wc.status==='ready'?wc.prepared+' 枠（'+wc.version+'）':'未準備';
  $('#s-catalog-version').textContent=state.catalog_version||'未確認';
  const cu=state.catalog_update||{};
  $('#latestCats').disabled=cu.status==='loading';
  $('#latestCats').textContent=cu.status==='loading'?'最新データを確認中…':'最新キャラに対応';
  $('#s-catalog-update').textContent=cu.status==='loading'?'確認・取得中':cu.status==='error'?(cu.error||'更新失敗'):cu.status==='ready'?cu.version+' / '+cu.count+'体':'未確認';
  if(cu.status==='ready'&&(catalogSignature===null||state.catalog_version!==lastCatalogVersion)){
    catsTries=0; loadCats();
  }
  lastCatalogVersion=state.catalog_version;
  $('#s-up').textContent=uptime(state.uptime);
  $('#s-queue').textContent=state.running_jobs+'/'+state.max_concurrent_jobs+' 件実行中・'+state.queued_jobs+' 件待機中'+(state.warmup_slots?'・準備 '+state.warmup_slots+' 枠':'');
  const slotLabels={free:'空き',running:'実行中',warmup:'準備中'};
  $('#s-slots').textContent=(state.slot_states||[]).map(x=>'枠'+x.slot+' '+(slotLabels[x.state]||'状態不明')).join(' / ')||'未確認';
  if(state.game_data_version) $('#s-gv').textContent=state.game_data_version+(state.learned_version?'（自動更新）':'');
  $('#s-bv').textContent=state.bcsfe_version||'-';
  const am=state.api_missing;
  $('#s-api').textContent=am==null?'未確認（初回の準備後）':am.length?'要確認：'+am.slice(0,3).join(', ')+(am.length>3?' ほか'+(am.length-3)+'件':''):'OK';
  const pa=$('#p-api'); pa.hidden=!(am&&am.length);
  if(jobRunning) pill('#p-job','busy','自分の処理を実行中');
  else if(state.running_jobs>0) pill('#p-job','busy','他の処理 '+state.running_jobs+' 件が実行中');
  else if(state.warmup_slots>0) pill('#p-job','wait','準備中');
  else pill('#p-job','idle','待機中');
}
let lastCatalogVersion=null;
async function ping(){
  if(document.hidden) return;
  try{
    const r=await fetch('/api/state',{cache:'no-store'});
    state=await r.json(); lastOk=Date.now(); fails=0;
  }catch(e){fails++;}
  renderState();
}
setInterval(ping,8000);
document.addEventListener('visibilitychange',()=>{if(!document.hidden)ping()});
$('#latestCats').addEventListener('click',async()=>{
  $('#latestCats').disabled=true;
  try{
    const r=await fetch('/api/cats/latest',{method:'POST'});
    const j=await r.json();
    toast(r.ok?'最新の日本版データを確認しています。状況タブで確認できます。':j.error||'更新を開始できませんでした');
    if(r.ok){ catalogSignature=null; catsTries=0; }
  }catch(e){toast('接続できませんでした');}
  await ping();
});
$('#warm').addEventListener('click',async()=>{
  $('#warm').disabled=true;
  try{await fetch('/api/warmup',{method:'POST'}); toast('準備を始めました');}catch(e){toast('接続できませんでした')}
  ping();
});

/* ---------- 実行 ---------- */
function setRunning(v){
  jobRunning=v;
  $('#go').disabled=v; $('#go').textContent=v?($('#dry').checked?'試運転中…':$('#clone').checked?'複製中…':'実行中…'):goLabel();
  $('#t-status').classList.toggle('running',v);
  $$('.tab').forEach(b=>{ if(b.dataset.tab!=='status'){ b.disabled=v; b.classList.toggle('locked',v); } });
  $('#lockNote').hidden=!v; updateBar();
  if(v&&$('#v-status').hidden) show('status');
  document.title=v?'実行中… | にゃんこ代行':'にゃんこ代行';
  renderState();
}
function renderSteps(j){
  $$('#steps li').forEach((li,i)=>{
    const n=i+1; let s='idle', mark=String(n);
    if(j){
      if(j.status==='done'){s='done'; mark='✓';}
      else if(n<j.step){s='done'; mark='✓';}
      else if(n===j.step){s=j.status==='error'?'bad':'active'; mark=j.status==='error'?'!':String(n);}
    }
    li.dataset.s=s; li.firstElementChild.textContent=mark;
  });
}
function resetNow(){ const m=$('#nowMsg'), b=$('#pbar'); if(m){ m.textContent='開始しています…'; m.dataset.s=''; } if(b){ b.style.width='3%'; b.dataset.s=''; } }
const NOW_MSG=['準備しています…','アカウントのデータを受け取っています…','ご注文の内容を反映しています…','新しい引継ぎコードを発行しています…'];
function renderNow(j){
  const m=$('#nowMsg'), bar=$('#pbar'); if(!m) return;
  let t, p;
  if(j.status==='queued'){ t='順番待ちです（あと '+j.queue_position+' 件）。このままお待ちください。'; p=4; }
  else if(j.status==='done'){ t=j.dry?'試運転が終わりました（コードは発行していません）。':'完了しました！下の引継ぎコードと暗証番号を控えてください。'; p=100; }
  else if(j.status==='error'){ t='うまくいきませんでした。'+(PAYMENTS?'お支払いはそのまま有効です（再送金は不要）。内容を直して、もう一度実行できます。':''); p=100; }
  else { const k=Math.max(1,Math.min(4,j.step||1)); t=NOW_MSG[k-1]+' 画面はこのままでお待ちください。'; p=Math.round((k-0.5)/4*100); }
  m.textContent=t; m.dataset.s=j.status; bar.style.width=p+'%'; bar.dataset.s=j.status;
}
function renderJob(j){
  renderSteps(j); renderNow(j);
  $('#log').textContent=j.status==='queued'?'待機中（順番: '+j.queue_position+'）':j.log.join('\n');
  $('#job-slot').textContent=j.status==='running'&&j.slot?'実行枠 '+j.slot+'/'+j.max_concurrent_jobs+' を使用中':j.status==='queued'?'空き枠待ち（順番: '+j.queue_position+'）':'実行枠は使用していません';
  $('#cancel-queue').hidden=j.status!=='queued';
  $('#log').scrollTop=$('#log').scrollHeight;
  $('#updated').textContent=clock(new Date());
  jobStart=Date.now()-j.elapsed*1000;
  $('#elapsed').textContent=mmss(j.elapsed);
  const w=$('#warn'); w.hidden=!(j.warn&&j.warn.length);
  if(!w.hidden){ w.textContent=''; const b=document.createElement('b'); b.textContent='注意'; w.append(b); j.warn.forEach(x=>{w.append(document.createElement('br'),x)}); }
  $('#fail').hidden=j.status!=='error';
  if(j.status==='error') $('#fail').textContent=j.error||'エラーが発生しました';
  $('#res-title').textContent=j.integrity_only?'バン保証の結果':j.clone?(j.status==='error'?'複製処理の途中結果':'複製処理の結果'):'完了しました';
  const showRes=!!j.result&&!j.dry;
  $('#res').hidden=!showRes;
  window.__resResult=showRes?j.result:null;
  if(showRes){ $('#res').classList.toggle('rvlock',needReview(j.result)); if(needReview(j.result)) rvOpen(j.result); }
  if(showRes&&j.clone){
    $('#res-single').hidden=true; $('#res-clone').hidden=false;
    const o=j.result.original||{}, cp=j.result.copy||{};
    $('#r-original-transfer').textContent=o.transfer||'未発行';
    $('#r-original-confirm').textContent=o.confirm||'未発行';
    $('#r-copy-transfer').textContent=cp.transfer||'未発行';
    $('#r-copy-confirm').textContent=cp.confirm||'未発行';
  }else if(showRes){
    $('#res-single').hidden=false; $('#res-clone').hidden=true;
    $('#r-transfer').textContent=j.result.transfer||'';
    $('#r-confirm').textContent=j.result.confirm||'';
  }
  const rp=$('#rep'); rp.textContent='';
  if(j.status==='done'){
    rp.hidden=false;
    rp.append(el('h3',null,j.clone?(j.dry?'試運転の結果':'複製の結果'):j.dry?'試運転の結果（変更内容）':'変更内容'));
    if(j.integrity_only){
      const repaired=j.integrity&&Number.isInteger(j.integrity.repaired_count)?j.integrity.repaired_count:0;
      rp.append(el('div','row',repaired===0?'既知の修復対象は見つかりませんでした。セーブの再登録はしていません。':'既知のエラーキャラ '+repaired+' 件を修復し、再読み込み・再監査を実施しました。'));
      if(j.dry&&repaired>0) rp.append(el('p','dry','試運転のため、修復内容はサーバーへアップロードしていません。'));
    }
    else if(j.clone) rp.append(el('div','row',j.dry?'元データを別インスタンスとして復元しました。試運転のためサーバー登録はしていません。':'元データを別インスタンスとして復元し、元・コピーのアカウント登録を試みました。'));
    else if(!j.report||!j.report.length) rp.append(el('div','row','表示できる変更はありませんでした。'));
    else j.report.forEach(r=>{ const d=el('div','row'); d.append(el('span',null,r.label),el('b',null,r.before.toLocaleString('ja-JP')+' → '+r.after.toLocaleString('ja-JP'))); rp.append(d); });
    if(!j.clone&&!j.integrity_only) rp.append(el('p','note','主な項目と表示状態の比較です。すべての変更は表示されません。'));
    if(j.dry) rp.append(el('p','dry','コードは発行していません。この内容でよければ、試運転をオフにしてもう一度実行してください。'));
  } else rp.hidden=true;
}
function stopJob(){
  clearInterval(pollTimer); clearInterval(tickTimer); pollTimer=tickTimer=null;
  setRunning(false);
}
async function poll(){
  if(!curJob) return;
  try{
    const r=await fetch('/status/'+curJob,{cache:'no-store'});
    const j=await r.json();
    if(!r.ok){
      sessionStorage.removeItem('job'); curJob=null; stopJob();
      $('#fail').hidden=false; $('#fail').textContent=j.error||'状況を取得できませんでした';
      return;
    }
    lastOk=Date.now(); fails=0;
    renderJob(j);
    if(j.status!=='running'&&j.status!=='queued'){
      stopJob(); setTimeout(loadResume,1500);
      document.title=(j.status==='done'?(j.dry?'試運転完了 | ':j.clone?'複製完了 | ':'完了 | '):'失敗 | ')+'にゃんこ代行';
      if(j.status==='done'&&j.result&&!j.dry){ keepResult(curJob,j); openDone(j); if(PAYMENTS) fetchCoupon(curJob); $('#transfer').value=''; $('#confirm').value=''; refresh(); }
      if(j.status==='error'&&PAYMENTS&&!j.outcome_unknown){ $('#fail').textContent=(j.error||'エラーが発生しました')+'　※お支払いは有効です。「① メニュー」画面の上の「未完了の注文」から、再送金なしで再実行できます。'; }
      if(j.status==='done'){toast(j.integrity_only?(j.integrity&&j.integrity.upload_required?'保全修復・再登録が完了しました':'バン保証の検査が完了しました'):j.dry?'試運転が完了しました':j.clone?'複製が完了しました':'完了しました'); if(navigator.vibrate) navigator.vibrate(120);}
    }
  }catch(e){fails++; renderState();} /* 一時的な通信エラーは次回に再試行 */
}
$('#cancel-queue').addEventListener('click',async()=>{
  if(!curJob) return;
  try{ await fetch('/cancel/'+curJob,{method:'POST'}); await poll(); }catch(e){ toast('接続できませんでした'); }
});
$('#download-result').addEventListener('click',async()=>{
  if(!curJob) return;
  try{
    const response=await fetch('/status/'+curJob,{cache:'no-store'});
    if(!response.ok) throw Error('expired');
    const job=await response.json();
    if(!job.result) throw Error('no result');
    const url=URL.createObjectURL(new Blob([JSON.stringify(job.result,null,2)],{type:'application/json'}));
    const link=document.createElement('a'); link.href=url; link.download='nyanko-result.json'; link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(e){toast('結果を保存できませんでした');}
});
/* ---------- 発行したコードを端末に残す ---------- */
const HKEY='nyanko-results';
function loadHist(){ try{ const v=JSON.parse(localStorage.getItem(HKEY)||'[]'); return Array.isArray(v)?v:[]; }catch(e){ return []; } }
function saveHist(list){ try{ localStorage.setItem(HKEY,JSON.stringify(list.slice(0,10))); return true; }catch(e){ return false; } }
function resultPairs(r){
  if(!r) return [];
  if(r.original||r.copy) return [['元アカウント',r.original],['複製アカウント',r.copy]].filter(x=>x[1]&&x[1].transfer);
  return r.transfer?[['',r]]:[];
}
function keepResult(id,j){
  const note=$('#keepNote');
  if(!$('#keepResults').checked){ note.className='keepnote warn'; note.textContent='この端末には保存していません。今すぐ控えてください。'; return; }
  const list=loadHist().filter(x=>x.id!==id);
  list.unshift({id, at:Date.now(), clone:!!j.clone, result:j.result});
  const ok=saveHist(list);
  note.className='keepnote'+(ok?'':' warn');
  note.textContent=ok?'このコードはこの端末に保存しました（「この端末に残した結果」から確認できます）。':'端末への保存に失敗しました。今すぐ控えてください。';
  renderHist();
}
function renderHist(){
  const list=loadHist(), box=$('#hist'); box.textContent='';
  $('#histPanel').hidden=!list.length;
  list.forEach(h=>{
    resultPairs(h.result).forEach(([label,r])=>{
      const row=el('div','hrow'), info=el('div','hn');
      const d=new Date(h.at);
      info.append(el('small',null,d.getMonth()+1+'/'+d.getDate()+' '+clock(d)+(label?'・'+label:'')));
      info.append(el('b',null,r.transfer),el('span',null,'　暗証番号 '),el('b',null,r.confirm));
      const cp=el('button','btn','コピー'); cp.type='button';
      cp.addEventListener('click',()=>rvGuard(h.result,async()=>{ const ok=await copyText(r.transfer+' '+r.confirm); toast(ok?'コードと暗証番号をコピーしました':'コピーできませんでした'); }));
      if(needReview(h.result)) row.classList.add('rvlock');
      row.append(info,cp); box.append(row);
    });
    const del=el('button','btn','この結果を削除'); del.type='button';
    del.addEventListener('click',()=>{ if(!window.confirm('この結果を端末から削除しますか？')) return; saveHist(loadHist().filter(x=>x.id!==h.id)); renderHist(); });
    const wrap=el('div','tools'); wrap.append(del); box.append(wrap);
  });
}
$('#histClear').addEventListener('click',()=>{ if(!window.confirm('この端末に残した結果をすべて削除しますか？')) return; try{localStorage.removeItem(HKEY)}catch(e){} renderHist(); });
try{ if(localStorage.getItem('nyanko-keep')==='0') $('#keepResults').checked=false; }catch(e){}
$('#keepResults').addEventListener('change',()=>{ try{localStorage.setItem('nyanko-keep',$('#keepResults').checked?'1':'0')}catch(e){} });

/* ---------- よく使う組み合わせ ---------- */
const PKEY='nyanko-presets';
function loadPresets(){ try{ const v=JSON.parse(localStorage.getItem(PKEY)||'[]'); return Array.isArray(v)?v:[]; }catch(e){ return []; } }
function currentPreset(name){
  const nums=parseNums($('#chars').value).nums, cats={};
  nums.forEach(n=>{ if(catSettings[n]) cats[n]=catSettings[n]; });
  return {name, items:picked('items'), subs:picked('subs'), others:picked('others'), events:picked('events'), qty:qtyMap(),
          chars:$('#chars').value, cats, safe:$('#safe').checked, dry:$('#dry').checked};
}
function applyPreset(p,silent){
  ['items','subs','others','events'].forEach(g=>$$('input[name='+g+']').forEach(e=>e.checked=(p[g]||[]).includes(e.value)));
  $$('input.q').forEach(e=>e.value=(p.qty&&p.qty[e.dataset.item]!=null)?String(p.qty[e.dataset.item]):'');
  $('#chars').value=p.chars||'';
  Object.keys(catSettings).forEach(k=>delete catSettings[k]);
  Object.entries(p.cats||{}).forEach(([n,c])=>catSettings[n]=Object.assign({base_level:'',plus_level:'',form:'',talents_all:false},c));
  $('#safe').checked=false; $('#dry').checked=false;   /* 安全モード・試運転は廃止 */
  $('#clone').checked=false; $('#integrity').checked=false;
  chipSig=null; refresh();
  if(!silent) toast('「'+p.name+'」を呼び出しました');
}
function renderPresets(){
  const box=$('#presets'); box.textContent='';
  const list=loadPresets();
  if(!list.length){ box.append(el('div','cmore','まだ保存していません。')); return; }
  list.forEach((p,i)=>{
    const row=el('div','preset'), info=el('div','pn',p.name);
    const n=(p.items||[]).length+(p.subs||[]).length+(p.others||[]).length+Object.keys(p.qty||{}).length;
    info.append(el('small',null,'項目 '+n+'件'+(p.chars?'・キャラ '+parseNums(p.chars).nums.length+'体':'')));
    const use=el('button','btn','呼び出す'); use.type='button'; use.addEventListener('click',()=>applyPreset(p));
    const del=el('button','btn','削除'); del.type='button';
    del.addEventListener('click',()=>{ if(!window.confirm('「'+p.name+'」を削除しますか？')) return; const l=loadPresets(); l.splice(i,1); try{localStorage.setItem(PKEY,JSON.stringify(l))}catch(e){} renderPresets(); });
    row.append(info,use,del); box.append(row);
  });
}
$('#presetSave').addEventListener('click',()=>{
  const name=$('#presetName').value.trim();
  if(!name){ toast('名前を入れてください'); $('#presetName').focus(); return; }
  const list=loadPresets().filter(p=>p.name!==name);
  list.unshift(currentPreset(name));
  if(list.length>20){ toast('保存できるのは20件までです。不要なものを削除してください'); return; }
  try{ localStorage.setItem(PKEY,JSON.stringify(list)); toast('「'+name+'」を保存しました'); $('#presetName').value=''; }
  catch(e){ toast('保存できませんでした（ブラウザの設定を確認してください）'); }
  renderPresets();
});
$('#presetName').addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); $('#presetSave').click(); } });

function watch(id){
  curJob=id; sessionStorage.setItem('job',id);
  try{ const sn=JSON.parse(sessionStorage.getItem('nyanko-seen-jobs')||'[]'); if(!sn.includes(id)) sessionStorage.setItem('nyanko-seen-jobs',JSON.stringify(sn.concat(id).slice(-20))); }catch(e){}
  setRunning(true);
  clearInterval(pollTimer); clearInterval(tickTimer);
  pollTimer=setInterval(poll,2000);
  tickTimer=setInterval(()=>{ if(jobRunning){ $('#elapsed').textContent=mmss((Date.now()-jobStart)/1000); } },1000);
  poll();
}
async function start(){
  const t=$('#transfer').value.trim(), c=$('#confirm').value.trim();
  if(!t!==!c){
    show('input'); $('#inputErr').textContent='引継ぎコードと暗証番号は「両方入力」か「両方空欄」にしてください。'; return;
  }
  if($('#integrity').checked){
    if(!t||!c){ show('input'); $('#inputErr').textContent='バン保証には、既存セーブの引継ぎコードと暗証番号の両方が必要です。'; return; }
    if(!$('#dry').checked){
      const accepted=window.confirm('既存セーブを検査し、確認済みのエラーキャラと直接参照だけを修復します。修復対象が見つかった場合は保存データを再登録し、新しい引継ぎコードを発行します。修復対象がなければ再登録しません。BAN回避や安全性を保証する機能ではありません。続行しますか？');
      if(!accepted) return;
    }
  } else if($('#clone').checked){
    if(!t||!c){ show('input'); $('#inputErr').textContent='複製には、引継ぎコードと暗証番号の両方が必要です。'; return; }
    if(!$('#dry').checked){
      const accepted=window.confirm('【試験提供・本番サーバー接続】元アカウントと複製アカウントの2つを別々に登録します。公式の機能ではないため、独立プレイや継続利用は保証されません。引継ぎコードは1回限りで、ゲーム内で引継ぎを完了すると元端末のデータが初期化されます。失敗時は元アカウントのコードだけが発行される場合もあります。理解したうえで続行しますか？');
      if(!accepted) return;
    }
  } else {
  if(parseNums($('#chars').value).err){ show('chars'); toast('キャラ番号を確認してください'); return; }
  const ptv=ptMin(); if(ptv.err){ show('others'); toast(ptv.err); return; }
  if(needOld()&&!special()&&(!t||!$('#oldDone').checked)){ show('subs'); toast(!t?'新しいアカウントでは、旧レジェンドの段階も一緒に選んでください':'「旧レジェンドの冠1はクリア済み」にチェックするか、旧レジェンドの段階も選んでください'); return; }
  if(ptv.min>0&&picked('others').includes('プレイ時間カンスト')){ show('others'); toast('「プレイ時間カンスト」と「プレイ時間を増やす」は同時に選べません'); return; }
  if(restartOn()&&(t||c)){ show('input'); $('#inputErr').textContent='初期垢作成は新しいアカウントで行います。引継ぎコードと暗証番号を空欄にしてください。'; return; }
  if(refresh()===0){ toast('項目を選ぶか、数量・キャラ番号を入力してください'); show('qty'); return; }
  const bad=overInputs()[0];
  if(bad){ show('qty'); const d=bad.closest('details'); if(d) d.open=true; toast('「'+(bad.dataset.label||bad.dataset.item)+'」が上限を超えています'); bad.scrollIntoView({block:'center'}); return; }
  }
  $('#fail').hidden=true; $('#res').hidden=true; $('#warn').hidden=true; $('#rep').hidden=true; $('#log').textContent=''; $('#keepNote').textContent='';
  renderSteps(null); resetNow(); $('#elapsed').textContent='00:00'; $('#updated').textContent='--:--:--';
  $('#orderErr').textContent='';
  if(PAYMENTS){ /* 支払い前は「実行中」の画面に切り替えない（注文を作るだけ） */
    const go=$('#go'); go.disabled=true; go.textContent='注文を確認中…';
    const restore=()=>{ go.disabled=false; go.textContent=goLabel(); };
    try{
      const r=await api('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(runBody(t,c))});
      const j=await r.json();
      restore();
      if(!r.ok){ $('#orderErr').textContent=j.error||'注文を作成できませんでした'; if(j.code==='maintenance'){ applyMaint({on:true,message:j.error,url:j.url||''},false); } $('#orderErr').scrollIntoView({block:'center'}); return; }
      if(j.order){ openPay(j.order,j.amount,j.breakdown,j.skipped,j.state); return; }
      setRunning(true); show('status'); jobStart=Date.now(); watch(j.job);
    }catch(e){ restore(); $('#orderErr').textContent='サーバーに接続できませんでした。少し待ってからもう一度お試しください。'; }
    return;
  }
  setRunning(true); show('status');
  $('#log').textContent='開始しました。サーバーが休止していた場合は、最初に1分ほどかかります。';
  try{
    const r=await api('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(runBody(t,c))});
    const j=await r.json();
    if(!r.ok){ setRunning(false); $('#log').textContent=''; $('#fail').hidden=false; $('#fail').textContent=j.error||'エラー'; return; }
    jobStart=Date.now(); watch(j.job);
  }catch(e){
    setRunning(false); $('#log').textContent='';
    $('#fail').hidden=false; $('#fail').textContent='サーバーに接続できませんでした。少し待ってからもう一度お試しください。';
  }
}
function runBody(t,c){
  return {safe:$('#safe').checked, dry:$('#dry').checked, clone:$('#clone').checked, integrity_only:$('#integrity').checked, transfer:t, confirm:c,
          items:picked('items'), qty:qtyMap(), chars:$('#chars').value, cat_settings:catSettingsPayload(), subs:picked('subs'), others:picked('others'),
          old_legend_done:!!($('#oldDone')&&$('#oldDone').checked), events:picked('events').map(Number), events_pre_done:!!($('#evPre')&&$('#evPre').checked), playtime_add_min:special()?0:ptMin().min, restart_pack:!special()&&restartOn(), coupon:IS_ADMIN?'':couponCode()};
}
$('#go').addEventListener('click',start);

/* ---------- アップデート中でも弾かれない通信 ---------- */
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
let updShown=0;
function updBanner(on){ updShown=Math.max(0,updShown+(on?1:-1)); $('#updBanner').hidden=updShown===0; }
/* サーバーの再起動中（接続できない・502/503/504・「アップデート中」「確認中」の応答）は、最大約5分、自動でやり直す。
   注文と支払いの状態はサーバー側に保存されているので、やり直しても二重に支払われたり実行されたりしない。 */
async function api(url,opts,tries){
  let shown=false, last=null;
  try{
    for(let i=0;i<(tries||60);i++){
      try{
        const r=await fetch(url,opts);
        if(![502,503,504].includes(r.status)){
          if(r.status===409){ const c=r.clone(); const j=await c.json().catch(()=>null); if(j&&j.code==='paying'){ last=r; await sleep(4000); continue; } }
          return r;
        }
        last=r;
      }catch(e){ last=null; }
      if(!shown){ shown=true; updBanner(true); }
      await sleep(5000);
    }
    if(last) return last;
    throw new Error('offline');
  }finally{ if(shown) updBanner(false); }
}

/* ---------- 入力内容の一時保存（再読み込みしても消えない・このタブを閉じるまで） ---------- */
const DKEY='nyanko-draft';
let draftTimer=null, draftReady=false;
function saveDraft(){
  if(!draftReady) return;
  clearTimeout(draftTimer);
  draftTimer=setTimeout(()=>{ try{
    const d=currentPreset('draft'); d.transfer=$('#transfer').value; d.confirm=$('#confirm').value;
    d.mode=$('#integrity').checked?'integrity':$('#clone').checked?'clone':restartOn()?'restart':'normal'; d.view=curView;
    sessionStorage.setItem(DKEY,JSON.stringify(d));
  }catch(e){} },300);
}
function restoreDraft(){
  try{
    const d=JSON.parse(sessionStorage.getItem(DKEY)||'null'); if(!d) return null;
    applyPreset(d,true);
    $('#transfer').value=d.transfer||''; $('#confirm').value=d.confirm||'';
    $('#integrity').checked=d.mode==='integrity'; $('#clone').checked=d.mode==='clone';
    const r=$('#restart'); if(r&&!r.disabled) r.checked=d.mode==='restart';
    return d;
  }catch(e){ return null; }
}

/* ---------- PayPay決済 ---------- */
let payOrder=null, payDead=false;
const PAY_MSG={
  empty_link:['リンクが空です','PayPayで作った送金リンクを貼り付けてください。'],
  not_paypay_link:['PayPayのリンクではありません','「https://pay.paypay.ne.jp/」で始まる送金リンクを貼ってください。'],
  multiple_links:['リンクが複数あります','1件だけにして、もう一度貼り付けてください。'],
  link_too_long:['入力が長すぎます','リンクだけを貼り付けてください。'],
  bad_link:['リンクの形式が正しくありません','前後に余計な文字が入っていないか確認してください。'],
  link_not_found:['リンクが見つかりません','リンクが間違っているか、取り消されています。新しく作り直してください。'],
  amount_mismatch:['金額が違います','表示の金額ちょうどで、新しい送金リンクを作ってください（古いリンクは使えません）。'],
  link_restricted:['パスコード付きのリンクは使えません','パスコードなしで送金リンクを作り直してください。'],
  link_blocked:['PayPay側で保留中のリンクです','PayPayアプリで保留を解除してから、もう一度ボタンを押してください（再送金は不要です）。'],
  updating:['アップデート中です','自動で続きます。このままお待ちください。'],
  maintenance:['メンテナンス中です','新規のご注文は、作業が終わるまでお待ちください。'],
  expired:['リンクの期限切れ','新しい送金リンクを作り直してください。'],
  already_received:['受け取り済みのリンクです','すでに使われたリンクです。新しいリンクを作ってください。'],
  link_cancelled:['リンクが取り消されています','新しい送金リンクを作ってください。'],
  not_pending:['受け取れない状態のリンクです','新しい送金リンクを作ってください。'],
  duplicate:['使用済みのリンクです','すでに実行に使われたリンクです。新しいリンクを作ってください。'],
  identity:['送金元を確認できません','自分宛て以外の、通常の送金リンクを使ってください。'],
  order_mismatch:['リンクの内容を確認できません','新しい送金リンクを作り直してください。'],
  link_incomplete:['リンク情報が不足しています','新しい送金リンクを作り直してください。'],
  invalid_link:['リンクを確認できません','リンクが正しいか確認してください。'],
  link_network:['通信エラー','少し待ってからもう一度送信してください（送金済みの場合は再送金しないでください）。'],
  rate_limited:['PayPayが混み合っています','しばらく待ってから、もう一度送信してください。'],
  paypay_unavailable:['PayPay側の一時エラー','しばらく待ってから、もう一度送信してください。'],
  session_rejected:['受取側の接続が切れています','送金はせず、管理者に再接続を依頼してください。'],
  login_required:['現在、決済を受け付けられません','受取側PayPayが未接続です。管理者にお知らせください。'],
  otp_required:['現在、決済を受け付けられません','受取側PayPayが再認証待ちです。管理者にお知らせください。'],
  cookie_missing:['現在、決済を受け付けられません','受取側の接続を確認できません。管理者にお知らせください。'],
  accept_uncertain:['PayPay側で保留になった可能性があります','PayPay側の安全確認で止まっています。送金した方のPayPayアプリに届く確認（承認）の通知、または取引履歴のこの送金から、保留を解除してください。解除されると自動で実行します（再送金は不要・この画面は閉じても大丈夫です）。'],
  accept_unconfirmed:['PayPay側で保留になりました','PayPay側の安全確認で止まっています。送金した方のPayPayアプリに届く確認（承認）の通知、または取引履歴のこの送金から、保留を解除してください。解除されると自動で実行します（再送金は不要・この画面は閉じても大丈夫です）。'],
  temporary_hold:['PayPay側で保留中です','PayPay側の安全確認で止まっています。送金した方のPayPayアプリに届く確認（承認）の通知、または取引履歴のこの送金から、保留を解除してください。解除されると自動で実行します（再送金は不要・この画面は閉じても大丈夫です）。'],
  still_pending:['保留の解除待ちです','PayPay側の安全確認で止まっています。送金した方のPayPayアプリに届く確認（承認）の通知、または取引履歴のこの送金から、保留を解除してください。解除されると自動で実行します（再送金は不要・この画面は閉じても大丈夫です）。'],
  hold_failed:['自動では確認できませんでした','リンクが取り消し・期限切れ等の状態です。お支払い済みの場合は、上の注文番号を管理者に伝えてください。'],
  not_found:['送金記録が見つかりません','管理者に連絡してください。'],
  too_many_fails:['失敗が続いています','10分ほど待ってからお試しください。'],
  order_missing:['注文の期限が切れました','もう一度「実行」から始めてください。'],
  paying:['確認中です','数秒待ってください。'],
  started:['すでに開始済みです','「状況」タブを確認してください。'],
  busy:['支払いは完了しています','待機枠がいっぱいです。少し待って、もう一度ボタンを押してください（再送金は不要です）。'],
  start_failed:['支払いは完了しています','開始に失敗しました。もう一度ボタンを押してください（再送金は不要です）。'],
  needs_admin:['以前の注文で使われたリンクです','お支払い済みの場合は、上の注文番号を管理者に伝えてください（再送金は不要です）。'],
  receiver_mismatch:['自動では確認できませんでした','お支払い済みの場合は、上の注文番号を管理者に伝えてください。'],
  done:['この注文は完了しています','新しく実行する場合は、もう一度「決済して実行」から始めてください。'],
  dead:['自動では確認できませんでした','お支払い済みの場合は、上の注文番号を管理者に伝えてください。確認が取れると、「① メニュー」画面の上の「未完了の注文」から実行を開始できます（再送金は不要です）。'],
  internal:['予期しないエラー','PayPayアプリで送金状態を確認し、送金済みなら管理者に連絡してください。'],
};
function payError(j,fallback,lv){
  const m=PAY_MSG[j&&j.code]||[(j&&j.error)||fallback,''];
  $('#payErrTitle').textContent=m[0];
  $('#payErrHint').textContent=(m[1]||'')+(()=>{ const m=(j&&j.code==='amount_mismatch'&&j.error)?j.error.match(/（(リンク[^）]*)）/):null; return m?' ['+m[1]+']':''; })();
  $('#payErr').dataset.lv=lv||'bad'; $('#payErr').hidden=false;
}
function openPay(id,amount,breakdown,skipped,state){
  watchOrders(true);
  payOrder=id; $('#payOrderNo').textContent=id; $('#payOrderShort').textContent='TN-'+String(id).slice(0,8); payDead=false; setHold(false); $('#payPanel').hidden=false; $('#payErr').hidden=true; $('#payLink').value='';
  $('#payAmount').textContent=Number(amount).toLocaleString('ja-JP');
  const ul=$('#payList'); ul.textContent='';
  (breakdown||[]).forEach(r=>{ const li=document.createElement('li'); li.textContent=r.label+' '+r.count+'件 × '+r.unit+'円'; ul.append(li); });
  $('#payInfo').textContent='入金が確認でき次第、自動で実行します（この画面は開いたままで大丈夫です）。';
  $('#paySkip').hidden=!(skipped&&skipped.length); $('#paySkipList').textContent=(skipped||[]).join('・');
  $('#payGo').disabled=false; document.body.style.overflow='hidden';
  if(state==='hold') setHold(true);
  else if(state==='paid'){ $('#payLinkField').hidden=true; $('#payGo').textContent='支払い済み・実行を開始'; $('#payInfo').textContent='支払いは確認済みです。ボタンを押すと実行を開始します（再送金は不要です）。'; }
  else setTimeout(()=>$('#payLink').focus(),50);
}
let holdTimer=null, holdTries=0;
function setHold(on){
  $('#payLinkField').hidden=on; $('#payGo').textContent=on?'今すぐ確認する':'支払って実行する'; $('#paySteps').hidden=on;
  if(on) $('#payInfo').textContent='保留中です。送金した方がPayPayアプリで保留を解除（承認）すると、自動で確認して実行します。同じリンクを送り直したり、再送金したりしないでください。この画面を閉じても、サーバーが確認を続けます。あとで同じブラウザで開けば、結果（引継ぎコード）を見られます。';
  $('#payCancel').textContent=on?'閉じる（確認は続きます）':'やめる';
  if(!on){ clearInterval(holdTimer); holdTimer=null; holdTries=0; return; }
  if(holdTimer) return; /* すでに自動確認中（回数は引き継ぐ） */
  holdTimer=setInterval(()=>{ if(!payOrder||payDead||$('#payPanel').hidden||++holdTries>20){ clearInterval(holdTimer); holdTimer=null; if(holdTries>20) $('#payInfo').textContent='保留の解除を待っています。サーバーが引き続き自動で確認します（この画面は閉じても大丈夫です）。解除したあとすぐ進めたいときは「今すぐ確認する」を押してください。'; return; } if(!$('#payGo').disabled) $('#payGo').click(); },45000);
}
function closePay(){ setTimeout(loadResume,300); clearInterval(holdTimer); holdTimer=null; payOrder=null; $('#payPanel').hidden=true; document.body.style.overflow=''; }
$('#payCancel').addEventListener('click',()=>{ closePay(); show('confirm'); });
$('#payLink').addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); $('#payGo').click(); } });
$('#payGo').addEventListener('click',async()=>{
  if(!payOrder||payDead) return;
  const btn=$('#payGo'); btn.disabled=true; payError({error:'決済を確認しています…',code:'x'},'決済を確認しています…','wait');
  $('#payErrTitle').textContent='決済を確認しています…'; $('#payErrHint').textContent='画面を閉じずにお待ちください。';
  try{
    const r=await api('/api/orders/'+payOrder+'/pay',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({link:$('#payLink').value})});
    const j=await r.json();
    if(!r.ok){
      if(j.code==='started'){ /* 通信が途切れている間に開始できていた → その実行の状況画面へ */
        try{ const m=await (await api('/api/orders/mine',{cache:'no-store'},6)).json(); const o=(m.orders||[]).find(x=>x.order===payOrder&&x.job);
          if(o){ closePay(); setRunning(true); show('status'); jobStart=Date.now(); watch(o.job); return; } }catch(e){}
      }
      payError(j,'決済を確認できませんでした');
      if(j.hold){ setHold(true); $('#payErr').dataset.lv='wait'; btn.disabled=false; return; }
      if(['dead','order_missing','started'].includes(j.code)){ payDead=true; return; }
      btn.disabled=false; return;
    }
    closePay(); setRunning(true); show('status'); jobStart=Date.now(); watch(j.job);
  }catch(e){
    payError({code:'link_network'},'通信エラー'); btn.disabled=false;
  }
});
function openDone(j){
  const box=$('#doneCodes'); box.textContent='';
  const pairs=resultPairs(j.result); if(!pairs.length) return;
  pairs.forEach(([label,r])=>{
    if(label) box.append(el('h4',null,label));
    [['引継ぎコード',r.transfer],['暗証番号',r.confirm]].forEach(([name,val])=>{
      const row=el('div','code'), d=el('div'); d.append(el('small',null,name),el('strong',null,val||'未発行'));
      const b=el('button','btn','コピー'); b.type='button';
      b.addEventListener('click',()=>rvGuard(j.result,async()=>{ const ok=await copyText(val||''); toast(ok?name+'をコピーしました':'コピーできませんでした'); }));
      row.append(d,b); box.append(row);
    });
  });
  showCoupon(j.coupon);
  $('#doneTitle').textContent=j.clone?'複製が完了しました':'完了しました';
  $('#doneCloneNote').hidden=!j.clone;
  $('#doneAck').checked=false; $('#doneClose').disabled=true;
  $('#donePanel').hidden=false; document.body.style.overflow='hidden';
  window.__doneResult=j.result;
  $('#donePanel').classList.toggle('rvlock',needReview(j.result));
  if(needReview(j.result)) rvOpen(j.result);
}
function showCoupon(code){
  const b=$('#doneCoupon'); if(!b) return; if(!code){ b.hidden=true; return; }
  b.textContent=''; b.append(el('b',null,'🎟 次回使える10%引きクーポン'));
  const row=el('div','code'), d=el('div'); d.append(el('small',null,'クーポンコード'),el('strong',null,code));
  const cb=el('button','btn','コピー'); cb.type='button'; cb.addEventListener('click',()=>rvGuard(window.__doneResult,async()=>{ const ok=await copyText(code); toast(ok?'クーポンをコピーしました':'コピーできませんでした'); }));
  row.append(d,cb); b.append(row, el('small',null,'次回 '+COUPON_MIN+'円以上のご注文で10%引き（1回きり・発行から30日間）。注文の確認画面で入力してください。'));
  b.hidden=false;
}
async function fetchCoupon(job){   /* 完了直後はクーポンの発行が少し遅れることがあるので、数回だけ確認する */
  for(let i=0;i<4;i++){
    await sleep(1500+i*1500);
    try{ const m=await (await fetch('/api/orders/mine',{cache:'no-store'})).json();
      const o=(m.orders||[]).find(x=>x.job===job&&x.coupon_issued); if(o){ if(!$('#donePanel').hidden) showCoupon(o.coupon_issued); return; } }catch(e){}
  }
}
$('#doneAck').addEventListener('change',()=>{ $('#doneClose').disabled=!$('#doneAck').checked; });
$('#doneClose').addEventListener('click',()=>{ if(needReview(window.__doneResult)){ rvOpen(window.__doneResult); return; } $('#donePanel').hidden=true; document.body.style.overflow=''; show('status'); });
window.addEventListener('beforeunload',e=>{ if(jobRunning){ e.preventDefault(); e.returnValue=''; } });
function openHelp(){ $('#helpPanel').hidden=false; document.body.style.overflow='hidden'; }
function closeHelp(){ $('#helpPanel').hidden=true; if($('#payPanel').hidden&&$('#donePanel').hidden) document.body.style.overflow=''; }
$('#helpBtn').addEventListener('click',openHelp);
$('#helpClose').addEventListener('click',closeHelp);
$('#helpPanel').addEventListener('click',e=>{ if(e.target.id==='helpPanel') closeHelp(); });
document.addEventListener('keydown',e=>{ if(e.key==='Escape'&&!$('#helpPanel').hidden) closeHelp(); });

/* ---------- 起動時 ---------- */

/* ---------- 未完了の注文の再開 ---------- */
const STATE_JA={unpaid:'支払い待ち',hold:'保留中（解除待ち）',paid:'支払い済み・未実行',dead:'確認が必要です',started:'実行中',done:'完了'};
let watchTimer=null;
/* 未完了の注文があるあいだ、状態を自動で確認し続ける。
   管理者が「支払い済み」にした・保留が解除された、などで実行が始まったら、自動で進み具合の画面に切り替える。 */
function watchOrders(on){
  if(on&&!watchTimer) watchTimer=setInterval(()=>{ if(!document.hidden) loadResume(true); },6000);
  if(!on&&watchTimer){ clearInterval(watchTimer); watchTimer=null; }
}
document.addEventListener('visibilitychange',()=>{ if(!document.hidden&&watchTimer) loadResume(true); });
function applyMaint(m,hasOrders){
  if(!m||!m.on){ $('#maintBar').hidden=true; $('#maintWall').hidden=true; return; }
  const msg=m.message||'ただいまメンテナンス中です。新規のご注文は、作業が終わるまでお待ちください。';
  $('#maintMsg').textContent=msg; $('#maintWallMsg').textContent=msg;
  [['#maintLink'],['#maintWallLink']].forEach(([id])=>{ const a=$(id); if(m.url){ a.href=m.url; a.hidden=false; } else a.hidden=true; });
  $('#maintBar').hidden=false;
  $('#maintWall').hidden=!!hasOrders;   /* 未完了の注文がある人には、続きを見られるよう全面表示にしない */
}
async function loadResume(quiet){
  try{
    const r=quiet?await fetch('/api/orders/mine',{cache:'no-store'}):await api('/api/orders/mine',{cache:'no-store'},6); if(!r.ok) return;
    const j=await r.json(); const list=j.orders||[];
    // 実行が始まった注文（管理者の承認・保留解除など）→ 見ていなければ自動で状況画面へ
    let seen=[]; try{ seen=JSON.parse(sessionStorage.getItem('nyanko-seen-jobs')||'[]'); }catch(e){}
    // 完了済みでサーバーの表示期限が過ぎていても、保存したコードを1回だけ自動で表示する
    const saved=list.find(o=>o.state==='done'&&!o.job&&o.result&&!seen.includes('r:'+o.order));
    if(saved&&!jobRunning&&$('#donePanel').hidden){
      try{ sessionStorage.setItem('nyanko-seen-jobs',JSON.stringify(seen.concat('r:'+saved.order).slice(-20))); }catch(e){}
      if(payOrder===saved.order) closePay();
      show('status'); openDone({result:saved.result,clone:saved.clone,coupon:saved.coupon_issued}); keepResult('o:'+saved.order,{result:saved.result,clone:saved.clone});
    }
    const live=list.find(o=>o.job&&(o.state==='started'||o.state==='done')&&!seen.includes(o.job));
    if(live&&!jobRunning&&curJob!==live.job){
      try{ sessionStorage.setItem('nyanko-seen-jobs',JSON.stringify(seen.concat(live.job).slice(-20))); }catch(e){}
      if(payOrder&&payOrder===live.order) closePay();
      if($('#donePanel').hidden){ toast('お支払いが確認できました。実行を開始します'); setRunning(true); show('status'); jobStart=Date.now(); watch(live.job); }
    }
    // 支払い画面を開いている注文が「支払い済み」になった → その場で開始ボタンに切り替える
    if(payOrder){ const me=list.find(o=>o.order===payOrder); if(me&&me.state==='paid'&&!$('#payPanel').hidden&&$('#payGo').textContent!=='支払い済み・実行を開始'){ payDead=false; clearInterval(holdTimer); holdTimer=null; $('#payLinkField').hidden=true; $('#payGo').disabled=false; $('#payGo').textContent='支払い済み・実行を開始'; $('#payErr').hidden=true; $('#payInfo').textContent='お支払いが確認できました。ボタンを押すと実行を開始します（再送金は不要です）。'; } }
    watchOrders(list.some(o=>['unpaid','hold','paid','dead','starting','paying'].includes(o.state)||(o.state==='started'&&o.job)));
    applyMaint(j.maint,list.some(o=>o.state!=='unpaid'));
    const box=$('#resumeList'); box.textContent='';
    $('#resumePanel').hidden=!list.length;
    list.forEach(o=>{
      if(o.coupon_issued&&o.state==='done') box.append(el('p','note','🎟 クーポン '+o.coupon_issued+'（次回'+COUPON_MIN+'円以上で10%引き・1回きり）'));
      const row=el('div','rrow'); const d=new Date(o.created*1000);
      row.append(el('span',null,(d.getMonth()+1)+'/'+d.getDate()+' '+clock(d)+'　'+Number(o.amount).toLocaleString('ja-JP')+'円　'+(STATE_JA[o.state]||o.state)));
      if(o.state==='done'&&!o.job&&o.result){
        const b=el('button','btn','コードを見る'); b.type='button';
        b.addEventListener('click',()=>{ show('status'); openDone({result:o.result,clone:o.clone,coupon:o.coupon_issued}); });
        row.append(b); box.append(row);
      }else if(o.job){
        const b=el('button','btn',o.state==='done'?'結果を見る':'状況を見る'); b.type='button';
        b.addEventListener('click',()=>{ show('status'); if(curJob!==o.job){ jobStart=Date.now(); watch(o.job); } });
        row.append(b); box.append(row);
      }else if(o.state==='dead'){
        const b=el('button','btn','注文番号をコピー'); b.type='button';
        b.addEventListener('click',async()=>{ const ok=await copyText(o.order); toast(ok?'注文番号をコピーしました。管理者に伝えてください':'コピーできませんでした'); });
        row.append(b); box.append(row);
        box.append(el('p','note','自動では確認できませんでした。お支払い済みの場合は、注文番号を管理者に伝えてください。確認が取れると、ここから実行を開始できます（再送金は不要です）。'));
      }else{
        const b=el('button','btn',o.state==='paid'?(o.retry?'同じ内容でもう一度実行':'実行を開始する'):'支払い画面を開く'); b.type='button';
        b.addEventListener('click',()=>openPay(o.order,o.amount,o.breakdown,o.skipped,o.state));
        row.append(b); box.append(row);
        if(o.state==='paid'&&o.retry) box.append(el('p','note','前回は実行できませんでした。お支払いは有効です（再送金は不要）。引継ぎコードなどを直したい場合は、入力し直して「決済して実行」を押すと、この支払いがそのまま使われます（同じ金額以下のとき）。'));
      }
    });
  }catch(e){}
}
$('#payCopy').addEventListener('click',async()=>{ const ok=await copyText($('#payOrderNo').textContent); toast(ok?'受付番号をコピーしました':'コピーできませんでした'); });
$('#claimGo').addEventListener('click',async()=>{
  const no=$('#claimNo').value.trim(); if(!no){ toast('注文番号を入れてください'); return; }
  try{
    const r=await api('/api/orders/'+encodeURIComponent(no)+'/claim',{method:'POST'}); const j=await r.json();
    if(!r.ok){ toast(j.error||'再開できませんでした'); return; }
    closeHelp(); $('#claimNo').value=''; loadResume(); openPay(j.order,j.amount,j.breakdown,j.skipped,j.state);
  }catch(e){ toast('接続できませんでした'); }
});
const draft0=restoreDraft();
applyMaint(MAINT,false);
show(draft0&&draft0.view&&draft0.view!=='status'&&TABS.includes(draft0.view)?draft0.view:'start'); refresh(); ping(); tabMore(); renderPresets(); renderHist(); loadResume();
draftReady=true;
openHelp(); /* 開くたびに確認と使い方を表示 */
const saved=sessionStorage.getItem('job');
if(saved){ show('status'); watch(saved); }
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
