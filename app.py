"""にゃんこ代行（TN SHOP）のサーバー本体。
利用者がメニューを選び、PayPay で支払うと、引継ぎコードのセーブデータを編集して新しいコードを返す。
画面は templates/、料金の計算は pricing.py、セーブ編集は engine.py、PayPay と台帳は paypay_gateway.py。
ツムツム代行（もう1つの大メニュー）は画面が /tsum、実行が tsum_service.py。注文・支払い・実行枠は共通。
仕組みとルールは AGENTS.md を読むこと。
"""
from __future__ import annotations

import base64
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
import tsum_service
from pricing import (  # noqa: F401  料金の計算（pricing.py）。app.py からも同じ名前で使えるようにしている
    COUPON_DAYS, COUPON_MIN, CRASH_ITEMS, DB_OFFSET, EV_PREFIX, EVENT_BY_ID, EVENT_MAPS, EVENT_SELECTABLE, PRICE_LABELS, PRICES,
    PT_KEY, PT_MAX_MINUTES, PT_MIN_MINUTES, RESTART_KEY, _ALL_EVENT_MAPS, _event_ids, _legend_kinds, _load_event_maps,
    _one_legend_tier, chara_units, compute_amount, compute_tsum_amount,
)

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
TSUM_TOTAL_STEPS = 6                  # ツムツム代行の段取り（回線→ログイン→接続→取得→実行→発行）
TSUM_CAPTCHA_WAIT = 240               # 画像認証（CAPTCHA）の入力を待つ秒数
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


def _lock_subject(job: dict, arguments: tuple) -> str:
    """同じアカウントを同時に処理しないための鍵。にゃんこは引継ぎコード、ツムツムはログインID。"""
    if job.get("mode") == "tsum":
        payload = arguments[0] if arguments else {}
        return "tsum:" + str((payload or {}).get("login") or "")
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
ADMIN_SESSION_SEC = 12 * 3600      # 管理者ログインの有効時間（そのあとは入り直し）


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
    """管理者としてログイン中か。ログインから ADMIN_SESSION_SEC を過ぎたら切る。"""
    if not session.get("admin"):
        return False
    at = session.get("admin_at")
    if not isinstance(at, (int, float)):
        session["admin_at"] = time.time()      # 以前からのログイン：いまから数える
        return True
    if time.time() - at > ADMIN_SESSION_SEC:
        session.pop("admin", None)
        session.pop("admin_at", None)
        return False
    return True


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


def _admin_post(token):
    """管理画面の操作。結果は次の画面（GET）で表示する＝再読み込みしても同じ操作が二重に送られない。"""
    msg = ""
    if True:
        provided = request.form.get("csrf", "")
        if not hmac.compare_digest(provided.encode(), token.encode()):
            return "不正なリクエストです。", 403
        action = request.form.get("action")
        if action == "unlock":
            ip = _client_ip()
            if _too_many_fails("admin:" + ip) or _too_many_fails("admin:*", 30):   # 1つの IP で5回・全体で30回（10分）
                msg = "失敗が続いたので、10分ほど待ってください。"
            elif ADMIN_PASSWORD and hmac.compare_digest(request.form.get("passcode", "").encode(), ADMIN_PASSWORD.encode()):
                session["admin"] = True
                session["admin_at"] = time.time()
                session["admin_csrf"] = secrets.token_urlsafe(32)
                return redirect("/admin")
            else:
                _note_fail("admin:" + ip)
                _note_fail("admin:*")
                msg = "管理者パスコードが違うか、設定されていません。"
        elif not _is_admin():
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
        elif action == "backup_download":
            return _backup_response()
        elif action == "lock":
            session.pop("admin", None)
            session.pop("admin_at", None)
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
    if request.form.get("action") == "unlock" and not _is_admin():
        session["admin_flash"] = msg
        return redirect("/admin")
    session["admin_flash"] = msg
    return redirect("/admin#" + ADMIN_TAB.get(request.form.get("action"), "home"))


def _admin_page(msg):
    unlocked = _is_admin()
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


def _is_tsum_arguments(args) -> bool:
    """ツムツム代行の注文は、実行用の内容を1つの辞書で持つ（にゃんこは11個の並び）。"""
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
        MAIN_HTML, ev_names={m["id"]: m["name"] for m in EVENT_MAPS}, legend_levels={k: v[1] for k, v in engine.LEGEND_TIERS.items()}, is_admin=_is_admin() and PAYMENTS_ENABLED, restart=restart_pack(), pt_min=PT_MIN_MINUTES, coupon_min=COUPON_MIN, maint=_maint(), uid=_uid(_owner()), payments=PAYMENTS_ENABLED, prices=PRICES, crash_items=CRASH_ITEMS, groups=groups, totals=totals, version=APP_VERSION,
        limits=engine.QUANTITY_ITEM_LIMITS, high=HIGH_RISK_ITEMS,
        type_names={k: v for k, v in engine.ITEM_TYPE_NAMES.items() if k != "本能玉"},
        orb_kinds=item_catalog.ORB_KIND_NAMES, orb_grades=item_catalog.ORB_GRADES,
        error_nums=sorted(i + DB_OFFSET for i in engine.ERROR_CAT_IDS), site=_site(),
    )


def _site() -> str:
    """X などのリンクプレビュー（og:image）に使う、このサイトの URL（https で出す）。"""
    scheme = "https" if (request.headers.get("X-Forwarded-Proto") == "https" or request.is_secure) else request.scheme
    return f"{scheme}://{request.host}"


@app.get("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.ico")


@app.get("/tsum")
def tsum_page():
    """ツムツム代行の画面（にゃんこ代行とは別の大メニュー）。"""
    menus = [dict(key=k, **{kk: vv for kk, vv in v.items() if kk != "price"},
                  amount=PRICES.get(v["price"], 0))
             for k, v in sorted(tsum_service.MENUS.items(), key=lambda kv: kv[1]["order"])]
    return render_template_string(
        TSUM_HTML, menus=menus, modes=tsum_service.MODE_LABELS, solo=list(tsum_service.SOLO_MENUS),
        is_admin=_is_admin() and PAYMENTS_ENABLED, maint=_maint(), payments=PAYMENTS_ENABLED,
        coupon_min=COUPON_MIN, uid=_uid(_owner()), version=APP_VERSION, total_steps=TSUM_TOTAL_STEPS, site=_site(),
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




def _place_order(job: dict, arguments: tuple, transfer_key, amount: int, breakdown: list, summary: str,
                 skipped: list, coupon_raw: str):
    """注文を作る（支払い待ち）。クーポン・未払い注文の整理・支払い済みの充当は、にゃんこ代行と
    ツムツム代行で共通。金額と内訳と要約は、メニューごとに呼び出し側が計算して渡す。"""
    _gc_orders()
    _reconcile_orders()
    coupon = unicodedata.normalize("NFKC", str(coupon_raw or "")).strip().upper().replace(" ", "")[:20]
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
    me = _owner()
    trimmed_coupons = []
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
            # 1人の未払いの注文が多すぎたら、入金リンクのない古いものから閉じる（メモリ・保存ファイルが増え続けないように）
            mine = sorted(((k, o) for k, o in _orders.items() if o["owner"] == me and o["state"] == "unpaid" and not o.get("link_code")),
                          key=lambda kv: kv[1]["created"])
            for k, o in mine[:max(0, len(mine) - (MAX_UNPAID_PER_OWNER - 1))]:
                _orders.pop(k, None)
                if o.get("coupon") and not o.get("coupon_used"):
                    trimmed_coupons.append((o["coupon"], k))
            order_id = secrets.token_urlsafe(16)
            _orders[order_id] = {"owner": me, "uid": _uid(me), "state": "unpaid", "amount": amount, "created": time.time(),
                                 "job": job, "arguments": arguments, "transfer_key": transfer_key,
                                 "breakdown": breakdown, "skipped": skipped, "summary": summary,
                                 "coupon": coupon or None, "base_amount": base_amount}
    for c, k in trimmed_coupons:          # 閉じた古い注文が押さえていたクーポンは、使えるように戻す
        try:
            paypay_gateway.coupon_release(c, k)
        except Exception:  # noqa: BLE001
            _log_orders.exception("coupon release failed")
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


def _tsum_summary(mode: str, menus: list, qty: dict) -> str:
    """管理画面に出す注文の要約。アカウント情報（メールアドレス・パスワード）は入れない。"""
    parts = [tsum_service.MODE_LABELS.get(mode, mode)]
    for key in menus:
        value = qty.get(key)
        parts.append(tsum_service.menu_label(key) + (f"={value:,}" if value is not None else ""))
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
    summary = _tsum_summary(mode, menus, qty)
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
                "job": job, "arguments": arguments, "transfer_key": transfer_key, "breakdown": [], "skipped": [],
                "summary": "【管理者・無料】" + summary}
        with _orders_lock:
            _orders[oid] = free
        resp = _start_paid_order(free)
        _persist_orders()
        return resp
    amount, rows = compute_tsum_amount(menus, qty)
    breakdown = [{"label": label, "count": n, "unit": p} for label, n, p in rows]
    return _place_order(job, arguments, transfer_key, amount, breakdown, summary, [], data.get("coupon", ""))


@app.post("/run")
def run():
    _gc_jobs()
    admin_free = _is_admin() and PAYMENTS_ENABLED   # 管理者でログイン中のブラウザは全メニュー無料
    if _maint()["on"] and PAYMENTS_ENABLED and not admin_free:
        return _maintenance_response()
    data = _clean_run_body(request.get_json(silent=True))
    if data is None:
        return jsonify(error="注文の内容を読み取れませんでした。画面を再読み込みしてから、もう一度お試しください。"), 400
    if isinstance(data.get("tsum"), dict):
        return _run_tsum_order(data, admin_free)
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
    amount, rows = compute_amount(items, subs, others, qty, cat_nums, clone, integrity_only, cat_settings)
    breakdown = [{"label": l, "count": n, "unit": p} for l, n, p in rows]
    summary = _summary(items, subs, others, qty, cat_nums, cat_settings, dry, clone, integrity_only, bool(transfer))
    return _place_order(job, arguments, transfer_key, amount, breakdown, summary, skipped, data.get("coupon", ""))


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
            "tsum": (o.get("job") or {}).get("mode") == "tsum",   # ツムツム代行の注文（画面が別なので区別する）
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
    ip = _client_ip()
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
        tsum=job.get("tsum"), captcha=job.get("captcha"),
    )


@app.post("/api/jobs/<job_id>/captcha")
def job_captcha(job_id: str):
    """ツムツム代行の画像認証（CAPTCHA）の答えを、実行中のプロセスへ渡す。"""
    data = request.get_json(silent=True) or {}
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
def _template(name: str) -> str:
    """画面の HTML（Jinja のひな形）。templates/ に置いてある（以前は app.py の中に直接書いていた）。"""
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", name), encoding="utf-8", newline="") as f:
        return f.read()


ADMIN_HTML = _template("admin.html")   # 管理画面


MAIN_HTML = _template("main.html")     # 利用者の画面（HTML・CSS・JavaScript）


TSUM_HTML = _template("tsum.html")     # ツムツム代行の画面

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
