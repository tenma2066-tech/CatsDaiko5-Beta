"""Playwright で X の画面を操作する部分。X の画面の作りが変わったら、ここの SEL を直す。"""
import os
import random
import threading
import time

from . import store

SEL = {
    "logged_in": '[data-testid="SideNav_NewTweet_Button"], [data-testid="AppTabBar_Home_Link"], [data-testid="SideNav_AccountSwitcher_Button"]',
    "compose_text": '[data-testid="tweetTextarea_0"]',
    "file_input": 'input[data-testid="fileInput"]',
    "attachments": '[data-testid="attachments"]',
    "post_button": '[data-testid="tweetButton"]',
    "toast": '[data-testid="toast"]',
    "conversation": '[data-testid="conversation"]',
    "dm_input": '[data-testid="dmComposerTextInput"]',
    "dm_send": '[data-testid="dmComposerSendButton"]',
    "dm_message": '[data-testid="messageEntry"]',
}
HOME = "https://x.com/home"
COMPOSE = "https://x.com/compose/post"
MESSAGES = "https://x.com/messages"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/131.0.0.0 Safari/537.36")


PROFILE_LOCK = threading.Lock()   # 同じプロフィールを2つのブラウザで同時に開かない


class NotLoggedIn(Exception):
    pass


def human_pause(a=0.6, b=1.8):
    time.sleep(random.uniform(a, b))


class Browser:
    """使うときだけ起動して、終わったら閉じる（メモリを使いっぱなしにしない）。"""

    def __init__(self, headless=True, display=None):
        self.headless = headless
        self.display = display

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        if not PROFILE_LOCK.acquire(timeout=90):
            raise RuntimeError("ほかの処理がブラウザを使っています。1〜2分待ってからもう一度お試しください。")
        try:
            return self._open(sync_playwright)
        except BaseException:
            PROFILE_LOCK.release()
            raise

    def _open(self, sync_playwright):
        os.makedirs(store.PROFILE_DIR, mode=0o700, exist_ok=True)
        os.chmod(store.PROFILE_DIR, 0o700)
        env = dict(os.environ)
        if self.display:
            env["DISPLAY"] = self.display
        self._pw = sync_playwright().start()
        self.ctx = self._pw.chromium.launch_persistent_context(
            store.PROFILE_DIR, headless=self.headless, user_agent=UA,
            executable_path=os.environ.get("XBOT_CHROMIUM") or None, locale="ja-JP", timezone_id="Asia/Tokyo",
            viewport={"width": 1100, "height": 900} if self.headless else None,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--disable-dev-shm-usage"]
                 + ([] if self.headless else ["--window-position=0,0", "--window-size=430,900", "--force-device-scale-factor=1"]),
            env=env)
        self.ctx.set_default_timeout(30000)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        finally:
            try:
                self._pw.stop()
            finally:
                PROFILE_LOCK.release()

    # ------------------------------------------------------------------ 共通
    def shot(self, name):
        os.makedirs(store.SHOT_DIR, exist_ok=True)
        fn = f"{int(time.time())}-{name}.png"
        try:
            self.page.screenshot(path=os.path.join(store.SHOT_DIR, fn))
        except Exception:
            return None
        files = sorted(os.listdir(store.SHOT_DIR))
        for old in files[:-60]:   # 画面の記録は新しい60枚だけ残す
            try:
                os.remove(os.path.join(store.SHOT_DIR, old))
            except OSError:
                pass
        return fn

    def logged_in(self) -> bool:
        self.page.goto(HOME, wait_until="domcontentloaded")
        try:
            self.page.wait_for_selector(SEL["logged_in"], timeout=20000)
            return True
        except Exception:
            return False

    def require_login(self):
        if not self.logged_in():
            raise NotLoggedIn("Xにログインしていません（管理サイトの「ログイン」から入ってください）")

    # ------------------------------------------------------------------ 投稿
    def post(self, text, image_paths, dry):
        """投稿する。dry=True なら入力まで行い、投稿ボタンは押さない。戻り値は画面の記録。"""
        self.require_login()
        p = self.page
        p.goto(COMPOSE, wait_until="domcontentloaded")
        box = p.wait_for_selector(SEL["compose_text"])
        box.click()
        human_pause()
        if text:
            p.keyboard.insert_text(text)
        if image_paths:
            p.set_input_files(SEL["file_input"], image_paths)
            p.wait_for_selector(SEL["attachments"], timeout=60000)
            # 画像のアップロードが終わり、投稿ボタンが押せるようになるまで待つ
            p.wait_for_function("s => { const b=document.querySelector(s); return b && !b.disabled && b.getAttribute('aria-disabled')!=='true'; }",
                                arg=SEL["post_button"], timeout=120000)
        human_pause()
        if dry:
            return self.shot("post-dry")
        p.click(SEL["post_button"])
        try:
            p.wait_for_selector(SEL["toast"], timeout=20000)
        except Exception:
            pass
        human_pause(2, 3)
        shot = self.shot("post")
        # 入力欄が残ったまま＝投稿できていない
        if p.query_selector(SEL["compose_text"]) and p.url.startswith(COMPOSE):
            raise RuntimeError("投稿ボタンを押しましたが、投稿できたことを確認できませんでした（画面の記録を確認してください）")
        return shot

    # ------------------------------------------------------------------ DM
    def list_conversations(self, limit):
        """会話一覧から [{conv, preview}] を返す。"""
        self.require_login()
        p = self.page
        p.goto(MESSAGES, wait_until="domcontentloaded")
        try:
            p.wait_for_selector(SEL["conversation"], timeout=25000)
        except Exception:
            return []
        human_pause()
        out = []
        for el in p.query_selector_all(SEL["conversation"])[:limit]:
            link = el.query_selector("a[href*='/messages/']")
            href = link.get_attribute("href") if link else None
            text = (el.inner_text() or "").strip()
            if not href:
                continue
            lines = [x for x in text.splitlines() if x.strip()]
            out.append({"conv": href.split("?")[0], "preview": lines[-1] if lines else "", "unread": "bold" in (el.get_attribute("class") or "")})
        return out

    def last_incoming(self, conv):
        """会話を開き、最後のメッセージが相手からなら (本文, はじめての相手か) を返す。自分からなら None。"""
        p = self.page
        p.goto("https://x.com" + conv, wait_until="domcontentloaded")
        p.wait_for_selector(SEL["dm_input"], timeout=25000)
        human_pause()
        entries = p.query_selector_all(SEL["dm_message"])
        if not entries:
            return None
        last = entries[-1]
        # 自分のメッセージは右寄せ（送信済みの印がある）。相手のものだけを対象にする
        mine = last.evaluate("e => { const r=e.getBoundingClientRect(); const w=e.closest('[data-testid=\"DmActivityViewport\"]')||document.body; "
                             "const wr=w.getBoundingClientRect(); return (r.left - wr.left) > (wr.right - r.right); }")
        if mine:
            return None
        text = (last.inner_text() or "").strip()
        # 相手が自分より前に1通も送っていない（＝自分が一度も返信していない）なら「はじめて」
        any_mine = any(e.evaluate("e => { const r=e.getBoundingClientRect(); const w=e.closest('[data-testid=\"DmActivityViewport\"]')||document.body; "
                                  "const wr=w.getBoundingClientRect(); return (r.left - wr.left) > (wr.right - r.right); }") for e in entries)
        return text, not any_mine

    def reply(self, conv, text, dry):
        p = self.page
        if not p.url.startswith("https://x.com" + conv):
            p.goto("https://x.com" + conv, wait_until="domcontentloaded")
        box = p.wait_for_selector(SEL["dm_input"])
        box.click()
        human_pause()
        p.keyboard.insert_text(text)
        human_pause()
        if dry:
            shot = self.shot("dm-dry")
            # 下書きは送らずに消す
            p.keyboard.press("Control+A")
            p.keyboard.press("Delete")
            return shot
        p.click(SEL["dm_send"])
        human_pause(1.5, 2.5)
        return self.shot("dm")

    def snapshot(self):
        """画面の確認用：ホーム・投稿画面・DM画面の記録を撮る（何も送らない）。"""
        shots = []
        for name, url in (("home", HOME), ("compose", COMPOSE), ("messages", MESSAGES)):
            try:
                self.page.goto(url, wait_until="domcontentloaded")
                human_pause(3, 4)
                shots.append(self.shot("check-" + name))
            except Exception:
                pass
        return [s for s in shots if s]
