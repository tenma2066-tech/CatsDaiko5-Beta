"""料金の計算（サーバー側の正）。画面（templates/main.html の renderCart）の見積もりはこれに合わせる。
金額が画面とずれないことは test_price_parity.py（ブラウザ）で確かめる。料金そのものは管理画面で変える（PRICES を書き換える）。
app.py から移した（2026-11-23、docs/AUDIT_PLAN.md C1）。app.py からも同じ名前で使える。"""
import json
import os

import engine

PRICES = {"chara": 50, "crash": 50, "ban_guarantee": 500, "clone": 100, "other": 25, "playtime10": 10, "restart": 50,
          # ツムツム代行（別の大メニュー。1メニューごとの料金）
          "tsum_coin": 50, "tsum_coin_max": 50, "tsum_level_max": 50, "tsum_score": 50, "tsum_score_max": 50,
          "tsum_tsum_lv": 50, "tsum_tsum_lv_pick": 50, "tsum_all_lv": 300, "tsum_medal": 50, "tsum_medal_max": 50,
          "tsum_nickname": 50, "tsum_box": 100, "tsum_premium": 100, "tsum_guest_create": 30,
          # ぷにぷに代行（3つ目の大メニュー）
          "puni_account": 50, "puni_aged": 100, "puni_farm_10": 50, "puni_hour": 100,
          "puni_hitodama": 50, "puni_coin_gacha": 60, "puni_stage_clear": 50,
          "puni_coin_gacha_normal": 50, "puni_missions": 30, "puni_presents": 30,
          "puni_deck": 30, "puni_level_open": 50}
PRICE_LABELS = {"chara": "キャラ指定（1体・1項目ごと）", "crash": "クラッシュ解除（1件）", "ban_guarantee": "バン保証",
                "clone": "アカウント複製", "other": "それ以外のメニュー（1件）", "playtime10": "プレイ時間（10分ごと）",
                "restart": "初期垢作成（リスタートパック・基本料金）",
                "tsum_coin": "ツム：コイン指定", "tsum_coin_max": "ツム：コインMAX",
                "tsum_level_max": "ツム：プレイヤーレベルMAX", "tsum_score": "ツム：スコア指定",
                "tsum_score_max": "ツム：スコアMAX", "tsum_tsum_lv": "ツム：ツムレベルMAX",
                "tsum_tsum_lv_pick": "ツム：指定ツムをレベルMAX", "tsum_all_lv": "ツム：全ツムをレベルMAX",
                "tsum_medal": "ツム：メダル指定", "tsum_medal_max": "ツム：メダルMAX", "tsum_nickname": "ツム：ニックネーム変更",
                "tsum_box": "ツム：セレクトBOX完売",
                "tsum_premium": "ツム：プレミアム完売", "tsum_guest_create": "ツム：ゲストアカウント生成",
                "puni_account": "ぷに：アカウント作成（1件）", "puni_aged": "ぷに：久垢作成（1件）",
                "puni_farm_10": "ぷに：自動周回（10周ごと）", "puni_hour": "ぷに：回し放題（1時間ごと）",
                "puni_hitodama": "ぷに：ひとだま補充", "puni_coin_gacha": "ぷに：コインガチャ回し切り",
                "puni_stage_clear": "ぷに：ステージクリア（1マップ単発）",
                "puni_coin_gacha_normal": "ぷに：通常コインガチャ回し切り", "puni_missions": "ぷに：ミッション報酬受取",
                "puni_presents": "ぷに：プレゼント受取", "puni_deck": "ぷに：最強編成", "puni_level_open": "ぷに：レベル上限解放"}
PT_KEY = "プレイ時間+分"          # qty に入れる「プレイ時間を増やす（分）」の特別なキー
PT_MIN_MINUTES = 60
PT_MAX_MINUTES = 2147483647 // (30 * 60)   # セーブの上限（1/30秒単位の int）を超えない
RESTART_KEY = "初期垢作成（リスタートパック）"   # others に入れる特別な項目
COUPON_MIN = 200                  # 200円以上の購入で発行・利用
COUPON_DAYS = 30

CRASH_ITEMS = ("ガマトトのクラッシュ修正", "オトート砲台のクラッシュ修正")

def _one_legend_tier(subs):
    """同じレジェンドの段階は、いちばん上だけを残す（下の段階は含まれるので、二重に料金を取らない）。"""
    best = {}
    for x in subs:
        t = engine.LEGEND_TIERS.get(x)
        if t and (t[0] not in best or t[1] > engine.LEGEND_TIERS[best[t[0]]][1]):
            best[t[0]] = x
    return [x for x in subs if x not in engine.LEGEND_TIERS or best.get(engine.LEGEND_TIERS[x][0]) == x]

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

DB_OFFSET = 1

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


def compute_tsum_amount(menus, qty=None) -> tuple[int, list]:
    """ツムツム代行の金額を決める（サーバー側の正）。戻り値は (合計, 内訳)。
    メニュー1件ごとに料金がかかる。数量（コインの枚数など）では値段は変わらない。"""
    import tsum_service
    rows = []
    for key in tsum_service.sort_menus(menus or []):
        price = PRICES.get(tsum_service.MENUS[key]["price"], 0)
        rows.append((f"ツムツム：{tsum_service.menu_label(key)}", 1, price))
    return sum(n * p for _, n, p in rows), rows


def compute_puni_amount(menus, qty=None) -> tuple[int, list]:
    """ぷにぷに代行の金額を決める（サーバー側の正）。戻り値は (合計, 内訳)。
    puni_farm は 10周ごとに1件、puni_unlimited は 1時間ごとに1件、
    puni_account/puni_aged は作成件数ぶん、ほかは 1件固定。
    件数の計算規則そのものは puni_service.pricing_units が決める。"""
    import puni_service
    rows = []
    for price_key, units in puni_service.pricing_units(menus or [], qty or {}):
        label = puni_service.MENUS.get(
            next((k for k, v in puni_service.MENUS.items() if v["price"] == price_key), ""), {}
        ).get("label") or PRICE_LABELS.get(price_key, price_key)
        price = PRICES.get(price_key, 0)
        rows.append((f"ぷにぷに：{label}", units, price))
    return sum(n * p for _, n, p in rows), rows
