"""Regression tests for targeted per-character editing."""
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import engine


class CatCustomizationTests(unittest.TestCase):
    def test_applies_level_form_and_talents_only_to_selected_character(self):
        class FakeTalent:
            def __init__(self, ability_id, level):
                self.id = ability_id
                self.level = level

        class FakePowerUp:
            def __init__(self, cat, save):
                self.cat = cat
            def get_max_possible_base(self):
                return 60
            def get_max_possible_plus(self):
                return 70

        selected = SimpleNamespace(
            id=10, unlocked=True,
            upgrade=SimpleNamespace(base=0, plus=0),
            max_upgrade_level=SimpleNamespace(base=30, plus=10),
            talents=None,
            set_form_true=lambda save, form, fourth_form=False:
                setattr(selected, "form_call", (form, fourth_form)),
            get_talent_from_id=lambda talent_id: next(
                (talent for talent in (selected.talents or []) if talent.id == talent_id), None
            ),
        )
        untouched = SimpleNamespace(
            id=11, unlocked=True,
            upgrade=SimpleNamespace(base=4, plus=2),
            talents=None,
        )
        talent_data = SimpleNamespace(
            get_cat_skill=lambda cat_id: SimpleNamespace(skills=[SimpleNamespace(ability_id=501)]),
            get_cat_talents=lambda cat: (None, [10], None, [501]),
        )
        save = SimpleNamespace(
            cats=SimpleNamespace(
                cats=[selected, untouched],
                read_nyanko_picture_book=lambda save: SimpleNamespace(
                    get_cat=lambda cat_id: SimpleNamespace(total_forms=3)
                ),
                read_talent_data=lambda save: talent_data,
            )
        )
        powerup_module = types.ModuleType("bcsfe.core.game.catbase.powerup")
        powerup_module.PowerUpHelper = FakePowerUp
        cat_module = types.ModuleType("bcsfe.core.game.catbase.cat")
        cat_module.Talent = FakeTalent
        modules = {
            "bcsfe.core.game.catbase.powerup": powerup_module,
            "bcsfe.core.game.catbase.cat": cat_module,
        }
        def unlock(save, ids):
            for cat in save.cats.cats:
                if cat.id in ids:
                    cat.unlocked = True
            return {"unlocked": [], "already": ids, "missing": [], "error_chars": [],
                    "hidden": [], "failed": []}
        with patch.object(engine, "unlock_cats_by_ids", side_effect=unlock), patch.dict(sys.modules, modules):
            result = engine.apply_cat_customizations(save, {
                10: {"base_level": 60, "plus_level": 70, "form": 3, "talents_all": True},
            })

        self.assertEqual(selected.upgrade.base, 59)
        self.assertEqual(selected.upgrade.plus, 70)
        self.assertEqual(selected.max_upgrade_level.base, 59)
        self.assertEqual(selected.max_upgrade_level.plus, 70)
        self.assertEqual(selected.form_call, (3, False))
        self.assertEqual(selected.talents[0].level, 10)
        self.assertEqual(untouched.upgrade.base, 4)
        self.assertEqual(untouched.upgrade.plus, 2)
        self.assertEqual(result["leveled"], [10])
        self.assertEqual(result["formed"], [10])
        self.assertEqual(result["talented"], [10])

    def test_form_failure_rolls_back_level_and_form_changes(self):
        class FakePowerUp:
            def __init__(self, cat, save):
                pass
            def get_max_possible_base(self):
                return 60
            def get_max_possible_plus(self):
                return 70

        selected = SimpleNamespace(
            id=3, unlocked=True,
            upgrade=SimpleNamespace(base=7, plus=8),
            max_upgrade_level=SimpleNamespace(base=9, plus=10),
            current_form=1, unlocked_forms=2, fourth_form=0, talents=None,
            set_form_true=lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("form failure")),
        )
        save = SimpleNamespace(cats=SimpleNamespace(
            cats=[selected],
            read_nyanko_picture_book=lambda save: SimpleNamespace(
                get_cat=lambda cat_id: SimpleNamespace(total_forms=3)
            ),
        ))
        powerup_module = types.ModuleType("bcsfe.core.game.catbase.powerup")
        powerup_module.PowerUpHelper = FakePowerUp
        with patch.object(engine, "unlock_cats_by_ids", return_value={
            "unlocked": [], "already": [3], "missing": [], "error_chars": [],
            "hidden": [], "failed": [],
        }), patch.dict(sys.modules, {"bcsfe.core.game.catbase.powerup": powerup_module}):
            result = engine.apply_cat_customizations(save, {
                3: {"base_level": 50, "form": 3},
            })
        self.assertEqual(selected.upgrade.base, 7)
        self.assertEqual(selected.upgrade.plus, 8)
        self.assertEqual(selected.max_upgrade_level.base, 9)
        self.assertEqual(selected.max_upgrade_level.plus, 10)
        self.assertEqual(selected.current_form, 1)
        self.assertEqual(selected.unlocked_forms, 2)
        self.assertEqual(result["failed"], [3])
        self.assertEqual(result["leveled"], [])
        self.assertEqual(result["formed"], [])

    def test_rejects_level_above_character_limit_without_touching_other_cats(self):
        class FakePowerUp:
            def __init__(self, cat, save):
                pass
            def get_max_possible_base(self):
                return 50
            def get_max_possible_plus(self):
                return 20

        selected = SimpleNamespace(
            id=2, unlocked=True, upgrade=SimpleNamespace(base=3, plus=4),
            max_upgrade_level=SimpleNamespace(base=0, plus=0), talents=None,
        )
        save = SimpleNamespace(cats=SimpleNamespace(
            cats=[selected],
            read_nyanko_picture_book=lambda save: None,
        ))
        powerup_module = types.ModuleType("bcsfe.core.game.catbase.powerup")
        powerup_module.PowerUpHelper = FakePowerUp
        with patch.object(engine, "unlock_cats_by_ids", return_value={
            "unlocked": [], "already": [2], "missing": [], "error_chars": [],
            "hidden": [], "failed": [],
        }), patch.dict(sys.modules, {"bcsfe.core.game.catbase.powerup": powerup_module}):
            result = engine.apply_cat_customizations(save, {2: {"base_level": 60}})
        self.assertEqual(selected.upgrade.base, 3)
        self.assertEqual(selected.upgrade.plus, 4)
        self.assertEqual(selected.max_upgrade_level.base, 0)
        self.assertEqual(selected.max_upgrade_level.plus, 0)
        self.assertEqual(result["failed"], [2])


if __name__ == "__main__":
    unittest.main()
