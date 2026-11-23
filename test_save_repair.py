"""Tests for conservative known-error-character repair planning."""
import types
import unittest

from save_repair import (
    apply_known_error_character_repairs,
    plan_known_error_character_repairs,
)


class FakeCat:
    def __init__(self, cat_id, unlocked=0):
        self.id = cat_id
        self.unlocked = unlocked
        self.current_form = 2
        self.unlocked_forms = 2
        self.gatya_seen = 1
        self.fourth_form = 0
        self.catseyes_used = 2
        self.catguide_collected = True
        self.talents = [types.SimpleNamespace(id=1, level=3)]
        self.upgrade = types.SimpleNamespace(base=10, plus=2)
        self.max_upgrade_level = types.SimpleNamespace(base=20, plus=5)

    def remove(self, reset=False, save_file=None):
        if getattr(self, "fail_remove", False):
            raise RuntimeError("simulated remove failure")
        if reset:
            self.unlocked = 0
            self.current_form = 0
            self.unlocked_forms = 0
            self.gatya_seen = 0
            self.fourth_form = 0
            self.catseyes_used = 0
            self.catguide_collected = False
            self.talents = [types.SimpleNamespace(id=1, level=0)]
            self.upgrade.base = 0
            self.upgrade.plus = 0
            cat_id = getattr(self.id, 'value', self.id)
            save_file.cats.chara_new_flags[cat_id] = 0


class SaveRepairTests(unittest.TestCase):
    def make_save(self):
        cats = [FakeCat(i, unlocked=1 if i == 2 else 0) for i in range(4)]
        lineup_entries = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        lineup_entries[0].cat_id = 2
        return types.SimpleNamespace(
            cats=types.SimpleNamespace(
                cats=cats,
                favourites={2: True},
                storage_items=[types.SimpleNamespace(item_id=2, item_type=1)],
                chara_new_flags={},
            ),
            lineups=types.SimpleNamespace(
                slots=[types.SimpleNamespace(slots=lineup_entries)]
            ),
        )

    def test_plan_detects_owned_cat_and_references_without_mutation(self):
        save = self.make_save()
        actions = plan_known_error_character_repairs(save, {2, 3})
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].cat_id, 2)
        self.assertIn("owned_known_error_character", actions[0].reasons)
        self.assertIn("lineup_reference", actions[0].reasons)
        self.assertEqual(save.cats.cats[2].unlocked, 1)

    def test_apply_resets_record_and_clears_references(self):
        save = self.make_save()
        actions = plan_known_error_character_repairs(save, {2})
        result = apply_known_error_character_repairs(save, actions)
        cat = save.cats.cats[2]
        self.assertEqual(result.repaired_count, 1)
        self.assertEqual(cat.unlocked, 0)
        self.assertEqual(cat.current_form, 0)
        self.assertFalse(cat.catguide_collected)
        self.assertEqual(cat.upgrade.base, 0)
        self.assertEqual(cat.max_upgrade_level.base, 0)
        self.assertNotIn(2, save.cats.favourites)
        self.assertEqual(save.cats.storage_items[0].item_type, 0)
        self.assertEqual(save.cats.storage_items[0].item_id, 0)
        self.assertEqual(save.lineups.slots[0].slots[0].cat_id, -1)
        self.assertEqual(save.cats.chara_new_flags[2], 0)

    def test_locked_unreferenced_record_is_not_repaired(self):
        save = self.make_save()
        actions = plan_known_error_character_repairs(save, {3})
        self.assertEqual(actions, ())

    def test_missing_remove_method_fails_before_mutation(self):
        save = self.make_save()
        actions = plan_known_error_character_repairs(save, {2})
        save.cats.cats[2].remove = None
        with self.assertRaises(RuntimeError):
            apply_known_error_character_repairs(save, actions)
        self.assertEqual(save.cats.cats[2].unlocked, 1)


    def test_inactive_favourite_does_not_trigger_repair(self):
        save = self.make_save()
        save.cats.cats[2].unlocked = 0
        save.cats.favourites = {2: False}
        save.cats.storage_items = []
        save.lineups.slots[0].slots[0].cat_id = -1
        actions = plan_known_error_character_repairs(save, {2})
        self.assertEqual(actions, ())



    def test_tuple_backed_references_are_planned_and_cleared(self):
        save = self.make_save()
        save.cats.cats[2].unlocked = 0
        save.cats.storage_items = (
            types.SimpleNamespace(item_id=types.SimpleNamespace(value=2),
                                  item_type=types.SimpleNamespace(value=1)),
        )
        entries = [types.SimpleNamespace(cat_id=-1) for _ in range(10)]
        entries[0].cat_id = types.SimpleNamespace(value=2)
        save.lineups.slots = (types.SimpleNamespace(slots=tuple(entries)),)
        actions = plan_known_error_character_repairs(save, {2})
        self.assertEqual(len(actions), 1)
        apply_known_error_character_repairs(save, actions)
        self.assertEqual(save.cats.storage_items[0].item_id, 0)
        self.assertEqual(save.cats.storage_items[0].item_type, 0)
        self.assertEqual(save.lineups.slots[0].slots[0].cat_id, -1)



    def test_wrapped_character_id_is_supported(self):
        save = self.make_save()
        save.cats.cats[2].id = types.SimpleNamespace(value=2)
        actions = plan_known_error_character_repairs(save, {2})
        self.assertEqual(len(actions), 1)
        apply_known_error_character_repairs(save, actions)
        self.assertEqual(save.cats.cats[2].unlocked, 0)


if __name__ == "__main__":
    unittest.main()



class RepairTransactionTests(unittest.TestCase):
    def make_transaction_save(self):
        save = SaveRepairTests().make_save()
        return save

    @staticmethod
    def serialize(save):
        # The fake serializer stores the current object snapshot in a byte token.
        import pickle
        return pickle.dumps(save)

    @staticmethod
    def deserialize(payload):
        import pickle
        return pickle.loads(payload)

    @staticmethod
    def passing_audit(save, known_error_cat_ids):
        from save_audit import AuditReport
        return AuditReport((), len(save.cats.cats))

    def test_verified_transaction_returns_fresh_repaired_save(self):
        from save_repair import repair_with_roundtrip_verification
        save = self.make_transaction_save()
        save.combo_unlocks = [2, 5, 11]
        save.combo_unlocked_10k_ur = True
        actions = plan_known_error_character_repairs(save, {2})
        verified, result, audit = repair_with_roundtrip_verification(
            save, actions, {2}, self.serialize, self.deserialize,
            self.passing_audit,
        )
        self.assertIsNot(verified, save)
        self.assertEqual(result.repaired_count, 1)
        self.assertEqual(verified.cats.cats[2].unlocked, 0)
        self.assertEqual(verified.combo_unlocks, [2, 5, 11])
        self.assertIs(verified.combo_unlocked_10k_ur, True)
        self.assertTrue(audit.ok)

    def test_known_error_roundtrip_restores_combo_fields_dropped_by_parser(self):
        from save_repair import repair_with_roundtrip_verification
        save = self.make_transaction_save()
        save.combo_unlocks = [2, 5, 11]
        save.combo_unlocked_10k_ur = True
        actions = plan_known_error_character_repairs(save, {2})

        def deserialize_without_combo_fields(payload):
            restored = self.deserialize(payload)
            restored.combo_unlocks = []
            restored.combo_unlocked_10k_ur = False
            return restored

        verified, _, _ = repair_with_roundtrip_verification(
            save, actions, {2}, self.serialize, deserialize_without_combo_fields,
            self.passing_audit,
        )
        self.assertEqual(verified.combo_unlocks, [2, 5, 11])
        self.assertIs(verified.combo_unlocked_10k_ur, True)

    def test_failed_post_audit_returns_original_backup(self):
        from save_repair import repair_with_roundtrip_verification, RepairVerificationError
        from save_audit import AuditReport, Finding
        save = self.make_transaction_save()
        actions = plan_known_error_character_repairs(save, {2})
        def failing_audit(target, known_error_cat_ids):
            return AuditReport((
                Finding("still_bad", "critical", "save.test", "post audit failure"),
            ), len(target.cats.cats))
        with self.assertRaises(RepairVerificationError) as caught:
            repair_with_roundtrip_verification(
                save, actions, {2}, self.serialize, self.deserialize,
                failing_audit,
            )
        self.assertIsNotNone(caught.exception.restored_save)
        self.assertEqual(caught.exception.restored_save.cats.cats[2].unlocked, 1)
        self.assertEqual(caught.exception.restored_save.cats.favourites[2], True)

    def test_failed_repair_restores_backup(self):
        from save_repair import repair_with_roundtrip_verification, RepairVerificationError
        save = self.make_transaction_save()
        actions = plan_known_error_character_repairs(save, {2})
        save.cats.cats[2].fail_remove = True
        with self.assertRaises(RepairVerificationError) as caught:
            repair_with_roundtrip_verification(
                save, actions, {2}, self.serialize, self.deserialize,
                self.passing_audit,
            )
        self.assertIsNotNone(caught.exception.restored_save)
        self.assertEqual(caught.exception.restored_save.cats.cats[2].unlocked, 1)
        self.assertEqual(caught.exception.restored_save.cats.favourites[2], True)

    def test_restore_failure_is_reported(self):
        from save_repair import repair_with_roundtrip_verification, RepairVerificationError
        save = self.make_transaction_save()
        actions = plan_known_error_character_repairs(save, {2})
        calls = {"count": 0}
        def flaky_deserialize(payload):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("simulated restore failure")
            return self.deserialize(payload)
        with self.assertRaises(RepairVerificationError) as caught:
            repair_with_roundtrip_verification(
                save, actions, {2}, self.serialize, flaky_deserialize,
                lambda target, known_error_cat_ids: (_ for _ in ()).throw(
                    RuntimeError("force post-repair failure")
                ),
            )
        self.assertIsNone(caught.exception.restored_save)
        self.assertIsNotNone(caught.exception.restore_error)


class OwnedOnlyVisibilityRepairTests(unittest.TestCase):
    def make_save(self):
        cats = [FakeCat(i, unlocked=1 if i == 1 else 0) for i in range(4)]
        cats[0].gatya_seen = 1
        cats[0].catguide_collected = True
        cats[1].gatya_seen = 1
        cats[1].catguide_collected = True
        cats[2].gatya_seen = 0
        cats[2].catguide_collected = False
        cats[3].gatya_seen = 7  # Unknown values must remain untouched.
        return types.SimpleNamespace(
            cats=types.SimpleNamespace(cats=cats),
            combo_unlocks=[2, 5, 11],
            combo_unlocked_10k_ur=True,
        )

    def test_only_unowned_visibility_flags_are_cleared(self):
        from save_repair import repair_unowned_character_visibility_flags
        save = self.make_save()
        owned_before = (
            save.cats.cats[1].unlocked,
            save.cats.cats[1].gatya_seen,
            save.cats.cats[1].catguide_collected,
            save.cats.cats[1].upgrade.base,
            save.cats.cats[1].talents[0].level,
        )
        result = repair_unowned_character_visibility_flags(save)
        self.assertEqual(result.changed_count, 1)
        self.assertEqual(result.gacha_flags_cleared, 1)
        self.assertEqual(result.encyclopedia_flags_cleared, 1)
        self.assertEqual(result.skipped_ambiguous, 1)
        self.assertEqual(save.cats.cats[0].gatya_seen, 0)
        self.assertFalse(save.cats.cats[0].catguide_collected)
        self.assertEqual(
            (
                save.cats.cats[1].unlocked,
                save.cats.cats[1].gatya_seen,
                save.cats.cats[1].catguide_collected,
                save.cats.cats[1].upgrade.base,
                save.cats.cats[1].talents[0].level,
            ),
            owned_before,
        )
        self.assertEqual(save.cats.cats[3].gatya_seen, 7)

    def test_misaligned_and_unknown_ownership_are_not_modified(self):
        from save_repair import repair_unowned_character_visibility_flags
        save = self.make_save()
        save.cats.cats[2].unlocked = 2
        save.cats.cats[3].id = 99
        result = repair_unowned_character_visibility_flags(save)
        self.assertEqual(result.changed_count, 1)
        self.assertEqual(save.cats.cats[2].gatya_seen, 0)
        self.assertEqual(save.cats.cats[3].gatya_seen, 7)

    def test_roundtrip_verification_preserves_owned_progress(self):
        import pickle
        from save_repair import repair_unowned_visibility_with_roundtrip_verification
        from save_audit import AuditReport
        save = self.make_save()
        verified, result, report = repair_unowned_visibility_with_roundtrip_verification(
            save,
            pickle.dumps,
            pickle.loads,
            lambda target: AuditReport((), len(target.cats.cats)),
        )
        self.assertIsNot(verified, save)
        self.assertEqual(result.changed_count, 1)
        self.assertEqual(verified.cats.cats[0].gatya_seen, 0)
        self.assertEqual(verified.cats.cats[0].catguide_collected, False)
        self.assertEqual(verified.cats.cats[1].unlocked, 1)
        self.assertEqual(verified.cats.cats[1].gatya_seen, 1)
        self.assertTrue(report.ok)

    def test_roundtrip_preserves_combo_encyclopedia_state(self):
        import pickle
        from save_repair import repair_unowned_visibility_with_roundtrip_verification
        from save_audit import AuditReport

        save = self.make_save()

        def deserialize_with_lost_combo_state(payload):
            restored = pickle.loads(payload)
            # Simulate a parser/rebuild path that forgets combo-only fields.
            restored.combo_unlocks = []
            restored.combo_unlocked_10k_ur = False
            return restored

        verified, _, _ = repair_unowned_visibility_with_roundtrip_verification(
            save, pickle.dumps, deserialize_with_lost_combo_state,
            lambda target: AuditReport((), len(target.cats.cats)),
        )
        self.assertEqual(verified.combo_unlocks, [2, 5, 11])
        self.assertIs(verified.combo_unlocked_10k_ur, True)
    def test_failed_roundtrip_restores_original_backup(self):
        import pickle
        from save_repair import (
            repair_unowned_visibility_with_roundtrip_verification,
            RepairVerificationError,
        )
        from save_audit import AuditReport, Finding
        save = self.make_save()
        def failing_audit(target):
            return AuditReport((
                Finding("forced_failure", "critical", "save.test", "failure"),
            ), len(target.cats.cats))
        with self.assertRaises(RepairVerificationError) as caught:
            repair_unowned_visibility_with_roundtrip_verification(
                save, pickle.dumps, pickle.loads, failing_audit
            )
        self.assertIsNotNone(caught.exception.restored_save)
        self.assertEqual(caught.exception.restored_save.cats.cats[0].gatya_seen, 1)
        self.assertTrue(caught.exception.restored_save.cats.cats[0].catguide_collected)
