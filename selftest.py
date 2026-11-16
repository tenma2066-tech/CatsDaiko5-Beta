"""にゃんこ代行 自己テスト

使い方（Renderには不要。手元で書き換えたときの動作確認用）:
    pip install flask
    python selftest.py

本物の bcsfe やゲームサーバーは使わず、ダミーに差し替えて
「画面とAPIの流れ」「キャラ解放」「安全モード」「試運転」「キャラ名検索」を確認します。
"""
import os
import sys
import threading
import time
import types

# ---- bcsfe のダミー ----------------------------------------------------------
sys.modules["bcsfe"] = types.ModuleType("bcsfe")
sys.modules["bcsfe"].__path__ = []
sys.modules["bcsfe"].core = types.SimpleNamespace(ServerHandler=type("ServerHandler", (), {}), SaveFile=None)
# ensure_cat_roster が追加枠を作るときに使う Cat.init のダミー
for _name in ("bcsfe.core", "bcsfe.core.game", "bcsfe.core.game.catbase"):
    _mod = types.ModuleType(_name)
    _mod.__path__ = []
    sys.modules[_name] = _mod
_cat_mod = types.ModuleType("bcsfe.core.game.catbase.cat")
_cat_mod.Cat = types.SimpleNamespace(init=lambda i: Cat(i), unlock=lambda *a, **k: None)
sys.modules["bcsfe.core.game.catbase.cat"] = _cat_mod
os.environ["PAYMENTS_ENABLED"] = "0"  # selftest は決済なしで実行
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

for _f in ("/tmp/bcsfe_game_version.json", "/tmp/bcsfe_cat_names.json"):
    try:
        os.remove(_f)
    except OSError:
        pass

import engine  # noqa: E402
import save_audit  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  OK  " if cond else "  NG  ") + name + (f"   {detail}" if detail and not cond else ""))


# ---- ダミーのセーブ ----------------------------------------------------------
class Cat:
    def __init__(self, i, unlocked=False):
        self.id, self.unlocked, self.gatya_seen, self.catguide_collected = i, int(unlocked), 0, False
        self.current_form = 0
        self.unlocked_forms = 0
        self.fourth_form = 0
        self.catseyes_used = 0
        self.upgrade = types.SimpleNamespace(base=0, plus=0)
        self.max_upgrade_level = types.SimpleNamespace(base=0, plus=0)
        self.talents = None

    def unlock(self, save):
        self.unlocked = 1

    def get_names_cls(self, save):
        return [f"キャラ{self.id + 1}", f"キャラ{self.id + 1}進化"]


class Save:
    def __init__(self, n=300):
        self.catfood, self.xp, self.np = 10, 20, 30
        self.normal_tickets = self.rare_tickets = self.platinum_tickets = self.legend_tickets = 0
        self.leadership = 0
        self.eq = 0
        pb = types.SimpleNamespace(cats=[
            types.SimpleNamespace(cat_id=i, is_displayed_in_catguide=(i != 7)) for i in range(n)])
        self.cats = types.SimpleNamespace(
            cats=[Cat(i, unlocked=(i == 0)) for i in range(n)],
            storage_items=[], favourites={}, chara_new_flags={},
            read_nyanko_picture_book=lambda sv: pb)
        self.catfruit = []
        self.catseyes = []
        self.catamins = []
        self.lucky_tickets = []
        self.event_capsules = []
        self.event_capsules_2 = []
        self.labyrinth_medals = []
        self.battle_items = types.SimpleNamespace(items=[])

    def unlock_equip_menu(self):
        self.eq = 1


class Handler:
    uploaded = 0

    version = None   # テストで「このセーブは15.9.0」のように変える

    def __init__(self):
        self.save_file = Save()
        self.save_file.game_version = Handler.version

    def get_codes(self, upload_managed_items=False, tries=3):
        Handler.uploaded += 1
        return ("TESTCODE123", "4321")


# ---- engine を差し替え ------------------------------------------------------
engine.bcsfe_init = lambda p: (object(), "15.7.2")
engine.build_initial_account_handler = lambda cc: Handler()
_real_ensure_cat_roster = engine.ensure_cat_roster       # 本物（図鑑補完のテストで使う）
ensure_calls = []
engine.ensure_cat_roster = lambda save: (ensure_calls.append(1) or len(save.cats.cats))
catalog_versions = []
fail_gvs = set()


def fake_savefile(cc, gv):
    catalog_versions.append(gv)
    if gv in fail_gvs:
        raise RuntimeError("このバージョンのデータは取得できません")
    return Save(300)


engine.core = types.SimpleNamespace(
    ServerHandler=types.SimpleNamespace(from_codes=staticmethod(lambda t, c, cc, gv: (Handler(), None))),
    GameVersion=types.SimpleNamespace(from_string=staticmethod(lambda v: "gv:" + v)),
    GameDataGetter=lambda cc, gv, do_print=False: types.SimpleNamespace(version=str(gv).removeprefix("gv:")),
    core_data=types.SimpleNamespace(init_data=lambda: None,
                                    get_game_data_getter=lambda save: types.SimpleNamespace(version=save.game_version)),
    SaveFile=fake_savefile)
applied = []


def fake_apply_items(save, items, quantity_map=None, **k):
    applied.append((list(items), dict(quantity_map) if quantity_map else None))
    for it in items:
        v = (quantity_map or {}).get(it, 999)
        if it == "猫缶":
            save.catfood = v
        if it == "XP":
            save.xp = v


engine.apply_item_amounts = fake_apply_items
_real_apply_subs_and_others = engine.apply_subs_and_others
for fn in ("apply_subs_and_others", "clear_beacon_events", "clear_item_packs", "clear_scheme_items",
           "clear_all_ads_and_popups"):
    setattr(engine, fn, lambda *a, **k: None)

import app as A  # noqa: E402

# This suite uses in-process BCSFE fakes; process isolation is covered separately.
def fake_launch(job_id, job, arguments, slot, transfer_key):
    def execute():
        try:
            A._run_job(job, *arguments)
        finally:
            A._finish_job(job_id, slot, transfer_key)
    threading.Thread(target=execute, daemon=True).start()


A._launch_job = fake_launch
c = A.app.test_client()


def run(body, wait=6.0):
    r = c.post("/run", json=body)
    if r.status_code != 200:
        return r.status_code, r.get_json()
    jid = r.get_json()["job"]
    t0 = time.time()
    while time.time() - t0 < wait:
        st = c.get("/status/" + jid).get_json()
        if st["status"] != "running":
            while jid in A._slots and time.time() - t0 < wait:
                time.sleep(0.001)
            return 200, st
        time.sleep(0.05)
    return 200, st


print("\n[1] ログインと画面")
check("ログインなしで / が開く", c.get("/").status_code == 200)
check("ログイン画面は廃止", c.get("/login").status_code == 404)
html = c.get("/").get_data(as_text=True)
check("4段階の手順+メニュー5種が出る", html.count('class="step tab"') == 4 and html.count('role="tab"') == 5, (html.count('class="step tab"'), html.count('role="tab"')))
for needle in ('id="safe"', 'id="dry"', 'id="clone"', 'id="integrity"', 'id="checks"', 'id="cs"', 'id="chars"', 'id="rep"', 'window.confirm'):
    check("画面に " + needle, needle in html)

print("\n[2] 番号の読み取り")
P = A.parse_cat_numbers
check("1, 5, 100-102", P("1, 5, 100-102") == ([1, 5, 100, 101, 102], None))
check("全角・読点・改行", P("１、２\n３") == ([1, 2, 3], None))
check("逆順の範囲", P("5-3")[0] == [3, 4, 5])
check("0はエラー", P("0")[1] is not None)
check("10000はエラー", P("10000")[1] is not None)
check("文字はエラー", P("abc")[1] is not None)
check("2000体超はエラー", P("1-2001")[1] is not None)
check("空は空リスト", P("  ") == ([], None))

print("\n[3] キャラ解放（engine）")
check("図鑑データの対象バージョンは15.7.2", engine.GAME_DATA_VERSION == "15.7.2", engine.GAME_DATA_VERSION)
short_save = Save(12)
short_save.cats.cats = short_save.cats.cats[:8]
kept_cat = short_save.cats.cats[0]
short_pb = short_save.cats.read_nyanko_picture_book(short_save)
for _entry in short_pb.cats:
    _entry.cat_id = types.SimpleNamespace(value=_entry.cat_id)
roster_count = _real_ensure_cat_roster(short_save)
check("古いセーブの図鑑枠を最新データで補完", roster_count == 12 and len(short_save.cats.cats) == 12, roster_count)
check("既存キャラのオブジェクトと所持状態を維持", short_save.cats.cats[0] is kept_cat and short_save.cats.cats[0].unlocked == 1)
check("図鑑枠の補完を繰り返しても重複しない", _real_ensure_cat_roster(short_save) == 12)
sv = Save()
r = engine.unlock_cats_by_ids(sv, [0, 1, 7, 155, 299, 500, 2])
check("解放", r["unlocked"] == [1, 299, 2], r)
check("所持済み", r["already"] == [0])
check("存在しない", r["missing"] == [500])
check("エラーキャラ除外", r["error_chars"] == [155])
check("図鑑に無い除外", r["hidden"] == [7])
check("装備メニュー解放", sv.eq == 1)
check("除外されたキャラは未解放のまま", not sv.cats.cats[7].unlocked and not sv.cats.cats[155].unlocked)

# BCSFEのIDラッパー（.value）でも全キャラ解放の図鑑判定が整数IDと一致すること
wrapped_save = Save(200)
for _cat in wrapped_save.cats.cats:
    _cat.id = types.SimpleNamespace(value=_cat.id)
wrapped_pb = wrapped_save.cats.read_nyanko_picture_book(wrapped_save)
for _entry in wrapped_pb.cats:
    _entry.cat_id = types.SimpleNamespace(value=_entry.cat_id)
check("図鑑側IDラッパーは正規化される", len(engine._picture_book_ids(wrapped_pb.cats)) == 199)
check("セーブ側IDラッパーは正規化される", engine._cat_internal_id(wrapped_save.cats.cats[1]) == 1)
bulk_failures = _real_apply_subs_and_others(wrapped_save, ["全キャラ解放&エラーキャラ削除"], [])
check("一括解放処理で予期しない失敗がない", not bulk_failures, bulk_failures)
check("両側IDラッパーでも図鑑掲載キャラを解放", all(wrapped_save.cats.cats[i].unlocked for i in range(200) if i not in engine.ERROR_CAT_IDS and i != 7), [i for i,c in enumerate(wrapped_save.cats.cats) if c.unlocked])
check("両側IDラッパーでも図鑑非掲載キャラは除外", not wrapped_save.cats.cats[7].unlocked)
check("一括解放は既知エラーキャラを解放しない", all(not wrapped_save.cats.cats[i].unlocked for i in engine.ERROR_CAT_IDS if i < len(wrapped_save.cats.cats)))
wrapped_individual = Save(12)
for _cat in wrapped_individual.cats.cats:
    _cat.id = types.SimpleNamespace(value=_cat.id)
wrapped_individual_pb = wrapped_individual.cats.read_nyanko_picture_book(wrapped_individual)
for _entry in wrapped_individual_pb.cats:
    _entry.cat_id = types.SimpleNamespace(value=_entry.cat_id)
wrapped_result = engine.unlock_cats_by_ids(wrapped_individual, [0, 1, 7])
check("個別解放も両側IDラッパーに対応", wrapped_result["unlocked"] == [1] and wrapped_result["hidden"] == [7], wrapped_result)
check("ID正規化は小数を切り捨てない", engine._integer_id(1.5) is None)
missing_catalog_save = Save(12)
missing_catalog_save.cats.read_nyanko_picture_book = lambda _sv: (_ for _ in ()).throw(RuntimeError("catalog unavailable"))
missing_catalog_result = engine.unlock_cats_by_ids(missing_catalog_save, [1])
check("図鑑取得失敗時は個別解放も安全側で停止", missing_catalog_result["hidden"] == [1] and not missing_catalog_save.cats.cats[1].unlocked, missing_catalog_result)

print("\n[4] 通常の実行（番号→内部IDの変換も確認）")
applied.clear()
code, st = run({"safe": False, "chars": "2, 3", "items": ["XP"]})
check("完了する", code == 200 and st["status"] == "done", st.get("error"))
check("コードが出る", st["result"] == {"transfer": "TESTCODE123", "confirm": "4321"})
check("変更内容にXPが出る", any(x["label"] == "XP" for x in st["report"]), st["report"])
check("所持キャラ数が増える", any(x["label"] == "所持キャラ数" and x["after"] == x["before"] + 2 for x in st["report"]), st["report"])
check("ログに解放2体", any("解放 2体" in x for x in st["log"]), st["log"])
check("通常実行で構造監査が記録される", st.get("audit", {}).get("ok") is True, st.get("audit"))
check("通常実行では詳細な構造監査ログを抑制", not any("セーブ構造検査" in x for x in st["log"]), st["log"])
check("構造監査レポートが状態APIに出る", isinstance(st.get("audit"), dict) and st["audit"].get("ok") is True, st.get("audit"))

print("\n[4a] 重大な構造異常時の書き込み停止")
original_audit = save_audit.audit_save
save_audit.audit_save = lambda save, **kwargs: save_audit.AuditReport((
    save_audit.Finding("test_corruption", "critical", "save.test", "テスト用の異常"),
), len(save.cats.cats))
uploaded_before = Handler.uploaded
code, blocked = run({"safe": False, "items": ["XP"]})
check("重大異常を検出すると処理を中止", blocked["status"] == "error", blocked.get("error"))
check("重大異常時はコード発行・アップロードしない", Handler.uploaded == uploaded_before, Handler.uploaded)
check("異常箇所をログに残す", any("save.test" in x and "テスト用の異常" in x for x in blocked["log"]), blocked["log"])
save_audit.audit_save = original_audit


print("\n[4b] セーブ保全専用モード")
code, invalid_integrity = c.post("/run", json={"integrity_only": True}).status_code, None
check("引継ぎ情報なしの保全チェックは400", code == 400, code)
uploaded_before_integrity = Handler.uploaded
code, integrity = run({"integrity_only": True, "transfer": "TESTCODE123", "confirm": "4321"})
check("保全チェックだけで完了する", code == 200 and integrity["status"] == "done" and integrity.get("integrity_only") is True, integrity)
check("修復対象がなければコード再発行・アップロードしない", Handler.uploaded == uploaded_before_integrity, Handler.uploaded)
check("保全チェック結果を返す", isinstance(integrity.get("audit"), dict) and integrity.get("integrity") == {"repaired_count": 0, "upload_required": False}, integrity)
check("保全チェックでは検査概要を表示", any("保全検査" in x for x in integrity["log"]), integrity["log"])

print("\n[5] 安全モード")
applied.clear()
code, st = run({"safe": True, "items": ["猫缶", "XP"], "qty": {"レアチケ": "5", "NP": "7"}})
check("完了する", st["status"] == "done", st.get("error"))
items_applied = [x for call in applied for x in call[0]]
check("猫缶とレアチケは外れる", "猫缶" not in items_applied and "レアチケ" not in items_applied, items_applied)
check("XPとNPは適用", "XP" in items_applied and "NP" in items_applied)
check("スキップが注意に出る", any("安全モード" in w for w in st["warn"]), st["warn"])
code, st = run({"safe": True, "items": ["猫缶"]})
check("高リスクだけなら400", code == 400, st)
applied.clear()
code, st = run({"safe": False, "items": ["猫缶"]})
check("安全モードOFFなら猫缶も適用", code == 200 and any("猫缶" in call[0] for call in applied))

print("\n[6] 試運転")
Handler.uploaded = 0
code, st = run({"safe": False, "dry": True, "items": ["XP"], "chars": "10"})
check("完了する", st["status"] == "done", st.get("error"))
check("コードは出ない", st["result"] is None)
check("アップロードしていない", Handler.uploaded == 0, Handler.uploaded)
check("dryフラグ", st["dry"] is True)
check("変更内容は出る", len(st["report"]) >= 1)
code, st = run({"safe": False, "items": ["XP"]})
check("通常実行ではアップロードする", Handler.uploaded == 1)

print("\n[7] 一部が壊れても止まらない")
orig = engine.unlock_cats_by_ids
engine.unlock_cats_by_ids = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
code, st = run({"safe": False, "items": ["XP"], "chars": "5"})
check("キャラ解放が失敗しても完了する", st["status"] == "done", st.get("error"))
check("コードは出る", st["result"] is not None)
check("失敗は注意に出る", any("キャラ解放に失敗" in w for w in st["warn"]), st["warn"])
engine.unlock_cats_by_ids = orig
orig_snap = engine.snapshot
engine.snapshot = lambda save: (_ for _ in ()).throw(RuntimeError("boom"))
code, st = run({"safe": False, "items": ["XP"]})
check("変更内容の取得が失敗しても実行は完了する", st["status"] == "done" and st["result"] is not None, st.get("error"))
check("その場合、変更内容は空で返る", st["report"] == [])
engine.snapshot = orig_snap

print("\n[8] 入力チェック")
check("片方だけの引継ぎは400", run({"transfer": "a", "items": ["XP"]})[0] == 400)
check("種類ごとの数量が通る", run({"qty": {"マタタビ#3": "5", "キャッツアイ#2": "7"}, "safe": False})[0] == 200)
check("種類ごとの数量が上限超えは400", run({"qty": {"マタタビ#3": "999"}, "safe": False})[0] == 400)
check("数量が上限超えは400", run({"qty": {"プラチケ": "100"}, "safe": False})[0] == 400)
check("キャラ番号エラーは400", run({"chars": "abc"})[0] == 400)
check("何も選ばないと400", run({})[0] == 400)

print("\n[9] キャラ名データ")
names, method = engine.get_cat_names(Save(20))
check("名前が取れる", names.get(0) == ["キャラ1", "キャラ1進化"], names.get(0))
check("方法が記録される", method == "Cat.get_names_cls", method)


class NoNameSave(Save):
    def __init__(self):
        super().__init__(50)
        for cat in self.cats.cats:
            cat.get_names_cls = None
n2, m2 = engine.get_cat_names(NoNameSave())
check("取れないときは空で諦める", n2 == {} and m2 is None)
check("snapshot", engine.snapshot(Save())["猫缶"] == 10)
A._names.update(state="none", items=[], method=None, error=None)
if os.path.exists(A._NAMES_FILE):
    os.remove(A._NAMES_FILE)
j = c.get("/api/cats").get_json()
check("初回はloadingかready", j["state"] in ("loading", "ready"), j["state"])
for _ in range(60):
    j = c.get("/api/cats").get_json()
    if j["state"] != "loading":
        break
    time.sleep(0.05)
check("取得完了", j["state"] == "ready" and j["count"] == 300, (j["state"], j["count"], j["error"]))
check("名前一覧は対象バージョンの空セーブから構築", bool(catalog_versions) and catalog_versions[-1] == "gv:15.7.2", catalog_versions)
check("番号は内部ID+1", j["items"][0]["n"] == 1 and j["items"][0]["name"] == "キャラ1")
check("第2形態以降はaltに入る", j["items"][4]["alt"] == "キャラ5進化")
A._names.update(state="none", items=[], method=None, error=None)
A._run_lock.acquire()
A._names_thread()
check("ジョブ実行中は『あとで』になる", A._names["state"] == "none" and "他の処理" in (A._names["error"] or ""))
A._run_lock.release()
r = c.post("/api/cats/refresh")
check("再取得APIが動く", r.status_code == 200)

print("\n[10] 同時実行")
A._run_lock.acquire()
code, st = run({"items": ["XP"], "safe": False})
check("キャラ名取得中も実行できる", code == 200 and st["status"] == "done")
A._run_lock.release()


print("\n[11] 自動更新：バージョンと互換チェック")
V = engine.version_to_string
check("文字列 15.7.2", V("15.7.2") == "15.7.2")
check("整数 150702", V(150702) == "15.7.2")
check("to_string を持つもの", V(types.SimpleNamespace(to_string=lambda: "16.0.1")) == "16.0.1")
check("game_version 属性(整数)", V(types.SimpleNamespace(game_version=150302)) == "15.3.2")
check("読めないものは None", V("abc") is None and V(None) is None)
check("バージョンの大小比較", engine.version_key("15.10.0") > engine.version_key("15.9.9") and engine.version_key("15.7") == (15, 7, 0))


def build_core(skip=()):
    ns = types.SimpleNamespace()
    for path in engine.REQUIRED_CORE_ATTRS:
        if path in skip:
            continue
        parts, cur = path.split("."), ns
        for part in parts[:-1]:
            if not hasattr(cur, part):
                setattr(cur, part, types.SimpleNamespace())
            cur = getattr(cur, part)
        if not hasattr(cur, parts[-1]):
            setattr(cur, parts[-1], types.SimpleNamespace())
    return ns


_stub_core = engine.core
engine.core = build_core()
check("必要な機能がそろっていれば問題なし", engine.check_bcsfe_api() == [], engine.check_bcsfe_api())
engine.core = build_core(skip=("Upgrade",))
check("足りない機能を検知する", engine.check_bcsfe_api() == ["core.Upgrade"], engine.check_bcsfe_api())
engine.core = _stub_core

print("\n[12] 独立アカウント複製")
import copy as _copy
class FakeData:
    def __init__(self, value): self.value = value
    def get_bytes(self): return self.value
class CloneSave(Save):
    def __init__(self, n=300):
        super().__init__(n)
        self.inquiry_code = "ORIGINAL-IQ"
        self.data = object()
    def to_data(self):
        return FakeData(_copy.deepcopy(self))
class CloneHandler:
    created = 0
    uploaded = []
    instances = []
    def __init__(self, save_file, print=False):
        self.save_file = save_file
        CloneHandler.instances.append(self)
    @staticmethod
    def from_codes(t, c, cc, gv):
        return CloneHandler(CloneSave()), None
    def create_new_account(self):
        CloneHandler.created += 1
        self.save_file.inquiry_code = "COPY-IQ-NEW"
        return True
    def get_codes(self, upload_managed_items=False, tries=3):
        CloneHandler.uploaded.append(self.save_file.inquiry_code)
        suffix = "ORG" if self.save_file.inquiry_code == "ORIGINAL-IQ" else "COPY"
        return ("CODE-"+suffix, "1111" if suffix == "ORG" else "2222")
def clone_savefile(data, cc=None):
    return _copy.deepcopy(data.value)
_core_before_clone = engine.core
engine.core = types.SimpleNamespace(
    ServerHandler=CloneHandler, Data=FakeData, SaveFile=clone_savefile,
    GameVersion=types.SimpleNamespace(from_string=staticmethod(lambda v: "gv:" + v)),
)
CloneHandler.created = 0; CloneHandler.uploaded = []; CloneHandler.instances = []
applied.clear()
clone_core = engine.core
code, st = run({"clone": True, "transfer": "abc", "confirm": "1234"})
check("複製が完了する", code == 200 and st["status"] == "done", st.get("error"))
check("元・複製の2組のコードが返る", st["result"] == {
    "original": {"transfer": "CODE-ORG", "confirm": "1111"},
    "copy": {"transfer": "CODE-COPY", "confirm": "2222"}}, st.get("result"))
check("別々の問い合わせコードで2回登録", CloneHandler.uploaded == ["ORIGINAL-IQ", "COPY-IQ-NEW"], CloneHandler.uploaded)
check("複製側に新規アカウントを作成", CloneHandler.created == 1, CloneHandler.created)
check("元と複製は別のSaveFileオブジェクト", len(CloneHandler.instances) == 2 and CloneHandler.instances[0].save_file is not CloneHandler.instances[1].save_file)
check("複製側だけ問い合わせコードが変わる", CloneHandler.instances[0].save_file.inquiry_code == "ORIGINAL-IQ" and CloneHandler.instances[1].save_file.inquiry_code == "COPY-IQ-NEW")
check("複製側も元データのXP値を保持", CloneHandler.instances[1].save_file.xp == CloneHandler.instances[0].save_file.xp)
check("複製処理で通常の編集は行わない", applied == [], applied)
engine.core = clone_core
CloneHandler.created = 0; CloneHandler.uploaded = []
code, st = run({"clone": True, "dry": True, "transfer": "abc", "confirm": "1234"})
check("複製の試運転が完了する", code == 200 and st["status"] == "done", st.get("error"))
check("試運転では新規アカウントを作らない", CloneHandler.created == 0, CloneHandler.created)
check("試運転ではコードを発行しない", CloneHandler.uploaded == [] and st["result"] is None, CloneHandler.uploaded)
engine.core = _core_before_clone

print("\n[13] 新しいゲームバージョンの自動学習")
check("最初は固定バージョン", A._effective_version() == "15.7.2", A._effective_version())
Handler.version = "15.9.0"
code, st = run({"safe": False, "items": ["XP"]})
check("新しい版のセーブを見たら覚える", A._effective_version() == "15.9.0", A._effective_version())
check("ファイルに保存される", os.path.exists(A._VERSION_FILE))
check("名前データも新しい版で取り直す", A._names["version"] == "15.9.0", A._names["version"])
Handler.version = "15.1.0"
run({"safe": False, "items": ["XP"]})
check("古い版のセーブでは下がらない", A._effective_version() == "15.9.0")
Handler.version = "99.0.0"
run({"safe": False, "items": ["XP"]})
check("桁違いに新しい値は採用しない", A._effective_version() == "15.9.0")
Handler.version = "15.9.0"
s_ = c.get("/api/state").get_json()
check("状態APIに出る", s_["game_data_version"] == "15.9.0" and s_["pinned_version"] == "15.7.2" and s_["learned_version"] == "15.9.0", s_)

print("\n[14] 名前データの自動更新")


def wait_names(cond, secs=3.0):
    t0 = time.time()
    while time.time() - t0 < secs:
        if cond():
            return True
        time.sleep(0.05)
    return False


A._names.update(updated=0.0, tried=0.0, refreshing=False)
catalog_versions.clear()
A._start_names()
ok = wait_names(lambda: A._names["updated"] > 0 and not A._names["refreshing"] and not A._run_lock.locked())
check("時間がたつと裏で取り直す", ok and A._names["state"] == "ready", A._names["state"])
check("新しい版のデータで作る", "gv:15.9.0" in catalog_versions, catalog_versions)
fail_gvs.add("gv:15.9.0")
catalog_versions.clear()
A._names.update(updated=0.0, tried=0.0, refreshing=False, version="15.7.2")
A._start_names()
ok = wait_names(lambda: A._names["updated"] > 0 and not A._names["refreshing"] and not A._run_lock.locked())
check("新しい版で作れなければ固定版で作り直す", ok and "gv:15.7.2" in catalog_versions and A._names["version"] == "15.7.2", (catalog_versions, A._names["version"]))
check("その間も一覧は使える", A._names["state"] == "ready" and len(A._names["items"]) == 300)
n_before = len(catalog_versions)
A._start_names()
time.sleep(0.2)
check("失敗が続いても30分は取り直さない", len(catalog_versions) == n_before, catalog_versions)
fail_gvs.clear()

print("\n[15] 状態API")
s_ = c.get("/api/state").get_json()
check("bcsfeバージョン欄がある", "bcsfe_version" in s_)
check("互換チェック欄がある（リスト）", isinstance(s_["api_missing"], list), s_.get("api_missing"))

# 後片付け（次回の実行に影響しないように）
A._learned["version"] = None
for _f in (A._VERSION_FILE, A._NAMES_FILE):
    try:
        os.remove(_f)
    except OSError:
        pass

print(f"\n結果: {len(PASS)}件OK / {len(FAIL)}件NG")
if FAIL:
    print("NG:", FAIL)
    sys.exit(1)
