"""Read-only structural checks for Battle Cats save objects.

The checks are deliberately limited to documented BCSFE object shapes and
basic data integrity. They do not infer private server-side enforcement rules.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


MAX_CAT_RECORDS = 10_000
MAX_INTERNAL_CAT_ID = 100_000
MAX_AUDIT_FINDINGS = 500
CURRENCY_FIELDS = (
    "catfood", "xp", "np", "normal_tickets", "rare_tickets",
    "platinum_tickets", "legend_tickets", "leadership", "platinum_shards",
)
NONNEGATIVE_LIST_FIELDS = (
    "catfruit", "catseyes", "catamins", "lucky_tickets",
    "event_capsules", "event_capsules_2", "labyrinth_medals",
)
CAT_INTEGER_FIELDS = (
    "unlocked", "current_form", "unlocked_forms", "gatya_seen",
    "fourth_form", "catseyes_used",
)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    path: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class AuditReport:
    findings: tuple[Finding, ...]
    cats_checked: int

    @property
    def critical_count(self) -> int:
        return sum(item.severity == "critical" for item in self.findings)

    @property
    def warning_count(self) -> int:
        return sum(item.severity == "warning" for item in self.findings)

    @property
    def ok(self) -> bool:
        return self.critical_count == 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "cats_checked": self.cats_checked,
            "critical_count": self.critical_count,
            "warning_count": self.warning_count,
            "findings": [item.as_dict() for item in self.findings],
        }


def _integer(value: Any) -> int | None:
    """Read an integer without silently truncating floats or accepting strings."""
    raw = getattr(value, "value", value)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _add(findings: list[Finding], code: str, severity: str,
         path: str, message: str) -> None:
    # Bound report size for malformed saves with thousands of repeated faults.
    # Hitting the cap is itself critical: callers must not treat a truncated
    # inspection as a clean save or upload it.
    if len(findings) >= MAX_AUDIT_FINDINGS:
        if not any(item.code == "audit_findings_limit_exceeded" for item in findings):
            findings.append(Finding(
                "audit_findings_limit_exceeded", "critical", "save",
                "異常項目が多すぎるため検査結果を制限しました。安全のため保存を中止してください。",
            ))
        return
    findings.append(Finding(code, severity, path, message))


def _audit_integer_list(findings: list[Finding], value: Any, path: str,
                        *, nonnegative: bool = True) -> None:
    if not isinstance(value, (list, tuple)):
        _add(findings, "invalid_list_field", "critical", path,
             "一覧データがリスト形式ではありません。")
        return
    for index, item in enumerate(value):
        number = _integer(item)
        if number is None:
            _add(findings, "invalid_list_value", "critical",
                 f"{path}[{index}]", "一覧内の値が整数ではありません。")
        elif nonnegative and number < 0:
            _add(findings, "negative_list_value", "critical",
                 f"{path}[{index}]", "数量一覧に負の値があります。")


def _audit_upgrade(findings: list[Finding], upgrade: Any, path: str) -> None:
    if upgrade is None:
        _add(findings, "missing_upgrade_object", "critical", path,
             "強化情報オブジェクトがありません。")
        return
    for field in ("base", "plus"):
        value = _integer(getattr(upgrade, field, None))
        if value is None:
            _add(findings, "invalid_upgrade_value", "critical",
                 f"{path}.{field}", "強化値が整数として読み取れません。")


def _audit_cat_records(findings: list[Finding], records: Any,
                       known_error_cat_ids: set[int] | None = None) -> int:
    if not isinstance(records, (list, tuple)):
        _add(findings, "invalid_cat_collection", "critical",
             "save.cats.cats", "キャラクター一覧がリスト形式ではありません。")
        return 0

    count = len(records)
    if count > MAX_CAT_RECORDS:
        _add(findings, "cat_collection_too_large", "critical",
             "save.cats.cats",
             f"キャラクター記録が検査上限 {MAX_CAT_RECORDS} 件を超えています。")
        return count
    if count == 0:
        _add(findings, "empty_cat_collection", "warning",
             "save.cats.cats",
             "キャラクター一覧が空です。対象バージョンを確認してください。")

    seen: dict[int, int] = {}
    for index, cat in enumerate(records):
        path = f"save.cats.cats[{index}]"
        if cat is None:
            _add(findings, "null_cat_record", "critical", path,
                 "キャラクター一覧に空のレコードがあります。")
            continue

        cid = _integer(getattr(cat, "id", None))
        if cid is None:
            _add(findings, "invalid_cat_id", "critical", path + ".id",
                 "キャラクターIDが整数として読み取れません。")
        elif cid < 0 or cid > MAX_INTERNAL_CAT_ID:
            _add(findings, "cat_id_out_of_range", "critical", path + ".id",
                 "キャラクターIDが検査上の許容範囲外です。")
        else:
            if cid in seen:
                _add(findings, "duplicate_cat_id", "critical", path + ".id",
                     f"内部ID {cid} が {seen[cid]} 番目のレコードと重複しています。")
            else:
                seen[cid] = index
            # BCSFE reads the cat collection position as the internal ID.
            if cid != index:
                _add(findings, "cat_id_position_mismatch", "critical",
                     path + ".id",
                     f"一覧位置 {index} と内部ID {cid} が一致しません。")

        for field in CAT_INTEGER_FIELDS:
            number = _integer(getattr(cat, field, None))
            if number is None:
                _add(findings, "invalid_cat_field", "critical",
                     f"{path}.{field}", "キャラクター状態が整数ではありません。")

        unlocked = _integer(getattr(cat, "unlocked", None))
        if (cid is not None and unlocked not in (None, 0)
                and known_error_cat_ids is not None and cid in known_error_cat_ids):
            _add(findings, "known_error_cat_unlocked", "warning",
                 path + ".unlocked",
                 "既知のエラーキャラ候補が所持状態です。関連情報を確認し、現時点では自動削除しません。")
        if unlocked is not None and unlocked not in (0, 1):
            _add(findings, "unusual_unlocked_flag", "warning",
                 path + ".unlocked",
                 "所持フラグが通常の0/1以外です。自動変更せず確認対象にします。")

        current_form = _integer(getattr(cat, "current_form", None))
        if current_form is not None and not 0 <= current_form <= 3:
            _add(findings, "unusual_current_form", "warning",
                 path + ".current_form",
                 "形態値が既知の範囲外です。バージョン依存の可能性があるため保留します。")

        unlocked_forms = _integer(getattr(cat, "unlocked_forms", None))
        if unlocked_forms is not None and not 0 <= unlocked_forms <= 3:
            _add(findings, "unusual_unlocked_forms", "warning",
                 path + ".unlocked_forms",
                 "解放形態値が既知の範囲外です。自動変更せず確認対象にします。")

        _audit_upgrade(findings, getattr(cat, "upgrade", None), path + ".upgrade")
        _audit_upgrade(findings, getattr(cat, "max_upgrade_level", None),
                       path + ".max_upgrade_level")

        collected = getattr(cat, "catguide_collected", None)
        if not isinstance(collected, bool):
            _add(findings, "invalid_catguide_flag", "critical",
                 path + ".catguide_collected",
                 "図鑑登録フラグが真偽値ではありません。")

        talents = getattr(cat, "talents", None)
        if talents is not None:
            if not isinstance(talents, (list, tuple)):
                _add(findings, "invalid_talent_collection", "critical",
                     path + ".talents", "本能データがリスト形式ではありません。")
            else:
                talent_ids: set[int] = set()
                for ti, talent in enumerate(talents):
                    tpath = f"{path}.talents[{ti}]"
                    if talent is None:
                        _add(findings, "null_talent_record", "critical", tpath,
                             "本能データに空のレコードがあります。")
                        continue
                    tid = _integer(getattr(talent, "id", None))
                    level = _integer(getattr(talent, "level", None))
                    if tid is None or level is None:
                        _add(findings, "invalid_talent_record", "critical", tpath,
                             "本能IDまたはレベルが整数ではありません。")
                    else:
                        if tid < 0 or level < 0:
                            _add(findings, "negative_talent_value", "critical", tpath,
                                 "本能IDまたはレベルが負の値です。")
                        if tid in talent_ids:
                            _add(findings, "duplicate_talent_id", "warning",
                                 tpath + ".id", "同一キャラ内で本能IDが重複しています。")
                        talent_ids.add(tid)
    return count


def _audit_cat_side_data(findings: list[Finding], cats_container: Any,
                         cat_count: int) -> None:
    storage = getattr(cats_container, "storage_items", None)
    if storage is not None:
        if not isinstance(storage, (list, tuple)):
            _add(findings, "invalid_storage_collection", "critical",
                 "save.cats.storage_items", "キャラ倉庫がリスト形式ではありません。")
        else:
            for index, item in enumerate(storage):
                path = f"save.cats.storage_items[{index}]"
                if item is None:
                    _add(findings, "null_storage_item", "critical", path,
                         "倉庫に空のアイテムレコードがあります。")
                    continue
                item_id = _integer(getattr(item, "item_id", None))
                item_type = _integer(getattr(item, "item_type", None))
                if item_id is None or item_type is None:
                    _add(findings, "invalid_storage_item", "critical", path,
                         "倉庫アイテムのIDまたは種類が整数ではありません。")
                    continue
                if item_id < 0:
                    _add(findings, "negative_storage_id", "critical",
                         path + ".item_id", "倉庫アイテムIDが負の値です。")
                if item_type not in (0, 1, 2):
                    _add(findings, "unknown_storage_type", "warning",
                         path + ".item_type",
                         "倉庫アイテム種類が既知の値以外です。バージョン依存の可能性があるため保留します。")
                elif item_type == 1 and not 0 <= item_id < cat_count:
                    _add(findings, "storage_cat_reference_invalid", "warning",
                         path + ".item_id",
                         "倉庫のキャラクター参照が現在の一覧範囲外です。自動削除せず確認対象にします。")

    for field, value_kind in (("favourites", "bool"), ("chara_new_flags", "int")):
        mapping = getattr(cats_container, field, None)
        if mapping is None:
            continue
        path = "save.cats." + field
        if not isinstance(mapping, dict):
            _add(findings, "invalid_cat_mapping", "critical", path,
                 "キャラクター関連辞書が辞書形式ではありません。")
            continue
        for key, value in mapping.items():
            cat_id = _integer(key)
            if cat_id is None:
                _add(findings, "invalid_cat_mapping_key", "critical", path,
                     "キャラクター関連辞書のキーが整数ではありません。")
                continue
            if cat_id < 0 or cat_id >= cat_count:
                _add(findings, "cat_mapping_reference_out_of_range", "warning",
                     f"{path}[{cat_id}]",
                     "キャラクター関連辞書が一覧外IDを参照しています。")
            if value_kind == "bool" and not isinstance(value, bool):
                _add(findings, "invalid_favourite_value", "critical",
                     f"{path}[{cat_id}]", "お気に入り値が真偽値ではありません。")
            elif value_kind == "int" and _integer(value) is None:
                _add(findings, "invalid_new_flag_value", "critical",
                     f"{path}[{cat_id}]", "新着フラグが整数ではありません。")


def _audit_lineups(findings: list[Finding], lineups: Any,
                   cat_count: int) -> None:
    if lineups is None:
        return
    slots = getattr(lineups, "slots", None)
    if not isinstance(slots, (list, tuple)):
        _add(findings, "invalid_lineup_collection", "critical",
             "save.lineups.slots", "編成一覧がリスト形式ではありません。")
        return

    selected = _integer(getattr(lineups, "selected_slot", None))
    unlocked = _integer(getattr(lineups, "unlocked_slots", None))
    if selected is None:
        _add(findings, "invalid_selected_lineup", "critical",
             "save.lineups.selected_slot", "選択中編成番号が整数ではありません。")
    elif selected < 0 or selected >= max(1, len(slots)):
        _add(findings, "selected_lineup_out_of_range", "warning",
             "save.lineups.selected_slot", "選択中編成番号が一覧範囲外です。")
    if unlocked is None:
        _add(findings, "invalid_unlocked_lineups", "critical",
             "save.lineups.unlocked_slots", "解放済み編成数が整数ではありません。")
    elif unlocked < 0 or unlocked > len(slots):
        _add(findings, "unusual_unlocked_lineups", "warning",
             "save.lineups.unlocked_slots", "解放済み編成数が一覧範囲外です。")

    for li, lineup in enumerate(slots):
        path = f"save.lineups.slots[{li}]"
        cat_slots = getattr(lineup, "slots", None)
        if not isinstance(cat_slots, (list, tuple)):
            _add(findings, "invalid_lineup_slots", "critical",
                 path + ".slots", "編成内のキャラ枠がリスト形式ではありません。")
            continue
        if len(cat_slots) != 10:
            _add(findings, "unexpected_lineup_width", "warning",
                 path + ".slots", "編成内のキャラ枠数が通常の10枠ではありません。")
        for si, entry in enumerate(cat_slots):
            entry_path = f"{path}.slots[{si}]"
            if entry is None:
                _add(findings, "null_lineup_entry", "critical", entry_path,
                     "編成に空の枠オブジェクトがあります。")
                continue
            cat_id = _integer(getattr(entry, "cat_id", None))
            if cat_id is None:
                _add(findings, "invalid_lineup_cat_id", "critical",
                     entry_path + ".cat_id", "編成キャラIDが整数ではありません。")
            elif cat_id != -1 and not 0 <= cat_id < cat_count:
                _add(findings, "lineup_cat_reference_out_of_range", "warning",
                     entry_path + ".cat_id",
                     "編成がキャラクター一覧外のIDを参照しています。")



def _audit_unowned_cat_references(findings: list[Finding], save: Any,
                                  records: Any) -> None:
    """Warn when active favourites/lineups point at a cat not marked owned.

    This is advisory only: game versions may retain references in edge cases,
    so it never triggers automatic repair.
    """
    if not isinstance(records, (list, tuple)):
        return
    owned = {
        index for index, cat in enumerate(records)
        if cat is not None and _integer(getattr(cat, "unlocked", None)) == 1
    }
    cats = getattr(save, "cats", None)
    favourites = getattr(cats, "favourites", None)
    if isinstance(favourites, dict):
        for key, active in favourites.items():
            cid = _integer(key)
            if active is True and cid is not None and 0 <= cid < len(records) and cid not in owned:
                _add(findings, "favourite_unowned_cat_reference", "warning",
                     f"save.cats.favourites[{cid}]",
                     "有効なお気に入りが未所持扱いのキャラクターを参照しています。自動変更せず確認対象にします。")
    lineups = getattr(save, "lineups", None)
    slots = getattr(lineups, "slots", None)
    unlocked_count = _integer(getattr(lineups, "unlocked_slots", None))
    if isinstance(slots, (list, tuple)) and unlocked_count is not None:
        # BCSFE stores all lineup presets in ``slots`` but only the first
        # ``unlocked_slots`` presets are available to the player. Remaining
        # presets may contain default/stale slot values and are not active
        # character references, so do not report them as unowned lineups.
        active_slots = slots[:max(0, min(unlocked_count, len(slots)))]
        for li, lineup in enumerate(active_slots):
            entries = getattr(lineup, "slots", None)
            if not isinstance(entries, (list, tuple)):
                continue
            for si, entry in enumerate(entries):
                if entry is None:
                    continue
                cid = _integer(getattr(entry, "cat_id", None))
                if cid is not None and cid != -1 and 0 <= cid < len(records) and cid not in owned:
                    _add(findings, "lineup_unowned_cat_reference", "warning",
                         f"save.lineups.slots[{li}].slots[{si}].cat_id",
                         "使用可能な編成が未所持扱いのキャラクターを参照しています。自動変更せず確認対象にします。")


def _audit_known_error_references(findings: list[Finding], save: Any,
                                  known_error_cat_ids: set[int] | None) -> None:
    """Warn about active references to known error IDs, even when not owned."""
    if not known_error_cat_ids:
        return
    cats = getattr(save, "cats", None)
    favourites = getattr(cats, "favourites", None)
    if isinstance(favourites, dict):
        for cat_id, active in favourites.items():
            cid = _integer(cat_id)
            if cid in known_error_cat_ids and active is True:
                _add(findings, "known_error_cat_referenced", "warning",
                     f"save.cats.favourites[{cid}]",
                     "既知のエラーキャラ候補がお気に入りから参照されています。")
    storage = getattr(cats, "storage_items", None)
    if isinstance(storage, (list, tuple)):
        for index, item in enumerate(storage):
            if item is None:
                continue
            cid = _integer(getattr(item, "item_id", None))
            item_type = _integer(getattr(item, "item_type", None))
            if item_type == 1 and cid in known_error_cat_ids:
                _add(findings, "known_error_cat_referenced", "warning",
                     f"save.cats.storage_items[{index}]",
                     "既知のエラーキャラ候補がキャラ倉庫から参照されています。")
    lineups = getattr(save, "lineups", None)
    slots = getattr(lineups, "slots", None)
    if isinstance(slots, (list, tuple)):
        for li, lineup in enumerate(slots):
            entries = getattr(lineup, "slots", None)
            if not isinstance(entries, (list, tuple)):
                continue
            for si, entry in enumerate(entries):
                if entry is None:
                    continue
                cid = _integer(getattr(entry, "cat_id", None))
                if cid in known_error_cat_ids:
                    _add(findings, "known_error_cat_referenced", "warning",
                         f"save.lineups.slots[{li}].slots[{si}]",
                         "既知のエラーキャラ候補が編成から参照されています。")



def _finite_number(value: Any) -> bool:
    import math
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
    )


def _audit_gamatoto(findings: list[Finding], gamatoto: Any) -> None:
    if gamatoto is None:
        return
    for field in ("xp", "dest_id", "recon_length", "unknown", "notif_value", "skin"):
        if _integer(getattr(gamatoto, field, None)) is None:
            _add(findings, "invalid_gamatoto_field", "critical",
                 f"save.gamatoto.{field}", "ガマトト情報が整数ではありません。")
    remaining = getattr(gamatoto, "remaining_seconds", None)
    if not _finite_number(remaining):
        _add(findings, "invalid_gamatoto_timer", "critical",
             "save.gamatoto.remaining_seconds", "ガマトト残り時間が有限数値ではありません。")
    elif remaining < 0:
        _add(findings, "negative_gamatoto_timer", "warning",
             "save.gamatoto.remaining_seconds", "ガマトト残り時間が負数です。時刻処理の影響もあるため自動変更しません。")
    for field in ("return_flag", "is_ad_present"):
        if not isinstance(getattr(gamatoto, field, None), bool):
            _add(findings, "invalid_gamatoto_flag", "critical",
                 f"save.gamatoto.{field}", "ガマトト状態フラグが真偽値ではありません。")
    helpers = getattr(gamatoto, "helpers", None)
    helper_list = getattr(helpers, "helpers", None)
    if not isinstance(helper_list, (list, tuple)):
        _add(findings, "invalid_gamatoto_helpers", "critical",
             "save.gamatoto.helpers.helpers", "ガマトト助手一覧がリスト形式ではありません。")
    else:
        for index, helper in enumerate(helper_list):
            helper_id = _integer(getattr(helper, "id", None))
            if helper_id is None:
                _add(findings, "invalid_gamatoto_helper_id", "critical",
                     f"save.gamatoto.helpers.helpers[{index}].id", "助手IDが整数ではありません。")
            elif helper_id < -1:
                _add(findings, "unusual_gamatoto_helper_id", "warning",
                     f"save.gamatoto.helpers.helpers[{index}].id", "助手IDが未設定値以外の負数です。自動変更しません。")
    collab_flags = getattr(gamatoto, "collab_flags", None)
    if not isinstance(collab_flags, dict):
        _add(findings, "invalid_gamatoto_collab_flags", "critical",
             "save.gamatoto.collab_flags", "コラボ状態辞書が辞書形式ではありません。")
    else:
        for key, value in collab_flags.items():
            if _integer(key) is None or not isinstance(value, bool):
                _add(findings, "invalid_gamatoto_collab_flag", "critical",
                     "save.gamatoto.collab_flags", "コラボ状態辞書のキーまたは値の型が不正です。")
                break
    collab_durations = getattr(gamatoto, "collab_durations", None)
    if not isinstance(collab_durations, dict):
        _add(findings, "invalid_gamatoto_collab_durations", "critical",
             "save.gamatoto.collab_durations", "コラボ時間辞書が辞書形式ではありません。")
    else:
        for key, value in collab_durations.items():
            if _integer(key) is None or not _finite_number(value):
                _add(findings, "invalid_gamatoto_collab_duration", "critical",
                     "save.gamatoto.collab_durations", "コラボ時間辞書のキーまたは値の型が不正です。")
                break


def _audit_ototo(findings: list[Finding], ototo: Any) -> None:
    if ototo is None:
        return
    for field in ("improve_id", "engineers"):
        value = _integer(getattr(ototo, field, None))
        if value is None:
            _add(findings, "invalid_ototo_field", "critical",
                 f"save.ototo.{field}", "オトート情報が整数ではありません。")
        elif value < 0:
            _add(findings, "negative_ototo_field", "critical",
                 f"save.ototo.{field}", "オトート情報に負数があります。")
    remaining = getattr(ototo, "remaining_seconds", None)
    if not _finite_number(remaining):
        _add(findings, "invalid_ototo_timer", "critical",
             "save.ototo.remaining_seconds", "オトート残り時間が有限数値ではありません。")
    elif remaining < 0:
        _add(findings, "negative_ototo_timer", "warning",
             "save.ototo.remaining_seconds", "オトート残り時間が負数です。自動変更しません。")
    if not isinstance(getattr(ototo, "return_flag", None), bool):
        _add(findings, "invalid_ototo_flag", "critical",
             "save.ototo.return_flag", "オトート状態フラグが真偽値ではありません。")
    materials = getattr(getattr(ototo, "base_materials", None), "materials", None)
    if not isinstance(materials, (list, tuple)):
        _add(findings, "invalid_ototo_materials", "critical",
             "save.ototo.base_materials.materials", "素材一覧がリスト形式ではありません。")
    else:
        for index, material in enumerate(materials):
            amount = _integer(getattr(material, "amount", None))
            path = f"save.ototo.base_materials.materials[{index}].amount"
            if amount is None:
                _add(findings, "invalid_ototo_material_amount", "critical",
                     path, "素材数が整数ではありません。")
            elif amount < 0:
                _add(findings, "negative_ototo_material_amount", "critical",
                     path, "素材数が負の値です。")
    cannons = getattr(ototo, "cannons", None)
    if cannons is None:
        _add(findings, "missing_ototo_cannons", "critical",
             "save.ototo.cannons", "オトート砲データがありません。")
        return
    cannon_map = getattr(cannons, "cannons", None)
    if not isinstance(cannon_map, dict):
        _add(findings, "invalid_ototo_cannon_map", "critical",
             "save.ototo.cannons.cannons", "砲データが辞書形式ではありません。")
    else:
        for cannon_id, cannon in cannon_map.items():
            path = f"save.ototo.cannons.cannons[{cannon_id}]"
            if _integer(cannon_id) is None or cannon is None:
                _add(findings, "invalid_ototo_cannon", "critical",
                     path, "砲IDまたは砲データが不正です。")
                continue
            if _integer(getattr(cannon, "development", None)) is None:
                _add(findings, "invalid_ototo_cannon_development", "critical",
                     path + ".development", "砲開発段階が整数ではありません。")
            _audit_integer_list(findings, getattr(cannon, "levels", None),
                                path + ".levels")
    selected = getattr(cannons, "selected_parts", None)
    if not isinstance(selected, (list, tuple)):
        _add(findings, "invalid_ototo_selected_parts", "critical",
             "save.ototo.cannons.selected_parts", "選択中砲パーツ一覧がリスト形式ではありません。")
    else:
        for index, parts in enumerate(selected):
            if not isinstance(parts, (list, tuple)) or len(parts) != 3:
                _add(findings, "invalid_ototo_selected_part_row", "critical",
                     f"save.ototo.cannons.selected_parts[{index}]",
                     "選択中砲パーツは3要素の一覧である必要があります。")
            else:
                _audit_integer_list(findings, parts,
                                    f"save.ototo.cannons.selected_parts[{index}]")


def _audit_missions(findings: list[Finding], missions: Any) -> None:
    if missions is None:
        return
    int_maps = (
        "clear_states", "requirements", "progress_types", "gamatoto_values",
        "nyancombo_values", "user_rank_values", "expiry_values",
    )
    for field in int_maps:
        mapping = getattr(missions, field, None)
        path = f"save.missions.{field}"
        if not isinstance(mapping, dict):
            _add(findings, "invalid_mission_map", "critical", path,
                 "ミッション情報が辞書形式ではありません。")
            continue
        for key, value in mapping.items():
            if _integer(key) is None or _integer(value) is None:
                _add(findings, "invalid_mission_entry", "critical", path,
                     "ミッションIDまたは値が整数ではありません。")
                break
            if field == "requirements" and value < 0:
                _add(findings, "negative_mission_requirement", "critical",
                     f"{path}[{key}]", "ミッション進捗値が負の値です。")
    preparing = getattr(missions, "preparing_values", None)
    if not isinstance(preparing, dict):
        _add(findings, "invalid_mission_map", "critical",
             "save.missions.preparing_values", "ミッション準備情報が辞書形式ではありません。")
    else:
        for key, value in preparing.items():
            if _integer(key) is None or not (
                isinstance(value, bool) or _integer(value) is not None
            ):
                _add(findings, "invalid_mission_entry", "critical",
                     "save.missions.preparing_values", "準備中ミッションのIDまたは値の型が不正です。")
                break
    weekly = getattr(missions, "weekly_missions", None)
    if not isinstance(weekly, dict):
        _add(findings, "invalid_weekly_missions", "critical",
             "save.missions.weekly_missions", "週間ミッション情報が辞書形式ではありません。")
    else:
        for key, value in weekly.items():
            if _integer(key) is None or not isinstance(value, bool):
                _add(findings, "invalid_weekly_mission_entry", "critical",
                     "save.missions.weekly_missions", "週間ミッションIDまたは値の型が不正です。")
                break



def _audit_story(findings: list[Finding], story: Any) -> None:
    """Validate the fixed-shape main-story chapter records used by BCSFE."""
    if story is None:
        return
    chapters = getattr(story, "chapters", None)
    if not isinstance(chapters, (list, tuple)):
        _add(findings, "invalid_story_chapters", "critical",
             "save.story.chapters", "ストーリー章一覧がリスト形式ではありません。")
        return
    if len(chapters) != 10:
        _add(findings, "unusual_story_chapter_count", "warning",
             "save.story.chapters", "ストーリー章数がBCSFE標準の10章と異なります。自動変更しません。")
    for ci, chapter in enumerate(chapters):
        path = f"save.story.chapters[{ci}]"
        if chapter is None:
            _add(findings, "null_story_chapter", "critical", path,
                 "ストーリー章データが空です。")
            continue
        for field in (
            "selected_stage", "progress", "time_until_treasure_chance",
            "treasure_chance_duration", "treasure_chance_value",
            "treasure_chance_stage_id", "treasure_festival_type",
        ):
            if _integer(getattr(chapter, field, None)) is None:
                _add(findings, "invalid_story_chapter_field", "critical",
                     f"{path}.{field}", "ストーリー章の値が整数ではありません。")
        stages = getattr(chapter, "stages", None)
        if not isinstance(stages, (list, tuple)):
            _add(findings, "invalid_story_stages", "critical",
                 f"{path}.stages", "ストーリーのステージ一覧がリスト形式ではありません。")
            continue
        if len(stages) != 51:
            _add(findings, "unusual_story_stage_count", "warning",
                 f"{path}.stages", "ステージ数がBCSFE標準の51件と異なります。自動変更しません。")
        for si, stage in enumerate(stages):
            spath = f"{path}.stages[{si}]"
            if stage is None:
                _add(findings, "null_story_stage", "critical", spath,
                     "ストーリーのステージ記録が空です。")
                continue
            for field in ("clear_times", "treasure", "itf_timed_score"):
                if _integer(getattr(stage, field, None)) is None:
                    _add(findings, "invalid_story_stage_field", "critical",
                         f"{spath}.{field}", "ステージ記録が整数ではありません。")


def _audit_event_stages(findings: list[Finding], event_stages: Any) -> None:
    """Validate nested event-map records without assuming event IDs or map counts."""
    if event_stages is None:
        return
    groups = getattr(event_stages, "chapters", None)
    if not isinstance(groups, (list, tuple)):
        _add(findings, "invalid_event_groups", "critical",
             "save.event_stages.chapters", "イベントマップ一覧がリスト形式ではありません。")
        return
    if len(groups) > 10_000:
        _add(findings, "event_groups_too_large", "critical",
             "save.event_stages.chapters", "イベントマップ一覧が検査上限を超えています。")
        return
    for gi, group in enumerate(groups):
        gpath = f"save.event_stages.chapters[{gi}]"
        maps = getattr(group, "chapters", None) if group is not None else None
        if not isinstance(maps, (list, tuple)):
            _add(findings, "invalid_event_maps", "critical", gpath,
                 "イベントマップグループの一覧が不正です。")
            continue
        for mi, map_record in enumerate(maps):
            mpath = f"{gpath}.chapters[{mi}]"
            stars = getattr(map_record, "chapters", None) if map_record is not None else None
            if not isinstance(stars, (list, tuple)):
                _add(findings, "invalid_event_stars", "critical", mpath,
                     "イベントマップの星別一覧が不正です。")
                continue
            restriction = _integer(getattr(map_record, "legend_restriction", None))
            if restriction is None:
                _add(findings, "invalid_event_legend_restriction", "critical",
                     mpath + ".legend_restriction", "出撃制限値が整数ではありません。")
            for si, subchapter in enumerate(stars):
                spath = f"{mpath}.chapters[{si}]"
                if subchapter is None:
                    _add(findings, "null_event_subchapter", "critical", spath,
                         "イベントの星別ステージ記録が空です。")
                    continue
                for field in ("selected_stage", "clear_progress", "chapter_unlock_state"):
                    if _integer(getattr(subchapter, field, None)) is None:
                        _add(findings, "invalid_event_subchapter_field", "critical",
                             f"{spath}.{field}", "イベントステージ進行値が整数ではありません。")
                stages = getattr(subchapter, "stages", None)
                if not isinstance(stages, (list, tuple)):
                    _add(findings, "invalid_event_stage_list", "critical",
                         spath + ".stages", "イベントステージ一覧がリスト形式ではありません。")
                    continue
                for sti, stage in enumerate(stages):
                    amount = _integer(getattr(stage, "clear_amount", None))
                    if amount is None:
                        _add(findings, "invalid_event_stage_clear_amount", "critical",
                             f"{spath}.stages[{sti}].clear_amount",
                             "イベントステージクリア回数が整数ではありません。")

    for field, kind in (
        ("chapter_completion_count", "int"),
        ("displayed_cleared_limit_text", "bool"),
        ("event_start_dates", "int"),
    ):
        mapping = getattr(event_stages, field, None)
        if mapping is None:
            continue
        if not isinstance(mapping, dict):
            _add(findings, "invalid_event_metadata_map", "critical",
                 f"save.event_stages.{field}", "イベント補助情報が辞書形式ではありません。")
            continue
        for key, value in mapping.items():
            valid_value = isinstance(value, bool) if kind == "bool" else _integer(value) is not None
            if _integer(key) is None or not valid_value:
                _add(findings, "invalid_event_metadata_entry", "critical",
                     f"save.event_stages.{field}", "イベント補助情報のキーまたは値の型が不正です。")
                break
    rewards = getattr(event_stages, "stages_reward_claimed", None)
    if rewards is not None:
        _audit_integer_list(findings, rewards,
                            "save.event_stages.stages_reward_claimed",
                            nonnegative=False)



def _audit_chapters_tree(findings: list[Finding], container: Any,
                         path: str, *, chapter_field: str = "selected_stage",
                         stage_field: str = "clear_times") -> None:
    """Audit the common Chapters -> map -> star -> chapter -> stage hierarchy."""
    maps = getattr(container, "chapters", None)
    if not isinstance(maps, (list, tuple)):
        _add(findings, "invalid_map_collection", "critical", path + ".chapters",
             "マップ一覧がリスト形式ではありません。")
        return
    if len(maps) > 10_000:
        _add(findings, "map_collection_too_large", "critical", path + ".chapters",
             "マップ一覧が検査上限を超えています。")
        return
    for mi, map_entry in enumerate(maps):
        mpath = f"{path}.chapters[{mi}]"
        chapters = getattr(map_entry, "chapters", None) if map_entry is not None else None
        if not isinstance(chapters, (list, tuple)):
            _add(findings, "invalid_map_star_collection", "critical", mpath,
                 "星別の章一覧がリスト形式ではありません。")
            continue
        for si, chapter in enumerate(chapters):
            cpath = f"{mpath}.chapters[{si}]"
            if chapter is None:
                _add(findings, "null_map_chapter", "critical", cpath,
                     "マップ章データが空です。")
                continue
            for field in (chapter_field, "clear_progress", "chapter_unlock_state"):
                if _integer(getattr(chapter, field, None)) is None:
                    _add(findings, "invalid_map_chapter_field", "critical",
                         f"{cpath}.{field}", "マップ章の進行値が整数ではありません。")
            stages = getattr(chapter, "stages", None)
            if not isinstance(stages, (list, tuple)):
                _add(findings, "invalid_map_stage_list", "critical",
                     cpath + ".stages", "マップステージ一覧がリスト形式ではありません。")
                continue
            for sti, stage in enumerate(stages):
                if _integer(getattr(stage, stage_field, None)) is None:
                    _add(findings, "invalid_map_stage_value", "critical",
                         f"{cpath}.stages[{sti}].{stage_field}",
                         "マップステージ値が整数ではありません。")


def _audit_map_wrappers(findings: list[Finding], save: Any) -> None:
    for field in ("tower", "challenge", "uncanny", "catamin_stages"):
        wrapper = getattr(save, field, None)
        if wrapper is None:
            continue
        _audit_chapters_tree(findings, getattr(wrapper, "chapters", None),
                             f"save.{field}.chapters")
        if field == "tower":
            states = getattr(wrapper, "item_obtain_states", None)
            if not isinstance(states, (list, tuple)):
                _add(findings, "invalid_tower_item_states", "critical",
                     "save.tower.item_obtain_states", "塔の報酬取得状態がリスト形式ではありません。")
            else:
                for i, row in enumerate(states):
                    if not isinstance(row, (list, tuple)) or any(
                        not isinstance(value, bool) for value in row
                    ):
                        _add(findings, "invalid_tower_item_state_row", "critical",
                             f"save.tower.item_obtain_states[{i}]",
                             "塔の報酬取得状態に真偽値以外があります。")
        elif field == "challenge":
            scores = getattr(wrapper, "scores", None)
            if not isinstance(scores, (list, tuple)):
                _add(findings, "invalid_challenge_scores", "critical",
                     "save.challenge.scores", "チャレンジスコア一覧がリスト形式ではありません。")
            else:
                _audit_integer_list(findings, scores, "save.challenge.scores",
                                    nonnegative=False)
            if not isinstance(getattr(wrapper, "shown_popup", None), bool):
                _add(findings, "invalid_challenge_popup_flag", "critical",
                     "save.challenge.shown_popup", "チャレンジ表示フラグが真偽値ではありません。")
        elif field in ("uncanny", "catamin_stages"):
            unknown = getattr(wrapper, "unknown", None)
            if not isinstance(unknown, (list, tuple)):
                _add(findings, "invalid_map_unknown_values", "critical",
                     f"save.{field}.unknown", "マップ補助値一覧がリスト形式ではありません。")
            else:
                _audit_integer_list(findings, unknown, f"save.{field}.unknown",
                                    nonnegative=False)

    ex_stages = getattr(save, "ex_stages", None)
    if ex_stages is not None:
        chapters = getattr(ex_stages, "chapters", None)
        if not isinstance(chapters, (list, tuple)):
            _add(findings, "invalid_ex_stage_chapters", "critical",
                 "save.ex_stages.chapters", "EXステージ章一覧がリスト形式ではありません。")
        else:
            for ci, chapter in enumerate(chapters):
                stages = getattr(chapter, "stages", None) if chapter is not None else None
                if not isinstance(stages, (list, tuple)):
                    _add(findings, "invalid_ex_stage_list", "critical",
                         f"save.ex_stages.chapters[{ci}].stages",
                         "EXステージ一覧がリスト形式ではありません。")
                    continue
                for si, stage in enumerate(stages):
                    if _integer(getattr(stage, "clear_amount", None)) is None:
                        _add(findings, "invalid_ex_stage_clear_amount", "critical",
                             f"save.ex_stages.chapters[{ci}].stages[{si}].clear_amount",
                             "EXステージクリア回数が整数ではありません。")

    outbreaks = getattr(save, "outbreaks", None)
    if outbreaks is not None:
        for field in ("chapters", "current_outbreaks"):
            mapping = getattr(outbreaks, field, None)
            if not isinstance(mapping, dict):
                _add(findings, "invalid_outbreak_map", "critical",
                     f"save.outbreaks.{field}", "ゾンビ襲来データが辞書形式ではありません。")
                continue
            for chapter_id, chapter in mapping.items():
                cpath = f"save.outbreaks.{field}[{chapter_id}]"
                if _integer(chapter_id) is None or chapter is None:
                    _add(findings, "invalid_outbreak_chapter", "critical", cpath,
                         "ゾンビ襲来の章IDまたは章データが不正です。")
                    continue
                if _integer(getattr(chapter, "id", None)) is None:
                    _add(findings, "invalid_outbreak_chapter_id", "critical",
                         cpath + ".id", "ゾンビ襲来の章IDが整数ではありません。")
                stages = getattr(chapter, "outbreaks", None)
                if not isinstance(stages, dict):
                    _add(findings, "invalid_outbreak_stages", "critical",
                         cpath + ".outbreaks", "ゾンビ襲来ステージ一覧が辞書形式ではありません。")
                    continue
                for stage_id, stage in stages.items():
                    if _integer(stage_id) is None or not isinstance(
                        getattr(stage, "cleared", None), bool
                    ):
                        _add(findings, "invalid_outbreak_stage", "critical",
                             f"{cpath}.outbreaks[{stage_id}]",
                             "ゾンビ襲来ステージのIDまたはクリア状態が不正です。")
        remaining = getattr(outbreaks, "zombie_event_remaining_time", None)
        if not _finite_number(remaining):
            _add(findings, "invalid_zombie_event_timer", "critical",
                 "save.outbreaks.zombie_event_remaining_time",
                 "ゾンビ襲来残り時間が有限数値ではありません。")
        elif remaining < 0:
            _add(findings, "negative_zombie_event_timer", "warning",
                 "save.outbreaks.zombie_event_remaining_time",
                 "ゾンビ襲来残り時間が負数です。自動変更しません。")



def _audit_star_group_list(findings: list[Finding], groups: Any, path: str,
                           *, chapter_field: str = "selected_stage",
                           stage_field: str = "clear_times") -> None:
    """Audit a list of ChaptersStars records (used by Aku and gauntlets)."""
    if not isinstance(groups, (list, tuple)):
        _add(findings, "invalid_star_group_list", "critical", path,
             "星別マップグループがリスト形式ではありません。")
        return
    for gi, group in enumerate(groups):
        chapters = getattr(group, "chapters", None) if group is not None else None
        gpath = f"{path}[{gi}].chapters"
        if not isinstance(chapters, (list, tuple)):
            _add(findings, "invalid_star_group_chapters", "critical", gpath,
                 "星別マップの章一覧がリスト形式ではありません。")
            continue
        for ci, chapter in enumerate(chapters):
            cpath = f"{gpath}[{ci}]"
            if chapter is None:
                _add(findings, "null_star_group_chapter", "critical", cpath,
                     "星別マップの章データが空です。")
                continue
            if _integer(getattr(chapter, chapter_field, None)) is None:
                _add(findings, "invalid_star_group_chapter_field", "critical",
                     f"{cpath}.{chapter_field}", "章の選択位置が整数ではありません。")
            if chapter_field != "current_stage":
                for field in ("clear_progress", "chapter_unlock_state"):
                    if _integer(getattr(chapter, field, None)) is None:
                        _add(findings, "invalid_star_group_chapter_field", "critical",
                             f"{cpath}.{field}", "章の進行値が整数ではありません。")
            stages = getattr(chapter, "stages", None)
            if not isinstance(stages, (list, tuple)):
                _add(findings, "invalid_star_group_stage_list", "critical",
                     f"{cpath}.stages", "ステージ一覧がリスト形式ではありません。")
                continue
            for si, stage in enumerate(stages):
                if _integer(getattr(stage, stage_field, None)) is None:
                    _add(findings, "invalid_star_group_stage_value", "critical",
                         f"{cpath}.stages[{si}].{stage_field}",
                         "ステージ記録が整数ではありません。")


def _audit_advanced_map_groups(findings: list[Finding], save: Any) -> None:
    aku = getattr(save, "aku", None)
    if aku is not None:
        _audit_star_group_list(findings, getattr(aku, "chapters", None),
                               "save.aku.chapters", chapter_field="current_stage")

    for field in ("gauntlets", "collab_gauntlets", "enigma_clears"):
        wrapper = getattr(save, field, None)
        if wrapper is not None:
            _audit_star_group_list(findings, getattr(wrapper, "chapters", None),
                                   f"save.{field}.chapters")
            unknown = getattr(wrapper, "unknown", None)
            if unknown is not None:
                if not isinstance(unknown, (list, tuple)):
                    _add(findings, "invalid_gauntlet_unknown", "critical",
                         f"save.{field}.unknown", "補助値一覧がリスト形式ではありません。")
                else:
                    _audit_integer_list(findings, unknown,
                                        f"save.{field}.unknown",
                                        nonnegative=False)


def _audit_subchapter_maps(findings, wrapper, path, stage_field, expected_type):
    groups = getattr(wrapper, "sub_chapters", None)
    if not isinstance(groups, (list, tuple)):
        _add(findings, "invalid_subchapter_map_groups", "critical", path + ".sub_chapters", "サブチャプターマップ一覧がリスト形式ではありません。")
        return
    if len(groups) > 10000:
        _add(findings, "subchapter_map_groups_too_large", "critical", path + ".sub_chapters", "サブチャプターマップ一覧が検査上限を超えています。")
        return
    for gi, group in enumerate(groups):
        stars = getattr(group, "sub_chapters", None) if group is not None else None
        if not isinstance(stars, (list, tuple)):
            _add(findings, "invalid_subchapter_star_list", "critical", path + ".sub_chapters[" + str(gi) + "]", "星別一覧がリスト形式ではありません。")
            continue
        for si, subchapter in enumerate(stars):
            stages = getattr(subchapter, "stages", None) if subchapter is not None else None
            if not isinstance(stages, (list, tuple)):
                _add(findings, "invalid_subchapter_stage_list", "critical", path, "ステージ一覧がリスト形式ではありません。")
                continue
            for sti, stage in enumerate(stages):
                value = getattr(stage, stage_field, None) if stage is not None else None
                valid = isinstance(value, bool) if expected_type == "bool" else _integer(value) is not None
                if not valid:
                    _add(findings, "invalid_subchapter_stage_value", "critical", path + ".sub_chapters[" + str(gi) + "].sub_chapters[" + str(si) + "].stages[" + str(sti) + "]", "ステージ記録の値または型が不正です。")


def _audit_item_reward_metadata(findings, wrapper):
    obtains = getattr(wrapper, "item_obtains", None)
    sets = getattr(obtains, "item_obtain_sets", None) if obtains is not None else None
    if not isinstance(sets, dict):
        _add(findings, "invalid_item_obtain_sets", "critical", "save.item_reward_stages.item_obtains", "アイテム取得セットが辞書形式ではありません。")
    else:
        for set_id, item_set in sets.items():
            items = getattr(item_set, "item_obtains", None) if item_set is not None else None
            if _integer(set_id) is None or not isinstance(items, dict):
                _add(findings, "invalid_item_obtain_set", "critical", "save.item_reward_stages.item_obtains", "アイテム取得セットが不正です。")
                continue
            for item_id, item in items.items():
                if _integer(item_id) is None or not isinstance(getattr(item, "flag", None), bool):
                    _add(findings, "invalid_item_obtain_flag", "critical", "save.item_reward_stages.item_obtains", "アイテム取得フラグが不正です。")
                    break
    unobtained = getattr(wrapper, "unobtained_items", None)
    mapping = getattr(unobtained, "unobtained_items", None) if unobtained is not None else None
    if not isinstance(mapping, dict):
        _add(findings, "invalid_unobtained_items", "critical", "save.item_reward_stages.unobtained_items", "未取得アイテム一覧が辞書形式ではありません。")
    else:
        for item_id, item in mapping.items():
            if _integer(item_id) is None or not isinstance(getattr(item, "unobtained", None), bool):
                _add(findings, "invalid_unobtained_item", "critical", "save.item_reward_stages.unobtained_items", "未取得アイテムが不正です。")
                break


def _audit_legend_quest(findings, wrapper):
    groups = getattr(wrapper, "chapters", None)
    if not isinstance(groups, (list, tuple)):
        _add(findings, "invalid_legend_quest_groups", "critical", "save.legend_quest.chapters", "レジェンドクエスト一覧が不正です。")
        return
    for gi, group in enumerate(groups):
        chapters = getattr(group, "chapters", None) if group is not None else None
        if not isinstance(chapters, (list, tuple)):
            _add(findings, "invalid_legend_quest_stars", "critical", "save.legend_quest.chapters[" + str(gi) + "]", "星別一覧が不正です。")
            continue
        for ci, chapter in enumerate(chapters):
            path = "save.legend_quest.chapters[" + str(gi) + "].chapters[" + str(ci) + "]"
            for field in ("selected_stage", "clear_progress", "chapter_unlock_state"):
                if _integer(getattr(chapter, field, None)) is None:
                    _add(findings, "invalid_legend_quest_chapter", "critical", path + "." + field, "章の進行値が整数ではありません。")
            stages = getattr(chapter, "stages", None)
            if not isinstance(stages, (list, tuple)):
                _add(findings, "invalid_legend_quest_stages", "critical", path, "ステージ一覧が不正です。")
                continue
            for si, stage in enumerate(stages):
                if any(_integer(getattr(stage, f, None)) is None for f in ("clear_times", "tries")):
                    _add(findings, "invalid_legend_quest_stage", "critical", path + ".stages[" + str(si) + "]", "クリア回数または挑戦回数が整数ではありません。")
    for field in ("unknown", "ids"):
        values = getattr(wrapper, field, None)
        if not isinstance(values, (list, tuple)):
            _add(findings, "invalid_legend_quest_metadata", "critical", "save.legend_quest." + field, "補助データがリスト形式ではありません。")
        else:
            _audit_integer_list(findings, values, "save.legend_quest." + field, nonnegative=False)


def _audit_zero_legends(findings, wrapper, path):
    groups = getattr(wrapper, "chapters", None)
    if not isinstance(groups, (list, tuple)):
        _add(findings, "invalid_zero_legend_groups", "critical", path + ".chapters", "マップ一覧が不正です。")
        return
    for gi, group in enumerate(groups):
        gpath = path + ".chapters[" + str(gi) + "]"
        if _integer(getattr(group, "unknown", None)) is None:
            _add(findings, "invalid_zero_legend_group_unknown", "critical", gpath, "マップ補助値が整数ではありません。")
        chapters = getattr(group, "chapters", None)
        if not isinstance(chapters, (list, tuple)):
            _add(findings, "invalid_zero_legend_stars", "critical", gpath, "星別一覧が不正です。")
            continue
        for ci, chapter in enumerate(chapters):
            cpath = gpath + ".chapters[" + str(ci) + "]"
            for field in ("selected_stage", "clear_progress", "unlock_state"):
                if _integer(getattr(chapter, field, None)) is None:
                    _add(findings, "invalid_zero_legend_chapter", "critical", cpath + "." + field, "章の進行値が整数ではありません。")
            stages = getattr(chapter, "stages", None)
            if not isinstance(stages, (list, tuple)):
                _add(findings, "invalid_zero_legend_stages", "critical", cpath, "ステージ一覧が不正です。")
                continue
            for si, stage in enumerate(stages):
                if _integer(getattr(stage, "clear_times", None)) is None:
                    _add(findings, "invalid_zero_legend_stage", "critical", cpath + ".stages[" + str(si) + "]", "クリア回数が整数ではありません。")


def _audit_dojo(findings, dojo):
    chapters = getattr(getattr(dojo, "chapters", None), "chapters", None)
    if not isinstance(chapters, dict):
        _add(findings, "invalid_dojo_chapters", "critical", "save.dojo.chapters.chapters", "道場マップが辞書形式ではありません。")
    else:
        for chapter_id, chapter in chapters.items():
            stages = getattr(chapter, "stages", None) if chapter is not None else None
            if _integer(chapter_id) is None or not isinstance(stages, dict):
                _add(findings, "invalid_dojo_chapter", "critical", "save.dojo.chapters.chapters", "道場の章データが不正です。")
                continue
            for stage_id, stage in stages.items():
                if _integer(stage_id) is None or _integer(getattr(stage, "score", None)) is None:
                    _add(findings, "invalid_dojo_stage", "critical", "save.dojo.chapters.chapters", "道場ステージIDまたはスコアが不正です。")
    if not isinstance(getattr(dojo, "item_lock_flags", None), bool):
        _add(findings, "invalid_dojo_flag", "critical", "save.dojo.item_lock_flags", "道場フラグが不正です。")
    locks = getattr(dojo, "item_locks", None)
    if not isinstance(locks, (list, tuple)) or any(not isinstance(x, bool) for x in locks):
        _add(findings, "invalid_dojo_item_locks", "critical", "save.dojo.item_locks", "道場アイテムロック一覧が不正です。")
    ranking = getattr(dojo, "ranking", None)
    if ranking is not None:
        for field in ("score", "ranking", "start_date", "end_date", "event_number"):
            if _integer(getattr(ranking, field, None)) is None:
                _add(findings, "invalid_dojo_ranking_value", "critical", "save.dojo.ranking." + field, "道場ランキング値が整数ではありません。")
        for field in ("has_submitted", "has_completed", "has_seen_results", "should_show_rank_description", "should_show_start_message", "submit_error_flag", "did_win_rewards"):
            if not isinstance(getattr(ranking, field, None), bool):
                _add(findings, "invalid_dojo_ranking_flag", "critical", "save.dojo.ranking." + field, "道場ランキングフラグが不正です。")


def _audit_cat_shrine(findings, shrine):
    for field in ("unknown", "shrine_gone"):
        if not isinstance(getattr(shrine, field, None), bool):
            _add(findings, "invalid_cat_shrine_flag", "critical", "save.cat_shrine." + field, "ネコ神社フラグが不正です。")
    for field in ("stamp_1", "stamp_2"):
        if not _finite_number(getattr(shrine, field, None)):
            _add(findings, "invalid_cat_shrine_timestamp", "critical", "save.cat_shrine." + field, "ネコ神社時刻が有限数値ではありません。")
    flags = getattr(shrine, "flags", None)
    if not isinstance(flags, (list, tuple)):
        _add(findings, "invalid_cat_shrine_flags", "critical", "save.cat_shrine.flags", "ネコ神社フラグ一覧が不正です。")
    else:
        _audit_integer_list(findings, flags, "save.cat_shrine.flags", nonnegative=False)
    for field in ("xp_offering", "dialogs"):
        if _integer(getattr(shrine, field, None)) is None:
            _add(findings, "invalid_cat_shrine_value", "critical", "save.cat_shrine." + field, "ネコ神社の値が整数ではありません。")


def _audit_gacha(findings, gatya):
    for field in ("rare_seed", "normal_seed", "event_seed", "stepup_stage_3_cooldown",
                  "previous_normal_roll", "previous_normal_roll_type", "previous_rare_roll",
                  "previous_rare_roll_type", "trade_progress"):
        if _integer(getattr(gatya, field, None)) is None:
            _add(findings, "invalid_gacha_value", "critical", "save.gatya." + field, "ガチャ情報が整数ではありません。")
    for field in ("unknown1", "roll_single", "roll_multi"):
        if not isinstance(getattr(gatya, field, None), bool):
            _add(findings, "invalid_gacha_flag", "critical", "save.gatya." + field, "ガチャフラグが真偽値ではありません。")
    stages = getattr(gatya, "step_up_stages", None)
    if not isinstance(stages, dict):
        _add(findings, "invalid_gacha_stepup_stages", "critical", "save.gatya.step_up_stages", "ステップアップ進行が辞書形式ではありません。")
    else:
        for key, value in stages.items():
            if _integer(key) is None or _integer(value) is None:
                _add(findings, "invalid_gacha_stepup_entry", "critical", "save.gatya.step_up_stages", "ステップアップ進行のキーまたは値が不正です。")
                break
    durations = getattr(gatya, "stepup_durations", None)
    if not isinstance(durations, dict):
        _add(findings, "invalid_gacha_stepup_durations", "critical", "save.gatya.stepup_durations", "ステップアップ時間が辞書形式ではありません。")
    else:
        for key, value in durations.items():
            if _integer(key) is None or not _finite_number(value):
                _add(findings, "invalid_gacha_stepup_duration", "critical", "save.gatya.stepup_durations", "ステップアップ時間のキーまたは値が不正です。")
                break


def _audit_enigma(findings, enigma):
    for field in ("energy_since_1", "energy_since_2", "enigma_level", "unknown_1"):
        if _integer(getattr(enigma, field, None)) is None:
            _add(findings, "invalid_enigma_value", "critical", "save.enigma." + field, "エニグマ情報が整数ではありません。")
    if not isinstance(getattr(enigma, "unknown_2", None), bool):
        _add(findings, "invalid_enigma_flag", "critical", "save.enigma.unknown_2", "エニグマフラグが不正です。")
    stages = getattr(enigma, "stages", None)
    if not isinstance(stages, (list, tuple)):
        _add(findings, "invalid_enigma_stages", "critical", "save.enigma.stages", "エニグマステージ一覧が不正です。")
    else:
        for i, stage in enumerate(stages):
            for field in ("level", "stage_id", "decoding_satus"):
                if _integer(getattr(stage, field, None)) is None:
                    _add(findings, "invalid_enigma_stage_value", "critical", "save.enigma.stages[" + str(i) + "]." + field, "エニグマステージ値が整数ではありません。")
            if not _finite_number(getattr(stage, "start_time", None)):
                _add(findings, "invalid_enigma_stage_time", "critical", "save.enigma.stages[" + str(i) + "].start_time", "エニグマ開始時刻が有限数値ではありません。")
    extra = getattr(enigma, "extra", None)
    if extra is not None and (not isinstance(extra, (list, tuple)) or len(extra) != 4
        or any(_integer(extra[i]) is None for i in range(3)) or not _finite_number(extra[3])):
        _add(findings, "invalid_enigma_extra", "critical", "save.enigma.extra", "エニグマ追加データの形式が不正です。")


def _audit_talent_orbs(findings, wrapper):
    orbs = getattr(wrapper, "orbs", None)
    if not isinstance(orbs, dict):
        _add(findings, "invalid_talent_orbs", "critical", "save.talent_orbs.orbs", "本能玉一覧が辞書形式ではありません。")
        return
    for key, orb in orbs.items():
        if _integer(key) is None or _integer(getattr(orb, "id", None)) is None or _integer(getattr(orb, "value", None)) is None:
            _add(findings, "invalid_talent_orb", "critical", "save.talent_orbs.orbs", "本能玉IDまたは値が整数ではありません。")
            break
        if _integer(key) != _integer(getattr(orb, "id", None)):
            _add(findings, "talent_orb_key_mismatch", "critical", "save.talent_orbs.orbs", "本能玉の辞書キーと内部IDが一致しません。")
            break


def _audit_gambling_event(findings, wrapper, path):
    completed = getattr(wrapper, "completed", None)
    if not isinstance(completed, dict):
        _add(findings, "invalid_gambling_completed", "critical", path + ".completed", "イベント完了情報が辞書形式ではありません。")
    else:
        for key, value in completed.items():
            if _integer(key) is None or not isinstance(value, bool):
                _add(findings, "invalid_gambling_completed_entry", "critical", path + ".completed", "イベント完了情報の型が不正です。")
                break
    values = getattr(wrapper, "values", None)
    if not isinstance(values, dict):
        _add(findings, "invalid_gambling_values", "critical", path + ".values", "イベント進行値が辞書形式ではありません。")
    else:
        for key, inner in values.items():
            if _integer(key) is None or not isinstance(inner, dict) or any(
                _integer(k) is None or _integer(v) is None for k, v in inner.items()
            ):
                _add(findings, "invalid_gambling_value_entry", "critical", path + ".values", "イベント進行値の型が不正です。")
                break
    times = getattr(wrapper, "start_times", None)
    if not isinstance(times, dict):
        _add(findings, "invalid_gambling_start_times", "critical", path + ".start_times", "イベント開始時刻が辞書形式ではありません。")
    else:
        for key, value in times.items():
            if _integer(key) is None or not _finite_number(value):
                _add(findings, "invalid_gambling_start_time", "critical", path + ".start_times", "イベント開始時刻の型が不正です。")
                break


def _audit_map_resets(findings, wrapper):
    data = getattr(wrapper, "data", None)
    if not isinstance(data, dict):
        _add(findings, "invalid_map_resets", "critical", "save.map_resets.data", "マップリセット情報が辞書形式ではありません。")
        return
    for key, records in data.items():
        if _integer(key) is None or not isinstance(records, (list, tuple)):
            _add(findings, "invalid_map_reset_group", "critical", "save.map_resets.data", "マップリセットのキーまたは一覧が不正です。")
            continue
        for i, record in enumerate(records):
            for field in ("yearly_end_timestamp", "monthly_end_timestamp",
                          "weekly_end_timestamp", "daily_end_timestamp"):
                if not _finite_number(getattr(record, field, None)):
                    _add(findings, "invalid_map_reset_timestamp", "critical",
                         "save.map_resets.data[" + str(key) + "][" + str(i) + "]." + field,
                         "マップリセット時刻が有限数値ではありません。")


def _audit_top_level_collections(findings, save):
    integer_lists = (
        "enemy_guide", "menu_unlocks", "unlock_popups_0", "new_dialogs_2",
        "uil1", "moneko_bonus", "daily_reward_initialized", "chara_flags",
        "chara_flags_2", "unlock_popups_8", "unit_drops", "combo_unlocks",
        "gatya_seen_lucky_drops", "catfruit", "catseyes", "catamins",
        "lucky_tickets", "labyrinth_medals", "stage_ids_10s", "uil2", "uil3",
        "uil4", "uil6", "uil7", "event_capsules", "event_capsules_counter",
    )
    for field in integer_lists:
        value = getattr(save, field, None)
        if value is not None:
            _audit_integer_list(findings, value, "save." + field, nonnegative=False)
    boolean_lists = (
        "achievements", "unlock_popups_6", "reset_item_reward_flags",
        "catfood_beginner_purchased", "catfood_beginner_expired", "ubl1",
        "ubl2", "ubl3",
    )
    for field in boolean_lists:
        value = getattr(save, field, None)
        if value is not None and (
            not isinstance(value, (list, tuple))
            or any(not isinstance(item, bool) for item in value)
        ):
            _add(findings, "invalid_save_boolean_list", "critical", "save." + field,
                 "セーブ内の真偽値一覧に不正な値があります。")
    string_lists = ("order_ids", "usl1", "usl2")
    for field in string_lists:
        value = getattr(save, field, None)
        if value is not None and (
            not isinstance(value, (list, tuple))
            or any(not isinstance(item, str) for item in value)
        ):
            _add(findings, "invalid_save_string_list", "critical", "save." + field,
                 "セーブ内の文字列一覧に不正な値があります。")
    locks = getattr(save, "first_locks", None)
    if locks is not None:
        if not isinstance(locks, dict):
            _add(findings, "invalid_first_locks", "critical", "save.first_locks",
                 "初回ロック情報が辞書形式ではありません。")
        else:
            for key, value in locks.items():
                if _integer(key) is None or not isinstance(value, bool):
                    _add(findings, "invalid_first_lock_entry", "critical", "save.first_locks",
                         "初回ロック情報のキーまたは値が不正です。")
                    break
    announcements = getattr(save, "announcements", None)
    if announcements is not None:
        if not isinstance(announcements, (list, tuple)):
            _add(findings, "invalid_announcements", "critical", "save.announcements",
                 "お知らせ情報がリスト形式ではありません。")
        else:
            for i, entry in enumerate(announcements):
                if not isinstance(entry, (list, tuple)) or len(entry) != 2 or any(
                    _integer(v) is None for v in entry
                ):
                    _add(findings, "invalid_announcement_entry", "critical",
                         "save.announcements[" + str(i) + "]",
                         "お知らせ情報の要素が2つの整数ではありません。")
                    break


def audit_save(save: Any, known_error_cat_ids: set[int] | None = None) -> AuditReport:
    """Inspect known BCSFE structures; known error-character matches are warnings only."""
    findings: list[Finding] = []
    cats_container = getattr(save, "cats", None)
    if cats_container is None:
        _add(findings, "missing_cat_container", "critical", "save.cats",
             "キャラクター情報のコンテナがありません。")
        return AuditReport(tuple(findings), 0)

    records = getattr(cats_container, "cats", None)
    if records is None:
        _add(findings, "missing_cat_collection", "critical",
             "save.cats.cats", "キャラクター一覧がありません。")
        return AuditReport(tuple(findings), 0)

    count = _audit_cat_records(findings, records, known_error_cat_ids)
    if count > MAX_CAT_RECORDS:
        return AuditReport(tuple(findings), count)
    _audit_cat_side_data(findings, cats_container, count)
    _audit_lineups(findings, getattr(save, "lineups", None), count)
    _audit_unowned_cat_references(findings, save, records)
    _audit_known_error_references(findings, save, known_error_cat_ids)
    _audit_gamatoto(findings, getattr(save, "gamatoto", None))
    _audit_ototo(findings, getattr(save, "ototo", None))
    _audit_missions(findings, getattr(save, "missions", None))
    _audit_story(findings, getattr(save, "story", None))
    _audit_event_stages(findings, getattr(save, "event_stages", None))
    _audit_map_wrappers(findings, save)
    _audit_advanced_map_groups(findings, save)
    _audit_top_level_collections(findings, save)
    gatya = getattr(save, "gatya", None)
    if gatya is not None:
        _audit_gacha(findings, gatya)
    enigma = getattr(save, "enigma", None)
    if enigma is not None:
        _audit_enigma(findings, enigma)
    talent_orbs = getattr(save, "talent_orbs", None)
    if talent_orbs is not None:
        _audit_talent_orbs(findings, talent_orbs)
    for field in ("wildcat_slots", "cat_scratcher"):
        wrapper = getattr(save, field, None)
        if wrapper is not None:
            _audit_gambling_event(findings, wrapper, "save." + field)
    map_resets = getattr(save, "map_resets", None)
    if map_resets is not None:
        _audit_map_resets(findings, map_resets)
    item_rewards = getattr(save, "item_reward_stages", None)
    if item_rewards is not None:
        _audit_subchapter_maps(findings, item_rewards, "save.item_reward_stages", "claimed", "bool")
        _audit_item_reward_metadata(findings, item_rewards)
    timed_scores = getattr(save, "timed_score_stages", None)
    if timed_scores is not None:
        _audit_subchapter_maps(findings, timed_scores, "save.timed_score_stages", "score", "int")
    legend_quest = getattr(save, "legend_quest", None)
    if legend_quest is not None:
        _audit_legend_quest(findings, legend_quest)
    for field in ("zero_legends", "dojo_chapters"):
        wrapper = getattr(save, field, None)
        if wrapper is not None:
            _audit_zero_legends(findings, wrapper, "save." + field)
    dojo = getattr(save, "dojo", None)
    if dojo is not None:
        _audit_dojo(findings, dojo)
    shrine = getattr(save, "cat_shrine", None)
    if shrine is not None:
        _audit_cat_shrine(findings, shrine)
    behemoth = getattr(save, "behemoth_culling", None)
    if behemoth is not None:
        _audit_star_group_list(findings, getattr(behemoth, "chapters", None), "save.behemoth_culling.chapters")
        unknown = getattr(behemoth, "unknown", None)
        if not isinstance(unknown, (list, tuple)):
            _add(findings, "invalid_behemoth_unknown", "critical", "save.behemoth_culling.unknown", "補助値一覧が不正です。")
        else:
            _audit_integer_list(findings, unknown, "save.behemoth_culling.unknown", nonnegative=False)

    for field in CURRENCY_FIELDS:
        if not hasattr(save, field):
            continue
        number = _integer(getattr(save, field))
        if number is None:
            _add(findings, "invalid_currency_field", "critical",
                 "save." + field, "所持数が整数ではありません。")
        elif number < 0:
            _add(findings, "negative_currency", "critical",
                 "save." + field, "所持数が負の値です。")

    for field in NONNEGATIVE_LIST_FIELDS:
        if hasattr(save, field):
            _audit_integer_list(findings, getattr(save, field), "save." + field)

    battle_items = getattr(save, "battle_items", None)
    if battle_items is not None:
        items = getattr(battle_items, "items", None)
        if not isinstance(items, (list, tuple)):
            _add(findings, "invalid_battle_items", "critical",
                 "save.battle_items.items",
                 "バトルアイテム一覧がリスト形式ではありません。")
        else:
            for index, item in enumerate(items):
                path = f"save.battle_items.items[{index}]"
                amount = _integer(getattr(item, "amount", None))
                if amount is None:
                    _add(findings, "invalid_battle_item_amount", "critical",
                         path + ".amount", "バトルアイテム数量が整数ではありません。")
                elif amount < 0:
                    _add(findings, "negative_battle_item_amount", "critical",
                         path + ".amount", "バトルアイテム数量が負の値です。")

    return AuditReport(tuple(findings), count)
