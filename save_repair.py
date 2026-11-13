"""Conservative, reversible-in-memory repairs for known invalid cat records.

Only IDs explicitly supplied by the application are considered. The module
never removes entries from the positional Cat list: BCSFE relies on list
positions matching internal IDs. Callers must create a serialized backup and
validate a serialize/reload round trip before upload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RepairAction:
    cat_id: int
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class RepairResult:
    actions: tuple[RepairAction, ...]

    @property
    def repaired_count(self) -> int:
        return len(self.actions)

    def as_dict(self) -> dict[str, Any]:
        return {
            "repaired_count": self.repaired_count,
            "actions": [
                {"cat_id": action.cat_id, "reasons": list(action.reasons)}
                for action in self.actions
            ],
        }


def _integer(value: Any) -> int | None:
    raw = getattr(value, "value", value)
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def _references(save: Any, cat_id: int) -> list[str]:
    reasons: list[str] = []
    cats = getattr(save, "cats", None)
    if cats is None:
        return reasons

    favourites = getattr(cats, "favourites", None)
    if isinstance(favourites, dict) and favourites.get(cat_id) is True:
        reasons.append("favourite_reference")

    storage = getattr(cats, "storage_items", None)
    if isinstance(storage, (list, tuple)):
        if any(
            _integer(getattr(item, "item_type", None)) == 1
            and _integer(getattr(item, "item_id", None)) == cat_id
            for item in storage
        ):
            reasons.append("storage_reference")

    lineups = getattr(save, "lineups", None)
    slots = getattr(lineups, "slots", None)
    if isinstance(slots, (list, tuple)):
        if any(
            _integer(getattr(entry, "cat_id", None)) == cat_id
            for lineup in slots
            for entry in (getattr(lineup, "slots", None) or [])
        ):
            reasons.append("lineup_reference")
    return reasons


def plan_known_error_character_repairs(
    save: Any, known_error_cat_ids: set[int] | tuple[int, ...] | list[int]
) -> tuple[RepairAction, ...]:
    """Return repair candidates without mutating the save."""
    cats = getattr(getattr(save, "cats", None), "cats", None)
    if not isinstance(cats, (list, tuple)):
        return ()

    actions: list[RepairAction] = []
    for cat_id in sorted(set(known_error_cat_ids)):
        if not isinstance(cat_id, int) or isinstance(cat_id, bool):
            continue
        if cat_id < 0 or cat_id >= len(cats):
            continue
        cat = cats[cat_id]
        if cat is None or _integer(getattr(cat, "id", None)) != cat_id:
            continue
        unlocked = _integer(getattr(cat, "unlocked", 0))
        reasons = _references(save, cat_id)
        if unlocked not in (0, False, None):
            reasons.insert(0, "owned_known_error_character")
        if reasons:
            actions.append(RepairAction(cat_id, tuple(dict.fromkeys(reasons))))
    return tuple(actions)


def apply_known_error_character_repairs(
    save: Any, actions: tuple[RepairAction, ...]
) -> RepairResult:
    """Reset known error-character records and clear direct references.

    Requires BCSFE's Cat.remove(reset=True, save_file=save). If that supported
    method is absent, fail before changing any record.
    """
    cats_container = getattr(save, "cats", None)
    records = getattr(cats_container, "cats", None)
    if not isinstance(records, list):
        raise RuntimeError("キャラクター一覧が変更可能なリストではありません。")

    for action in actions:
        if action.cat_id < 0 or action.cat_id >= len(records):
            raise RuntimeError(f"修復対象ID {action.cat_id} が一覧範囲外です。")
        cat = records[action.cat_id]
        if _integer(getattr(cat, "id", None)) != action.cat_id:
            raise RuntimeError(f"修復対象ID {action.cat_id} の位置が一致しません。")
        if not callable(getattr(cat, "remove", None)):
            raise RuntimeError(
                f"キャラID {action.cat_id} にBCSFE標準のremove処理がありません。"
            )

    # All targets are preflighted before the first mutation.
    for action in actions:
        cat_id = action.cat_id
        cat = records[cat_id]
        cat.remove(reset=True, save_file=save)
        max_upgrade = getattr(cat, "max_upgrade_level", None)
        reset_max = getattr(max_upgrade, "reset", None)
        if callable(reset_max):
            reset_max()
        elif max_upgrade is not None:
            for field in ("base", "plus"):
                if hasattr(max_upgrade, field):
                    setattr(max_upgrade, field, 0)

        favourites = getattr(cats_container, "favourites", None)
        if isinstance(favourites, dict):
            favourites.pop(cat_id, None)

        storage = getattr(cats_container, "storage_items", None)
        if isinstance(storage, (list, tuple)):
            for item in storage:
                if (_integer(getattr(item, "item_type", None)) == 1
                        and _integer(getattr(item, "item_id", None)) == cat_id):
                    item.item_id = 0
                    item.item_type = 0

        lineups = getattr(save, "lineups", None)
        slots = getattr(lineups, "slots", None)
        if isinstance(slots, (list, tuple)):
            for lineup in slots:
                entries = getattr(lineup, "slots", None)
                if isinstance(entries, (list, tuple)):
                    for entry in entries:
                        if _integer(getattr(entry, "cat_id", None)) == cat_id:
                            entry.cat_id = -1

    return RepairResult(tuple(actions))


class RepairVerificationError(RuntimeError):
    """A repair failed verification; carries a deserialized backup when possible."""

    def __init__(self, message: str, restored_save: Any = None,
                 restore_error: Exception | None = None):
        super().__init__(message)
        self.restored_save = restored_save
        self.restore_error = restore_error


def repair_with_roundtrip_verification(
    save: Any,
    actions: tuple[RepairAction, ...],
    known_error_cat_ids,
    serialize,
    deserialize,
    audit,
) -> tuple[Any, RepairResult, Any]:
    """Backup, repair, serialize/reload, and audit before a caller may upload.

    serialize(save) must return immutable backup bytes. deserialize(bytes)
    must create a fresh save object. On any failure, a fresh backup instance
    is attached to RepairVerificationError for the caller to restore.
    """
    backup_bytes = serialize(save)
    combo_progress_before = _snapshot_combo_progress(save)
    try:
        result = apply_known_error_character_repairs(save, actions)
        # Known-error cleanup is another independent round-trip. It must not
        # reset the combo encyclopedia after visibility repair preserved it.
        _restore_combo_progress(save, combo_progress_before)
        verified_save = deserialize(serialize(save))
        _restore_combo_progress(verified_save, combo_progress_before)
        if not _combo_progress_matches(verified_save, combo_progress_before):
            raise RuntimeError("にゃんこコンボ図鑑の状態を修復前と同じに保てませんでした。")
        post_audit = audit(
            verified_save, known_error_cat_ids=known_error_cat_ids
        )
        remaining = [
            finding for finding in post_audit.findings
            if finding.code in (
                "known_error_cat_unlocked",
                "known_error_cat_referenced",
            )
        ]
        if not post_audit.ok or remaining:
            raise RuntimeError("修復後の構造検査で問題が残りました。")
        return verified_save, result, post_audit
    except Exception as repair_error:
        restored_save = None
        restore_error = None
        try:
            restored_save = deserialize(backup_bytes)
        except Exception as exc:  # noqa: BLE001
            restore_error = exc
        message = f"修復後の検証に失敗しました: {repair_error}"
        if restore_error is not None:
            message += f"。バックアップ復元にも失敗しました: {restore_error}"
        raise RepairVerificationError(
            message, restored_save=restored_save, restore_error=restore_error
        ) from repair_error


@dataclass(frozen=True)
class VisibilityRepairResult:
    checked_unowned: int
    changed_count: int
    gacha_flags_cleared: int
    encyclopedia_flags_cleared: int
    skipped_ambiguous: int

    def as_dict(self) -> dict[str, int]:
        return {
            "checked_unowned": self.checked_unowned,
            "changed_count": self.changed_count,
            "gacha_flags_cleared": self.gacha_flags_cleared,
            "encyclopedia_flags_cleared": self.encyclopedia_flags_cleared,
            "skipped_ambiguous": self.skipped_ambiguous,
        }


def repair_unowned_character_visibility_flags(save: Any) -> VisibilityRepairResult:
    """Clear only stale gacha/encyclopedia flags on structurally valid unowned cats.

    Ownership is intentionally strict: only integer unlocked == 0 is eligible.
    Owned, malformed, missing, or position-mismatched records are never changed.
    The positional cat list is never reordered or shortened.
    """
    records = getattr(getattr(save, "cats", None), "cats", None)
    if not isinstance(records, list):
        raise RuntimeError("キャラクター一覧が変更可能なリストではありません。")

    checked = changed = gacha_cleared = encyclopedia_cleared = skipped = 0
    for index, cat in enumerate(records):
        if cat is None or _integer(getattr(cat, "id", None)) != index:
            continue
        unlocked = _integer(getattr(cat, "unlocked", None))
        if unlocked != 0:
            # Strictly leave owned (1) and every unknown ownership state alone.
            continue
        checked += 1

        gacha_seen = _integer(getattr(cat, "gatya_seen", None))
        encyclopedia_collected = getattr(cat, "catguide_collected", None)
        if gacha_seen not in (0, 1) or not isinstance(encyclopedia_collected, bool):
            skipped += 1
            continue

        cat_changed = False
        if gacha_seen == 1:
            cat.gatya_seen = 0
            gacha_cleared += 1
            cat_changed = True
        if encyclopedia_collected:
            cat.catguide_collected = False
            encyclopedia_cleared += 1
            cat_changed = True
        if cat_changed:
            changed += 1

    return VisibilityRepairResult(
        checked, changed, gacha_cleared, encyclopedia_cleared, skipped
    )


def _stable_cat_value(value: Any) -> Any:
    """Convert common BCSFE scalar/list wrappers into comparable primitives."""
    raw = getattr(value, "value", value)
    if raw is None or isinstance(raw, (bool, int, float, str, bytes)):
        return raw
    if isinstance(raw, (list, tuple)):
        return tuple(_stable_cat_value(item) for item in raw)
    if isinstance(raw, dict):
        return tuple(sorted(
            (_stable_cat_value(key), _stable_cat_value(item))
            for key, item in raw.items()
        ))
    return repr(raw)


def _owned_cat_protected_state(cat: Any) -> tuple[Any, ...]:
    """Snapshot core progression fields for owned cats across serialization."""
    values = []
    for field in (
        "id", "unlocked", "current_form", "unlocked_forms", "gatya_seen",
        "fourth_form", "catseyes_used", "catguide_collected",
    ):
        values.append((field, _stable_cat_value(getattr(cat, field, None))))
    for field in ("upgrade", "max_upgrade_level"):
        upgrade = getattr(cat, field, None)
        values.append((field, tuple(
            (part, _stable_cat_value(getattr(upgrade, part, None)))
            for part in ("base", "plus")
        )))
    talents = getattr(cat, "talents", None)
    if talents is None:
        talent_state = None
    else:
        talent_state = tuple(
            (
                _stable_cat_value(getattr(talent, "id", None)),
                _stable_cat_value(getattr(talent, "level", None)),
            )
            for talent in talents
        )
    values.append(("talents", talent_state))
    return tuple(values)


def _snapshot_combo_progress(save: Any) -> dict[str, Any]:
    """Capture the combo encyclopedia state, which is outside character flags."""
    state = {}
    for field in ("combo_unlocks", "combo_unlocked_10k_ur"):
        if not hasattr(save, field):
            continue
        value = getattr(save, field)
        if isinstance(value, list):
            value = list(value)
        elif isinstance(value, tuple):
            value = tuple(value)
        state[field] = value
    return state


def _restore_combo_progress(save: Any, state: dict[str, Any]) -> None:
    """Restore combo encyclopedia fields without deriving them from cat flags."""
    for field, value in state.items():
        if isinstance(value, list):
            value = list(value)
        elif isinstance(value, tuple):
            value = tuple(value)
        setattr(save, field, value)


def _combo_progress_matches(save: Any, state: dict[str, Any]) -> bool:
    for field, expected in state.items():
        if not hasattr(save, field):
            return False
        if _stable_cat_value(getattr(save, field)) != _stable_cat_value(expected):
            return False
    return True

def repair_unowned_visibility_with_roundtrip_verification(
    save: Any, serialize, deserialize, audit
) -> tuple[Any, VisibilityRepairResult, Any]:
    """Back up, repair, reload, and verify owned progress before upload is allowed."""
    backup_bytes = serialize(save)
    combo_progress_before = _snapshot_combo_progress(save)
    records = getattr(getattr(save, "cats", None), "cats", None)
    if not isinstance(records, (list, tuple)):
        raise RuntimeError("キャラクター一覧を検証できません。")
    owned_before = {
        index: _owned_cat_protected_state(cat)
        for index, cat in enumerate(records)
        if cat is not None and _integer(getattr(cat, "id", None)) == index
        and _integer(getattr(cat, "unlocked", None)) == 1
    }
    visibility_targets = [
        index for index, cat in enumerate(records)
        if cat is not None
        and _integer(getattr(cat, "id", None)) == index
        and _integer(getattr(cat, "unlocked", None)) == 0
        and _integer(getattr(cat, "gatya_seen", None)) in (0, 1)
        and isinstance(getattr(cat, "catguide_collected", None), bool)
    ]
    try:
        result = repair_unowned_character_visibility_flags(save)
        # This operation is strictly character-flag-only. Preserve the combo
        # encyclopedia state even if a BCSFE round-trip rebuilds it.
        _restore_combo_progress(save, combo_progress_before)
        verified_save = deserialize(serialize(save))
        _restore_combo_progress(verified_save, combo_progress_before)
        if not _combo_progress_matches(verified_save, combo_progress_before):
            raise RuntimeError("にゃんこコンボ図鑑の状態を修復前と同じに保てませんでした。")
        verified_records = getattr(
            getattr(verified_save, "cats", None), "cats", None
        )
        if not isinstance(verified_records, (list, tuple)):
            raise RuntimeError("再読み込み後のキャラクター一覧が不正です。")
        if len(verified_records) != len(records):
            raise RuntimeError("修復前後でキャラクター一覧の件数が変化しました。")
        for cat_id in visibility_targets:
            cat = verified_records[cat_id]
            if (_integer(getattr(cat, "id", None)) != cat_id
                    or _integer(getattr(cat, "unlocked", None)) != 0
                    or _integer(getattr(cat, "gatya_seen", None)) != 0
                    or getattr(cat, "catguide_collected", None) is not False):
                raise RuntimeError(
                    f"未所持キャラID {cat_id} のガチャ/図鑑記録が残っています。"
                )
        for cat_id, before_state in owned_before.items():
            cat = verified_records[cat_id]
            if (cat is None or _integer(getattr(cat, "id", None)) != cat_id
                    or _integer(getattr(cat, "unlocked", None)) != 1
                    or _owned_cat_protected_state(cat) != before_state):
                raise RuntimeError(
                    f"所持キャラID {cat_id} の状態が修復前後で一致しません。"
                )
        post_audit = audit(verified_save)
        if not post_audit.ok:
            raise RuntimeError("修復後のセーブ構造検査に失敗しました。")
        return verified_save, result, post_audit
    except Exception as repair_error:
        restored_save = None
        restore_error = None
        try:
            restored_save = deserialize(backup_bytes)
        except Exception as exc:  # noqa: BLE001
            restore_error = exc
        message = f"ガチャ/図鑑記録の修復検証に失敗しました: {repair_error}"
        if restore_error is not None:
            message += f"。バックアップ復元にも失敗しました: {restore_error}"
        raise RepairVerificationError(
            message, restored_save=restored_save, restore_error=restore_error
        ) from repair_error
