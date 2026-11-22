"""DM の「ふつうの会話を2件目以降も開けるか」を調べる実験（読むだけ・何も送らない）。

本番の常駐とは別のプロセスで、プロフィールの「写し」を使って動かす（lab.sh が用意する）。
会話を開くので、X 上ではその会話が「既読」になる。それ以外の変化はない。

調べる開き方：
  M0  いまのやり方：毎回 一覧のURLを開き直す → 項目をクリック
  M1  一覧を1回だけ開く → 項目をクリック → ブラウザの「戻る」（ページを読み直さない）→ 次の項目
  M2  一覧を1回だけ開く → 項目をクリック → そのまま横の一覧の次の項目をクリック（パソコン幅の2列表示）
  M3  会話ごとにブラウザを起動し直す → 一覧 → 項目をクリック
  M4  本番と同じ順：先にリクエスト画面（と「その他」）を開いてから、同じブラウザで M0 のやり方
  M5  リクエスト画面を開いたあと、ブラウザを起動し直して M0 のやり方

出力は件数・秒数・成否だけ（相手の名前・本文・IDは出さない）。
使い方: XBOT_DATA_DIR=<写し> python dm_lab.py [件数=3] [方法=M0,M1,M2,M3]
"""
import sys
import time

from xbot.browser import CHAT, DMBrowser

ITEM = '[data-testid^="dm-conversation-item-"]'
COMPOSER = '[data-testid="dm-composer-textarea"]'
MSG = '[data-testid^="message-text-"]'


def _keys(b, n):
    b.dm_open_inbox()
    return b.dm_chat_keys(n)


def _check(b, key, before_url):
    """項目をクリックして、入力欄とメッセージが出るまで待つ。(成否, 秒, URLが変わったか, メッセージ数)"""
    p = b.page
    t0 = time.time()
    try:
        p.locator(f'[data-testid="dm-conversation-item-{key}"]').first.click(timeout=15000)
        p.wait_for_selector(COMPOSER, timeout=20000)
        for _ in range(15):
            if p.locator(MSG).count():
                break
            time.sleep(1)
        ok = p.locator(MSG).count() > 0
        err = ""
    except Exception as e:  # noqa: BLE001
        ok, err = False, type(e).__name__
    return ok, round(time.time() - t0, 1), p.url != before_url, p.locator(MSG).count(), err


def _row(method, i, res):
    ok, secs, moved, nmsg, err = res
    print(f"[lab] {method} #{i + 1} {'OK' if ok else 'NG'} {secs}s url_changed={moved} messages={nmsg} {err}", flush=True)
    return ok


def m0(n):
    with DMBrowser() as b:
        keys = _keys(b, n)
        for i, k in enumerate(keys):
            b.dm_open_inbox()
            _row("M0", i, _check(b, k, b.page.url))
    return len(keys)


def m1(n):
    with DMBrowser() as b:
        keys = _keys(b, n)
        for i, k in enumerate(keys):
            if i:
                b.page.go_back()
                try:
                    b.page.wait_for_selector(ITEM, timeout=15000)
                except Exception:  # noqa: BLE001
                    print(f"[lab] M1 #{i + 1} 一覧に戻れませんでした", flush=True)
                    break
            _row("M1", i, _check(b, k, b.page.url))
    return len(keys)


def m2(n):
    with DMBrowser() as b:
        keys = _keys(b, n)
        for i, k in enumerate(keys):
            _row("M2", i, _check(b, k, b.page.url))
    return len(keys)


def m3(n):
    with DMBrowser() as b:
        keys = _keys(b, n)
    for i, k in enumerate(keys):
        with DMBrowser() as b:
            b.dm_open_inbox()
            _row("M3", i, _check(b, k, b.page.url))
    return len(keys)


def _visit_requests(b):
    b.dm_open_inbox(requests=True)
    nreq = len(b.dm_request_keys())
    b.dm_open_inbox(requests=True, other=True)
    print(f"[lab] リクエスト画面を開きました（リクエスト {nreq}件・許可はしない）", flush=True)


def m4(n):
    with DMBrowser() as b:
        _visit_requests(b)
        b.dm_open_inbox()
        keys = b.dm_chat_keys(n)
        print(f"[lab] M4 リクエストのあとの一覧：{len(keys)}件", flush=True)
        for i, k in enumerate(keys):
            b.dm_open_inbox()
            _row("M4", i, _check(b, k, b.page.url))
    return len(keys)


def m5(n):
    with DMBrowser() as b:
        _visit_requests(b)
    return m0(n)


# ---------------------------------------------------------------- 仕組みを調べる（会話は開かない＝既読にならない）
def _count(b, waits=(3, 8, 15)):
    """一覧の件数を、時間をおいて数える。"""
    out, t = [], 0
    for w in waits:
        time.sleep(w - t)
        t = w
        out.append(b.page.locator(ITEM).count())
    return out


def _screen(b):
    """いま画面に出ている部品（data-testid、数字は伏せる）と、URL の形・保存領域のキー名。"""
    p = b.page
    path = p.url.split("?")[0].replace("https://x.com", "")
    path = "".join("#" if c.isdigit() else c for c in path)[:40]
    info = p.evaluate(r"""() => {
      const tids = {}; const walk = (r) => r.querySelectorAll('*').forEach(e => {
        const t = e.getAttribute && e.getAttribute('data-testid');
        if (t && /^dm-|^message|^pin|request|inbox|conversation/i.test(t)) { const k = t.replace(/[0-9]+/g,'#').replace(/[A-Za-z0-9_-]{16,}/g,'*'); tids[k]=(tids[k]||0)+1; }
        if (e.shadowRoot) walk(e.shadowRoot); });
      walk(document);
      const keys = (st) => { try { return Object.keys(st).filter(k => /chat|dm|inbox|request|xchat|conversation|filter/i.test(k)).map(k => k.replace(/[0-9]+/g,'#').slice(0,50)); } catch (e) { return ['?']; } };
      return {tids: Object.entries(tids).sort((a,b)=>b[1]-a[1]).slice(0,25), local: keys(localStorage), session: keys(sessionStorage)};
    }""")
    return path, info


def _requests(b, other):
    b.dm_open_inbox(requests=True)
    if other:
        b.dm_open_inbox(requests=True, other=True)


def mech(name, fn):
    t0 = time.time()
    try:
        with DMBrowser() as b:
            res = fn(b)
        print(f"[lab] {name}: {res}（{round(time.time() - t0)}秒）", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[lab] {name}: 中断 {type(e).__name__}（{round(time.time() - t0)}秒）", flush=True)


def t_a(b):          # 基準：起動 → 一覧
    b.goto(CHAT)
    return {"list": _count(b)}


def t_b1(b):         # リクエスト（メインだけ）→ 一覧
    _requests(b, False)
    b.goto(CHAT)
    return {"list": _count(b)}


def t_b2(b):         # リクエスト（メイン＋その他）→ 一覧
    _requests(b, True)
    b.goto(CHAT)
    return {"list": _count(b)}


def t_c(b):          # リクエスト → 一覧 → 再読み込み
    _requests(b, True)
    b.goto(CHAT)
    first = _count(b, (3,))
    b.page.reload(wait_until="domcontentloaded")
    return {"before": first, "after_reload": _count(b)}


def t_d(b):          # リクエスト → 同じブラウザで新しいタブ → 一覧
    _requests(b, True)
    p2 = b.ctx.new_page()
    old = b.page
    b.page = p2
    b.goto(CHAT)
    res = {"new_tab": _count(b)}
    b.page = old
    try:
        p2.close()
    except Exception:  # noqa: BLE001
        pass
    return res


def t_e(b):          # リクエスト → 画面の中のリンクで受信トレイへ（URL を直接開かない）
    _requests(b, True)
    p = b.page
    for sel in ('a[href="/i/chat"]', 'a[href="/i/chat/"]', '[data-testid="dm-inbox-back-button"]', '[aria-label="戻る"]', '[aria-label="Back"]'):
        loc = p.locator(sel).first
        if loc.count():
            try:
                loc.click(timeout=8000)
                return {"via": sel.replace('"', "'"), "list": _count(b)}
            except Exception as e:  # noqa: BLE001
                return {"via": sel.replace('"', "'"), "click_error": type(e).__name__}
    return {"via": "見つからない"}


def t_f(b):          # リクエスト → 一覧：画面に何が出ているか
    _requests(b, True)
    b.goto(CHAT)
    time.sleep(8)
    path, info = _screen(b)
    return {"path": path, **info, "list": b.page.locator(ITEM).count()}


def t_g(b):          # 基準の画面（比べる用）
    b.goto(CHAT)
    time.sleep(8)
    path, info = _screen(b)
    return {"path": path, **info, "list": b.page.locator(ITEM).count()}


def _panel(b):
    """DM 画面が描かれているか（受信トレイの部品があるか）。"""
    time.sleep(8)
    return b.page.locator('[data-testid="dm-container"]').count() > 0


def _back_in_app(b):
    loc = b.page.locator('a[href="/i/chat"]').first
    loc.click(timeout=8000)
    time.sleep(3)


def t_h(b):          # その他 → URL でリクエスト画面 → 描かれるか・「その他」ボタンは出るか
    _requests(b, True)
    b.goto(CHAT + "/requests")
    ok = _panel(b)
    return {"requests_panel": ok, "other_btn": b.page.locator('[data-testid="dm-message-requests-other-button"]').count()}


def t_i(b):          # その他 → 画面の中のリンクで戻る → そのあと URL で一覧
    _requests(b, True)
    _back_in_app(b)
    b.goto(CHAT)
    return {"list_after_heal_then_url": _count(b)}


def t_j(b):          # その他 → 画面の中のリンクで戻る → URL でリクエスト → その他 → リンクで戻る（2回目も戻れるか）
    _requests(b, True)
    _back_in_app(b)
    _requests(b, True)
    _back_in_app(b)
    return {"list_after_two_rounds": _count(b)}


MECH = {"H": t_h, "I": t_i, "J": t_j, "A": t_a, "B1": t_b1, "B2": t_b2, "C": t_c, "D": t_d, "E": t_e, "F": t_f, "G": t_g}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    methods = (sys.argv[2] if len(sys.argv) > 2 else "M0,M1,M2,M3").split(",")
    table = {"M0": m0, "M1": m1, "M2": m2, "M3": m3, "M4": m4, "M5": m5}
    print(f"[lab] 開始：会話 {n}件 × 方法 {','.join(methods)}（読むだけ）", flush=True)
    for m in methods:
        if m.startswith("X"):          # X<名前> = 仕組みの調査（n 回くり返す・会話は開かない）
            for r in range(n):
                mech(f"{m[1:]} {r + 1}回目", MECH[m[1:]])
                time.sleep(3)
            continue
        t0 = time.time()
        try:
            got = table[m](n)
            print(f"[lab] {m} 終了：一覧 {got}件・{round(time.time() - t0)}秒", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[lab] {m} 中断：{type(e).__name__}", flush=True)
        time.sleep(5)
    print(f"[lab] 終了（{CHAT.split('/')[2]}）", flush=True)


if __name__ == "__main__":
    main()
