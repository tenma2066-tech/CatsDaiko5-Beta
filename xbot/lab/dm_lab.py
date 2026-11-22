"""DM の「ふつうの会話を2件目以降も開けるか」を調べる実験（読むだけ・何も送らない）。

本番の常駐とは別のプロセスで、プロフィールの「写し」を使って動かす（lab.sh が用意する）。
会話を開くので、X 上ではその会話が「既読」になる。それ以外の変化はない。

調べる開き方：
  M0  いまのやり方：毎回 一覧のURLを開き直す → 項目をクリック
  M1  一覧を1回だけ開く → 項目をクリック → ブラウザの「戻る」（ページを読み直さない）→ 次の項目
  M2  一覧を1回だけ開く → 項目をクリック → そのまま横の一覧の次の項目をクリック（パソコン幅の2列表示）
  M3  会話ごとにブラウザを起動し直す → 一覧 → 項目をクリック

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


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    methods = (sys.argv[2] if len(sys.argv) > 2 else "M0,M1,M2,M3").split(",")
    table = {"M0": m0, "M1": m1, "M2": m2, "M3": m3}
    print(f"[lab] 開始：会話 {n}件 × 方法 {','.join(methods)}（読むだけ）", flush=True)
    for m in methods:
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
