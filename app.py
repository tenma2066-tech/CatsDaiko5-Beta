"""にゃんこ代行 Web版（ベータ）
公開ショップ・PayPay決済と、管理者向けセーブデータ処理画面を提供します。
"""
from __future__ import annotations

import asyncio
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
from auth_store import PasswordStore
from github_auth_store import GitHubPasswordStore, PasswordStoreUnavailable
import save_audit
import save_repair
import paypay_gateway
import shop_store

APP_VERSION = "1.15"
GAME_DATA_VERSION = engine.GAME_DATA_VERSION
CAT_NAMES_CACHE_SCHEMA = 2

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
if not APP_PASSWORD:
    raise RuntimeError("環境変数 APP_PASSWORD が空です。Renderの Environment で設定してください。")

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or hashlib.sha256(
    ("nyanko-web:" + APP_PASSWORD + ":" + os.environ.get("ADMIN_PASSWORD", "")).encode()
).hexdigest()
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
if os.environ.get("AUTH_GITHUB_REPO") or os.environ.get("AUTH_GITHUB_TOKEN"):
    password_store = GitHubPasswordStore(
        os.environ.get("AUTH_GITHUB_REPO"), os.environ.get("AUTH_GITHUB_TOKEN"),
        os.environ.get("AUTH_GITHUB_BRANCH", "main"), os.environ.get("AUTH_GITHUB_PATH", "auth/passwords.json"),
        APP_PASSWORD, bootstrap=os.environ.get("AUTH_GITHUB_BOOTSTRAP") == "1",
    )
else:
    password_store = PasswordStore(os.environ.get("AUTH_DB_PATH", "/tmp/catsdaiko-auth/passwords.sqlite3"), APP_PASSWORD)


@app.errorhandler(PasswordStoreUnavailable)
def _password_store_unavailable(exc):
    return jsonify(error=str(exc)), 503

ITEMS = [
    "猫缶", "XP", "NP", "にゃんチケ", "レアチケ", "プラチケ", "レジェチケ",
    "イベチケ&福チケ", "バトルアイテム", "ネコビタン", "城素材", "キャッツアイ",
    "マタタビ", "本能玉", "リーダーシップ", "地底メダル",
]
SUBS = [
    "全キャラ解放&エラーキャラ削除", "全キャラレベルMAX", "全キャラ最高形態",
    "全キャラ本能解放", "全ステージクリア&全お宝金",
    "日本編 第1章クリア", "日本編 第2章クリア", "日本編 第3章クリア",
    "未来編 第1章クリア", "未来編 第2章クリア", "未来編 第3章クリア",
    "宇宙編 第1章クリア", "宇宙編 第2章クリア", "宇宙編 第3章クリア",
    "ゾンビステージクリア", "旧レジェンドステージクリア", "真レジェンドステージクリア",
    "ゼロレジェンドステージクリア", "魔界編全クリア", "にゃんこ塔全クリア",
    "イベントステージ全クリア", "ガマトトLvMax", "ガマトト助手全員レジェンド化",
    "にゃんこ神社LvMax",
]
OTHERS = [
    "プレイ時間カンスト", "ゴールド会員化", "編成スロット最大",
    "にゃんこメダル全解放", "敵キャラ図鑑全埋め", "施設全強化", "ミッション全クリア",
    "未所持キャラのガチャ・図鑑記録を整理",
]
_SUB_PRIORITY = {
    "全キャラ解放&エラーキャラ削除": 0,
    "全キャラ最高形態": 1,
    "全キャラレベルMAX": 2,
}

# bcsfe の配布元（PyPI / GitHub）の説明に「BANされることがある」と書かれている項目。
# これ以外が安全という意味ではない（公開情報が無いだけ）。
HIGH_RISK_ITEMS = ["猫缶", "レアチケ", "プラチケ", "レジェチケ"]

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


def _combo_progress_debug(save) -> str:
    """調査用: コンボ・NEW/通知関連の保存項目を各処理段階で比較する。"""
    fields = (
        "combo_unlocks",
        "combo_unlocked_10k_ur",
        "new_dialogs_2",
        "unlock_popups_0",
        "unlock_popups_6",
        "unlock_popups_8",
        "unlock_popups_11",
    )
    parts = []
    for field in fields:
        try:
            value = getattr(save, field, "<なし>")
            if isinstance(value, (list, tuple)):
                rendered = f"len={len(value)}, value={list(value)[:24]!r}"
            else:
                rendered = repr(value)
            parts.append(f"{field}={rendered}")
        except Exception:  # noqa: BLE001
            parts.append(f"{field}=<取得失敗>")
    try:
        popup_obj = getattr(save, "unlock_popups", None)
        popups = getattr(popup_obj, "popups", None)
        if isinstance(popups, dict):
            pairs = sorted(
                (key, getattr(value, "seen", value))
                for key, value in popups.items()
            )
            parts.append(f"unlock_popups=count:{len(pairs)}, first:{pairs[:24]!r}")
        else:
            parts.append(f"unlock_popups={popup_obj!r}")
    except Exception:  # noqa: BLE001
        parts.append("unlock_popups=<取得失敗>")
    try:
        cats_obj = getattr(save, "cats", None)
        flags = getattr(cats_obj, "chara_new_flags", None)
        if isinstance(flags, dict):
            pairs = sorted(flags.items(), key=lambda item: str(item[0]))
            parts.append(f"chara_new_flags=count:{len(pairs)}, first:{pairs[:12]!r}")
        else:
            parts.append(f"chara_new_flags={flags!r}")
        cats = getattr(cats_obj, "cats", None)
        if isinstance(cats, (list, tuple)):
            ids = [getattr(cat, "id", None) for cat in cats]
            parts.append(
                f"cat_roster=count:{len(ids)}, first_ids:{ids[:8]!r}, last_ids:{ids[-8:]!r}"
            )
    except Exception:  # noqa: BLE001
        parts.append("cat_state=<取得失敗>")
    return "; ".join(parts)


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
        threading.Thread(target=cleanup_loop, daemon=True).start()


@app.after_request
def _private_response(response):
    if request.path.startswith("/status/") or request.path in ("/admin", "/login"):
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

    warn_failures(engine.apply_item_amounts(save, items))
    if qty:                                          # 数量指定（同じ項目は数量指定を優先）
        warn_failures(engine.apply_item_amounts(save, list(qty), quantity_map=qty))
    subs_sorted = sorted(subs, key=lambda x: _SUB_PRIORITY.get(x, 99))
    warn_failures(engine.apply_subs_and_others(save, subs_sorted, others))

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


def _issue_codes_once(job: dict, handler, label: str):
    # BCSFE itself retries unless tries=1. Do not repeat a write whose result
    # may have been lost after the server accepted it.
    job["issuing_codes"] = True
    _publish_job(job)
    try:
        codes = handler.get_codes(upload_managed_items=False, tries=1)
        if codes:
            job["issuing_codes"] = False
            return codes
    except Exception:  # noqa: BLE001
        pass  # Never expose a transport exception containing request data.
    job["issuing_codes"] = False
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
                _step(job, 3, "セーブ保全チェックと既知異常の確認中…")
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
                _step(job, 4, "保全チェック完了。修復対象はありませんでした")
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
            _log(job, "試運転完了。複製データの独立インスタンス化を確認しました。サーバー登録・コード発行はしていません。")
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


def _logged_in() -> bool:
    ident = session.get("password_id")
    return session.get("ok") is True and isinstance(ident, int) and password_store.contains(ident)


@app.get("/health")
def health():
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    msg = ""
    if request.method == "POST":
        ip = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        if _too_many_fails(ip):
            msg = "失敗が続いたので、10分ほど待ってください。"
        elif (ident := password_store.authenticate(request.form.get("password", ""))) is not None:
            session.clear()
            session["ok"] = True
            session["password_id"] = ident
            session["job_owner"] = secrets.token_urlsafe(32)
            session.permanent = False
            return redirect("/console")
        else:
            _fail.setdefault(ip, []).append(time.time())
            msg = "パスワードが違います。"
    return render_template_string(LOGIN_HTML, msg=msg)


@app.get("/logout")
def logout():
    session.clear()
    return redirect("/login")


@app.route("/admin", methods=["GET", "POST"])
def admin():
    if not _logged_in():
        return redirect("/login")
    msg = ""
    token = session.setdefault("admin_csrf", secrets.token_urlsafe(32))
    if request.method == "POST":
        provided = request.form.get("csrf", "")
        if not hmac.compare_digest(provided.encode(), token.encode()):
            return "不正なリクエストです。", 403
        action = request.form.get("action")
        if action == "unlock":
            admin_password = os.environ.get("ADMIN_PASSWORD", "")
            ip = request.remote_addr or ""
            if _too_many_fails("admin:" + ip):
                msg = "失敗が続いたので、10分ほど待ってください。"
            elif admin_password and hmac.compare_digest(request.form.get("passcode", "").encode(), admin_password.encode()):
                session["admin"] = True
                session["admin_csrf"] = secrets.token_urlsafe(32)
                return redirect("/admin")
            else:
                _fail.setdefault("admin:" + ip, []).append(time.time())
                msg = "管理者パスコードが違うか、設定されていません。"
        elif not session.get("admin"):
            return "管理者モードが必要です。", 403
        elif action == "add":
            try:
                password_store.add(request.form.get("label", "").strip(), request.form.get("password", ""))
                msg = "パスワードを追加しました。"
            except ValueError as exc:
                msg = str(exc)
        elif action == "delete":
            try:
                password_store.delete(int(request.form.get("id", "")))
                msg = "パスワードを削除しました。"
            except ValueError as exc:
                msg = str(exc) or "指定が正しくありません。"
        elif action == "lock":
            session.pop("admin", None)
            return redirect("/admin")
    return render_template_string(ADMIN_HTML, msg=msg, csrf=session["admin_csrf"],
                                  unlocked=bool(session.get("admin")),
                                  github_storage=isinstance(password_store, GitHubPasswordStore),
                                  github_bootstrap=(isinstance(password_store, GitHubPasswordStore) and password_store.bootstrap),
                                  ephemeral=(isinstance(password_store, PasswordStore) and
                                             (not os.environ.get("AUTH_DB_PATH") or
                                              os.path.abspath(str(password_store.path)).startswith("/tmp/"))),
                                  passwords=password_store.list() if session.get("admin") else [])




# ---------------------------------------------------------------- 公開ショップ・注文・PayPay
def _shop_page(title, body):
    return render_template_string("""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="color-scheme" content="light dark">
<title>{{ title }} | にゃんこ代行</title>
<style>
:root{color-scheme:light dark;--bg:#e8edf2;--panel:#fff;--ink:#17181c;--muted:#5b6472;--line:#17181c;--accent:#ffc42e;--bad:#d93636;--soft:#f1f4f8}
@media(prefers-color-scheme:dark){:root{--bg:#121418;--panel:#1d2026;--ink:#f1f3f6;--muted:#9aa4b2;--line:#444b57;--soft:#262a32}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,"Noto Sans JP",sans-serif;line-height:1.55}
main{max-width:620px;margin:0 auto;padding:24px 15px 60px}header{display:flex;justify-content:space-between;align-items:center;margin-bottom:18px}
h1{font-size:23px;margin:0}h2{font-size:18px;margin:0 0 10px}.muted{color:var(--muted);font-size:13px}
.card{background:var(--panel);border:2px solid var(--line);border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 3px 0 var(--line)}
input,button,select{font:inherit;width:100%;padding:12px;border:2px solid var(--line);border-radius:10px;background:var(--soft);color:var(--ink);margin:5px 0}
button{background:var(--accent);color:#17181c;font-weight:800;cursor:pointer}
label{display:block;font-weight:700;font-size:14px;margin-top:10px}
.price{font-size:21px;font-weight:900}.error{color:var(--bad);font-weight:700;white-space:pre-wrap}.row{display:flex;justify-content:space-between;gap:10px;align-items:center}
a{color:inherit}.hidden{display:none!important}
</style></head><body><main><header><h1>にゃんこ代行</h1><a href="/admin">管理者</a></header>{{ body|safe }}</main></body></html>""", title=title, body=body)

@app.get("/")
def storefront_root():
    return redirect("/shop")

@app.get("/shop")
def shop_home():
    try:
        products = shop_store.catalog()
    except shop_store.ShopError:
        products = []
    product_json = json.dumps(products, ensure_ascii=False)
    body = r"""<div class="card"><h2>商品を選択</h2><p class="muted">ログイン不要でご注文いただけます。商品を選び、引継ぎ情報を入力してください。</p>
<div id="products"></div><p id="catalog-error" class="error"></p></div>
<div class="card" id="checkout" hidden><h2 id="selected-title">注文内容</h2><p class="price" id="selected-price"></p>
<label>引継ぎコード<input id="transfer" maxlength="128" autocomplete="off" autocapitalize="off"></label>
<label>暗証番号<input id="pin" maxlength="32" inputmode="numeric" autocomplete="off"></label>
<p class="muted">入力情報は暗号化して保存し、注文処理にのみ使用します。</p>
<button id="place">注文を確定する</button><p id="error" class="error"></p></div>
<script>
const products=__PRODUCTS__;let selected=null;
const yen=n=>new Intl.NumberFormat('ja-JP').format(n)+'円';
const list=document.getElementById('products');
if(!products.length){document.getElementById('catalog-error').textContent='現在、販売商品が設定されていません。管理者にお問い合わせください。';}
products.forEach(p=>{const el=document.createElement('button');el.type='button';el.style.textAlign='left';el.innerHTML='';
el.textContent=p.title+'　'+yen(p.price_yen)+(p.description?' — '+p.description:'');
el.addEventListener('click',()=>{selected=p;document.getElementById('selected-title').textContent=p.title;document.getElementById('selected-price').textContent=yen(p.price_yen);document.getElementById('checkout').hidden=false;document.getElementById('checkout').scrollIntoView({behavior:'smooth'});});list.appendChild(el);});
document.getElementById('place').addEventListener('click',async()=>{
 const error=document.getElementById('error');error.textContent='';
 if(!selected)return;
 const button=document.getElementById('place');button.disabled=true;button.textContent='注文を作成中…';
 try{
  const r=await fetch('/api/shop/orders',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({sku:selected.id,transfer_code:document.getElementById('transfer').value,pin:document.getElementById('pin').value})});
  const data=await r.json();if(!r.ok)throw Error(data.error||'注文を作成できませんでした。');
  location.href='/shop/order/'+encodeURIComponent(data.id)+'?key='+encodeURIComponent(data.access_token);
 }catch(e){error.textContent=e.message;}finally{button.disabled=false;button.textContent='注文を確定する';}
});
</script>""".replace("__PRODUCTS__", product_json)
    return _shop_page("商品一覧", body)

@app.post("/api/shop/orders")
def shop_create_order():
    data = request.get_json(silent=True) or {}
    try:
        order = shop_store.create_order(data.get("sku", ""), data.get("transfer_code", ""), data.get("pin", ""))
        return jsonify(order), 201
    except shop_store.ShopError as exc:
        return jsonify(error=str(exc)), 400

@app.get("/shop/order/<order_id>")
def shop_order_page(order_id):
    key = request.args.get("key", "")
    order = shop_store.public_order(order_id, key)
    if not order:
        return _shop_page("注文確認", '<div class="card"><h2>注文が見つかりません</h2><p>注文URLを確認してください。</p></div>'), 404
    body = r"""<div class="card"><h2>注文内容</h2><p>注文番号：<b>__ID__</b></p><p>商品：<b>__TITLE__</b></p><p class="price">__AMOUNT__円</p>
<p id="status">決済待ち</p><p class="muted">PayPayアプリで送金リンクを作成し、そのリンクを下に貼り付けてください。金額が注文額と一致した場合に受取確認を行います。</p>
<label>PayPay送金リンク<input id="paypay-link" maxlength="2048" placeholder="https://pay.paypay.ne.jp/..." autocomplete="off"></label>
<button id="pay">PayPayで支払う</button><p id="error" class="error"></p><p class="muted">受取処理中に通信が途切れた場合、二重受取を避けるため自動再試行しません。画面の案内に従って管理者へお問い合わせください。</p></div>
<script>
const id="__ID__",key=new URLSearchParams(location.search).get('key')||'';
async function refresh(){try{const r=await fetch('/api/shop/order/'+id+'?key='+encodeURIComponent(key));if(!r.ok)return;const o=await r.json();const names={awaiting_payment:'決済待ち',paid:'決済確認済み・作業待ち',review:'管理者による確認中',fulfilling:'作業中',completed:'完了',cancelled:'キャンセル'};document.getElementById('status').textContent=names[o.status]||o.status;if(o.status!=='awaiting_payment'){document.getElementById('pay').disabled=true;document.getElementById('paypay-link').disabled=true;}}catch(e){}}
document.getElementById('pay').addEventListener('click',async()=>{const b=document.getElementById('pay'),e=document.getElementById('error');e.textContent='';b.disabled=true;b.textContent='PayPay受取を確認中…';try{const r=await fetch('/api/shop/order/'+id+'/paypay',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({key,link:document.getElementById('paypay-link').value})});const d=await r.json();if(!r.ok)throw Error(d.error||'決済を確認できませんでした。');e.textContent=d.message||'決済を確認しました。';await refresh();}catch(x){e.textContent=x.message;b.disabled=false;}finally{b.textContent='PayPayで支払う';}});
refresh();
</script>""".replaceAll("__ID__", order.id).replace("__TITLE__", order.title).replace("__AMOUNT__", String(order.amount))
    return _shop_page("注文確認", body)

@app.get("/api/shop/order/<order_id>")
def shop_order_status(order_id):
    order = shop_store.public_order(order_id, request.args.get("key", ""))
    if not order:
        return jsonify(error="注文が見つかりません。"), 404
    return jsonify(order)

@app.post("/api/shop/order/<order_id>/paypay")
def shop_paypay_submit(order_id):
    data = request.get_json(silent=True) or {}
    order = shop_store.public_order(order_id, data.get("key", ""))
    if not order:
        return jsonify(error="注文が見つかりません。"), 404
    if order["status"] != "awaiting_payment":
        return jsonify(error="この注文は決済受付状態ではありません。"), 409
    try:
        digest = asyncio.run(paypay_gateway.receive_payment(data.get("link", ""), order["amount"], order_id))
        shop_store.set_status(order_id, "paid", payment_digest=digest)
        return jsonify(ok=True, status="paid", message="PayPayの受取を確認しました。管理者が注文を確認します。")
    except paypay_gateway.PayPayError as exc:
        if exc.code in {"accept_uncertain", "accept_unconfirmed", "temporary_hold", "duplicate"}:
            try: shop_store.set_status(order_id, "review", note=exc.code)
            except shop_store.ShopError: pass
        return jsonify(error=exc.message), 400
    except shop_store.ShopError as exc:
        return jsonify(error=str(exc)), 400

@app.get("/admin/paypay")
def admin_paypay_page():
    if not _logged_in(): return redirect("/login")
    if not session.get("admin"): return redirect("/admin")
    try: pp_status = paypay_gateway.status()
    except paypay_gateway.PayPayError as exc: pp_status = {"connected":False,"ready":False,"error":exc.message}
    csrf = session.setdefault("admin_csrf", secrets.token_urlsafe(32))
    otp_waiting = bool(paypay_gateway._pending_otp)
    body = """<div class="card"><h2>PayPay接続</h2><p>接続状態：<b>"""+("受取可能" if pp_status.get("ready") else "未接続・再認証が必要")+"""</b></p>
<p class="muted">SMS認証はPayPayが要求した場合のみです。認証情報は暗号化して保存されます。</p>
<form method="post" action="/admin/paypay/connect"><input type="hidden" name="csrf" value="{{ csrf }}">
<label>電話番号<input name="phone" type="tel" required autocomplete="off"></label><label>PayPayパスワード<input name="password" type="password" required autocomplete="off"></label><button>PayPayに接続</button></form>
"""+("""<form method="post" action="/admin/paypay/otp"><input type="hidden" name="csrf" value="{{ csrf }}"><label>SMS認証コード<input name="otp" inputmode="numeric" maxlength="4" required></label><button>SMS認証</button></form>""" if otp_waiting else "")+"""<p class="error">{{ msg }}</p></div><p><a href="/admin/orders">注文管理</a> ・ <a href="/console">代行ツール</a></p>"""
    return render_template_string(_shop_page("PayPay設定", body), csrf=csrf, msg=request.args.get("msg",""))

@app.post("/admin/paypay/connect")
def admin_paypay_connect():
    if not _logged_in() or not session.get("admin"): return "管理者モードが必要です。", 403
    token=session.get("admin_csrf","")
    if not hmac.compare_digest(request.form.get("csrf","").encode(),token.encode()): return "不正なリクエストです。",403
    try:
        result=asyncio.run(paypay_gateway.begin_login(request.form.get("phone",""),request.form.get("password","")))
        return redirect("/admin/paypay?msg="+("SMS認証コードを入力してください。" if result.get("otp_required") else "PayPayに接続しました。"))
    except paypay_gateway.PayPayError as exc:
        return redirect("/admin/paypay?msg="+__import__("urllib.parse",fromlist=["quote"]).quote(exc.message))

@app.post("/admin/paypay/otp")
def admin_paypay_otp():
    if not _logged_in() or not session.get("admin"): return "管理者モードが必要です。", 403
    token=session.get("admin_csrf","")
    if not hmac.compare_digest(request.form.get("csrf","").encode(),token.encode()): return "不正なリクエストです。",403
    try:
        asyncio.run(paypay_gateway.finish_login(request.form.get("otp","")))
        return redirect("/admin/paypay?msg=PayPayに接続しました。")
    except paypay_gateway.PayPayError as exc:
        return redirect("/admin/paypay?msg="+__import__("urllib.parse",fromlist=["quote"]).quote(exc.message))

@app.get("/admin/orders")
def admin_orders_page():
    if not _logged_in(): return redirect("/login")
    if not session.get("admin"): return redirect("/admin")
    rows=shop_store.admin_orders()
    csrf=session.setdefault("admin_csrf",secrets.token_urlsafe(32))
    order_body=render_template_string("""{% for o in rows %}<div class="card"><b>{{ o.id }} / {{ o.title }} / {{ o.amount }}円</b><p>状態：{{ o.status }}</p>
<p>引継ぎコード：<code>{{ o.credentials.transfer_code or o.credentials.error or '' }}</code><br>暗証番号：<code>{{ o.credentials.pin or '' }}</code></p>
<form method="post" action="/admin/orders/{{ o.id }}/status"><input type="hidden" name="csrf" value="{{ csrf }}">
<select name="status">{% for st in transitions[o.status] %}<option value="{{ st }}">{{ st }}</option>{% endfor %}</select><button>状態を更新</button></form></div>{% else %}<p>注文はありません。</p>{% endfor %}""", rows=rows, csrf=csrf,
        transitions={"awaiting_payment":["review","cancelled"],"paid":["fulfilling","cancelled"],"review":["paid","cancelled"],"fulfilling":["completed"],"completed":[],"cancelled":[]})
    return _shop_page("注文管理", '<div class="card"><h2>注文管理</h2><p><a href="/admin/paypay">PayPay設定</a> ・ <a href="/admin">管理者</a></p>'+order_body+'</div>')

@app.post("/admin/orders/<order_id>/status")
def admin_order_status(order_id):
    if not _logged_in() or not session.get("admin"): return "管理者モードが必要です。",403
    token=session.get("admin_csrf","")
    if not hmac.compare_digest(request.form.get("csrf","").encode(),token.encode()): return "不正なリクエストです。",403
    try:
        status=request.form.get("status","")
        # Review -> paid is an explicit manual confirmation after checking PayPay.
        shop_store.set_status(order_id,status,note="管理者による状態更新")
    except shop_store.ShopError as exc:
        return str(exc),400
    return redirect("/admin/orders")


@app.get("/console")
def index():
    if not _logged_in():
        return redirect("/login")
    groups = [
        ("items", "アイテム", "選んだものを最大値まで付与します", ITEMS),
        ("qty", "数量指定", "個数を決めて付与します。空欄は変更しません", ITEMS),
        ("chars", "キャラ管理", "キャラを選択し、レベル・形態・本能を1体ずつ設定します", []),
        ("subs", "サブ", "キャラ・ステージなどをまとめて解放・強化します", SUBS),
        ("others", "その他", "会員状態やミッションなどを設定します", OTHERS),
    ]
    totals = {k: len(v) for k, _, _, v in groups}
    return render_template_string(
        MAIN_HTML, groups=groups, totals=totals, version=APP_VERSION,
        limits=engine.QUANTITY_ITEM_LIMITS, high=HIGH_RISK_ITEMS,
        error_nums=sorted(i + DB_OFFSET for i in engine.ERROR_CAT_IDS),
    )


@app.get("/api/state")
def api_state():
    """画面の接続表示用。軽い処理だけを返す。"""
    if not _logged_in():
        return jsonify(error="ログインしてください"), 401
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
    if not _logged_in():
        return jsonify(error="ログインしてください"), 401
    if _warm_cache_lock.acquire(blocking=False):
        _warm_cache.update(status="loading", prepared=0, error=None)
        threading.Thread(target=_warmup_thread, daemon=True).start()
    return jsonify(status=_warm_cache["status"])


@app.get("/api/cats")
def api_cats():
    """キャラ名の一覧（検索用）。未取得なら裏で取りにいく。"""
    if not _logged_in():
        return jsonify(error="ログインしてください"), 401
    _start_names()
    return jsonify(version=_names.get("version"), updated=_names.get("updated"), state=_names["state"], count=len(_names["items"]), method=_names["method"],
                   error=_names["error"], items=_names["items"] if _names["state"] == "ready" else [])


@app.post("/api/cats/latest")
def api_cats_latest():
    if not _logged_in():
        return jsonify(error="ログインしてください"), 401
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
    if not _logged_in():
        return jsonify(error="ログインしてください"), 401
    if _names["state"] in ("ready", "error", "none"):
        _names.update(state="none", items=[], method=None, error=None)
        _start_names()
    return jsonify(state=_names["state"])


@app.post("/run")
def run():
    if not _logged_in():
        return jsonify(error="ログインしてください"), 401
    _gc_jobs()
    data = request.get_json(silent=True) or {}
    transfer = str(data.get("transfer", "")).strip()
    confirm = str(data.get("confirm", "")).strip()
    if bool(transfer) != bool(confirm):
        return jsonify(error="引継ぎコードと暗証番号は「両方入力」か「両方空欄」にしてください。"), 400
    clone = bool(data.get("clone", False))
    integrity_only = bool(data.get("integrity_only", False))
    if integrity_only and clone:
        return jsonify(error="保全チェックとアカウント複製は同時に実行できません。"), 400
    if integrity_only and not (transfer and confirm):
        return jsonify(error="保全チェックには、既存セーブの引継ぎコードと暗証番号が必要です。"), 400
    if clone:  # 実験的な独立アカウント複製：編集項目は使わない。引継ぎコードが必須
        if not (transfer and confirm):
            return jsonify(error="複製には、引継ぎコードと暗証番号が必要です。"), 400
        data = {"clone": True, "dry": data.get("dry", False), "safe": False}
    safe = bool(data.get("safe", True))
    dry = bool(data.get("dry", False))
    if integrity_only:
        safe = False
        data = {"transfer": transfer, "confirm": confirm, "dry": dry}
    items = [x for x in data.get("items", []) if x in ITEMS]
    subs = [x for x in data.get("subs", []) if x in SUBS]
    others = [x for x in data.get("others", []) if x in OTHERS]
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
        for field, low, high in (("base_level", 1, 100), ("plus_level", 0, 999), ("form", 1, 3)):
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
            if k not in ITEMS:
                continue
            sv = str(v).strip()
            if sv == "":
                continue
            if not (sv.isascii() and sv.isdigit()):
                return jsonify(error=f"「{k}」の数量は0以上の整数で入力してください。"), 400
            n = int(sv)
            lim = engine.QUANTITY_ITEM_LIMITS.get(k, 999)
            if n > lim:
                return jsonify(error=f"「{k}」の上限は {lim:,} です。"), 400
            qty[k] = n
    skipped: list[str] = []
    if safe:  # 安全モード：高リスク項目を外す
        skipped = [x for x in HIGH_RISK_ITEMS if x in items or x in qty]
        items = [x for x in items if x not in skipped]
        qty = {k: v for k, v in qty.items() if k not in skipped}
    if not clone and not integrity_only and not (items or subs or others or qty or cat_nums):
        if skipped:
            return jsonify(error="安全モードで高リスク項目（" + "、".join(skipped) + "）を外した結果、実行する項目が残りませんでした。"), 400
        return jsonify(error="項目を選ぶか、数量・キャラ番号を入力してください。"), 400
    job_id = uuid.uuid4().hex
    job = {
        "status": "running", "step": 0, "log": [], "warn": [], "report": [], "skipped": skipped, "dry": dry, "clone": clone, "integrity_only": integrity_only,
        "result": None, "error": None,
        "created": time.time(), "finished": None, "owner": session.setdefault("job_owner", secrets.token_urlsafe(32)),
        "mode": "integrity" if integrity_only else ("download" if confirm else "new"),
        "count": len(items) + len(subs) + len(others) + len(qty) + len(cat_nums),
    }
    transfer_key = hmac.new(app.secret_key.encode(), transfer.encode(), hashlib.sha256).digest() if transfer else None
    arguments = (transfer, confirm, items, subs, others, qty, cat_nums, cat_settings, dry, clone, integrity_only)
    with _jobs_lock:
        if transfer_key is not None and transfer_key in _active_transfers:
            return jsonify(error="この引継ぎコードの処理はすでに実行中です。完了までお待ちください。"), 409
        try:
            slot = _slots.index(None)
        except ValueError:
            if len(_pending_jobs) >= MAX_QUEUED_JOBS:
                return jsonify(error="待機枠がいっぱいです。完了後にもう一度お試しください。"), 429
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


@app.get("/status/<job_id>")
def status(job_id: str):
    if not _logged_in():
        return jsonify(error="ログインしてください"), 401
    _gc_jobs()
    with _jobs_lock:
        job = _jobs.get(job_id)
        position = next((i for i, entry in enumerate(_pending_jobs, 1) if entry[0] == job_id), None)
        slot_number = next((i for i, value in enumerate(_slots, 1) if value == job_id), None)
        parallel = _parallel_state_locked()
        # Copy the mutable fields used below while holding the same lock as
        # worker updates and slot transitions.
        job = dict(job) if job else None
    if not job or not session.get("job_owner") or job.get("owner") != session.get("job_owner"):
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
    if not _logged_in():
        return jsonify(error="ログインしてください"), 401
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job or not session.get("job_owner") or job.get("owner") != session.get("job_owner"):
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
ADMIN_HTML = """<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>管理者</title>
<style>body{font-family:system-ui;max-width:38rem;margin:2rem auto;padding:0 1rem}input,button{font:inherit;padding:.5rem;margin:.25rem 0}form{margin:1rem 0}.msg{color:#a23}</style></head><body>
<a href="/console">戻る</a> ・ <a href="/admin/paypay">PayPay設定</a> ・ <a href="/admin/orders">注文管理</a><h1>管理者モード</h1><p class="msg">{{ msg }}</p>
{% if github_storage %}<p>パスワードのハッシュを非公開GitHubリポジトリに保存しています。</p>
{% if github_bootstrap %}<p class="msg">初期化が済んだら、Renderの AUTH_GITHUB_BOOTSTRAP を削除してください。</p>{% endif %}
{% elif ephemeral %}<p class="msg">保存先は一時領域です。再起動や再デプロイでパスワードの変更が失われる場合があります。</p>
{% else %}<p>パスワードのハッシュを設定したファイルに保存しています。</p>{% endif %}
{% if not unlocked %}
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="unlock">
<label>管理者パスコード <input name="passcode" type="password" autocomplete="current-password" required></label><button>入る</button></form>
{% else %}
<h2>ログイン用パスワード</h2><p>値は表示できません。削除するとそのパスワードでログインしたセッションも終了します。</p>
{% for id, label in passwords %}<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="delete"><input type="hidden" name="id" value="{{ id }}">{{ label }} <button>削除</button></form>{% endfor %}
<h2>追加</h2><form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="add">
<label>名前 <input name="label" maxlength="80" required></label><br><label>パスワード <input name="password" type="password" minlength="8" maxlength="256" autocomplete="new-password" required></label><br><button>追加</button></form>
<form method="post"><input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="action" value="lock"><button>管理者モードを終了</button></form>
{% endif %}</body></html>"""

LOGIN_HTML = r"""<!doctype html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<title>ログイン | にゃんこ代行</title>
<style>
:root{--bg:#e8edf2;--panel:#fff;--ink:#17181c;--muted:#5b6472;--line:#17181c;--accent:#ffc42e;--bad:#d93636}
@media (prefers-color-scheme:dark){:root{--bg:#121418;--panel:#1d2026;--ink:#f1f3f6;--muted:#9aa4b2;--line:#444b57}}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--ink);font-family:"Hiragino Maru Gothic ProN","Hiragino Sans","Noto Sans JP",system-ui,sans-serif}
main{max-width:420px;margin:0 auto;padding:max(12vh,24px) 18px 24px}
.card{background:var(--panel);border:2px solid var(--line);border-radius:16px;box-shadow:0 4px 0 var(--line);padding:22px 18px}
h1{margin:0 0 4px;font-size:22px;font-weight:800}
p{margin:0 0 16px;color:var(--muted);font-size:14px}
input,button{font:inherit;font-size:18px;width:100%;padding:13px 14px;border-radius:12px;border:2px solid var(--line)}
input{background:var(--bg);color:var(--ink)}
input:focus-visible,button:focus-visible{outline:3px solid #2f7df6;outline-offset:2px}
button{margin-top:12px;background:var(--accent);color:#17181c;font-weight:800;box-shadow:0 3px 0 var(--line);cursor:pointer}
button:active{transform:translateY(2px);box-shadow:0 1px 0 var(--line)}
.msg{color:var(--bad);margin-top:12px;font-size:14px;min-height:1.2em}
</style></head><body><main><div class="card">
<h1>にゃんこ代行</h1><p>パスワードを入力してください。</p>
<form method="post"><input type="password" name="password" placeholder="パスワード" autocomplete="current-password" autofocus>
<button>ログイン</button></form><div class="msg" role="alert">{{ msg }}</div>
</div></main></body></html>"""

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
.note{margin:10px 0 0;font-size:12px;color:var(--muted)}
.fail{margin-top:12px;border:2px solid var(--bad);background:var(--badbg);border-radius:14px;padding:12px;font-size:14px;font-weight:700}
.srv{margin-top:12px;font-size:13px;color:var(--muted)}
.srv .row{display:flex;justify-content:space-between;gap:10px;padding:7px 0;border-top:1px solid var(--soft)}
.srv .row b{color:var(--ink);font-weight:700;text-align:right}

/* 下部バー */
.bar{position:fixed;left:0;right:0;bottom:0;z-index:6;background:var(--bg);border-top:2px solid var(--line);
  padding:10px 14px calc(10px + env(safe-area-inset-bottom,0px))}
.bar .in{max-width:640px;margin:0 auto;display:flex;align-items:center;gap:12px}
.bar .info{flex:1;min-width:0;font-size:13px;color:var(--muted);line-height:1.35}
.bar .info b{display:block;color:var(--ink);font-size:15px}
#go{flex:none;min-width:130px;min-height:52px;padding:0 20px;border:2px solid var(--line);border-radius:14px;
  background:var(--accent);color:#17181c;font-size:18px;font-weight:800;cursor:pointer;box-shadow:0 4px 0 var(--line)}
#go:active{transform:translateY(3px);box-shadow:0 1px 0 var(--line)}
#go:disabled{background:var(--soft);color:var(--muted);cursor:default;box-shadow:0 2px 0 var(--line)}
.toast{position:fixed;left:50%;bottom:calc(100px + env(safe-area-inset-bottom,0px));transform:translateX(-50%);z-index:9;
  background:var(--ink);color:var(--bg);padding:9px 16px;border-radius:999px;font-size:14px;font-weight:700;max-width:90vw}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
</style></head><body>

<div class="wrap"><header>
  <div class="top"><h1>にゃんこ代行</h1><a href="/admin">管理者</a><a href="/logout">ログアウト</a></div>
  <div class="pills" aria-live="polite">
    <span class="pill" id="p-conn" data-s="wait"><i class="dot"></i><span>接続を確認中</span></span>
    <span class="pill" id="p-data" data-s="idle"><i class="dot"></i><span>ゲームデータ</span></span>
    <span class="pill" id="p-job" data-s="idle"><i class="dot"></i><span>待機中</span></span>
    <span class="pill" id="p-api" data-s="bad" hidden><i class="dot"></i><span>bcsfe 要確認</span></span>
  </div>
</header></div>

<div class="tabs"><div class="wrap">
  <div class="tablist" role="tablist" aria-label="メニュー">
    <button class="tab" role="tab" id="t-input" aria-controls="v-input" aria-selected="true" data-tab="input">入力</button>
    {% for key, title, desc, lst in groups %}
    <button class="tab" role="tab" id="t-{{ key }}" aria-controls="v-{{ key }}" aria-selected="false" data-tab="{{ key }}">{{ title }}<span class="badge" id="cnt-{{ key }}">0</span></button>
    {% endfor %}
    <button class="tab" role="tab" id="t-status" aria-controls="v-status" aria-selected="false" data-tab="status">状況<span class="live"></span></button>
  </div>
</div></div>

<main class="wrap">

  <section class="view" id="v-input" role="tabpanel" aria-labelledby="t-input">
    <div class="panel">
      <h2>引継ぎ情報</h2>
      <p class="sub">両方空欄にすると、初期アカウントから作ります。</p>
      <label class="field"><span>引継ぎコード</span>
        <input type="text" id="transfer" placeholder="例：aBcD1234e" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false"></label>
      <label class="field"><span>暗証番号</span>
        <input type="text" id="confirm" placeholder="例：1234" autocomplete="off" inputmode="numeric"></label>
      <div class="mode" id="mode"></div>
      <div class="err" id="inputErr" role="alert"></div>
    </div>
    <div class="panel">
      <h2>実行モード</h2>
      <label class="sw"><input type="checkbox" id="safe" checked>
        <span><b>安全モード</b><small>bcsfeの配布元に「BANされることがある」と書かれている項目（猫缶・レアチケ・プラチケ・レジェチケ）を、今回の実行から自動で外します。</small></span></label>
      <label class="sw"><input type="checkbox" id="dry">
        <span><b>試運転（コードを発行しない）</b><small>書き換えまで行い、変更内容だけ見せます。アップロードはしません。</small></span></label>
      <label class="sw"><input type="checkbox" id="clone">
        <span><b>独立アカウント複製（実験機能）</b><small>入力セーブを別データとして読み込み、複製側に新しい問い合わせコードを発行して、元・複製それぞれの引継ぎコードを出します。PONOS非公式の方法で、成功・継続利用は保証されません。まず「試運転」で確認できます。</small></span></label>
      <label class="sw"><input type="checkbox" id="integrity">
        <span><b>セーブ保全チェック（既知異常の自動修復）</b><small>既存セーブを検査し、確認済みのエラーキャラと直接参照だけを修復します。修復不要なら再登録しません。修復が必要な場合は新しい引継ぎコードを発行します。既存セーブの引継ぎコードが必要です。</small></span></label>
    </div>
    <div class="panel">
      <h2>実行前チェック</h2>
      <ul class="checks" id="checks" aria-live="polite"></ul>
    </div>
    <div class="panel">
      <h2>選んだ項目</h2>
      <div class="sum">
        {% for key, title, desc, lst in groups %}
        <div>{{ 'キャラ' if key == 'chars' else title }}<b id="sel-{{ key }}">{{ '0体' if key == 'chars' else '0 / ' ~ totals[key] }}</b></div>
        {% endfor %}
      </div>
    </div>
  </section>

  {% for key, title, desc, lst in groups %}
  <section class="view" id="v-{{ key }}" role="tabpanel" aria-labelledby="t-{{ key }}" hidden>
    <div class="panel">
      <h2>{{ title }}</h2>
      <p class="sub">{{ desc }}</p>
      {% if key == 'qty' %}
      <div class="tools">
        <button type="button" class="btn" id="qfill">すべて最大値</button>
        <button type="button" class="btn" id="qclear">クリア</button>
        <span class="count" id="n-qty">0 / {{ totals[key] }}</span>
      </div>
      <div class="list">
        {% for x in lst %}{% set mx = limits.get(x, 999) %}
        <div class="qrow" data-item="{{ x }}">
          <div class="qname">{{ x }}<small>上限 {{ "{:,}".format(mx) }}</small><em class="dupnote">MAX選択より優先</em></div>
          <input class="q" type="text" inputmode="numeric" autocomplete="off" placeholder="未指定" data-item="{{ x }}" data-max="{{ mx }}" aria-label="{{ x }}の数量">
          <button type="button" class="btn qmax" data-item="{{ x }}">最大</button>
        </div>
        {% endfor %}
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
      {% else %}
      <div class="tools">
        <button type="button" class="btn" data-all="{{ key }}" data-v="1">全選択</button>
        <button type="button" class="btn" data-all="{{ key }}" data-v="0">解除</button>
        <span class="count" id="n-{{ key }}">0 / {{ totals[key] }}</span>
      </div>
      <div class="list">
        {% for x in lst %}<label class="opt"><input type="checkbox" name="{{ key }}" value="{{ x }}"><span>{{ x }}</span></label>{% endfor %}
      </div>
      {% endif %}
    </div>
  </section>
  {% endfor %}

  <section class="view" id="v-status" role="tabpanel" aria-labelledby="t-status" hidden>
    <div class="panel">
      <h2>実行の進み具合</h2>
      <ol class="steps" id="steps">
        <li data-s="idle"><i>1</i>準備</li>
        <li data-s="idle"><i>2</i>取得</li>
        <li data-s="idle"><i>3</i>書き換え</li>
        <li data-s="idle"><i>4</i>発行</li>
      </ol>
      <div class="meta">
        <div>実行枠<b id="job-slot">割り当て確認中</b></div>
        <div>経過時間<b id="elapsed">--:--</b></div>
        <div>最終更新<b id="updated">--:--:--</b></div>
      </div>
      <div id="log" aria-live="polite"></div>
      <button class="btn" id="cancel-queue" type="button" hidden>待機を取り消す</button>
      <div class="warnbox" id="warn" hidden></div>
      <div class="rep" id="rep" hidden></div>
      <div class="fail" id="fail" hidden></div>
      <div class="result" id="res" hidden>
        <h3 id="res-title">完了しました</h3>
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
        <p class="note">この表示は30分で消えます。保存するファイルには引継ぎコードが含まれます。厳重に管理してください。</p>
        <button class="btn" id="download-result" type="button">結果を保存</button>
      </div>
    </div>

    <div class="panel">
      <h2>サーバーの状態</h2>
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
    </div>
  </section>

</main>

<div class="bar"><div class="in">
  <div class="info"><b id="total">選択 0件</b><span id="barmode">初期アカウントから作成</span></div>
  <button id="go" type="button">実行</button>
</div></div>
<div class="toast" id="toast" hidden></div>

<script>
const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
const TOTALS={{ totals|tojson }};
const HIGH={{ high|tojson }};
const ERRNUMS={{ error_nums|tojson }};
const GROUPS=['items','subs','others'];
const TABS=['input','items','qty','chars','subs','others','status'];
const STEP_TEXT=['準備','取得','書き換え','発行'];
let curJob=null, pollTimer=null, tickTimer=null, jobStart=0, jobRunning=false;
let lastOk=0, fails=0, state=null;
let CATS=[], CATSTATE='none', catsTries=0, catsAsked=false;
const NAMEOF=new Map();

/* ---------- タブ ---------- */
function show(t){
  TABS.forEach(k=>{
    const on=k===t;
    $('#v-'+k).hidden=!on;
    $('#t-'+k).setAttribute('aria-selected',on?'true':'false');
  });
  $('#t-'+t).scrollIntoView({block:'nearest',inline:'center'});
  window.scrollTo({top:0}); setTimeout(tabMore,350);
}
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
function qtyMap(){
  const m={}; $$('input.q').forEach(e=>{ if(e.value!=='') m[e.dataset.item]=e.value; }); return m;
}
function refresh(){
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
  $('#sel-qty').textContent=qn+' / '+TOTALS.qty; $('#n-qty').textContent=qn+' / '+TOTALS.qty;
  const maxPicked=new Set(picked('items'));
  $$('.qrow').forEach(r=>{
    const e=r.querySelector('input.q'), v=e.value;
    r.classList.toggle('has',v!=='');
    r.classList.toggle('over',v!==''&&Number(v)>Number(e.dataset.max));
    r.classList.toggle('dup',v!==''&&maxPicked.has(r.dataset.item));
  });
  GROUPS.forEach(g=>{
    const n=picked(g).length; total+=n;
    const b=$('#cnt-'+g); b.textContent=n; b.classList.toggle('on',n>0);
    $('#sel-'+g).textContent=n+' / '+TOTALS[g];
    $('#n-'+g).textContent=n+' / '+TOTALS[g];
  });
  const t=$('#transfer').value.trim(), c=$('#confirm').value.trim();
  let m, bm;
  if(!t&&!c){m='<b>初期アカウント</b>から新しく作ります。'; bm='初期アカウントから作成';}
  else if(t&&c){m=$('#integrity').checked?'<b>引継ぎコード</b>のセーブを保全チェックします。':$('#clone').checked?'<b>引継ぎコード</b>のデータから、元・複製の2アカウントを別々に作成します。':'<b>引継ぎコード</b>のデータを取得して書き換えます。'; bm=$('#integrity').checked?'既存セーブの保全チェック':'引継ぎコードから取得';}
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
$$('input.q').forEach(e=>e.addEventListener('input',()=>{const n=normDigits(e.value); if(n!==e.value)e.value=n; refresh();}));
$$('.qmax').forEach(b=>b.addEventListener('click',()=>{
  const e=document.querySelector('input.q[data-item="'+b.dataset.item+'"]'); e.value=e.dataset.max; refresh();
}));
$('#chars').addEventListener('input',refresh);
$('#charClear').addEventListener('click',()=>{$('#chars').value=''; refresh();});
$('#qfill').addEventListener('click',()=>{$$('input.q').forEach(e=>e.value=e.dataset.max); refresh();});
$('#qclear').addEventListener('click',()=>{$$('input.q').forEach(e=>e.value=''); refresh();});
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
    const fs=el('select'); [['','変更なし'],['1','第1形態'],['2','第2形態'],['3','第3形態']].forEach(([v,t])=>{const o=el('option',null,t);o.value=v;fs.append(o);}); fs.value=cfg.form;
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
    if(r.status===401){location.href='/login'; return;}
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
function goLabel(){ return $('#integrity').checked?($('#dry').checked?'保全チェック（試運転）':'保全チェック'):$('#dry').checked?'試運転':$('#clone').checked?'複製':'実行'; }
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
    if(!t0||!c0) add('bad','保全チェックには、既存セーブの引継ぎコードと暗証番号が必要です。');
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
    else add('warn',risky.join('・')+' は、bcsfeの配布元に「BANされることがある」と書かれている項目です。安全モードをオンにすると外せます。');
  }
  const over=[...document.querySelectorAll('.qrow.over')].map(r=>r.dataset.item);
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
  if(total===0&&!pc.err) add('warn','まだ項目が選ばれていません。');
  if($('#dry').checked) add('ok','試運転：コードは発行せず、変更内容だけ確認します。');
  if(!L.some(x=>x[0]!=='ok')) add('ok','問題は見つかりませんでした。');
  const ul=$('#checks'); ul.textContent='';
  L.forEach(([lv,tx])=>{ const li=document.createElement('li'); li.dataset.lv=lv;
    li.append(el('i',null,lv==='ok'?'✓':lv==='warn'?'!':'✕'),el('span',null,tx)); ul.append(li); });
}
function renderExtras(pc,qm,total,bm){
  renderChecks(pc,qm,total);
  if(!pc.err) renderChips(pc.nums);
  renderResults();
  $('#barmode').textContent=bm+($('#dry').checked?'・試運転':'')+($('#integrity').checked?'・保全チェック':$('#clone').checked?'・複製':($('#safe').checked?'・安全':''));
  if($('#clone').checked) $('#total').textContent='複製モード';
  if($('#integrity').checked) $('#total').textContent='保全チェック';
  if(!jobRunning) $('#go').textContent=goLabel();
}
try{ if(localStorage.getItem('nyanko-safe')==='0') $('#safe').checked=false; }catch(e){}
$('#safe').addEventListener('change',()=>{ try{localStorage.setItem('nyanko-safe',$('#safe').checked?'1':'0')}catch(e){} refresh(); });
$('#dry').addEventListener('change',refresh);
$('#clone').addEventListener('change',()=>{ if($('#clone').checked) $('#integrity').checked=false; refresh(); });
$('#integrity').addEventListener('change',()=>{ if($('#integrity').checked) $('#clone').checked=false; refresh(); });
$('#t-chars').addEventListener('click',()=>{ if(!catsAsked) loadCats(); });

/* ---------- 小物 ---------- */
let toastTimer=null;
function toast(t){const e=$('#toast'); e.textContent=t; e.hidden=false; clearTimeout(toastTimer); toastTimer=setTimeout(()=>e.hidden=true,2200);}
function pad(n){return String(n).padStart(2,'0')}
function mmss(s){s=Math.max(0,Math.floor(s)); return pad(Math.floor(s/60))+':'+pad(s%60)}
function clock(d){return pad(d.getHours())+':'+pad(d.getMinutes())+':'+pad(d.getSeconds())}
function uptime(s){const h=Math.floor(s/3600), m=Math.floor(s%3600/60); return h>0?h+'時間'+m+'分':m+'分'+pad(s%60)+'秒'}
function pill(id,s,text){const e=$(id); e.dataset.s=s; e.lastElementChild.textContent=text;}
async function copyText(text){
  try{await navigator.clipboard.writeText(text); return true}catch(e){
    const a=document.createElement('textarea'); a.value=text; a.style.position='fixed'; a.style.opacity='0';
    document.body.appendChild(a); a.select(); let ok=false; try{ok=document.execCommand('copy')}catch(_){}
    a.remove(); return ok;
  }
}
$$('[data-copy]').forEach(b=>b.addEventListener('click',async()=>{
  const ok=await copyText($('#'+b.dataset.copy).textContent);
  toast(ok?'コピーしました':'コピーできませんでした。長押しで選択してください');
}));

/* ---------- 接続状況 ---------- */
function renderState(){
  const age=Date.now()-lastOk;
  if(!lastOk||fails>=2){
    pill('#p-conn',fails>=2?'bad':'wait',fails>=2?'接続できません':'接続を確認中');
    $('#s-conn').textContent=fails>=2?'切断（休止中なら1分ほどで復帰します）':'確認中';
  }else{
    pill('#p-conn','ok','接続OK'); $('#s-conn').textContent='接続OK';
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
  else if(state.warmup_slots>0) pill('#p-job','wait','実行枠の準備中');
  else pill('#p-job','idle','待機中');
}
let lastCatalogVersion=null;
async function ping(){
  if(document.hidden) return;
  try{
    const r=await fetch('/api/state',{cache:'no-store'});
    if(r.status===401){location.href='/login'; return;}
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
    if(r.status===401){location.href='/login'; return;}
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
function renderJob(j){
  renderSteps(j);
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
  $('#res-title').textContent=j.integrity_only?'保全チェック結果':j.clone?(j.status==='error'?'複製処理の途中結果':'複製処理の結果'):'完了しました';
  const showRes=!!j.result&&!j.dry;
  $('#res').hidden=!showRes;
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
    if(r.status===401){location.href='/login'; return;}
    const j=await r.json();
    if(!r.ok){
      sessionStorage.removeItem('job'); curJob=null; stopJob();
      $('#fail').hidden=false; $('#fail').textContent=j.error||'状況を取得できませんでした';
      return;
    }
    lastOk=Date.now(); fails=0;
    renderJob(j);
    if(j.status!=='running'&&j.status!=='queued'){
      stopJob();
      document.title=(j.status==='done'?(j.dry?'試運転完了 | ':j.clone?'複製完了 | ':'完了 | '):'失敗 | ')+'にゃんこ代行';
      if(j.status==='done'){toast(j.integrity_only?(j.integrity&&j.integrity.upload_required?'保全修復・再登録が完了しました':'保全チェック完了'):j.dry?'試運転が完了しました':j.clone?'複製が完了しました':'完了しました'); if(navigator.vibrate) navigator.vibrate(120);}
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
function watch(id){
  curJob=id; sessionStorage.setItem('job',id);
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
    if(!t||!c){ show('input'); $('#inputErr').textContent='保全チェックには、既存セーブの引継ぎコードと暗証番号の両方が必要です。'; return; }
    if(!$('#dry').checked){
      const accepted=window.confirm('既存セーブを検査し、確認済みのエラーキャラと直接参照だけを修復します。修復対象が見つかった場合は保存データを再登録し、新しい引継ぎコードを発行します。修復対象がなければ再登録しません。BAN回避や安全性を保証する機能ではありません。続行しますか？');
      if(!accepted) return;
    }
  } else if($('#clone').checked){
    if(!t||!c){ show('input'); $('#inputErr').textContent='複製には、引継ぎコードと暗証番号の両方が必要です。'; return; }
    if(!$('#dry').checked){
      const accepted=window.confirm('【実験機能・本番サーバー接続】元アカウントと複製アカウントの2つを別々に登録します。PONOS非公式の方法で、独立プレイや継続利用は保証されません。引継ぎコードは1回限りで、ゲーム内で引継ぎを完了すると元端末のデータが初期化されます。失敗時は元アカウントのコードだけが発行される場合もあります。理解したうえで続行しますか？');
      if(!accepted) return;
    }
  } else {
  if(parseNums($('#chars').value).err){ show('chars'); toast('キャラ番号を確認してください'); return; }
  if(refresh()===0){ toast('項目を選ぶか、数量・キャラ番号を入力してください'); show('items'); return; }
  const bad=document.querySelector('.qrow.over');
  if(bad){ show('qty'); toast('「'+bad.dataset.item+'」が上限を超えています'); bad.scrollIntoView({block:'center'}); return; }
  }
  $('#fail').hidden=true; $('#res').hidden=true; $('#warn').hidden=true; $('#rep').hidden=true; $('#log').textContent='';
  renderSteps(null); $('#elapsed').textContent='00:00'; $('#updated').textContent='--:--:--';
  setRunning(true); show('status');
  $('#log').textContent='開始しました。サーバーが休止していた場合は、最初に1分ほどかかります。';
  try{
    const r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({
      safe:$('#safe').checked, dry:$('#dry').checked, clone:$('#clone').checked, integrity_only:$('#integrity').checked, transfer:t, confirm:c, items:picked('items'), qty:qtyMap(), chars:$('#chars').value, cat_settings:catSettingsPayload(), subs:picked('subs'), others:picked('others')})});
    const j=await r.json();
    if(r.status===401){location.href='/login'; return;}
    if(!r.ok){ setRunning(false); $('#log').textContent=''; $('#fail').hidden=false; $('#fail').textContent=j.error||'エラー'; return; }
    jobStart=Date.now(); watch(j.job);
  }catch(e){
    setRunning(false); $('#log').textContent='';
    $('#fail').hidden=false; $('#fail').textContent='サーバーに接続できませんでした。少し待ってからもう一度お試しください。';
  }
}
$('#go').addEventListener('click',start);

/* ---------- 起動時 ---------- */
refresh(); ping(); tabMore();
const saved=sessionStorage.getItem('job');
if(saved){ show('status'); watch(saved); }
</script></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
