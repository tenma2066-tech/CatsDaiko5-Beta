"""種類ごとの数量指定・本能玉・施設全強化・本能解放・第4形態の回帰テスト（ネットワーク不要）。"""
import os
import types
import unittest
from unittest import mock



import engine
import item_catalog


class Amount:
    def __init__(self, amount=0):
        self.amount = amount


class Orbs:
    def __init__(self):
        self.orbs = {}

    def set_orb(self, orb_id, value):
        self.orbs[orb_id] = types.SimpleNamespace(id=orb_id, value=value)


def fake_save():
    return types.SimpleNamespace(
        catseyes=[0] * 6, catfruit=[0] * 29, catamins=[0] * 3, labyrinth_medals=[0] * 4,
        battle_items=types.SimpleNamespace(items=[Amount() for _ in range(6)]),
        ototo=types.SimpleNamespace(base_materials=types.SimpleNamespace(materials=[Amount() for _ in range(16)])),
        talent_orbs=Orbs(), event_capsules=[0] * 3, event_capsules_2=[0] * 2, lucky_tickets=[0] * 4,
        platinum_shards=0, hundred_million_ticket=0,
    )


class ItemTypeTests(unittest.TestCase):
    def test_catalog_shapes(self):
        self.assertEqual(len(item_catalog.ORB_NAMES), 310)
        self.assertEqual(item_catalog.ORB_NAMES[4], "ダメージアップ【赤】 S")
        self.assertEqual(engine.type_label("マタタビ#29"), "虹獣石")
        self.assertEqual(engine.type_label("キャッツアイ#6"), "キャッツアイ【闇】")
        self.assertEqual(engine.type_label("城素材#16"), "アンモナイトZ")
        self.assertIsNone(engine.split_type_key("マタタビ#30"))
        self.assertIsNone(engine.split_type_key("猫缶#1"))

    def test_each_type_is_set_and_overrides_whole_item(self):
        save = fake_save()
        qty = {"本能玉#5": 7, "バトルアイテム#2": 11, "ネコビタン#3": 12, "城素材#16": 13,
               "地底メダル#4": 14, "キャッツアイ#6": 15, "マタタビ#29": 16,
               "イベチケ&福チケ#1": 17, "イベチケ&福チケ#2": 18, "マタタビ": 5}
        self.assertEqual(engine.apply_item_amounts(save, list(qty), quantity_map=qty), [])
        self.assertEqual(save.talent_orbs.orbs[4].value, 7)
        self.assertEqual(save.battle_items.items[1].amount, 11)
        self.assertEqual(save.catamins[2], 12)
        self.assertEqual(save.ototo.base_materials.materials[15].amount, 13)
        self.assertEqual(save.labyrinth_medals[3], 14)
        self.assertEqual(save.catseyes[5], 15)
        self.assertEqual(save.catfruit[28], 16)
        self.assertEqual(save.catfruit[0], 5)
        self.assertEqual(save.event_capsules, [17] * 3)
        self.assertEqual(save.event_capsules_2, [17] * 2)
        self.assertEqual(save.lucky_tickets, [18] * 4)

    def test_failure_is_reported_with_type_name(self):
        save = fake_save()
        save.catfruit = [0] * 10  # 古いセーブで種類が足りない
        self.assertEqual(engine.apply_item_amounts(save, ["マタタビ#29"], quantity_map={"マタタビ#29": 1}), ["虹獣石"])

    def test_orbs_granted_on_account_without_orb_records(self):
        save = fake_save()
        with mock.patch("bcsfe.core.game.catbase.talent_orbs.OrbInfoList.create", return_value=None):
            self.assertEqual(engine.apply_item_amounts(save, ["本能玉"]), [])
        self.assertEqual(len(save.talent_orbs.orbs), 310)
        self.assertEqual({o.value for o in save.talent_orbs.orbs.values()}, {998})

    def test_new_simple_items(self):
        save = fake_save()
        self.assertEqual(engine.apply_item_amounts(save, ["プラチナのかけら", "1億DL記念チケット"]), [])
        self.assertEqual(save.platinum_shards, 99)
        self.assertEqual(save.hundred_million_ticket, 9999)

    def test_snapshot_lists_each_type(self):
        save = fake_save()
        save.catfruit[28] = 3
        snap = engine.snapshot(save)
        self.assertEqual(snap["マタタビ：虹獣石"], 3)
        self.assertEqual(snap["マタタビ合計"], 3)
        self.assertIn("城素材：アンモナイトZ", snap)
        self.assertIn("本能玉の種類数", snap)


class FakeTalent:
    def __init__(self, talent_id, level):
        self.id = talent_id
        self.level = level


class FakeCat:
    def __init__(self, talents):
        self.id = 9
        self.unlocked = 1
        self.talents = talents

    def get_talent_from_id(self, talent_id):
        return next((t for t in self.talents if t.id == talent_id), None)


class FakeTalentData:
    def get_cat_skill(self, cat_id):
        return types.SimpleNamespace(skills=[types.SimpleNamespace(ability_id=i) for i in (10, 55, 20, 0)])

    def get_cat_talents(self, cat):
        ids = [t.id for t in cat.talents if t.id != 0]
        levels = {10: 10, 55: 1, 20: 10}
        return (None, [levels[i] for i in ids], None, ids)


class TalentTests(unittest.TestCase):
    def test_existing_talents_get_missing_ones_added(self):
        cat = FakeCat([FakeTalent(10, 3)])
        with mock.patch("bcsfe.core.game.catbase.cat.Talent", FakeTalent):
            self.assertTrue(engine.max_cat_talents(None, cat, FakeTalentData()))
        self.assertEqual([(t.id, t.level) for t in cat.talents], [(10, 10), (55, 1), (20, 10)])

    def test_bulk_talents_use_same_helper(self):
        cat = FakeCat([FakeTalent(10, 3)])
        save = types.SimpleNamespace(cats=types.SimpleNamespace(
            cats=[cat], read_talent_data=lambda _save: FakeTalentData()))
        with mock.patch("bcsfe.core.game.catbase.cat.Talent", FakeTalent):
            self.assertEqual(engine.apply_subs_and_others(save, ["全キャラ本能解放"], []), [])
        self.assertEqual(len(cat.talents), 3)


class FacilityTests(unittest.TestCase):
    def test_facility_upgrade_does_not_lower_materials(self):
        save = fake_save()
        save.ototo.base_materials.materials[0].amount = 9999
        save.ototo.engineers = 0
        save.ototo.cannons = None
        save.special_skills = types.SimpleNamespace(get_valid_skills=lambda: [], skills=[])
        with mock.patch.object(engine.core, "AbilityData", lambda _s: types.SimpleNamespace(ability_data=None)), \
                mock.patch.object(engine.core.Ototo, "get_max_engineers", lambda _s: 10):
            engine.apply_subs_and_others(save, [], ["施設全強化"])
        self.assertEqual(save.ototo.base_materials.materials[0].amount, 9999)
        self.assertEqual(save.ototo.base_materials.materials[1].amount, 999)

    def test_fix_items_run_before_facility_upgrade(self):
        calls = []
        save = types.SimpleNamespace(gamatoto=types.SimpleNamespace(skin=0))
        with mock.patch.object(engine, "claim_user_rank_rewards", lambda _s: calls.append("rank") or True):
            failures = engine.apply_subs_and_others(save, [], ["ユーザーランク報酬全受け取り", "ガマトトのクラッシュ修正"])
        self.assertEqual(failures, [])
        self.assertEqual(save.gamatoto.skin, 2)
        self.assertEqual(calls, ["rank"])


class AppTests(unittest.TestCase):
    def setUp(self):
        import app
        self.app = app
        self.client = app.app.test_client()
        self.patch = mock.patch.object(app, "PAYMENTS_ENABLED", False)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()

    def test_new_options_are_listed(self):
        self.assertIn("プラチナのかけら", self.app.ITEMS)
        self.assertNotIn("1億DL記念チケット", self.app.ITEMS)   # メニューから外した（古い注文のため engine には残す）
        self.assertNotIn('data-item="1億DL記念チケット"', self.client.get("/").get_data(as_text=True))
        for name in ("ユーザーランク報酬全受け取り", "チュートリアルクリア",
                     "ガマトトのクラッシュ修正", "オトート砲台のクラッシュ修正"):
            self.assertIn(name, self.app.OTHERS)
        self.assertIn("プラチナのかけら", self.app.HIGH_RISK_ITEMS)
        self.assertFalse(hasattr(self.app, "_combo_progress_debug"))

    def test_page_renders_orb_grid_and_presets(self):
        html = self.client.get("/").get_data(as_text=True)
        self.assertIn('data-item="本能玉#310"', html)
        self.assertIn('data-label="虹獣石"', html)
        self.assertIn('id="presetSave"', html)
        self.assertIn('id="keepResults"', html)
        self.assertIn("第4形態", html)

    def test_typed_limit_message_uses_type_name(self):
        r = self.client.post("/run", json={"qty": {"本能玉#5": "999"}, "safe": False})
        self.assertEqual(r.status_code, 400)
        self.assertIn("ダメージアップ【赤】 S", r.get_json()["error"])

    def test_form_four_is_accepted_and_five_rejected(self):
        r = self.client.post("/run", json={"chars": "1", "cat_settings": {"1": {"form": 5}}, "safe": False})
        self.assertEqual(r.status_code, 400)
        self.assertIn("1〜4", r.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
