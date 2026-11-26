"""配布元のゲームデータにまだ無い新キャラ（engine.SUPPLEMENT_CATS）の扱い。

- Ver16.0 以降のセーブで、セーブにキャラ枠があるときだけ解放する
- 古いセーブ・図鑑に出ない召喚ユニット（887）は解放しない
- 配布元のデータに載ったら、そちらの判定を優先する
"""
import os
import unittest
from types import SimpleNamespace

import engine

os.environ.setdefault("APP_PASSWORD", "test-only")


class FakeCat:
    def __init__(self, cid):
        self.id = cid
        self.unlocked = 0
        self.gatya_seen = 0

    def unlock(self, save):
        self.unlocked = 1
        self.gatya_seen = 1


def entry(cid, shown=True):
    return SimpleNamespace(cat_id=cid, is_displayed_in_catguide=shown)


def fake_save(version, ids, book):
    return SimpleNamespace(
        game_version=version,
        cats=SimpleNamespace(cats=[FakeCat(i) for i in ids],
                             read_nyanko_picture_book=lambda save: SimpleNamespace(cats=book)),
        unlock_equip_menu=lambda: None,
    )


OLD_BOOK = [entry(i) for i in range(10)] + [entry(875, False), entry(876, False), entry(881)]
NEW_IDS = sorted(engine.SUPPLEMENT_CATS)


class SupplementCats(unittest.TestCase):
    def test_list_matches_sources(self):
        self.assertEqual(NEW_IDS, [882, 883, 884, 885, 886])
        self.assertNotIn(887, engine.SUPPLEMENT_CATS)                # 精霊（召喚ユニット）は図鑑外
        self.assertEqual(engine.SUPPLEMENT_CATS[886][0], "悠久の冒険者エターニス")
        self.assertFalse(set(NEW_IDS) & engine.ERROR_CAT_IDS)

    def test_unlocks_on_a_16_save_that_has_the_slots(self):
        save = fake_save("16.0.0", list(range(10)) + [881] + list(range(882, 888)), OLD_BOOK)
        r = engine.unlock_cats_by_ids(save, NEW_IDS + [887])
        self.assertEqual(r["unlocked"], NEW_IDS)
        self.assertEqual(r["hidden"], [887])
        self.assertTrue(all(c.unlocked for c in save.cats.cats if c.id in engine.SUPPLEMENT_CATS))

    def test_not_on_an_old_save(self):
        save = fake_save("15.7.2", list(range(10)) + [881], OLD_BOOK)
        r = engine.unlock_cats_by_ids(save, [882, 886])
        self.assertEqual((r["unlocked"], r["missing"]), ([], [882, 886]))   # 枠が無い
        save = fake_save("15.7.2", list(range(10)) + [881, 882], OLD_BOOK)  # 枠があっても版が古い
        self.assertEqual(engine.unlock_cats_by_ids(save, [882])["hidden"], [882])

    def test_real_data_wins_once_published(self):
        book = OLD_BOOK + [entry(i) for i in range(882, 886)] + [entry(886, False)]
        save = fake_save("16.0.0", list(range(882, 888)), book)
        r = engine.unlock_cats_by_ids(save, NEW_IDS)
        self.assertEqual((r["unlocked"], r["hidden"]), ([882, 883, 884, 885], [886]))

    def test_unreadable_picture_book_unlocks_nothing(self):
        save = fake_save("16.0.0", list(range(882, 887)), None)
        save.cats.read_nyanko_picture_book = lambda s: (_ for _ in ()).throw(RuntimeError("no data"))
        self.assertEqual(engine.unlock_cats_by_ids(save, NEW_IDS)["hidden"], NEW_IDS)

    def test_bulk_unlock_includes_new_cats_but_not_the_summon(self):
        save = fake_save("16.0.0", list(range(10)) + list(range(882, 888)), OLD_BOOK)
        save.rank_up_sale_value = 0
        engine.apply_subs_and_others(save, ["全キャラ解放&エラーキャラ削除"], [])
        got = {c.id for c in save.cats.cats if c.unlocked}
        self.assertTrue(set(NEW_IDS) <= got)
        self.assertNotIn(887, got)


class CatalogList(unittest.TestCase):
    def test_new_cats_are_listed_until_the_data_has_them(self):
        import app
        saved = dict(app._names)
        try:
            app._set_names({0: ["ネコ"], 881: ["比古清十郎"]}, "test", "15.7.2")
            items = {i["n"]: i for i in app._names["items"]}
            self.assertEqual(items[887]["name"], "悠久の冒険者エターニス")   # 画面の番号は内部ID＋1
            self.assertEqual(items[887]["new"], 1)
            self.assertNotIn(888, items)
            self.assertNotIn("new", items[882])
            app._set_names({882: ["監獄ネコ", "脱獄ネコ"]}, "test", "16.0.0")
            items = {i["n"]: i for i in app._names["items"]}
            self.assertNotIn("new", items[883])                          # データにあるものは補足扱いしない
            self.assertEqual(items[884]["new"], 1)
        finally:
            app._names.clear()
            app._names.update(saved)


DATA = os.path.expanduser("~/.local/share/bcsfe/game_data/jp/15.7.2")


@unittest.skipUnless(os.path.isdir(DATA), "15.7.2 のゲームデータが手元に無い（配布元から取得していない）")
class WithRealGameData(unittest.TestCase):
    """本物の bcsfe とゲームデータ（15.7.2）で：16.0 のセーブに見立てて解放できるか。"""

    def test_real_save(self):
        import app
        from bcsfe.core.game.catbase.cat import Cat
        cc, gv = app._get_bcsfe()
        app._select_game_data(cc, gv, "15.7.2")
        save = engine.core.SaveFile(cc=cc, gv=gv)
        engine.ensure_cat_roster(save)
        self.assertEqual(max(engine._cat_internal_id(c) for c in save.cats.cats), 881)
        self.assertEqual(engine.unlock_cats_by_ids(save, [882])["missing"], [882])
        save.cats.cats.extend(Cat.init(i) for i in range(882, 888))        # 16.0 のゲームが書いたセーブの枠
        save.game_version = engine.core.GameVersion.from_string("16.0.0")
        r = engine.unlock_cats_by_ids(save, NEW_IDS + [887])
        self.assertEqual((r["unlocked"], r["hidden"]), (NEW_IDS, [887]))
        data = save.to_data()                                              # 書き出して読み直しても残る
        again = engine.core.SaveFile(engine.core.Data(data.get_bytes()), cc=cc)
        self.assertTrue(all(c.unlocked for c in again.cats.cats if engine._cat_internal_id(c) in engine.SUPPLEMENT_CATS))


if __name__ == "__main__":
    unittest.main()
