"""にゃんこ代行 Web版 - 書き換えエンジン
元の main.py から、データを書き換える部分だけをそのまま取り出したものです。
Discord・PayPay・売上・ポイントの処理は含まれていません。
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from bcsfe import core

import item_catalog


def _safe_server_error(key, result):
    """Keep transport diagnostics without writing arbitrary server/account data."""
    known = {"password_fail", "auth_token_fail", "save_key_fail",
             "upload_fail_aws", "upload_fail_transfers", "no_internet"}
    category = key if isinstance(key, str) and key in known else "server_error"
    response = getattr(result, "response", None)
    status = getattr(response, "status_code", None)
    status = status if type(status) is int and 100 <= status <= 599 else "unavailable"
    core.core_data.logger.log_error(f"BCSFE {category}: HTTP {status}; request/response details omitted")


# BCSFE's original hook includes raw headers, bodies and signed URLs. Replace it
# before any account operation, in both the web process and spawned workers.
core.ServerHandler.log_error = staticmethod(_safe_server_error)


_TEMPLATE_SAV_FILE = Path(os.path.dirname(os.path.abspath(__file__))) / "template_save.sav"
_TEMPLATE_CACHE_FILE = Path(os.path.dirname(os.path.abspath(__file__))) / "template_save_cache.json"

GAME_DATA_VERSION = "15.7.2"


# --- bcsfe の互換チェック・ゲームバージョンの扱い（自動更新用） -----------------------
# このアプリが実際に使っている bcsfe の入口。bcsfe が更新されて名前や場所が変わると、ここで検知できる。
REQUIRED_CORE_ATTRS = [
    "ServerHandler", "ServerHandler.from_codes", "ServerHandler.create_new_account", "ServerHandler.get_codes",
    "SaveFile", "SaveFile.to_data", "Data", "Data.get_bytes", "Path",
    "CountryCode", "CountryCode.from_code", "GameVersion", "GameVersion.from_string",
    "set_config_path", "set_log_path", "set_data_dir_path", "core_data", "core_data.init_data",
    "Upgrade", "GamatotoLevels", "MissionConditions", "MedalNames", "CatShrineLevels",
    "AbilityData", "Ototo", "Random", "GamatotoMembersName",
]


def check_bcsfe_api() -> list:
    """使っている bcsfe の機能が全部そろっているか調べる。足りないものの一覧を返す（空なら問題なし）。"""
    missing = []
    for path in REQUIRED_CORE_ATTRS:
        obj = core
        try:
            for part in path.split("."):
                obj = getattr(obj, part)
        except AttributeError:
            missing.append("core." + path)
    try:
        import importlib
        cat_cls = importlib.import_module("bcsfe.core.game.catbase.cat").Cat
        for a in ("init", "unlock"):
            if not hasattr(cat_cls, a):
                missing.append("Cat." + a)
    except Exception:
        missing.append("bcsfe.core.game.catbase.cat.Cat")
    return missing


_VERSION_RE = re.compile(r"^\d+\.\d+(\.\d+)?$")


def version_to_string(gv):
    """GameVersion・文字列・整数(150702)のどれからでも '15.7.2' の形にする。できなければ None。"""
    if gv is None:
        return None
    if isinstance(gv, str):
        t = gv.strip()
        return t if _VERSION_RE.match(t) else None
    fn = getattr(gv, "to_string", None)
    if callable(fn):
        try:
            t = fn()
            if isinstance(t, str) and _VERSION_RE.match(t.strip()):
                return t.strip()
        except Exception:
            pass
    raw = gv if isinstance(gv, int) and not isinstance(gv, bool) else getattr(gv, "game_version", None)
    try:
        n = int(raw)
        if n > 0:
            return f"{n // 10000}.{(n // 100) % 100}.{n % 100}"
    except Exception:
        pass
    return None


def version_key(v) -> tuple:
    """'15.7.2' → (15, 7, 2)。比べるための値。読めなければ (0, 0, 0)。"""
    try:
        parts = [int(x) for x in str(v).split(".")][:3]
        return tuple(parts + [0] * (3 - len(parts)))
    except Exception:
        return (0, 0, 0)


QUANTITY_ITEM_LIMITS: dict[str, int] = {
    "猫缶":         50000,
    "XP":           99999999,
    "NP":           9999,
    "にゃんチケ":   2999,
    "レアチケ":     999,
    "プラチケ":     99,
    "プラチナのかけら": 99,
    "レジェチケ":   99,
    "イベチケ&福チケ": 9999,
    "1億DL記念チケット": 9999,
    "バトルアイテム": 9999,
    "ネコビタン":    9999,
    "城素材":        9999,
    "キャッツアイ":  9999,
    "マタタビ":      998,
    "本能玉":        998,
    "リーダーシップ": 9999,
    "地底メダル":    9999,
}


# 種類ごとに個数を指定できる項目。キー表記は「項目名#番号」（番号は1始まり）。
# 名前の並びはセーブ内の配列の並びどおり（item_catalog.py 参照）。
ITEM_TYPE_NAMES: dict[str, list[str]] = {
    "キャッツアイ": item_catalog.CATSEYE_NAMES,
    "マタタビ": item_catalog.CATFRUIT_NAMES,
    "バトルアイテム": item_catalog.BATTLE_ITEM_NAMES,
    "ネコビタン": item_catalog.CATAMIN_NAMES,
    "城素材": item_catalog.BASE_MATERIAL_NAMES,
    "地底メダル": item_catalog.LABYRINTH_MEDAL_NAMES,
    "本能玉": item_catalog.ORB_NAMES,
    "イベチケ&福チケ": item_catalog.EVENT_TICKET_TYPE_NAMES,
}
ITEM_TYPE_COUNTS: dict[str, int] = {k: len(v) for k, v in ITEM_TYPE_NAMES.items()}
# 単純な整数リストとして保存されている項目
_ITEM_TYPE_LIST_ATTRS = {
    "キャッツアイ": "catseyes", "マタタビ": "catfruit",
    "ネコビタン": "catamins", "地底メダル": "labyrinth_medals",
}


def split_type_key(key: str):
    """「マタタビ#3」→ ("マタタビ", 3)。種類指定でなければ None。"""
    base, sep, num = str(key).partition("#")
    if not sep or base not in ITEM_TYPE_COUNTS or not (num.isascii() and num.isdigit()):
        return None
    n = int(num)
    return (base, n) if 1 <= n <= ITEM_TYPE_COUNTS[base] else None


def type_label(key: str) -> str:
    """「マタタビ#29」→「虹獣石」。種類指定でなければそのまま返す。"""
    typed = split_type_key(key)
    return ITEM_TYPE_NAMES[typed[0]][typed[1] - 1] if typed else str(key)


def _base_materials(save):
    return save.ototo.base_materials.materials


def _set_typed_amount(save, base: str, num: int, value: int) -> None:
    """1種類だけ個数を設定する。セーブにその種類の枠が無ければ IndexError。"""
    index = num - 1
    if base in _ITEM_TYPE_LIST_ATTRS:
        values = getattr(save, _ITEM_TYPE_LIST_ATTRS[base])
        if index >= len(values):
            raise IndexError(base)
        values[index] = value
    elif base == "バトルアイテム":
        save.battle_items.items[index].amount = value
    elif base == "城素材":
        _base_materials(save)[index].amount = value
    elif base == "本能玉":
        save.talent_orbs.set_orb(index, value)
    elif base == "イベチケ&福チケ":
        if index == 0:
            save.event_capsules = [value] * len(save.event_capsules)
            if hasattr(save, "event_capsules_2"):
                save.event_capsules_2 = [value] * len(save.event_capsules_2)
        else:
            if not save.lucky_tickets:
                raise IndexError(base)
            save.lucky_tickets = [value] * len(save.lucky_tickets)
    else:
        raise KeyError(base)


def all_orb_ids(save) -> list[int]:
    """付与できる本能玉のID一覧。ゲームデータから読めればそれを使い、
    読めなければ同梱の一覧（item_catalog）を使う。セーブにある既存のIDも必ず含める。"""
    ids: set[int] = set()
    try:
        from bcsfe.core.game.catbase.talent_orbs import OrbInfoList
        info = OrbInfoList.create(save)
        if info is not None:
            ids.update(int(o.raw_orb_info.orb_id) for o in info.orb_info_list)
    except Exception:
        pass
    if not ids:
        ids.update(range(len(item_catalog.ORB_NAMES)))
    try:
        ids.update(int(k) for k in save.talent_orbs.orbs.keys())
    except Exception:
        pass
    return sorted(ids)


def apply_item_amounts(
    save,
    items: "list[str]",
    quantity_map: "dict[str, int] | None" = None,
    quantity_item: "str | None" = None,
    quantity_count: "int | None" = None,
) -> list[str]:
    """アイテム系の付与をまとめて行う（基本代行・全マシ・数量型・代行全適応垢の共通処理）。

    個数の指定が無いときは QUANTITY_ITEM_LIMITS（＝パネルに表示している値）を使う。
    以前は基本代行と代行全適応垢で別々に数値が書かれていて、
    全適応垢の方がにゃんチケ999枚（基本代行は2999枚）のように少ない、
    というズレが8項目で起きていた。ここ1か所に集約してズレを防ぐ。
    失敗した項目は、種類指定なら「虹獣石」のような種類名で返す。
    """
    failures = []
    # 種類ごとの指定は、項目全体の指定より後に適用して優先する
    items = sorted(items, key=lambda x: split_type_key(x) is not None)
    for item in items:
        try:
            typed = split_type_key(item)
            if typed is not None:
                base, num = typed
                v = quantity_map.get(item) if quantity_map else None
                _set_typed_amount(save, base, num, QUANTITY_ITEM_LIMITS[base] if v is None else v)
                continue
            if quantity_map is not None:
                qty = quantity_map.get(item)
            elif quantity_item == item and quantity_count is not None:
                qty = quantity_count
            else:
                qty = None
            v = qty if qty is not None else QUANTITY_ITEM_LIMITS.get(item, 999)

            if item == '猫缶':
                save.catfood = v
            elif item == 'XP':
                save.xp = v
            elif item == 'NP':
                save.np = v
            elif item == 'にゃんチケ':
                save.normal_tickets = v
            elif item == 'レアチケ':
                save.rare_tickets = v
            elif item == 'プラチケ':
                save.platinum_tickets = v
            elif item == 'プラチナのかけら':
                save.platinum_shards = v
            elif item == 'レジェチケ':
                save.legend_tickets = v
            elif item == '1億DL記念チケット':
                save.hundred_million_ticket = v
            elif item == 'イベチケ&福チケ':
                if hasattr(save, 'event_capsules'):
                    save.event_capsules = [v] * len(save.event_capsules)
                if hasattr(save, 'event_capsules_2'):
                    save.event_capsules_2 = [v] * len(save.event_capsules_2)
                if hasattr(save, 'lucky_tickets') and save.lucky_tickets:
                    save.lucky_tickets = [v] * len(save.lucky_tickets)
            elif item == 'バトルアイテム':
                for bi in save.battle_items.items:
                    bi.amount = v
            elif item == 'ネコビタン':
                if hasattr(save, 'catamins'):
                    for i in range(len(save.catamins)):
                        save.catamins[i] = v
            elif item == '城素材':
                if (hasattr(save, 'ototo') and save.ototo
                        and hasattr(save.ototo, 'base_materials') and save.ototo.base_materials
                        and hasattr(save.ototo.base_materials, 'materials')):
                    for material in save.ototo.base_materials.materials:
                        material.amount = v
            elif item == 'キャッツアイ':
                for i in range(len(save.catseyes)):
                    save.catseyes[i] = v
            elif item == 'マタタビ':
                for i in range(len(save.catfruit)):
                    save.catfruit[i] = v
            elif item == '本能玉':
                # 既存の記録だけでなく、全種類の本能玉を付与する（初期アカウントは記録が0件のため）
                for orb_id in all_orb_ids(save):
                    save.talent_orbs.set_orb(orb_id, v)
            elif item == 'リーダーシップ':
                save.leadership = v
            elif item == '地底メダル':
                if hasattr(save, 'labyrinth_medals') and save.labyrinth_medals:
                    save.labyrinth_medals = [v] * len(save.labyrinth_medals)
        except Exception:
            failures.append(type_label(item))
    return failures


# Historical exclusion list shared by individual and bulk unlock/repair paths.
# Changes require checking the current JP catalog and adding a regression test.
ERROR_CAT_IDS = frozenset({
    155, 182, 285, 320, 339, 353,
    432, 433, 465, 492, 497, 498, 499, 500,
    673, 740, 741, 742, 743, 744, 745, 788,
})


def _integer_id(value):
    """Normalize BCSFE integer IDs, including wrappers exposing .value."""
    raw = getattr(value, "value", value)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _cat_internal_id(cat):
    return _integer_id(getattr(cat, "id", None))


def _picture_book_ids(entries, *, displayed_only=True):
    result = set()
    for entry in entries or ():
        if displayed_only and not getattr(entry, "is_displayed_in_catguide", False):
            continue
        cid = _integer_id(getattr(entry, "cat_id", None))
        if cid is not None:
            result.add(cid)
    return result


def unlock_cats_by_ids(save, ids) -> dict:
    """内部ID（0始まり）で指定したキャラだけを解放する。

    戻り値は内部IDのリストの辞書:
      unlocked=今回解放 / already=すでに所持 / missing=セーブに存在しない /
      error_chars=エラーキャラになるので除外 / hidden=図鑑に無いので除外 / failed=解放に失敗
    判定は「全キャラ解放」と同じ（図鑑に表示されるキャラだけを対象にする）。
    """
    res = {k: [] for k in ("unlocked", "already", "missing", "error_chars", "hidden", "failed")}
    try:
        pb = save.cats.read_nyanko_picture_book(save)
        obtainable = _picture_book_ids(pb.cats if pb and pb.cats else ())
    except Exception:
        # Never unlock against an unverified/unknown catalog.
        obtainable = set()

    by_id = {}
    for cat in save.cats.cats:
        cid = _cat_internal_id(cat)
        if cid is not None:
            by_id[cid] = cat

    for cid in ids:
        cat = by_id.get(cid)
        if cat is None:
            res["missing"].append(cid)
        elif cid in ERROR_CAT_IDS:
            res["error_chars"].append(cid)
        elif obtainable is not None and cid not in obtainable:
            res["hidden"].append(cid)
        elif cat.unlocked:
            res["already"].append(cid)
        else:
            try:
                try:
                    cat.unlock(save)
                except Exception:
                    cat.unlocked = 1
                    cat.gatya_seen = 1
                res["unlocked"].append(cid)
            except Exception:
                res["failed"].append(cid)

    if res["unlocked"]:
        try:
            save.unlock_equip_menu()
        except Exception:
            try:
                if hasattr(save, 'menu_unlocks') and len(save.menu_unlocks) > 2:
                    save.menu_unlocks[2] = max(save.menu_unlocks[2], 1)
            except Exception:
                pass
    return res


# 実行前後の比較（変更内容の表示）に使う項目
SNAPSHOT_FIELDS = [
    ("猫缶", "catfood"), ("XP", "xp"), ("NP", "np"),
    ("にゃんチケ", "normal_tickets"), ("レアチケ", "rare_tickets"),
    ("プラチケ", "platinum_tickets"), ("プラチナのかけら", "platinum_shards"),
    ("レジェチケ", "legend_tickets"), ("1億DL記念チケット", "hundred_million_ticket"),
    ("リーダーシップ", "leadership"),
]


def _typed_amounts(save, base: str) -> list[int]:
    """種類ごとの個数を、名前一覧と同じ並びで返す（比較表用）。"""
    if base in _ITEM_TYPE_LIST_ATTRS:
        return [int(n) for n in getattr(save, _ITEM_TYPE_LIST_ATTRS[base])]
    if base == "バトルアイテム":
        return [int(i.amount) for i in save.battle_items.items]
    if base == "城素材":
        return [int(m.amount) for m in _base_materials(save)]
    raise KeyError(base)


# 比較表で種類ごとに表示する項目（本能玉は310種あるので合計と種類数だけにする）
_SNAPSHOT_TYPED = ("キャッツアイ", "マタタビ", "バトルアイテム", "ネコビタン", "城素材", "地底メダル")


def snapshot(save) -> dict:
    """主なアイテム数と所持キャラ数を控える。取れない項目は黙って飛ばす（本処理を止めない）。"""
    out = {}
    for label, attr in SNAPSHOT_FIELDS:
        try:
            out[label] = int(getattr(save, attr))
        except Exception:
            pass
    try:
        out["所持キャラ数"] = sum(1 for c in save.cats.cats if c.unlocked)
    except Exception:
        pass
    for label, read in (
        ("キャラ枠数", lambda: len(save.cats.cats)),
        ("ガチャ表示済みキャラ", lambda: sum(bool(c.gatya_seen) for c in save.cats.cats)),
        ("図鑑登録済みキャラ", lambda: sum(bool(c.catguide_collected) for c in save.cats.cats)),
        ("表示済みポップアップ", lambda: sum(bool(p.seen) for p in save.unlock_popups.popups.values())),
        ("未取得の設定アイテム", lambda: len(save.scheme_items.to_obtain)),
        ("本能玉合計", lambda: sum(int(o.value) for o in save.talent_orbs.orbs.values())),
        ("本能玉の種類数", lambda: sum(1 for o in save.talent_orbs.orbs.values() if int(o.value) > 0)),
        ("イベチケ合計", lambda: sum(int(n) for n in save.event_capsules)),
        ("福引チケット合計", lambda: sum(int(n) for n in save.lucky_tickets)),
    ):
        try:
            out[label] = read()
        except (AttributeError, TypeError, ValueError):
            pass
    for base in _SNAPSHOT_TYPED:
        try:
            amounts = _typed_amounts(save, base)
        except (AttributeError, TypeError, ValueError, KeyError):
            continue
        out[f"{base}合計"] = sum(amounts)
        names = ITEM_TYPE_NAMES[base]
        for index, amount in enumerate(amounts):
            name = names[index] if index < len(names) else f"{base} {index + 1}"
            out[f"{base}：{name}"] = amount
    return out


def _as_name_list(r):
    if r is None:
        return None
    if isinstance(r, str):
        r = r.strip()
        return [r] if r else None
    if isinstance(r, (list, tuple)):
        out = [x.strip() for x in r if isinstance(x, str) and x.strip()]
        return out or None
    for attr in ("names", "name"):
        v = getattr(r, attr, None)
        if v is not None and v is not r:
            got = _as_name_list(v)
            if got:
                return got
    return None


def get_cat_names(save):
    """キャラ名を取り出す。戻り値は ({内部ID: [第1形態名, 第2形態名, ...]}, 使えた方法 or None)。

    bcsfe のバージョンで名前の取り方が違う可能性があるので、複数の呼び方を順に試す。
    個別の名前ファイルが欠けているキャラがあっても、後続キャラの取得は継続する。
    """
    names, used, tried = {}, None, 0
    for cat in save.cats.cats:
        cid = _cat_internal_id(cat)
        if cid is None:
            continue
        forms = None
        for meth in ("get_names_cls", "get_names", "get_cat_names", "get_name"):
            fn = getattr(cat, meth, None)
            if not callable(fn):
                continue
            try:
                try:
                    r = fn(save)
                except TypeError:
                    r = fn()
            except Exception:
                continue
            forms = _as_name_list(r)
            if forms:
                used = used or f"Cat.{meth}"
                break
        if not forms:
            forms = _as_name_list(getattr(cat, "names", None))
            if forms:
                used = used or "Cat.names"
        if forms:
            names[cid] = forms
        else:
            tried += 1
    return names, used


def ensure_cat_roster(save) -> int:
    """最新の図鑑データに合わせてキャラ枠を補完する。

    古いテンプレートセーブに既存キャラが入っていても早期returnせず、
    現在の図鑑に存在するIDのうち不足しているものだけを追加する。
    既存キャラオブジェクト・所持状態は上書きしない。
    """
    try:
        cats = save.cats.cats
        pic_book = save.cats.read_nyanko_picture_book(save)
        entries = pic_book.cats if pic_book and pic_book.cats else []
        if not entries:
            return len(cats)

        existing_ids = {
            cid for cid in (_cat_internal_id(cat) for cat in cats)
            if cid is not None
        }
        missing_ids = []
        for entry in entries:
            cid = _integer_id(getattr(entry, "cat_id", None))
            if cid is None:
                continue
            if cid not in existing_ids:
                missing_ids.append(cid)
                existing_ids.add(cid)

        if missing_ids:
            from bcsfe.core.game.catbase.cat import Cat
            cats.extend(Cat.init(cid) for cid in missing_ids)
            cats.sort(key=lambda cat: (
                _cat_internal_id(cat) is None,
                _cat_internal_id(cat) if _cat_internal_id(cat) is not None else 0,
            ))
            # Routine successful roster completion is intentionally quiet.
        return len(cats)
    except Exception:
        print("[図鑑更新] キャラ枠の補完に失敗しました。")
        try:
            return len(save.cats.cats)
        except Exception:
            return 0


def build_initial_account_handler(cc):
    save_obj = None

    if _TEMPLATE_SAV_FILE.exists():
        try:
            with open(_TEMPLATE_SAV_FILE, "rb") as f:
                raw = f.read()
            save_obj = core.SaveFile(core.Data(raw), cc=cc)
        except Exception as e:
            print(f"[初期垢] template_save.sav 読み込み失敗: {e}")
            save_obj = None
    elif _TEMPLATE_CACHE_FILE.exists():
        print(
            f"[初期垢] {_TEMPLATE_CACHE_FILE.name} が見つかりましたが、"
            "現行bcsfeはJSONからのセーブ復元に対応していないため使用できません。"
            f"{_TEMPLATE_SAV_FILE.name}（生バイトのセーブファイル）を同じフォルダに配置してください。"
        )

    if save_obj is None:
        return None

    try:
        new_inquiry = core.Random.get_hex_string(32)
        for attr in ("inquiry_code", "inquiry", "nyanko_inquiry_code"):
            obj = getattr(save_obj, attr, None)
            if obj is None:
                continue
            try:
                obj.value = new_inquiry
            except Exception:
                setattr(save_obj, attr, new_inquiry)
            break
    except Exception as e:
        print(f"[初期垢] inquiry_code生成失敗: {e}")

    try:
        return core.ServerHandler(save_obj, print=False)
    except TypeError:
        return core.ServerHandler(save_obj)


def claim_user_rank_rewards(save) -> bool:
    """ユーザーランク報酬をすべて受け取り済みにする。成功したら True。"""
    try:
        if not hasattr(save, 'user_rank_rewards') or not save.user_rank_rewards:
            return False
        from bcsfe.core.game.catbase.user_rank_rewards import Reward as _Reward
        rewards_list = save.user_rank_rewards.rewards

        try:
            rank_gifts = core.core_data.get_rank_gifts(save)
            if rank_gifts and rank_gifts.rank_gift:
                max_index = max(rg.index for rg in rank_gifts.rank_gift)
                while len(rewards_list) <= max_index:
                    rewards_list.append(_Reward(True))
        except Exception:
            SAFE_MAX = 2000
            while len(rewards_list) < SAFE_MAX:
                rewards_list.append(_Reward(True))

        for reward in rewards_list:
            reward.claimed = True
        return True
    except Exception as e:
        print(f"Error in claim_user_rank_rewards: {e}")
        return False

def clear_beacon_events(save):
    try:
        if hasattr(save, 'beacon_base') and save.beacon_base:
            for key in save.beacon_base.bool_array.keys():
                save.beacon_base.bool_array[key] = True
    except:
        pass

def clear_item_packs(save):
    try:
        if hasattr(save, 'item_pack') and save.item_pack:
            for key in save.item_pack.displayed_packs.keys():
                save.item_pack.displayed_packs[key] = True
            save.item_pack.three_days_started = False
    except:
        pass

def clear_scheme_items(save):
    try:
        if hasattr(save, 'scheme_items') and save.scheme_items:
            save.scheme_items.to_obtain = []
    except:
        pass

def _auto_run(func, save_file, inputs: list):
    import sys, io
    fallback = (["1", "10", "y", "999", "4", "0"] * 20)
    responses = list(inputs) + fallback
    fake_stdin = io.StringIO("\n".join(str(r) for r in responses) + "\n")
    old_stdin, old_stdout, old_stderr = sys.stdin, sys.stdout, sys.stderr
    sys.stdin  = fake_stdin
    sys.stdout = io.StringIO()
    sys.stderr = io.StringIO()
    try:
        func(save_file)
    except (EOFError, StopIteration, SystemExit):
        return False
    except Exception as e:
        return False
    finally:
        sys.stdin  = old_stdin
        sys.stdout = old_stdout
        sys.stderr = old_stderr
    return True


def clear_all_ads_and_popups(save):
    """既読広告・ポップアップだけを整理する。キャラのガチャ/図鑑状態は変更しない。"""
    try:
        if hasattr(save, 'unlock_popups') and save.unlock_popups:
            for popup_id in list(save.unlock_popups.popups.keys()):
                save.unlock_popups.popups[popup_id].seen = True


        if hasattr(save, 'mysale') and save.mysale:
            for key in list(save.mysale.dict_2.keys()):
                save.mysale.dict_2[key] = True

        if hasattr(save, 'shown_maxcollab_mg'):
            save.shown_maxcollab_mg = True

        if hasattr(save, 'energy_notification'):
            save.energy_notification = False

        if hasattr(save, 'beacon_base') and save.beacon_base:
            for key in save.beacon_base.bool_array.keys():
                save.beacon_base.bool_array[key] = True

        if hasattr(save, 'item_pack') and save.item_pack:
            for key in save.item_pack.displayed_packs.keys():
                save.item_pack.displayed_packs[key] = True
            save.item_pack.three_days_started = False

        if hasattr(save, 'scheme_items') and save.scheme_items:
            save.scheme_items.to_obtain = []

        if hasattr(save, 'show_ban_message'):
            save.show_ban_message = False

    except Exception as e:
        print(f"Error in clear_all_ads_and_popups: {e}")
        pass


MAIN_STORY_ARC_RANGES: dict[str, range] = {
    "日本編": range(0, 3),
    "未来編": range(3, 6),
    "宇宙編": range(6, 9),
}

# StoryChapters.get_real_chapters() uses logical indices 0-8 and skips raw slot 3.
MAIN_STORY_CHAPTER_OPTIONS: dict[str, int] = {
    "日本編 第1章クリア": 0,
    "日本編 第2章クリア": 1,
    "日本編 第3章クリア": 2,
    "未来編 第1章クリア": 3,
    "未来編 第2章クリア": 4,
    "未来編 第3章クリア": 5,
    "宇宙編 第1章クリア": 6,
    "宇宙編 第2章クリア": 7,
    "宇宙編 第3章クリア": 8,
}


def _clear_main_story_chapter(save, chapter_index: int) -> None:
    """指定した日本/未来/宇宙編の1章だけをクリアし、その章のお宝をすべて金にする。"""
    chapters = save.story.get_real_chapters()
    if chapter_index < 0 or chapter_index >= len(chapters):
        raise IndexError("main story chapter is unavailable")
    chapter = chapters[chapter_index]
    chapter.clear_chapter()
    # お宝も選択した章だけに適用する。他章の進行・お宝状態には触れない。
    for stage in chapter.get_valid_treasure_stages():
        treasure = getattr(stage, "treasure", None)
        if treasure is None:
            continue
        try:
            treasure.value = 3
        except Exception:
            try:
                stage.treasure = 3
            except Exception:
                raise


def _clear_bcsfe_chapter(chapter) -> None:
    chapter.chapter_unlock_state = 3
    chapter.clear_progress = len(chapter.stages)
    for stage in chapter.stages:
        stage.clear_times = 1


def _clear_bcsfe_chapters_stars(chapters_stars_list) -> None:
    for chapters_stars in chapters_stars_list:
        for chapter in chapters_stars.chapters:
            _clear_bcsfe_chapter(chapter)


def bcsfe_init(base_path: str = "/tmp/bcsfe_fukusei"):
    """bcsfe の初期化。ゲームデータの読み込みで数秒止まるので、
    必ず asyncio.to_thread 経由で呼ぶこと（直接呼ぶとBot全体が固まる）。"""
    os.makedirs(base_path, exist_ok=True)
    path_obj = core.Path(base_path)
    core.set_config_path(path_obj.add("config.yaml"))
    core.set_log_path(path_obj.add("log.txt"))
    core.core_data.init_data()
    return core.CountryCode.from_code("jp"), core.GameVersion.from_string(GAME_DATA_VERSION)


def max_cat_talents(save, cat, talent_data) -> bool:
    """1体の本能・超本能をすべて最大にする。足りない本能の記録は追加する。

    ゲームデータにそのキャラの本能が無ければ False（何も変更しない）。
    """
    cat_skill = talent_data.get_cat_skill(cat.id) if talent_data else None
    if not cat_skill:
        return False
    from bcsfe.core.game.catbase.cat import Talent
    original = list(cat.talents) if cat.talents is not None else None
    fresh = cat.talents is None
    if fresh:
        cat.talents = []
    existing = {getattr(talent, "id", None) for talent in cat.talents}
    for skill in cat_skill.skills:
        # ID 0 はゲームデータ上の空き枠。新規作成時は従来どおり作るが、
        # 既存の記録へ補うときは空き枠を増やさない。
        if skill.ability_id == 0 and not fresh:
            continue
        if skill.ability_id not in existing:
            # あとから追加された本能（超本能など）も、既存の本能がある場合に補う
            cat.talents.append(Talent(skill.ability_id, 0))
            existing.add(skill.ability_id)
    data = talent_data.get_cat_talents(cat)
    if not data:
        cat.talents = original
        return False
    _, max_levels, _, talent_ids = data
    for index, talent_id in enumerate(talent_ids):
        talent = cat.get_talent_from_id(talent_id)
        if talent is None or index >= len(max_levels):
            raise RuntimeError("talent record is unavailable")
        talent.level = max_levels[index]
    return True


def apply_cat_customizations(save, settings_by_id: dict) -> dict:
    """指定キャラだけを解放し、指定されたレベル・形態・本能を適用する。"""
    result = {"unlocked": [], "already": [], "missing": [], "error_chars": [],
              "hidden": [], "failed": [], "leveled": [], "formed": [],
              "talented": [], "unsupported": []}
    if not isinstance(settings_by_id, dict) or not settings_by_id:
        return result
    ids = sorted({int(cid) for cid in settings_by_id})
    unlock_result = unlock_cats_by_ids(save, ids)
    for key in ("unlocked", "already", "missing", "error_chars", "hidden", "failed"):
        result[key].extend(unlock_result.get(key, []))
    blocked = set(unlock_result.get("missing", []) + unlock_result.get("error_chars", [])
                  + unlock_result.get("hidden", []) + unlock_result.get("failed", []))
    by_id = {_cat_internal_id(cat): cat for cat in save.cats.cats
             if _cat_internal_id(cat) is not None}
    try:
        picture_book = save.cats.read_nyanko_picture_book(save)
    except Exception:
        picture_book = None
    for cid in ids:
        if cid in blocked:
            continue
        cat = by_id.get(cid)
        if cat is None or not getattr(cat, "unlocked", False):
            result["failed"].append(cid)
            continue
        setting = settings_by_id.get(cid) or {}
        # A per-cat customization is transactional: if any requested operation
        # fails, restore this cat's prior upgrade/form/talent state.
        original = {
            "base": getattr(cat.upgrade, "base", None),
            "plus": getattr(cat.upgrade, "plus", None),
            "max_base": getattr(cat.max_upgrade_level, "base", None),
            "max_plus": getattr(cat.max_upgrade_level, "plus", None),
            "current_form": getattr(cat, "current_form", None),
            "unlocked_forms": getattr(cat, "unlocked_forms", None),
            "fourth_form": getattr(cat, "fourth_form", None),
            "talents": list(cat.talents) if cat.talents is not None else None,
            "talent_levels": [(talent, getattr(talent, "level", None))
                              for talent in (cat.talents or [])],
        }
        try:
            base = setting.get("base_level")
            plus = setting.get("plus_level")
            if base is not None or plus is not None:
                from bcsfe.core.game.catbase.powerup import PowerUpHelper
                helper = PowerUpHelper(cat, save)
                max_base = int(helper.get_max_possible_base() or 0)
                max_plus = int(helper.get_max_possible_plus() or 0)
                if base is not None:
                    base = int(base)
                    if base < 1 or base > max_base:
                        raise ValueError(f"基本レベル上限は{max_base}です")
                if plus is not None:
                    plus = int(plus)
                    if plus < 0 or plus > max_plus:
                        raise ValueError(f"プラス値上限は{max_plus}です")
                # Validate the complete level pair before changing either field.
                if base is not None:
                    cat.upgrade.base = base - 1
                    cat.max_upgrade_level.base = max(
                        int(getattr(cat.max_upgrade_level, "base", 0)), base - 1
                    )
                if plus is not None:
                    cat.upgrade.plus = plus
                    cat.max_upgrade_level.plus = max(
                        int(getattr(cat.max_upgrade_level, "plus", 0)), plus
                    )
                result["leveled"].append(cid)
            form = setting.get("form")
            if form is not None:
                form = int(form)
                pb_cat = picture_book.get_cat(cat.id) if picture_book else None
                total_forms = int(getattr(pb_cat, "total_forms", 0) or 0) if pb_cat else 0
                if not pb_cat or form > total_forms:
                    result["unsupported"].append(cid)
                elif form == 4:
                    cat.set_form_true(save, 4, fourth_form=True)
                    result["formed"].append(cid)
                else:
                    cat.set_form_true(save, form, fourth_form=False)
                    if form < 4 and getattr(cat, "fourth_form", 0):
                        cat.fourth_form = 0
                    result["formed"].append(cid)
            if setting.get("talents_all"):
                talent_data = save.cats.read_talent_data(save)
                if max_cat_talents(save, cat, talent_data):
                    result["talented"].append(cid)
                else:
                    result["unsupported"].append(cid)
        except Exception:
            for result_key in ("leveled", "formed", "talented"):
                if cid in result[result_key]:
                    result[result_key].remove(cid)
            try:
                if original["base"] is not None:
                    cat.upgrade.base = original["base"]
                if original["plus"] is not None:
                    cat.upgrade.plus = original["plus"]
                if original["max_base"] is not None:
                    cat.max_upgrade_level.base = original["max_base"]
                if original["max_plus"] is not None:
                    cat.max_upgrade_level.plus = original["max_plus"]
                if original["current_form"] is not None:
                    cat.current_form = original["current_form"]
                if original["unlocked_forms"] is not None:
                    cat.unlocked_forms = original["unlocked_forms"]
                if original["fourth_form"] is not None:
                    cat.fourth_form = original["fourth_form"]
                cat.talents = original["talents"]
                for talent, level in original["talent_levels"]:
                    if level is not None:
                        talent.level = level
            except Exception:
                # The original values are simple save fields; report failure
                # even if a future BCSFE schema prevents complete restoration.
                pass
            result["failed"].append(cid)
    for key in result:
        result[key] = sorted(set(result[key]))
    return result


_FIX_OTHERS = ("ガマトトのクラッシュ修正", "オトート砲台のクラッシュ修正")


def apply_subs_and_others(save, subs_to_apply, others_to_apply):
    """サブ系・その他系の書き換え（元コードそのまま）"""
    failures = []
    for sub in subs_to_apply:
        try:
            if sub == '全キャラ解放&エラーキャラ削除':
                try:
                    try:
                        pb = save.cats.read_nyanko_picture_book(save)
                        if not pb or not pb.cats:
                            raise RuntimeError("picture book unavailable")
                        obtainable_ids = _picture_book_ids(pb.cats)
                    except Exception:
                        obtainable_ids = set()
                        failures.append(sub)
                    for cat in save.cats.cats:
                        cid = _cat_internal_id(cat)
                        if cat.unlocked or cid is None or cid in ERROR_CAT_IDS:
                            continue
                        if cid not in obtainable_ids:
                            continue
                        try:
                            cat.unlock(save)
                        except Exception:
                            cat.unlocked  = 1
                            cat.gatya_seen = 1
                    try:
                        save.unlock_equip_menu()
                    except Exception:
                        if hasattr(save, 'menu_unlocks') and len(save.menu_unlocks) > 2:
                            save.menu_unlocks[2] = max(save.menu_unlocks[2], 1)
                    save.rank_up_sale_value = 0x7FFFFFFF
                except Exception as e:
                    failures.append(sub)
                try:
                    removed = 0
                    for _cat in list(save.cats.cats):
                        _cid = _cat_internal_id(_cat)
                        if _cid not in ERROR_CAT_IDS:
                            continue
                        try:
                            _cat.remove(reset=True, save_file=save)
                        except Exception:
                            try:
                                _cat.remove(save_file=save)
                            except Exception:
                                for _la in ('unlocked', 'is_unlocked', 'owned'):
                                    try:
                                        setattr(_cat, _la, False)
                                        break
                                    except Exception:
                                        pass
                        try:
                            _cat.upgrade.base.value = 0
                        except Exception:
                            try:
                                _cat.upgrade.base = 0
                            except Exception:
                                pass
                        removed += 1
                    # Routine successful cleanup is intentionally quiet.
                except Exception as e:
                    failures.append(sub)

            elif sub == '全キャラレベルMAX':
                try:
                    from bcsfe.core.game.catbase.powerup import PowerUpHelper as _PUH
                    for cat in [c for c in save.cats.cats if c.unlocked]:
                        try:
                            pu = _PUH(cat, save)
                            max_base = pu.get_max_possible_base()
                            max_plus = pu.get_max_possible_plus()
                            if max_base <= 0:
                                max_base = 30
                            if max_plus <= 0:
                                max_plus = pu.get_max_max_plus_upgrade_level()
                            cat.upgrade.base = max_base - 1
                            cat.upgrade.plus = max_plus
                            cat.max_upgrade_level.base = max_base - 1
                            cat.max_upgrade_level.plus = max_plus
                            cat.catseyes_used = 0
                        except Exception:
                            failures.append(sub)
                    save.rank_up_sale_value = 0x7FFFFFFF
                except Exception as e:
                    failures.append(sub)

            elif sub == '全キャラ最高形態':
                try:
                    pb = save.cats.read_nyanko_picture_book(save)
                    for cat in [c for c in save.cats.cats if c.unlocked]:
                        pbc = pb.get_cat(cat.id)
                        if not pbc:
                            continue
                        cat.set_form_true(save, pbc.total_forms, fourth_form=True)
                except Exception as e:
                    failures.append(sub)
            elif sub == '全キャラ本能解放':
                try:
                    td = save.cats.read_talent_data(save)
                    if not td:
                        raise RuntimeError("talent data unavailable")
                    for cat in [c for c in save.cats.cats if c.unlocked]:
                        try:
                            max_cat_talents(save, cat, td)
                        except Exception:
                            failures.append(sub)
                except Exception as e:
                    failures.append(sub)

            elif sub in MAIN_STORY_CHAPTER_OPTIONS:
                try:
                    _clear_main_story_chapter(save, MAIN_STORY_CHAPTER_OPTIONS[sub])
                except Exception:
                    failures.append(sub)

            elif sub == '全ステージクリア&全お宝金':
                try:
                    try:
                        if not _auto_run(core.game.map.story.StoryChapters.clear_story, save, ["10"]):
                            failures.append(sub)
                    except Exception as _e:
                        failures.append(sub)
                    try:
                        if not _auto_run(core.game.map.story.StoryChapters.edit_treasures, save,
                                         ["0"] * 20 + ["3"] * 20):
                            failures.append(sub)
                    except Exception as _e:
                        failures.append(sub)
                    try:
                        for _ch in save.story.get_real_chapters():
                            for _st in _ch.get_valid_treasure_stages():
                                _tv = getattr(_st, 'treasure', None)
                                if _tv is None:
                                    continue
                                try:
                                    _tv.value = 3
                                except Exception:
                                    try:
                                        setattr(_st, 'treasure', 3)
                                    except Exception:
                                        pass
                    except Exception as _e:
                        failures.append(sub)
                except Exception as e:
                    failures.append(sub)

            elif sub == 'ゾンビステージクリア':
                try:
                    outbreaks = save.outbreaks
                    from bcsfe.core.game.map.outbreaks import Chapter as ObChapter, Outbreak

                    STAGE_COUNT = 48
                    for true_id in range(9):
                        raw_id = true_id if true_id < 3 else true_id + 1
                        if raw_id not in outbreaks.chapters:
                            outbreaks.chapters[raw_id] = ObChapter(
                                raw_id, {i: Outbreak(True) for i in range(STAGE_COUNT)}
                            )
                        else:
                            ch = outbreaks.chapters[raw_id]
                            for ob in ch.outbreaks.values():
                                ob.cleared = True
                            for i in range(STAGE_COUNT):
                                if i not in ch.outbreaks:
                                    ch.outbreaks[i] = Outbreak(True)

                    for chapter in outbreaks.current_outbreaks.values():
                        for stage in chapter.outbreaks.values():
                            stage.cleared = False

                except Exception as e:
                    failures.append(sub)

            elif sub == '旧レジェンドステージクリア':
                try:
                    if hasattr(save, 'event_stages') and len(save.event_stages.chapters) > 0:
                        es = save.event_stages
                        type_i = 0
                        for mi, ms in enumerate(es.chapters[type_i].chapters):
                            for si, sc in enumerate(ms.chapters):
                                sc.chapter_unlock_state = 3
                                sc.clear_progress = len(sc.stages)
                                for si2 in range(len(sc.stages)):
                                    es.clear_stage(
                                        type_i, mi, si, si2,
                                        clear_amount=1, overwrite_clear_progress=True
                                    )
                except Exception as e:
                    failures.append(sub)

            elif sub == '真レジェンドステージクリア':
                try:
                    if hasattr(save, 'uncanny') and save.uncanny:
                        uncanny = save.uncanny
                        chaps = uncanny.chapters
                        for mi, map_cs in enumerate(chaps.chapters):
                            if mi < len(uncanny.unknown):
                                uncanny.unknown[mi] = max(1, uncanny.unknown[mi])
                            for chapter in map_cs.chapters:
                                chapter.chapter_unlock_state = 3
                                chapter.clear_progress = len(chapter.stages)
                                for stage in chapter.stages:
                                    stage.clear_times = 1
                except Exception as e:
                    failures.append(sub)

            elif sub == 'ゼロレジェンドステージクリア':
                try:
                    if hasattr(save, 'zero_legends') and save.zero_legends:
                        zl = save.zero_legends
                        for mc in zl.chapters:
                            for chapter in mc.chapters:
                                chapter.unlock_state = 3
                                chapter.clear_progress = len(chapter.stages)
                                for stage in chapter.stages:
                                    stage.clear_stage(1)
                except Exception as e:
                    failures.append(sub)

            elif sub == '魔界編全クリア':
                try:
                    if hasattr(save, 'aku') and save.aku and hasattr(save.aku, 'chapters'):
                        for chapters_stars in save.aku.chapters:
                            for chapter in chapters_stars.chapters:
                                for stage in chapter.stages:
                                    stage.clear_stage(1)
                except Exception as e:
                    failures.append(sub)

            elif sub == 'にゃんこ塔全クリア':
                try:
                    tower = save.tower
                    for chapters_stars in tower.chapters.chapters:
                        for chapter in chapters_stars.chapters:
                            chapter.chapter_unlock_state = 3
                            chapter.clear_progress = len(chapter.stages)
                            for stage in chapter.stages:
                                stage.clear_times = 1
                    for i in range(len(tower.item_obtain_states)):
                        for j in range(len(tower.item_obtain_states[i])):
                            tower.item_obtain_states[i][j] = True
                except Exception as e:
                    failures.append(sub)

            elif sub == 'イベントステージ全クリア':
                try:
                    AKU_UNLOCK_MAP_IDS = {255, 256, 257, 258, 265, 266, 268}
                    if hasattr(save, 'event_stages'):
                        es = save.event_stages
                        total_types = len(es.chapters)
                        for type_idx in range(1, total_types):
                            for mi, ms in enumerate(es.chapters[type_idx].chapters):
                                if type_idx == 1 and mi in AKU_UNLOCK_MAP_IDS:
                                    continue
                                for si, sc in enumerate(ms.chapters):
                                    for si2 in range(len(sc.stages)):
                                        es.clear_stage(
                                            type_idx, mi, si, si2,
                                            clear_amount=1, overwrite_clear_progress=True
                                        )
                except Exception as e:
                    failures.append(sub)
            elif sub == 'ガマトトLvMax':
                try:
                    levels = core.GamatotoLevels(save)
                    max_lv = levels.get_max_level()
                    if max_lv:
                        save.gamatoto.xp = levels.get_xp_from_level(max_lv)
                except Exception as e:
                    failures.append(sub)
            elif sub == 'ガマトト助手全員レジェンド化':
                try:
                    from bcsfe.core.game.gamoto.gamatoto import Helper, Helpers
                    m_name = core.GamatotoMembersName(save)
                    g_levels = core.GamatotoLevels(save)
                    if m_name.members:
                        max_rarity = max(m.rarity for m in m_name.members)
                        legends = m_name.get_all_rarity(max_rarity)
                        if legends:
                            max_h = g_levels.get_total_helpers() or 10
                            h_list = []
                            for i in range(max_h):
                                member = legends[i % len(legends)]
                                h_list.append(Helper(member.member_id))
                            save.gamatoto.helpers = Helpers(h_list)
                            save.gamatoto.return_flag = True
                except Exception as e:
                    failures.append(sub)

            elif sub == 'にゃんこ神社LvMax':
                try:
                    if hasattr(save, 'cat_shrine') and save.cat_shrine:
                        shrine_levels = core.CatShrineLevels(save)
                        max_xp = shrine_levels.get_max_xp()
                        max_lvl = shrine_levels.get_max_level()
                        if max_xp and max_lvl:
                            save.cat_shrine.xp_offering = max_xp
                            save.cat_shrine.dialogs = max_lvl - 1
                            save.cat_shrine.shrine_gone = False
                            save.cat_shrine.stamp_1 = 0.0
                            save.cat_shrine.stamp_2 = 0.0
                except Exception as _e:
                    failures.append(sub)
        except Exception as _e:
            failures.append(sub)

    # 砲台のクラッシュ修正は砲台を初期化するので、「施設全強化」より先に行う
    others_to_apply = sorted(others_to_apply, key=lambda x: 0 if x in _FIX_OTHERS else 1)
    for other in others_to_apply:
        try:
            if other == 'ガマトトのクラッシュ修正':
                save.gamatoto.skin = 2
            elif other == 'オトート砲台のクラッシュ修正':
                from bcsfe.core.game.gamoto.ototo import Cannons
                save.ototo.cannons = Cannons.init(save.game_version)
            elif other == 'チュートリアルクリア':
                core.StoryChapters.clear_tutorial(save)
            elif other == 'ユーザーランク報酬全受け取り':
                if not claim_user_rank_rewards(save):
                    failures.append(other)
            elif other == 'プレイ時間カンスト':
                if hasattr(save, 'officer_pass') and save.officer_pass and hasattr(save.officer_pass, 'play_time'):
                    save.officer_pass.play_time = 2147483647
            elif other == 'ゴールド会員化':
                if hasattr(save, 'officer_pass') and save.officer_pass and hasattr(save.officer_pass, 'gold_pass'):
                    import random
                    officer_id = random.randint(1, 2**16 - 1)
                    save.officer_pass.gold_pass.get_gold_pass(officer_id, 365, save)
            elif other == '編成スロット最大':
                if hasattr(save, 'lineups') and save.lineups:
                    save.lineups.unlocked_slots = save.lineups.slot_names_length
            elif other == 'にゃんこメダル全解放':
                if hasattr(save, 'medals') and save.medals:
                    try:
                        medal_names = core.MedalNames(save)
                        if medal_names.medal_names:
                            for medal_id, medal in enumerate(medal_names.medal_names):
                                if not medal:
                                    continue
                                save.medals.add_medal(medal_id)
                    except Exception as e:
                        failures.append(other)
            elif other == '敵キャラ図鑑全埋め':
                for i in range(len(save.enemy_guide)):
                    save.enemy_guide[i] = 1

            elif other == '施設全強化':
                try:
                    skills = save.special_skills
                    ability_data = core.AbilityData(save)
                    if ability_data.ability_data:
                        valid_skills = skills.get_valid_skills()
                        for i, skill in enumerate(valid_skills):
                            ability = ability_data.get_ability_data_item(i)
                            if ability is None:
                                continue
                            skills.set_upgrade(
                                i,
                                core.Upgrade(
                                    ability.max_plus_level,
                                    ability.max_base_level - 1,
                                ),
                                max_base=ability.max_base_level - 1,
                                max_plus=ability.max_plus_level,
                            )
                    for skill in skills.skills:
                        skill.seen = 1
                except Exception as e:
                    failures.append(other)

                try:
                    if hasattr(save, 'ototo'):
                        save.ototo.engineers = core.Ototo.get_max_engineers(save)
                        if save.ototo.cannons is not None:
                            from bcsfe.core.game.gamoto.ototo import CastleRecipeUnlock, Cannon as _Cannon
                            from collections import defaultdict
                            cannon_recipe = CastleRecipeUnlock(save)
                            _cannon_parts: dict = defaultdict(int)
                            if cannon_recipe.level_part_recipe_unlocks is not None:
                                _cannon_parts: dict = defaultdict(int)
                                for _r in cannon_recipe.level_part_recipe_unlocks:
                                    _cannon_parts[_r.cannon_id] = max(
                                        _cannon_parts[_r.cannon_id], _r.part_id
                                    )
                                for _cid, _max_pid in _cannon_parts.items():
                                    if _cid not in save.ototo.cannons.cannons:
                                        save.ototo.cannons.cannons[_cid] = _Cannon(
                                            3, [0] * (_max_pid + 1)
                                        )
                            for cannon_id, cannon in save.ototo.cannons.cannons.items():
                                cannon.development = 3
                                if cannon_recipe.level_part_recipe_unlocks is not None:
                                    _max_pid = _cannon_parts.get(cannon_id, len(cannon.levels) - 1)
                                    while len(cannon.levels) < _max_pid + 1:
                                        cannon.levels.append(0)
                                for part_id in range(len(cannon.levels)):
                                    max_lv = cannon_recipe.get_max_level(cannon_id, part_id)
                                    if not max_lv:
                                        continue
                                    cannon.levels[part_id] = max_lv - 1 if part_id == 0 else max_lv
                        if hasattr(save.ototo, 'base_materials'):
                            # 「城素材」で指定した個数を減らさない（999未満のときだけ補充する）
                            for material in save.ototo.base_materials.materials:
                                material.amount = max(int(material.amount), 999)
                except Exception as e:
                    failures.append(other)

            elif other == 'ミッション全クリア':
                try:
                    missions = save.missions
                    m_conditions = core.MissionConditions(save)
                    for m_id in list(missions.clear_states.keys()):
                        missions.clear_states[m_id] = 2
                        condition = m_conditions.get_condition(m_id)
                        if condition:
                            missions.requirements[m_id] = condition.progress_count
                except Exception as e:
                    failures.append(other)
        except Exception as _e:
            failures.append(other)

    return list(dict.fromkeys(failures))
