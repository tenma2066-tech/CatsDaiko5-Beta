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
IPHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/18.5 Mobile/15E148 Safari/604.1")
MAC_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
          "Version/18.5 Safari/605.1.15")
WARP_PROXY = os.environ.get("XBOT_WARP_PROXY", "socks5://127.0.0.1:40000")


def settings_for_browser():
    s = store.get_all()
    engine = s.get("engine") or "iphone"
    ua = s.get("ua") or ""
    if engine == "iphone" and "iPhone" not in ua:
        ua = IPHONE_UA
    proxy = {"server": WARP_PROXY} if s.get("network") == "warp" and warp_ready() else None
    return engine, ua, proxy


def warp_ready() -> bool:
    """WARP（プロキシ）が動いているか。動いていなければ直接つなぐ。"""
    import socket
    try:
        host, port = WARP_PROXY.split("://")[1].rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=2):
            return True
    except OSError:
        return False


PROFILE_LOCK = threading.Lock()   # 同じプロフィールを2つのブラウザで同時に開かない


CHAT = "https://x.com/i/chat"


class DMBrowser:
    """DM（チャット）用：iPhone 側のログイン情報をパソコン（Chromium）側に写して開く。"""

    def __enter__(self):
        if (store.get("engine") or "iphone") == "iphone":
            with Browser(engine="iphone") as b0:
                ck = b0.ctx.cookies(["https://x.com"])
            self.b = Browser(engine="pc").__enter__()
            names = {c["name"] for c in self.b.ctx.cookies(["https://x.com"])}
            if "auth_token" not in names or any(c["name"] == "auth_token" for c in ck):
                self.b.ctx.add_cookies(ck)
        else:
            self.b = Browser(engine="pc").__enter__()
        return self.b

    def __exit__(self, *exc):
        return self.b.__exit__(*exc)


def chat_pin_visible(page) -> bool:
    try:
        el = page.query_selector('[data-testid="pin-code-input-container"]')
        return bool(el and el.is_visible())
    except Exception:
        return False


class NotLoggedIn(Exception):
    pass


def human_pause(a=0.6, b=1.8):
    time.sleep(random.uniform(a, b))


class Browser:
    """使うときだけ起動して、終わったら閉じる（メモリを使いっぱなしにしない）。"""

    def __init__(self, headless=True, display=None, engine=None):
        self.headless = headless
        self.display = display
        self.engine = engine

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
        env = dict(os.environ)
        if self.display:
            env["DISPLAY"] = self.display
        engine, ua, proxy = settings_for_browser()
        if self.engine:
            engine = self.engine
        self.engine_used = engine
        self._pw = sync_playwright().start()
        try:
            if engine in ("iphone", "mac") and self.headless:
                mac = engine == "mac"   # DM（チャット）はスマホ版のサイトでは表示されないため、同じログインのままパソコン版で開く
                # iPhone の Safari と同じ種類のブラウザで、画面サイズ・タッチ操作も iPhone に合わせる
                prof = store.PROFILE_DIR + "-iphone"
                os.makedirs(prof, mode=0o700, exist_ok=True)
                os.chmod(prof, 0o700)
                self.ctx = self._pw.webkit.launch_persistent_context(
                    prof, headless=True, user_agent=MAC_UA if mac else (ua or IPHONE_UA), locale="ja-JP", timezone_id="Asia/Tokyo",
                    viewport={"width": 1280, "height": 900} if mac else {"width": 390, "height": 844},
                    device_scale_factor=2 if mac else 3, is_mobile=not mac, has_touch=not mac,
                    proxy=proxy, env=env)
            else:
                os.makedirs(store.PROFILE_DIR, mode=0o700, exist_ok=True)
                os.chmod(store.PROFILE_DIR, 0o700)
                self.ctx = self._pw.chromium.launch_persistent_context(
                    store.PROFILE_DIR, headless=self.headless, user_agent=UA,
                    executable_path=os.environ.get("XBOT_CHROMIUM") or None,
                    locale="ja-JP", timezone_id="Asia/Tokyo",
                    viewport={"width": 1100, "height": 900} if self.headless else None,
                    args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--disable-dev-shm-usage"]
                         + ([] if self.headless else ["--window-position=0,0", "--window-size=430,900", "--force-device-scale-factor=1"]),
                    proxy=proxy, env=env)
        except BaseException:
            self._pw.stop()
            raise
        self.ctx.set_default_timeout(30000)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        return self

    def exit_info(self):
        """X から見えるこのブラウザの出口（WARP 経由か、どの地域か）。"""
        try:
            self.page.goto("https://www.cloudflare.com/cdn-cgi/trace", wait_until="domcontentloaded", timeout=20000)
            kv = dict(l.split("=", 1) for l in (self.page.inner_text("body") or "").splitlines() if "=" in l)
            return f"WARP={kv.get('warp', '?')} 地域={kv.get('loc', '?')}"
        except Exception as e:  # noqa: BLE001
            return f"出口を確認できませんでした（{type(e).__name__}）"

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

    def probe(self, url):
        """DM画面の作りを調べる（本文・名前は取らず、部品の名前と数だけ）。"""
        import re
        p = self.page
        errs = []
        p.on("console", lambda m: errs.append(m.type) if m.type == "error" else None)
        p.on("pageerror", lambda e: errs.append("pageerror:" + str(e)[:80]))
        p.goto(url, wait_until="domcontentloaded")
        time.sleep(25)
        info = p.evaluate(r"""() => {
          const c = {}; document.querySelectorAll('[data-testid]').forEach(e => { const k = e.getAttribute('data-testid'); c[k] = (c[k]||0)+1; });
          const hrefs = {}; document.querySelectorAll('a[href]').forEach(a => { const h = a.getAttribute('href').replace(/[0-9]+/g,'#').replace(/\/[A-Za-z0-9_]{3,15}$/,'/@'); if (/messages|chat|request/i.test(h)) hrefs[h]=(hrefs[h]||0)+1; });
          const tabs = [...document.querySelectorAll('[role=tab],[role=link],button')].map(e => (e.getAttribute('aria-label')||e.innerText||'').trim()).filter(t => /リクエスト|request|受信|Inbox|未読|すべて|All|チャット|Chat|メッセージ|Message/i.test(t) && t.length < 30);
          return {testids: c, hrefs, tabs: [...new Set(tabs)].slice(0,15), rows: document.querySelectorAll('[role=listitem],[role=row],[role=option]').length};
        }""")
        frames = []
        for f in p.frames[1:][:6]:
            try:
                fi = f.evaluate(r"""() => {
                  const c = {}; document.querySelectorAll('[data-testid]').forEach(e => { const k = e.getAttribute('data-testid'); c[k] = (c[k]||0)+1; });
                  const t = document.body ? document.body.innerText : '';
                  const kw = ['パスコード','暗証','PIN','passcode','Passcode','プレミアム','Premium','認証','リクエスト','Request','未読','暗号','encrypt','設定','Set up','作成','入力'].filter(k => t.includes(k));
                  return {testids: Object.entries(c).sort((a,b)=>b[1]-a[1]).slice(0,40), keywords: kw, buttons: document.querySelectorAll('button,[role=button]').length,
                          inputs: [...document.querySelectorAll('input')].map(i => (i.type||'')+':'+(i.getAttribute('inputmode')||'')+':'+(i.maxLength||'')).slice(0,8),
                          items: document.querySelectorAll('[role=listitem],[role=row],[role=option],li').length};
                }""")
                frames.append({"url": re.sub(r"[0-9]+", "#", f.url.split("?")[0])[:80], **fi})
            except Exception as e:  # noqa: BLE001
                frames.append({"url": f.url[:60], "error": type(e).__name__})
        info["frames"] = frames
        info["detail"] = p.evaluate(r"""() => { const r = document.querySelector('[data-testid=xchatEmbedRoute]'); const o = document.querySelector('[data-testid=xchatEmbedOverlays]');
            const pw = document.querySelector('[data-testid=paywallScrim]');
            return {route_children: r ? r.querySelectorAll('*').length : -1, route_text: r ? r.innerText.length : -1,
                    overlay_children: o ? o.querySelectorAll('*').length : -1, paywall_visible: pw ? !!(pw.offsetWidth||pw.offsetHeight) : null,
                    body_text: document.body.innerText.length, iframes: document.querySelectorAll('iframe').length,
                    has_crypto_subtle: !!(window.crypto && crypto.subtle), has_wasm: typeof WebAssembly === 'object', has_idb: !!window.indexedDB}; }""")
        info["shadow"] = p.evaluate(r"""() => { let hosts = 0, text = 0, inputs = 0, tids = {};
            const walk = (root) => { root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) { hosts++; text += (e.shadowRoot.textContent||'').length;
                inputs += e.shadowRoot.querySelectorAll('input,textarea,[contenteditable]').length;
                e.shadowRoot.querySelectorAll('[data-testid]').forEach(x => { const k=x.getAttribute('data-testid'); tids[k]=(tids[k]||0)+1; }); walk(e.shadowRoot); } }); };
            walk(document); const ifr = document.querySelector('iframe');
            return {hosts, text, inputs, tids: Object.entries(tids).slice(0,30), iframe_src: ifr ? (ifr.getAttribute('src')||'').replace(/[0-9]+/g,'#').slice(0,80) : null,
                    iframe_size: ifr ? [ifr.offsetWidth, ifr.offsetHeight] : null, paywall_text: (document.querySelector('[data-testid=paywallScrim]')||{}).innerText ? document.querySelector('[data-testid=paywallScrim]').innerText.length : 0,
                    title: document.title.replace(/[0-9]+/g,'#').slice(0,40)}; }""")
        info["errors"] = [e for e in errs if e.startswith("pageerror")][:5] + [f"console_errors={errs.count('error')}"]
        # 覆い（有料案内など）が出ているか
        info["kw_main"] = [k for k in ("パスコード", "暗証", "Premium", "プレミアム", "認証済み", "購読", "Subscribe", "チャット", "暗号") if k in (p.inner_text("body") or "")]
        path = re.sub(r"[0-9]+", "#", p.url.split("?")[0])
        return path, info, self.shot("probe")

    def deep_probe(self):
        """チャット画面の作りを、Shadow DOM の中まで調べる（本文・名前は取らない）。"""
        return self.page.evaluate(r"""() => {
          const tids = {}, roles = {}, labels = {}, hrefs = {}; let edit = 0, unreadish = 0;
          const generic = /^(チャット|メッセージ|リクエスト|受信|すべて|未読|新しいチャット|検索|戻る|送信|設定|承認|許可|受け入れる|拒否|削除|ブロック|Accept|Decline|Delete|Requests?|Inbox|Chat|Messages?|Send|Back|New chat|Search|Unread|All|情報|もっと見る|More)$/;
          const walk = (root) => root.querySelectorAll('*').forEach(e => {
            const t = e.getAttribute && e.getAttribute('data-testid'); if (t) tids[t] = (tids[t]||0)+1;
            const r = e.getAttribute && e.getAttribute('role'); if (r) roles[r] = (roles[r]||0)+1;
            const l = e.getAttribute && (e.getAttribute('aria-label')||''); if (l && generic.test(l.trim())) labels[l.trim()] = (labels[l.trim()]||0)+1;
            if ((e.tagName==='BUTTON'||r==='tab'||r==='button') && e.innerText && generic.test(e.innerText.trim())) labels['text:'+e.innerText.trim()] = (labels['text:'+e.innerText.trim()]||0)+1;
            if (e.tagName==='A' && e.getAttribute('href')) { const h = e.getAttribute('href').replace(/[0-9]+/g,'#').replace(/[A-Za-z0-9_-]{16,}/g,'*'); hrefs[h]=(hrefs[h]||0)+1; }
            if (e.isContentEditable || e.tagName==='TEXTAREA') edit++;
            if (/unread/i.test((e.className && e.className.baseVal===undefined ? e.className : '')||'') ) unreadish++;
            if (e.shadowRoot) walk(e.shadowRoot);
          });
          walk(document);
          const top = o => Object.entries(o).sort((a,b)=>b[1]-a[1]).slice(0,60);
          return {tids: top(tids), roles: top(roles), labels: top(labels), hrefs: top(hrefs).slice(0,20), editable: edit, unreadish};
        }""")

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
