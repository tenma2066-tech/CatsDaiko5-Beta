#!/usr/bin/env python3
"""
bot.py -- PayPay ⇄ Kyash 自動換金BOT

流れ:
  初回だけ本人確認 (aml.py)
    PayPay と Kyash それぞれで ¥1 (既定) の送金リンクを作ってBOTに貼る
    → 送信者 (PayPay=externalId / Kyash=publicId) をDiscordアカウントに紐付ける
  換金 (どちら向きでも両方の登録が要る)
    PayPay → Kyash : PayPayの送金リンクを貼る
                     → 受け取る**前**に送信者・残高種別・金額・上限・払える残高を検査
                     → 受け取る → 手数料を引いた額を、登録済みのKyash垢へ送る
    Kyash → PayPay : Kyashの送金リンクを貼る
                     → 受け取る**前**に公開ページの Kyash ID・金額・上限・払える残高を検査
                     → 受け取る → 明細の publicId で送信者を裏取り
                     → 手数料を引いた額を PayPay で払い出す (送金リンク / 登録垢へ直接)

安全側の作り:
  - 登録と違う垢から来た送金は、受け取らずに換金を拒否する (設定で変えられない)
  - 金額は常にリンクの実額 (受け取った額) で計算する
  - 払い出しの成否が分からない時は UNCERTAIN で止める (投げ直すと二重払いになる)
  - 注文の状態遷移は compare-and-set のみ (/再送 の連打や自動処理と競合しても二重に払わない)
  - 受け取る前に払い出し側の残高を確認し、払えないなら受け取らない
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
import traceback
from uuid import uuid4
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

import aml
import kyash_bridge as kyash
import paypay_bridge as paypay
import store
import wallet_detect

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE = os.environ.get("EXCHANGE_DATA_DIR", "/var/lib/paykyash")
SECRETS_FILE = os.environ.get("EXCHANGE_SECRETS_FILE", "/etc/paykyash/secrets.json")
SECRETS_CANDIDATES = (SECRETS_FILE,)
JST = timezone(timedelta(hours=9))

COLOR = {
    "panel": 0xFF0033,
    "ok": 0x00C853,
    "warn": 0xFAB005,
    "error": 0xD63031,
    "info": 0x74B9FF,
    "aml": 0x6C5CE7,
}
DIR_LABEL = store.DIRECTION_LABEL
SVC_LABEL = store.SERVICE_LABEL
SVC_EMOJI = {"paypay": "🟥", "kyash": "🟦"}

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!pk ", intents=intents, help_command=None)

_user_locks: dict[int, asyncio.Lock] = {}


def _lock_for(user_id: int) -> asyncio.Lock:
    return _user_locks.setdefault(int(user_id), asyncio.Lock())


# ============================================================
# secrets.json (BOTトークン / 管理者)
#
# 管理者IDは secrets.json に**事前に**書いておく。Discordから増やす導線は
# 用意しない (config.json は運用中にBOT自身が書き換えるファイルなので、
# そこに権限を置くと壊れた/差し替えられただけで権限の持ち主が変わりうる)。
# ============================================================

_secrets_error = ""


def load_secrets() -> dict:
    global _secrets_error
    for path in SECRETS_CANDIDATES:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            continue
        except Exception as e:
            _secrets_error = f"{os.path.basename(path)} が壊れている: {e}"
            continue
    return {}


def _die(title: str, *lines: str) -> None:
    print("=" * 60)
    print(f" {title}")
    print(f" 場所: {SECRETS_FILE}")
    print("-" * 60)
    for ln in lines:
        print(f" {ln}")
    print("=" * 60)
    sys.exit(1)


def load_token() -> str:
    env = os.environ.get("PK_BOT_TOKEN", "").strip()
    if env:
        return env
    tok = (load_secrets().get("bot_token") or "").strip()
    if tok and not tok.startswith("ここに"):
        return tok
    _die(f"BOTトークンが未設定。{_secrets_error}",
         "secrets.json の bot_token に、Discord BOT のトークンを貼って。",
         '  {"bot_token": "MTIz...", "admin_id": 123456789012345678}',
         "もしくは環境変数 PK_BOT_TOKEN を設定。")
    raise SystemExit(1)


def _parse_ids(value) -> list[int]:
    out: list[int] = []
    if value is None:
        return out
    items = value if isinstance(value, (list, tuple)) else str(value).replace(",", " ").split()
    for v in items:
        try:
            n = int(str(v).strip())
        except (TypeError, ValueError):
            continue
        if n > 0:
            out.append(n)
    return out


def admin_ids() -> list[int]:
    ids = _parse_ids(os.environ.get("PK_ADMIN_ID", ""))
    if ids:
        return ids
    sec = load_secrets()
    return _parse_ids(sec.get("admin_id")) + _parse_ids(sec.get("admin_ids"))


def require_admin_ids() -> list[int]:
    ids = admin_ids()
    if ids:
        return ids
    _die(f"管理者IDが未設定。{_secrets_error}",
         "secrets.json の admin_id に、あなたのDiscordユーザーIDを入れて。",
         '  {"bot_token": "MTIz...", "admin_id": 123456789012345678}',
         "",
         "ユーザーIDの調べ方:",
         "  Discordの 設定 → 詳細設定 → 開発者モード をON",
         "  → 自分のアイコンを右クリック →「ユーザーIDをコピー」")
    raise SystemExit(1)


def is_admin(user_id: int) -> bool:
    return int(user_id) in admin_ids()


def jst_now() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")


def yen(n) -> str:
    try:
        return f"¥{int(n):,}"
    except (TypeError, ValueError):
        return "¥?"


def _e(title: str, desc: str = "", color: str = "info") -> discord.Embed:
    return discord.Embed(title=title, description=desc[:4000], color=COLOR.get(color, color)
                         if isinstance(color, str) else color)


# ============================================================
# ログ
# ============================================================

def _channel_for(kind: str) -> int:
    """kind: "tx"(取引) / "error"(内部) / "detect"(本人確認・検知)。未設定なら上位へ。"""
    cfg = store.load_config()
    tx = int(cfg.get("log_channel_id") or 0)
    err = int(cfg.get("error_channel_id") or 0) or tx
    if kind == "tx":
        return tx
    if kind == "detect":
        return int(cfg.get("detect_channel_id") or 0) or err
    return err


async def notify_admin(text: str, kind: str = "error"):
    ch_id = _channel_for(kind)
    if not ch_id:
        print(f"[admin] {text}")
        return
    ch = bot.get_channel(int(ch_id))
    if not ch:
        print(f"[admin] チャンネル {ch_id} が見つからない: {text}")
        return
    try:
        await ch.send(text[:1900], allowed_mentions=discord.AllowedMentions.none())
    except Exception as e:
        print(f"[admin] 送信失敗: {e}")


_notify_next: dict[str, float] = {}


async def notify_admin_throttled(tag: str, text: str, interval: float = 600) -> None:
    now = time.time()
    if _notify_next.get(tag, 0.0) > now:
        return
    _notify_next[tag] = now + interval
    await notify_admin(text)


async def log_event(title: str, description: str, kind: str = "error",
                    color: int | str | None = None, fields: list[tuple] | None = None):
    ch_id = _channel_for(kind)
    if not ch_id:
        print(f"[{kind}] {title}: {description[:200]}")
        return
    ch = bot.get_channel(int(ch_id))
    if not ch:
        return
    c = COLOR.get(color, COLOR["warn"]) if isinstance(color, str) or color is None else color
    e = discord.Embed(title=title, description=description[:3900], color=c,
                      timestamp=datetime.now(JST))
    for name, value, inline in (fields or []):
        e.add_field(name=name, value=str(value)[:1024] or "-", inline=inline)
    try:
        await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except Exception as ex:
        print(f"[{kind}] 送信失敗: {ex}")


async def log_trade(o: dict) -> None:
    ch_id = _channel_for("tx")
    if not ch_id:
        return
    ch = bot.get_channel(int(ch_id))
    if not ch:
        return
    e = discord.Embed(title=f"💱 換金成立 {o['id']}  ({DIR_LABEL[o['direction']]})",
                      color=COLOR["ok"], timestamp=datetime.now(JST))
    e.add_field(name="ユーザー", value=f"<@{o['user_id']}>\n`{o['user_id']}`", inline=True)
    e.add_field(name="受取", value=yen(o["in_amount"]), inline=True)
    e.add_field(name="払い出し", value=yen(o["out_amount"]), inline=True)
    e.add_field(name="手数料", value=f"{yen(o['fee_jpy'])} ({o['fee_percent']}%)", inline=True)
    pay = o.get("payout") or {}
    e.add_field(name="方式", value=PAYOUT_LABEL.get(pay.get("method"), pay.get("method") or "?"),
                inline=True)
    dest = o.get("dest") or {}
    if dest:
        e.add_field(name="送り先", value=aml.display(dest), inline=False)
    try:
        await ch.send(embed=e, allowed_mentions=discord.AllowedMentions.none())
    except Exception as ex:
        print(f"[log] 送信失敗: {ex}")


# ============================================================
# 返信の行き先
# ============================================================

async def _dm(user_id: int, **kw) -> bool:
    try:
        u = await bot.fetch_user(int(user_id))
        await u.send(**kw)
        return True
    except Exception as e:
        print(f"[dm] 送信失敗 user={user_id}: {type(e).__name__}: {e}")
        return False


def reply_to(interaction: discord.Interaction):
    """結果の返し先: その場の応答 (本人にだけ見える)。送れなければDMへ回す。"""
    uid = interaction.user.id

    async def reply(**kw):
        try:
            await interaction.followup.send(ephemeral=True, **kw)
        except Exception as e:
            print(f"[reply] 応答できないのでDMへ回す: {type(e).__name__}: {e}")
            await _dm(uid, **kw)
    return reply


def reply_dm(user_id: int):
    async def reply(**kw):
        await _dm(user_id, **kw)
    return reply


# ============================================================
# 料金
# ============================================================

PAYOUT_LABEL = {"transfer": "Kyash 直接送金", "link": "送金リンク",
                "direct": "PayPay 直接送金", "return": "送信者へ返金"}


def quote(cfg: dict, direction: str, amount: int, extra_fee: float = 0.0) -> dict:
    """受取額 → 手数料と払い出し額 (払い出しは1円未満切り捨て = 手数料は切り上げ)"""
    d = store.direction_cfg(cfg, direction)
    pct = round(float(d.get("fee_percent", 0) or 0) + float(extra_fee or 0), 3)
    fixed = max(0, int(d.get("fee_fixed_jpy", 0) or 0))
    fee = int(math.ceil(int(amount) * pct / 100.0 - 1e-9)) + fixed
    fee = min(max(fee, 0), int(amount))
    return {"in": int(amount), "fee_percent": pct, "fee_fixed": fixed,
            "fee_jpy": fee, "out": int(amount) - fee}


def range_error(cfg: dict, direction: str, amount: int, user_id: int,
                exclude_today: int = 0) -> str:
    """受付範囲と日次上限。問題なければ空文字。"""
    d = store.direction_cfg(cfg, direction)
    if not (int(d["min_jpy"]) <= amount <= int(d["max_jpy"])):
        return (f"リンクの金額: {yen(amount)}\n"
                f"受付範囲: {yen(d['min_jpy'])} 〜 {yen(d['max_jpy'])}")
    used = store.today_total_jpy(user_id) - int(exclude_today)
    limit = int(cfg.get("daily_limit_jpy") or 0)
    if limit and used + amount > limit:
        return (f"1日あたり {yen(limit)} まで。\n"
                f"本日の利用済み: {yen(used)} / 今回 {yen(amount)}")
    return ""


def hold_options(cfg: dict) -> dict:
    try:
        pages = int(cfg.get("hold_history_pages", 5) or 5)
    except (TypeError, ValueError):
        pages = 5
    return {"history_pages": max(1, min(pages, 20))}


# ============================================================
# 与信 (受け取る前に、払い出し側の残高で払えるかを見る)
#
# 払えないのに受け取ると、相手の金を握ったまま払い出せない状態になり、
# 手作業の返金が要る。手を出す前に断れば被害は売上機会だけで済む。
# ============================================================

_stock_lock = asyncio.Lock()
_reserved: dict[str, tuple[str, int]] = {}      # 受取記録キー → (払い出し側, 額)
_balance_cache: dict[str, tuple[float, int | None]] = {}


async def side_balance(side: str, cfg: dict, fresh: bool = False) -> int | None:
    ttl = max(0, int(cfg.get("balance_cache_sec", 60) or 0))
    cached = _balance_cache.get(side)
    if cached and not fresh and time.time() - cached[0] < ttl:
        return cached[1]
    try:
        if side == "kyash":
            v = await asyncio.to_thread(kyash.balance)
        else:
            v = await asyncio.to_thread(paypay.sendable_balance)
    except Exception as e:
        print(f"[与信] {side} 残高取得で例外: {type(e).__name__}: {e}")
        v = None
    _balance_cache[side] = (time.time(), v)
    return v


def invalidate_balance(side: str) -> None:
    _balance_cache.pop(side, None)


async def capacity(cfg: dict, side: str, need: int, exclude_key: str = "") -> dict:
    """払い出し側の残高 - (受取中の確保分 + 受け取り済みで未払いの分 + 予備) >= need か"""
    if not cfg.get("check_balance_before_receive", True):
        return {"ok": True, "skipped": True}
    bal = await side_balance(side, cfg, fresh=True)
    if bal is None:
        return {"ok": False, "why": "残高を確認できない", "balance": None, "need": need}
    held = sum(a for k, (s, a) in _reserved.items() if s == side and k != exclude_key)
    unpaid = store.unpaid_out(side)
    reserve = max(0, int(cfg.get("payout_reserve_jpy", 0) or 0))
    free = int(bal) - held - unpaid - reserve
    return {"ok": free >= need, "why": "" if free >= need else "残高不足",
            "balance": int(bal), "need": need, "held": held, "unpaid": unpaid,
            "reserve": reserve, "free": free}


def release_reserve(key: str) -> None:
    _reserved.pop(key, None)


async def warn_capacity(cap: dict, side: str, amount: int, who: str) -> None:
    await notify_admin_throttled(
        f"cap_{side}_{cap.get('why')}",
        f"🈳 **{SVC_LABEL[side]}の払い出し残高が足りず、換金を受け取らずに断った**\n"
        f"{who} / 必要 {yen(cap.get('need'))} / 残高 {yen(cap.get('balance'))} "
        f"(未払い {yen(cap.get('unpaid', 0))} ・ 受取中 {yen(cap.get('held', 0))} ・ "
        f"予備 {yen(cap.get('reserve', 0))})\n"
        f"-# 理由: {cap.get('why')}。補充すれば自動で受付が戻る。")


def capacity_embed(side: str) -> discord.Embed:
    return _e("🈳 いま受け付けられない",
              f"{SVC_LABEL[side]}側の払い出し残高が足りないため、安全のため中止した。\n\n"
              "**受け取っていないので、リンクはそのまま手元にある。**\n"
              "しばらく待ってからもう一度試して。", "warn")


# ============================================================
# 本人確認 (初回だけ)
# ============================================================

REG_HOWTO = {
    "paypay": ("PayPayアプリ →「送る」→ 金額を入れて **リンクを作成**\n"
               "パスコードを付けた場合は、貼る時にパスコードも入れて。"),
    "kyash": ("Kyashアプリ →「送金」→「リンクで送る」→ 金額を入れて **リンクを作成**\n"
              "`https://kyash.me/payments/...` の形のURLをそのまま貼って。"),
}


def reg_status_lines(user_id: int) -> str:
    lines = []
    for svc in store.SERVICES:
        b = store.binding(user_id, svc)
        if b:
            lines.append(f"{SVC_EMOJI[svc]} **{SVC_LABEL[svc]}**: ✅ 登録済み — {aml.display(b)}")
        else:
            lines.append(f"{SVC_EMOJI[svc]} **{SVC_LABEL[svc]}**: ❌ 未登録")
    return "\n".join(lines)


def reg_intro_embed(cfg: dict, user_id: int, missing: list[str] | None = None) -> discord.Embed:
    lo, hi = aml.amount_range(cfg)
    amt = yen(lo) if lo == hi else f"{yen(lo)}〜{yen(hi)} (その都度指定)"
    e = _e("🪪 本人確認 (初回だけ)",
           "マネロン・詐欺対策のため、換金の前に **PayPay と Kyash の両方** を"
           "このDiscordアカウントに紐付ける。**登録は初回だけ。**\n\n"
           f"やり方: 各サービスで **{amt} の送金リンク** を作ってBOTに貼るだけ。\n"
           "BOTがリンクの送信者を記録し、以後は**登録した垢から送られたリンクしか受け取らない**。\n"
           "（登録と違う垢から送られたリンクは、受け取らずに換金を拒否する）", "aml")
    e.add_field(name="あなたの登録状況", value=reg_status_lines(user_id), inline=False)
    if missing:
        e.add_field(name="次にやること",
                    value="下のボタンから " + " と ".join(f"**{SVC_LABEL[s]}**" for s in missing)
                          + " を登録して。", inline=False)
    e.set_footer(text="1つの垢は1つのDiscordアカウントにしか登録できない")
    return e


def challenge_embed(cfg: dict, ch: dict, note: str = "") -> discord.Embed:
    svc = ch["service"]
    exp = int(ch["expires_at"])
    e = _e(f"{SVC_EMOJI[svc]} {SVC_LABEL[svc]} の本人確認",
           (f"{note}\n\n" if note else "")
           + f"**{yen(ch['amount'])} ちょうど**の送金リンクを作って、"
             f"**<t:{exp}:R>** までに下の「📋 リンクを貼る」から貼って。\n\n"
           + REG_HOWTO[svc], "aml")
    if svc == "paypay":
        e.add_field(name="この¥のゆくえ",
                    value=("確認のためBOTが受け取る。" if cfg.get("reg_receive_paypay", True)
                           else "BOTは受け取らない。登録後にPayPayアプリでキャンセルすれば戻る。"),
                    inline=False)
    else:
        e.add_field(name="この¥のゆくえ",
                    value="Kyashは受け取らないと送信者が分からないので、確認のためBOTが受け取る。",
                    inline=False)
    e.add_field(name="貼り直し", value=f"あと {aml.attempts_left(cfg, ch['user_id'], svc)} 回",
                inline=True)
    e.add_field(name="期限", value=f"<t:{exp}:T>", inline=True)
    return e


def reg_attempt_error(cfg: dict, user_id: int, service: str, gate: dict) -> discord.Embed:
    r = gate.get("reason")
    if r == "COOLDOWN":
        return _e("⏳ 少し待って", f"あと {gate.get('wait', 0):.0f} 秒待ってから貼り直して。", "warn")
    if r == "RATE_LIMIT":
        return _e("⛔ 試行回数が多すぎる",
                  f"1時間に試せる回数を超えた。{int(gate.get('wait', 0) // 60) + 1}分ほど待って。",
                  "error")
    if r == "TOO_MANY":
        return _e("⛔ 貼り直しの回数切れ", "「🔁 やり直す」で金額を出し直して最初から。", "error")
    if r == "EXPIRED":
        return _e("⏰ 時間切れ", "「🔁 やり直す」で金額を出し直して最初から。", "warn")
    return _e("❔ 登録の受付が始まっていない",
              "パネルの「🪪 本人確認」から始めて。", "warn")


async def reg_issue(interaction: discord.Interaction, service: str, note: str = "") -> None:
    cfg = store.load_config()
    acc = store.get_account(interaction.user.id) or {}
    if acc.get("state") == "BLOCKED":
        return await interaction.response.send_message(embed=blocked_embed(), ephemeral=True)
    if service == "kyash" and not kyash.ready():
        await notify_admin_throttled("kyash_not_ready",
                                     "🔑 **Kyashが未ログインで本人確認を受けられない** `/kyashログイン`")
        return await interaction.response.send_message(
            embed=_e("🛠 準備中", "BOT側のKyashの準備ができていない。管理者に知らせた。", "warn"),
            ephemeral=True)
    r = aml.issue_challenge(cfg, interaction.user.id, service)
    if not r["ok"]:
        return await interaction.response.send_message(
            embed=reg_attempt_error(cfg, interaction.user.id, service, r), ephemeral=True)
    await interaction.response.send_message(
        embed=challenge_embed(cfg, r["challenge"], note), view=RegSubmitView(service),
        ephemeral=True)


def blocked_embed() -> discord.Embed:
    return _e("⛔ 利用停止中", "このアカウントは管理者によって利用停止されている。", "error")


class RegStartView(discord.ui.View):
    """本人確認のサービス選択。timeout=None + 固定 custom_id で再起動後も押せる。"""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="PayPayを登録", style=discord.ButtonStyle.danger, emoji="🟥",
                       custom_id="pk:reg:paypay")
    async def reg_paypay(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reg_issue(interaction, "paypay")

    @discord.ui.button(label="Kyashを登録", style=discord.ButtonStyle.primary, emoji="🟦",
                       custom_id="pk:reg:kyash")
    async def reg_kyash(self, interaction: discord.Interaction, button: discord.ui.Button):
        await reg_issue(interaction, "kyash")


class RegSubmitView(discord.ui.View):
    def __init__(self, service: str):
        super().__init__(timeout=None)
        self.service = service
        submit = discord.ui.Button(label="リンクを貼る", style=discord.ButtonStyle.success,
                                   emoji="📋", custom_id=f"pk:regsub:{service}")
        redo = discord.ui.Button(label="やり直す", style=discord.ButtonStyle.secondary,
                                 emoji="🔁", custom_id=f"pk:regredo:{service}")
        submit.callback = self._submit
        redo.callback = self._redo
        self.add_item(submit)
        self.add_item(redo)

    async def _submit(self, interaction: discord.Interaction):
        if not aml.get_challenge(interaction.user.id, self.service):
            return await interaction.response.send_message(
                embed=reg_attempt_error(store.load_config(), interaction.user.id, self.service,
                                        {"reason": "EXPIRED"}), ephemeral=True)
        await interaction.response.send_modal(RegLinkModal(self.service))

    async def _redo(self, interaction: discord.Interaction):
        await reg_issue(interaction, self.service, note="🔁 出し直した。")


# 再起動後もボタンを受けるために setup_hook で add_view する分だけを持つ。
# ★メッセージに付けて送るのは毎回新しいインスタンスにすること。
#   discord.py はエフェメラルに timeout=None のビューを付けると、その
#   インスタンスの timeout を15分に書き換える。共有の1個を送ると15分後に
#   永続ビューごと止まり、再起動までボタンが効かなくなる。
REG_SUBMIT_VIEWS: dict[str, RegSubmitView] = {}


class RegLinkModal(discord.ui.Modal):
    def __init__(self, service: str):
        super().__init__(title=f"{SVC_LABEL[service]} の本人確認", timeout=600)
        self.service = service
        self.link = discord.ui.TextInput(
            label="送金リンクのURL",
            placeholder=("https://pay.paypay.ne.jp/..." if service == "paypay"
                         else "https://kyash.me/payments/..."),
            max_length=300, required=True)
        self.add_item(self.link)
        self.passcode = None
        if service == "paypay":
            self.passcode = discord.ui.TextInput(
                label="パスコード (付けた場合だけ)", placeholder="4桁", max_length=8,
                required=False)
            self.add_item(self.passcode)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        link = str(self.link.value or "").strip()
        lock = _lock_for(interaction.user.id)
        if lock.locked():
            return await interaction.followup.send(
                embed=_e("⏳ 処理中", "前の処理が終わるまで待って。", "warn"), ephemeral=True)
        async with lock:
            try:
                if self.service == "paypay":
                    pc = str(self.passcode.value or "").strip() if self.passcode else ""
                    await process_reg_paypay(interaction, link, pc or None)
                else:
                    await process_reg_kyash(interaction, link)
            except Exception as e:
                traceback.print_exc()
                await interaction.followup.send(
                    embed=_e("❌ 内部エラー", f"`{type(e).__name__}`", "error"), ephemeral=True)


def reg_done_embed(user_id: int, service: str, identity: dict, extra: str = "") -> discord.Embed:
    e = _e(f"✅ {SVC_LABEL[service]} を登録した",
           f"{aml.display(identity)}\n\n以後は**この垢から送られたリンクだけ**を受け付ける。"
           + (f"\n{extra}" if extra else ""), "ok")
    e.add_field(name="登録状況", value=reg_status_lines(user_id), inline=False)
    g = aml.gate(user_id)
    if g["state"] == "register":
        e.add_field(name="次にやること",
                    value=" / ".join(SVC_LABEL[s] for s in g["missing"]) + " も登録すると換金できる。",
                    inline=False)
    else:
        e.add_field(name="準備完了", value="パネルから換金できる。", inline=False)
    return e


async def _reg_fail(interaction, cfg, uid, service, title, msg, note_not_received=True):
    left = aml.attempts_left(cfg, uid, service)
    tail = []
    if note_not_received:
        tail.append("-# 受け取っていない。" + ("PayPayアプリでキャンセルすれば戻る。"
                                         if service == "paypay" else "Kyashアプリで取り消せば戻る。"))
    if left:
        tail.append(f"-# 貼り直しはあと{left}回 (「📋 リンクを貼る」から)。")
    kw = {"view": RegSubmitView(service)} if left else {}    # view=None は送れない (TypeError)
    await interaction.followup.send(
        embed=_e(title, msg + ("\n\n" + "\n".join(tail) if tail else ""), "error"),
        ephemeral=True, **kw)


async def process_reg_paypay(interaction: discord.Interaction, link: str, passcode: str | None):
    store.load_orders()
    cfg = store.load_config()
    user = interaction.user
    gate = aml.take_attempt(cfg, user.id, "paypay")
    if not gate["ok"]:
        return await interaction.followup.send(
            embed=reg_attempt_error(cfg, user.id, "paypay", gate), ephemeral=True)
    ch = gate["challenge"]

    chk = await asyncio.to_thread(paypay.inspect_link, link)
    if not chk.get("ok"):
        return await _reg_fail(interaction, cfg, user.id, "paypay", "❌ リンクが使えない",
                               chk.get("error") or "不明なエラー",
                               note_not_received=chk.get("reason") != "NOT_PENDING")
    info = chk["info"]
    ident = aml.paypay_identity(info)
    lkey = store.paypay_link_key(info)
    j = aml.judge_registration(cfg, user.id, "paypay", ident, amount=int(info.get("amount") or 0),
                               want=ch["amount"], ref=lkey)
    if not j["ok"]:
        store.log_detection(user.id, str(user), "REG", "DENY", int(info.get("amount") or 0),
                            note=f"PayPay登録: {aml.reason_text(j['reason'])}",
                            sender=aml.display(ident))
        if j["reason"] in ("DUPLICATE", "BLOCKED_ID"):
            await log_event("🚩 PayPay登録を拒否", f"{user.mention} の登録を拒否した",
                            kind="detect", color="error",
                            fields=[("理由", aml.reason_text(j["reason"]), True),
                                    ("送信者", aml.display(ident), False),
                                    ("持ち主", f"<@{(j.get('owner') or {}).get('user_id')}>"
                                     if j.get("owner") else "-", True)])
        return await _reg_fail(interaction, cfg, user.id, "paypay", "❌ 登録できない", j["message"])

    receive = bool(cfg.get("reg_receive_paypay", True))
    if receive and info.get("has_password") and not passcode:
        return await _reg_fail(interaction, cfg, user.id, "paypay", "🔒 パスコードが必要",
                               "このリンクはパスコード付き。パスコード欄にも入れて貼り直して。")
    if not lkey or not store.reserve_link(lkey, user.id, note="本人確認"):
        return await _reg_fail(interaction, cfg, user.id, "paypay", "❌ このリンクは使えない",
                               "既に使われたリンク。新しく作り直して。")

    note = ""
    if receive:
        res = await asyncio.to_thread(paypay.receive_link, link, passcode, None,
                                      **hold_options(cfg))
        st = res.get("status")
        if st == "failed":
            store.release_link(lkey)
            if res.get("reason") in ("RELOGIN", "NO_TOKEN"):
                await notify_admin_throttled("relogin", "🔑 **PayPayのセッションが切れている** "
                                                        "管理者が接続状態を確認してください。")
            return await _reg_fail(interaction, cfg, user.id, "paypay", "❌ 受け取れなかった",
                                   res.get("error") or "不明なエラー")
        store.confirm_link(lkey, f"REG:{user.id}", int(info.get("amount") or 0), kind="REG")
        if st == "held":
            note = ("-# ¥はPayPayの一時保留になった。PayPayアプリで承認するかキャンセルして"
                    " (登録自体は完了している)。")
    else:
        store.confirm_link(lkey, f"REG:{user.id}", 0, kind="REG", received=False)
        note = "-# このリンクは受け取っていない。PayPayアプリからキャンセルすれば戻る。"

    store.bind(user.id, str(user), "paypay", ident, ref=lkey, amount=int(info.get("amount") or 0))
    aml.drop_challenge(user.id, "paypay")
    store.log_detection(user.id, str(user), "REG", "BIND", int(info.get("amount") or 0),
                        note="PayPay登録", sender=aml.display(ident))
    await log_event("🪪 PayPayを登録", f"{user.mention} がPayPay垢を紐付けた", kind="detect",
                    color="aml", fields=[("PayPay", aml.display(ident), False)])
    await interaction.followup.send(embed=reg_done_embed(user.id, "paypay", ident, note),
                                    view=RegStartView() if aml.gate(user.id)["missing"] else PanelView(),
                                    ephemeral=True)


async def _kyash_return_small(sender: dict, amount: int, why: str) -> bool:
    """登録で受け取ってしまった少額を送信者へ返す (ベストエフォート)"""
    if not sender or not sender.get("public_id"):
        return False
    r = await asyncio.to_thread(kyash.send, amount, sender["public_id"], sender.get("user_name", ""))
    if r.get("status") != "sent":
        await notify_admin(f"⚠️ Kyash登録の返金 {yen(amount)} に失敗 ({why}): "
                           f"@{sender.get('user_name')} / {r.get('error')}")
        return False
    invalidate_balance("kyash")
    return True


async def process_reg_kyash(interaction: discord.Interaction, link: str):
    store.load_orders()
    cfg = store.load_config()
    user = interaction.user
    gate = aml.take_attempt(cfg, user.id, "kyash")
    if not gate["ok"]:
        return await interaction.followup.send(
            embed=reg_attempt_error(cfg, user.id, "kyash", gate), ephemeral=True)
    ch = gate["challenge"]

    chk = await asyncio.to_thread(kyash.inspect_link, link)
    if not chk.get("ok"):
        return await _reg_fail(interaction, cfg, user.id, "kyash", "❌ リンクが使えない",
                               chk.get("error") or "不明なエラー",
                               note_not_received=chk.get("reason") not in ("NOT_PENDING",))
    if int(chk["amount"]) != int(ch["amount"]):
        return await _reg_fail(interaction, cfg, user.id, "kyash", "❌ 金額が違う",
                               f"リンクの金額が **{yen(chk['amount'])}** になっている。\n"
                               f"**{yen(ch['amount'])} ちょうど**のリンクを作り直して貼って。")
    page = aml.kyash_page_identity(chk)
    pre = aml.precheck_kyash_registration(user.id, page)
    if not pre["ok"]:
        store.log_detection(user.id, str(user), "REG", "DENY", chk["amount"],
                            note=f"Kyash登録(受取前): {aml.reason_text(pre['reason'])}",
                            sender=aml.display(page))
        return await _reg_fail(interaction, cfg, user.id, "kyash", "❌ 登録できない",
                               pre["message"], note_not_received=False)

    lkey = store.kyash_link_key(chk["uuid"])
    if not store.reserve_link(lkey, user.id, note="本人確認"):
        return await _reg_fail(interaction, cfg, user.id, "kyash", "❌ このリンクは使えない",
                               "既に使われたリンク。新しく作り直して。")
    await interaction.followup.send("-# 受け取って送信者を確認している… (最大30秒ほど)",
                                    ephemeral=True)
    res = await asyncio.to_thread(kyash.claim_and_identify, chk["uuid"], chk["amount"],
                                  store.used_kyash_tx())
    st = res.get("status")
    if st == "failed":
        store.release_link(lkey)
        if res.get("reason") == "NO_TOKEN":
            await notify_admin_throttled("kyash_relogin",
                                         "🔑 **Kyashのセッションが切れている** `/kyashログイン`")
        return await _reg_fail(interaction, cfg, user.id, "kyash", "❌ 受け取れなかった",
                               res.get("error") or "不明なエラー")
    if st != "received":
        # 受け取った (可能性がある) が送信者がまだ分からない。後追いで特定して登録を仕上げる
        store.confirm_link(lkey, f"REG:{user.id}", chk["amount"], kind="REG",
                           reg_pending=int(user.id), reg_user_name=str(user),
                           claimed_at=res.get("claimed_at") or time.time(),
                           page_user_name=page.get("user_name", ""))
        aml.drop_challenge(user.id, "kyash")
        await notify_admin(f"🕵️ Kyash登録の送信者を即時に特定できなかった: {user.mention} "
                           f"({res.get('error')}) → 数分おきに明細を見直して自動で仕上げる")
        return await interaction.followup.send(
            embed=_e("⏳ 送信者を確認中",
                     "リンクは受け取ったが、Kyashの明細への反映が遅れていて送信者をまだ確認できない。\n"
                     "**数分以内に自動で確認して、結果をDMで知らせる。** このまま待って。", "warn"),
            ephemeral=True)
    await finish_kyash_registration(user.id, str(user), lkey, res["sender"], chk["amount"],
                                    page, reply_to(interaction))


async def finish_kyash_registration(uid: int, user_name: str, lkey: str, sender: dict,
                                    amount: int, page: dict, tell) -> bool:
    cfg = store.load_config()
    ident = aml.kyash_identity(sender)
    if not ident.get("user_name") and page.get("user_name"):
        ident["user_name"] = page["user_name"]
    j = aml.judge_registration(cfg, uid, "kyash", ident, amount=amount, want=amount, ref=lkey)
    store.confirm_link(lkey, f"REG:{uid}", amount, kind="REG", kyash_tx=sender.get("tx", ""),
                       reg_pending=None)
    if not j["ok"]:
        returned = await _kyash_return_small(sender, amount, f"登録拒否 {j['reason']}")
        store.log_detection(uid, user_name, "REG", "DENY", amount,
                            note=f"Kyash登録: {aml.reason_text(j['reason'])}",
                            sender=aml.display(ident))
        await log_event("🚩 Kyash登録を拒否", f"<@{uid}> の登録を拒否した", kind="detect",
                        color="error", fields=[("理由", aml.reason_text(j["reason"]), True),
                                               ("送信者", aml.display(ident), False),
                                               ("返金", "済" if returned else "失敗/対象外", True)])
        await tell(embed=_e("❌ 登録できない",
                            j["message"] + ("\n\n-# 受け取った¥は送信者へ返した。" if returned else ""),
                            "error"))
        return False
    if page.get("user_name") and ident.get("user_name") and not aml.same_kyash_id(
            page["user_name"], ident["user_name"]):
        await notify_admin(f"🔎 Kyash登録で、公開ページのID (@{page['user_name']}) と明細のID "
                           f"(@{ident['user_name']}) が違った: <@{uid}>。明細の方で登録した。")
    store.bind(uid, user_name, "kyash", ident, ref=lkey, amount=amount)
    store.log_detection(uid, user_name, "REG", "BIND", amount, note="Kyash登録",
                        sender=aml.display(ident))
    await log_event("🪪 Kyashを登録", f"<@{uid}> がKyash垢を紐付けた", kind="detect",
                    color="aml", fields=[("Kyash", aml.display(ident), False)])
    await tell(embed=reg_done_embed(uid, "kyash", ident),
               view=RegStartView() if aml.gate(uid)["missing"] else PanelView())
    return True


# ============================================================
# パネル / 換金の入口
# ============================================================

def panel_embed(cfg: dict) -> discord.Embed:
    lines = []
    for d in store.DIRECTIONS:
        dc = store.direction_cfg(cfg, d)
        fee = f"{dc['fee_percent']}%" + (f" + {yen(dc['fee_fixed_jpy'])}" if dc.get("fee_fixed_jpy")
                                         else "")
        state = "" if dc.get("enabled", True) else " 〔停止中〕"
        lines.append(f"**{DIR_LABEL[d]}**{state}\n手数料 {fee} ／ {yen(dc['min_jpy'])}〜{yen(dc['max_jpy'])}")
    lo, hi = aml.amount_range(cfg)
    reg_amt = yen(lo) if lo == hi else f"{yen(lo)}〜{yen(hi)}"
    e = _e("💱 PayPay ⇄ Kyash 自動換金", "\n\n".join(lines), "panel")
    e.add_field(name="1日の上限", value=yen(cfg.get("daily_limit_jpy", 0)), inline=True)
    e.add_field(name="使い方", inline=False, value=(
        f"1️⃣ **初回だけ** 🪪本人確認 で PayPay と Kyash を登録 (各 {reg_amt} の送金リンクを貼るだけ)\n"
        "2️⃣ 換金したい方向のボタン → 送金リンクを貼る\n"
        "3️⃣ 受け取りを確認したら、手数料を引いた額を**登録済みの垢へ**自動で払い出す"))
    e.add_field(name="⚠️ 注意", inline=False, value=(
        "登録した垢**以外**から送られたリンクは、**受け取らずに換金を拒否**する。\n"
        "金額はリンクの額で決まる。"))
    if not cfg.get("enabled", True):
        e.add_field(name="🛠 受付停止中", value=cfg.get("maintenance_note") or "メンテナンス中",
                    inline=False)
    return e


STATUS_JP = {
    "RECEIVING": "受取確認中",
    "PAID": "払い出し待ち", "HOLD": "確認待ち", "SENDING": "送金中", "SENT": "完了",
    "UNCERTAIN": "要確認", "FAILED": "失敗(対応中)", "RETURNING": "返金中",
    "RETURNED": "送信者へ返金済", "REFUNDED": "返金済", "CANCELED": "取消",
}


def user_status_embed(user: discord.abc.User) -> discord.Embed:
    e = _e("📜 あなたの状況", "", "info")
    e.add_field(name="登録", value=reg_status_lines(user.id), inline=False)
    rows = []
    for o in store.user_orders(user.id, 10):
        rows.append(f"`{o['id']}` {o['created_at'][5:16]} {DIR_LABEL[o['direction']]} "
                    f"{yen(o['in_amount'])}→{yen(o['out_amount'])} "
                    f"**{STATUS_JP.get(o['status'], o['status'])}**")
    e.add_field(name="直近の換金", value="\n".join(rows) or "まだ無い", inline=False)
    e.add_field(name="本日の利用", value=yen(store.today_total_jpy(user.id)), inline=True)
    return e


async def open_exchange(interaction: discord.Interaction, direction: str):
    cfg = store.load_config()
    if not cfg.get("enabled", True):
        return await interaction.response.send_message(
            embed=_e("🛠 受付停止中", cfg.get("maintenance_note") or "メンテナンス中。しばらく待って。",
                     "warn"), ephemeral=True)
    if not store.direction_cfg(cfg, direction).get("enabled", True):
        return await interaction.response.send_message(
            embed=_e("🛠 この方向は停止中", f"{DIR_LABEL[direction]} は今受け付けていない。", "warn"),
            ephemeral=True)
    g = aml.gate(interaction.user.id)
    if g["state"] == "blocked":
        return await interaction.response.send_message(embed=blocked_embed(), ephemeral=True)
    if g["state"] == "register":
        return await interaction.response.send_message(
            embed=reg_intro_embed(cfg, interaction.user.id, g["missing"]), view=RegStartView(),
            ephemeral=True)
    await interaction.response.send_modal(ExchangeModal(direction))


class ExchangeModal(discord.ui.Modal):
    def __init__(self, direction: str):
        super().__init__(title=f"{DIR_LABEL[direction]} 換金", timeout=600)
        self.direction = direction
        src = "paypay" if direction == "p2k" else "kyash"
        self.link = discord.ui.TextInput(
            label=f"{SVC_LABEL[src]}の送金リンク (登録した垢で作ったもの)",
            placeholder=("https://pay.paypay.ne.jp/..." if src == "paypay"
                         else "https://kyash.me/payments/..."),
            max_length=300, required=True)
        self.add_item(self.link)
        self.passcode = None
        if src == "paypay":
            self.passcode = discord.ui.TextInput(label="パスコード (付けた場合だけ)",
                                                 placeholder="4桁", max_length=8, required=False)
            self.add_item(self.passcode)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        link = str(self.link.value or "").strip()
        try:
            if self.direction == "p2k":
                pc = str(self.passcode.value or "").strip() if self.passcode else ""
                await process_p2k(interaction, link, pc or None)
            else:
                await process_k2p(interaction, link)
        except Exception as e:
            traceback.print_exc()
            await notify_admin(f"💥 換金処理で例外: {interaction.user.mention} "
                               f"`{type(e).__name__}: {e}`")
            await interaction.followup.send(
                embed=_e("❌ 内部エラー", f"`{type(e).__name__}`\n管理者に知らせた。", "error"),
                ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="PayPay → Kyash", style=discord.ButtonStyle.danger, emoji="💱",
                       custom_id="pk:ex:p2k", row=0)
    async def ex_p2k(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_exchange(interaction, "p2k")

    @discord.ui.button(label="Kyash → PayPay", style=discord.ButtonStyle.primary, emoji="💱",
                       custom_id="pk:ex:k2p", row=0)
    async def ex_k2p(self, interaction: discord.Interaction, button: discord.ui.Button):
        await open_exchange(interaction, "k2p")

    @discord.ui.button(label="本人確認", style=discord.ButtonStyle.secondary, emoji="🪪",
                       custom_id="pk:regmenu", row=1)
    async def reg_menu(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = store.load_config()
        g = aml.gate(interaction.user.id)
        if g["state"] == "blocked":
            return await interaction.response.send_message(embed=blocked_embed(), ephemeral=True)
        await interaction.response.send_message(
            embed=reg_intro_embed(cfg, interaction.user.id, g["missing"]), view=RegStartView(),
            ephemeral=True)

    @discord.ui.button(label="状況・履歴", style=discord.ButtonStyle.secondary, emoji="📜",
                       custom_id="pk:status", row=1)
    async def status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=user_status_embed(interaction.user),
                                                ephemeral=True)


def aml_deny_embed(service: str, av: dict, page_name: str = "") -> discord.Embed:
    reason = av.get("reason")
    b = av.get("binding") or {}
    if reason == "NO_IDENTITY":
        body = (f"{SVC_LABEL[service]}側から送信者を読み取れなかったので、照合できない。\n"
                "安全のため受け取らずに中止した。少し待ってからもう一度試して。")
    elif reason == "BLOCKED":
        body = "このアカウントは管理者によって利用停止されている。"
    else:
        body = (f"このリンクは、本人確認で登録した{SVC_LABEL[service]}アカウント"
                f" ({aml.display(b)}) から送られていない。\n"
                "**登録した垢以外からの送金は受け付けない**ので、換金を拒否した。")
        if service == "kyash" and page_name:
            body += (f"\n\n-# このリンクの送信者: @{page_name}\n"
                     "-# Kyash ID を変更しただけなら、🪪本人確認 から Kyash を登録し直すと更新される"
                     " (同じKyash垢なら付け替えにはならない)。")
    body += ("\n\n**受け取っていない**ので、リンクは"
             + ("PayPayアプリでキャンセルすれば" if service == "paypay" else "Kyashアプリで取り消せば")
             + "全額戻る。")
    return _e("🚫 換金を拒否した (受け取っていない)", body, "error")


async def _deny_log(user, direction: str, av: dict, amount: int, sender: str):
    owner = av.get("owner") or {}
    store.log_detection(user.id, str(user), "AML", "DENY", amount,
                        note=f"{DIR_LABEL[direction]}: {aml.reason_text(av.get('reason'))}",
                        sender=sender)
    await log_event("🚩 名義不一致で換金を拒否 (受け取っていない)",
                    f"{user.mention} の {DIR_LABEL[direction]} を拒否した", kind="detect",
                    color="error",
                    fields=[("理由", aml.reason_text(av.get("reason")), True),
                            ("金額", yen(amount), True),
                            ("登録", aml.display(av.get("binding")), False),
                            ("今回の送信者", sender or "(読めず)", False),
                            ("その垢の持ち主", f"<@{owner.get('user_id')}>" if owner else "-", True)])


async def _entry_checks(interaction: discord.Interaction, cfg: dict, direction: str) -> bool:
    """換金の入口 (受け付けるか / 登録済みか / 両サービスの準備ができているか)。False なら返信済み。"""
    user = interaction.user
    if not cfg.get("enabled", True) or not store.direction_cfg(cfg, direction).get("enabled", True):
        await interaction.followup.send(
            embed=_e("🛠 受付停止中", cfg.get("maintenance_note") or "しばらく待って。", "warn"),
            ephemeral=True)
        return False
    g = aml.gate(user.id)
    if g["state"] == "blocked":
        await interaction.followup.send(embed=blocked_embed(), ephemeral=True)
        return False
    if g["state"] == "register":
        await interaction.followup.send(embed=reg_intro_embed(cfg, user.id, g["missing"]),
                                        view=RegStartView(), ephemeral=True)
        return False
    if not kyash.ready():
        await notify_admin_throttled("kyash_not_ready",
                                     "🔑 **Kyashが未ログインで換金を受けられない** `/kyashログイン`")
        await interaction.followup.send(
            embed=_e("🛠 準備中", "BOT側のKyashの準備ができていない。管理者に知らせた。\n"
                                 "-# 受け取っていない。", "warn"), ephemeral=True)
        return False
    if not (paypay.account_info() or {}).get("access_token"):
        await notify_admin_throttled("paypay_not_ready",
                                     "🔑 **PayPayが未ログインで換金を受けられない** 管理者に連絡してください。")
        await interaction.followup.send(
            embed=_e("🛠 準備中", "BOT側のPayPayの準備ができていない。管理者に知らせた。\n"
                                 "-# 受け取っていない。", "warn"), ephemeral=True)
        return False
    return True


# ============================================================
# PayPay → Kyash
# ============================================================

async def process_p2k(interaction: discord.Interaction, link: str, passcode: str | None):
    store.load_orders()
    cfg = store.load_config()
    user = interaction.user
    if not await _entry_checks(interaction, cfg, "p2k"):
        return
    lock = _lock_for(user.id)
    if lock.locked():
        return await interaction.followup.send(
            embed=_e("⏳ 処理中", "前の換金がまだ処理中。終わるまで待って。", "warn"), ephemeral=True)
    async with lock:
        # --- 1) 受け取る前にリンクを下見する (ここで弾けば相手の金には触れない) ---
        chk = await asyncio.to_thread(paypay.inspect_link, link)
        seen = chk.get("info")
        if seen:
            prev = store.get_hold(store.paypay_link_key(seen))
            if prev and int(prev.get("user_id", 0)) == user.id:
                if prev.get("state") in store.HOLD_SETTLED:
                    return await interaction.followup.send(
                        embed=_e("✅ このリンクは処理済み",
                                 f"注文 `{prev.get('order_id_local') or '?'}` として受け付け済み。",
                                 "info"), ephemeral=True)
                if can_resume_hold(prev, user.id, seen):
                    return await resume_hold_from_paste(interaction, prev, seen)
        if not chk.get("ok"):
            return await interaction.followup.send(
                embed=_e("❌ リンクが使えない", chk.get("error") or "不明なエラー", "error"),
                ephemeral=True)
        info = chk["info"]
        amount = int(info.get("amount") or 0)
        if amount <= 0:
            return await interaction.followup.send(
                embed=_e("❌ リンクの金額を確認できない",
                         "安全のため受け取らずに中止した。リンクを作り直して。", "error"),
                ephemeral=True)
        if info.get("has_password") and not passcode:
            return await interaction.followup.send(
                embed=_e("🔒 パスコードが必要", "このリンクはパスコード付き。パスコード欄にも入れて。",
                         "warn"), ephemeral=True)
        err = range_error(cfg, "p2k", amount, user.id)
        if err:
            return await interaction.followup.send(
                embed=_e("❌ 受け付けられない金額",
                         err + "\n-# 受け取っていないので、リンクはそのまま使える。", "error"),
                ephemeral=True)

        # --- 2) 送信者の照合 (受け取る前。違えば受け取らずに拒否) ---
        ident = aml.paypay_identity(info)
        av = aml.check_paypay_sender(user.id, ident)
        print(f"[本人確認] {user} P→K {yen(amount)} → {av['action']} "
              f"({aml.reason_text(av.get('reason'))})")
        if av["action"] != "allow":
            if av["action"] == "register":
                return await interaction.followup.send(
                    embed=reg_intro_embed(cfg, user.id, ["paypay"]), view=RegStartView(),
                    ephemeral=True)
            await _deny_log(user, "p2k", av, amount, aml.display(ident))
            return await interaction.followup.send(embed=aml_deny_embed("paypay", av),
                                                   ephemeral=True)
        if av.get("renamed"):
            old, new = av["renamed"]
            store.update_binding(user.id, "paypay", event="RENAMED", note=f"{old} → {new}",
                                 name=new)

        # --- 3) 残高種別 (マネー / マネラ) ---
        raw_type, money_amt, light_amt = wallet_detect.detect(info)
        wtype = wallet_detect.effective_type(cfg, raw_type, money_amt, light_amt)
        policy = wallet_detect.policy_for(cfg, wtype)
        extra_fee = wallet_detect.extra_fee_percent(cfg, wtype, money_amt, light_amt)
        detect_line = wallet_detect.summary_line(raw_type, money_amt, light_amt)
        if policy["mode"] == "deny":
            store.log_detection(user.id, str(user), "WALLET", "DENY", amount,
                                note=f"{wallet_detect.LABELS[wtype]} を拒否",
                                money=money_amt, light=light_amt)
            return await interaction.followup.send(
                embed=_e("🚫 この送金は受け取れない",
                         f"検知結果: **{detect_line}**\n\n"
                         f"**{wallet_detect.LABELS[wtype]}** での送金は受け付けていない。\n"
                         "**受け取っていない**ので、PayPayアプリでキャンセルすれば戻る。\n"
                         "-# PayPayマネーで送り直して。", "error"), ephemeral=True)

        q = quote(cfg, "p2k", amount, extra_fee)
        if q["out"] < 1:
            return await interaction.followup.send(
                embed=_e("❌ 金額が小さすぎる", "手数料を引くと払い出しが0円になる。", "error"),
                ephemeral=True)

        # --- 4) リンクを予約 → 払える残高を確保 → 受取記録を作ってから受け取る ---
        lkey = store.paypay_link_key(info)
        if not lkey or not store.reserve_link(lkey, user.id, note=yen(amount)):
            prev = store.link_used(lkey) or {}
            return await interaction.followup.send(
                embed=_e("❌ このリンクは既に使われている",
                         f"{prev.get('at', '?')} に"
                         f"{'受取済み' if prev.get('state') == 'USED' else '処理中'}。\n"
                         "-# 二重換金を防ぐため受け取っていない。", "error"), ephemeral=True)
        async with _stock_lock:
            cap = await capacity(cfg, "kyash", q["out"])
            if not cap.get("ok"):
                store.release_link(lkey)
                await warn_capacity(cap, "kyash", amount, user.mention)
                return await interaction.followup.send(embed=capacity_embed("kyash"),
                                                       ephemeral=True)
            _reserved[lkey] = ("kyash", q["out"])
            rec = store.add_hold(
                lkey, state="RECEIVING", direction="p2k", user_id=user.id, user_name=str(user),
                code=info.get("code", ""), order_id=info.get("order_id", ""), amount=amount,
                passcode=passcode or "", wallet_type=wtype, money_amount=money_amt,
                light_amount=light_amt, extra_fee=extra_fee, detect_line=detect_line,
                sender=ident.get("name", ""), sender_id=ident.get("id", ""),
                wallet_hold=(f"残高種別: {wallet_detect.LABELS[wtype]}"
                             if policy["mode"] == "hold" else ""),
                last_status=info.get("order_status", ""), reserve_out=q["out"])

        try:
            res = await asyncio.to_thread(paypay.receive_link, link, passcode, None,
                                          **hold_options(cfg))
        except Exception as e:
            traceback.print_exc()
            res = {"status": "unknown", "reason": "EXCEPTION", "error": f"{type(e).__name__}: {e}"}

        st = res.get("status")
        if st in ("received", "already_received"):
            await settle_p2k(rec, int(res.get("amount") or 0),
                             res.get("order_id") or info.get("order_id", ""),
                             reply_to(interaction), via=st)
            return
        if st in ("held", "unknown"):
            return await start_watching(interaction, rec, res)

        # 受け取れなかった (相手の金には触れていない)
        release_reserve(lkey)
        store.release_link(lkey)
        store.finish_hold(lkey, "FAILED", result_code=res.get("result_code", ""),
                          note=f"{res.get('reason', '')} {res.get('error', '')}".strip()[:200])
        if cfg.get("log_receive_fail", True):
            await log_event("❌ PayPay受取失敗", f"{user.mention} のリンクを受け取れなかった",
                            kind="error", color="error",
                            fields=[("理由", res.get("reason") or "?", True),
                                    ("resultCode", f"`{res.get('result_code') or '-'}`", True),
                                    ("金額", yen(amount), True),
                                    ("メッセージ", res.get("error") or "?", False)])
        if res.get("reason") in ("RELOGIN", "NO_TOKEN"):
            await notify_admin_throttled("relogin", "🔑 **PayPayのセッションが切れていて受け取れない** "
                                                    "管理者が接続状態を確認してください。")
            msg = "受取用のPayPayアカウントの確認が必要な状態。管理者に知らせた。"
        else:
            msg = res.get("error") or "不明なエラー"
        return await interaction.followup.send(
            embed=_e("❌ 受取失敗", msg + "\n\n-# 受け取っていないので、リンクはそのまま使える"
                                         "（キャンセルすれば全額戻る）。", "error"), ephemeral=True)


async def settle_p2k(hold: dict, received: int, paypay_order_id: str, tell, *, via: str) -> str:
    """PayPayの着金が確定した後 (記録 → 再検証 → 注文 → Kyashへ払い出し)"""
    key = hold["key"]
    fresh = store.get_hold(key) or hold
    if fresh.get("state") in store.HOLD_SETTLED:
        return "ALREADY"
    cfg = store.load_config()
    uid = int(hold["user_id"])
    received = paypay.settled_amount(received, hold.get("amount")) or int(hold.get("amount") or 0)
    q = quote(cfg, "p2k", received, float(hold.get("extra_fee", 0) or 0))

    reasons = []
    if hold.get("wallet_hold"):
        reasons.append(hold["wallet_hold"])
    if not cfg.get("auto_send", True):
        reasons.append("自動払い出しOFF")
    dest = store.binding(uid, "kyash")
    if not dest:
        reasons.append("Kyashの登録が解除されている")
    if (store.get_account(uid) or {}).get("state") == "BLOCKED":
        reasons.append("利用停止中")
    if via not in ("received", "already_received"):
        # 保留の間に他の換金が進んでいても上限を超えないよう、その場で検査し直す
        existing = store.receipt_order(store.load_orders()["orders"], "p2k", key, paypay_order_id)
        counted = (existing and existing["status"] not in store.NOT_COUNTED
                   and existing["created_at"].startswith(store.today()))
        err = range_error(cfg, "p2k", received, uid, exclude_today=received if counted else 0)
        if err:
            reasons.append("上限: " + err.replace("\n", " / "))

    order, _ = store.get_or_create_order(
        "p2k", uid, hold.get("user_name", ""), received, q["out"], q["fee_jpy"], q["fee_percent"],
        status="HOLD" if reasons else "PAID", hold_reason="; ".join(reasons),
        hold_code="REVIEW" if reasons else "",
        paypay_in={"order_id": paypay_order_id, "sender": hold.get("sender", ""),
                   "sender_id": hold.get("sender_id", ""), "wallet_type": hold.get("wallet_type"),
                   "money": hold.get("money_amount", 0), "light": hold.get("light_amount", 0),
                   "detect": hold.get("detect_line", "")},
        dest=({"service": "kyash", "id": dest["id"], "user_name": dest.get("user_name", ""),
               "name": dest.get("name", "")} if dest else {}),
        via=via, receipt_key=key)
    store.confirm_link(key, order["id"], received)
    store.record_usage(uid, received, order["id"])
    store.finish_hold(key, "RECEIVED", order_id_local=order["id"], received=received)
    release_reserve(key)
    invalidate_balance("paypay")
    if order["status"] not in ("PAID", "HOLD"):
        return "ALREADY"
    print(f"[{jst_now()}] {order['id']} P→K 着金 {yen(received)} → 払い出し {yen(q['out'])} ({via})")

    if reasons or order["status"] == "HOLD":
        if order["status"] == "PAID":
            store.claim_order(order["id"], ("PAID",), "HOLD", hold_code="REVIEW",
                              hold_reason="; ".join(reasons))
        await tell(embed=_e("📥 受け取った (確認待ち)",
                            f"{yen(received)} を受け取った。注文 `{order['id']}`\n"
                            "管理者の確認が終わり次第、登録済みのKyashへ払い出す。", "warn"))
        await log_event("⏸ 払い出しを保留", f"<@{uid}> の {order['id']} を確認待ちにした",
                        kind="error", fields=[("理由", "; ".join(reasons), False),
                                              ("受取", yen(received), True),
                                              ("払い出し予定", yen(q["out"]), True),
                                              ("対応", f"`/再送 {order['id']}` で払い出す", False)])
        return "HOLD"
    await payout(order["id"], tell)
    return "PAID"


# ============================================================
# Kyash → PayPay
# ============================================================

async def process_k2p(interaction: discord.Interaction, link: str):
    store.load_orders()
    cfg = store.load_config()
    user = interaction.user
    if not await _entry_checks(interaction, cfg, "k2p"):
        return
    lock = _lock_for(user.id)
    if lock.locked():
        return await interaction.followup.send(
            embed=_e("⏳ 処理中", "前の換金がまだ処理中。終わるまで待って。", "warn"), ephemeral=True)
    async with lock:
        # --- 1) 公開ページを見るだけ (受け取らない) ---
        chk = await asyncio.to_thread(kyash.inspect_link, link)
        if not chk.get("ok"):
            return await interaction.followup.send(
                embed=_e("❌ リンクが使えない", chk.get("error") or "不明なエラー", "error"),
                ephemeral=True)
        amount = int(chk["amount"])
        err = range_error(cfg, "k2p", amount, user.id)
        if err:
            return await interaction.followup.send(
                embed=_e("❌ 受け付けられない金額",
                         err + "\n-# 受け取っていないので、リンクはそのまま使える。", "error"),
                ephemeral=True)

        # --- 2) 送信者の照合 (受け取る前・Kyash ID で。違えば受け取らずに拒否) ---
        page = aml.kyash_page_identity(chk)
        av = aml.check_kyash_page(user.id, page)
        print(f"[本人確認] {user} K→P {yen(amount)} → {av['action']} "
              f"({aml.reason_text(av.get('reason'))})")
        if av["action"] != "allow":
            if av["action"] == "register":
                return await interaction.followup.send(
                    embed=reg_intro_embed(cfg, user.id, ["kyash"]), view=RegStartView(),
                    ephemeral=True)
            if av.get("reason") == "NO_IDENTITY":
                await notify_admin_throttled(
                    "kyash_page_parse",
                    "⚠️ **Kyashリンクの公開ページから送信者IDを読めない** → Kyash→PayPay を全部断っている\n"
                    "-# Kyash側のページ構成が変わった可能性。kyash_api.parse_link_sender を確認して。")
            await _deny_log(user, "k2p", av, amount, aml.display(page))
            return await interaction.followup.send(
                embed=aml_deny_embed("kyash", av, page.get("user_name", "")), ephemeral=True)

        q = quote(cfg, "k2p", amount)
        if q["out"] < 1:
            return await interaction.followup.send(
                embed=_e("❌ 金額が小さすぎる", "手数料を引くと払い出しが0円になる。", "error"),
                ephemeral=True)

        # --- 3) リンクを予約 → 払える残高を確保 ---
        lkey = store.kyash_link_key(chk["uuid"])
        async with _stock_lock:
            if store.link_used(lkey):
                return await interaction.followup.send("このリンクは受付済みです。", ephemeral=True)
            cap = await capacity(cfg, "paypay", q["out"])
            if not cap.get("ok"):
                await warn_capacity(cap, "paypay", amount, user.mention)
                return await interaction.followup.send(embed=capacity_embed("paypay"), ephemeral=True)
            # RECEIVING 自体が与信の確保分。受取 API より前に永続化する。
            order, created = store.get_or_create_order(
                "k2p", user.id, str(user), amount, q["out"], q["fee_jpy"], q["fee_percent"],
                status="RECEIVING", receipt_key=lkey,
                kyash_in={"uuid": chk["uuid"], "claimed_at": time.time(),
                          "page_user_name": page.get("user_name", ""), "page_name": page.get("name", ""),
                          "tx": "", "sender": {}}, dest={})
            if not created:
                return await interaction.followup.send("このリンクは受付済みです。", ephemeral=True)
            if not store.reserve_link(lkey, user.id, note=yen(amount), order_id=order["id"]):
                store.claim_order(order["id"], ("RECEIVING",), "CANCELED", receipt_rejected=True,
                                  note="リンク予約が競合したため、受取前に中止")
                return await interaction.followup.send("このリンクは受付済みです。", ephemeral=True)
        try:
            res = await asyncio.to_thread(kyash.claim_and_identify, chk["uuid"], amount,
                                          store.used_kyash_tx())
        except Exception as e:
            traceback.print_exc()
            res = {"status": "unknown", "error": f"{type(e).__name__}: {e}",
                   "claimed_at": time.time()}
        st = res.get("status")
        if st == "failed":
            store.claim_order(order["id"], ("RECEIVING",), "CANCELED", receipt_rejected=True,
                              note="Kyash受取未成立")
            store.release_link(lkey)
            if res.get("reason") == "NO_TOKEN":
                await notify_admin_throttled("kyash_relogin",
                                             "🔑 **Kyashのセッションが切れている** `/kyashログイン`")
            if cfg.get("log_receive_fail", True):
                await log_event("❌ Kyash受取失敗", f"{user.mention} のリンクを受け取れなかった",
                                kind="error", color="error",
                                fields=[("理由", res.get("reason") or "?", True),
                                        ("金額", yen(amount), True),
                                        ("メッセージ", res.get("error") or "?", False)])
            return await interaction.followup.send(
                embed=_e("❌ 受取失敗", (res.get("error") or "不明なエラー")
                         + "\n\n-# 受け取っていないので、リンクはそのまま使える。", "error"),
                ephemeral=True)

        kin = dict(order["kyash_in"], claimed_at=res.get("claimed_at") or order["kyash_in"]["claimed_at"],
                   receipt=st)
        review = st not in ("received", "unidentified")
        order = store.claim_order(order["id"], ("RECEIVING",), "HOLD", kyash_in=kin,
                                  hold_code="RECEIPT_REVIEW" if review else "IDENTIFYING",
                                  hold_reason="着金の確認が必要" if review else "送信者の確認中")
        if not order:
            return
        store.confirm_link(lkey, order["id"], amount)
        invalidate_balance("kyash")
        if review:
            await notify_admin(f"{order['id']} のKyash着金は未確定です。入金明細を確認してください。")
            return await interaction.followup.send(f"注文 {order['id']} は着金確認待ちです。", ephemeral=True)
        store.record_usage(user.id, amount, order["id"])
        tell = reply_to(interaction)
        if st == "received":
            await apply_k2p_sender(order["id"], res["sender"], tell)
            return
        why = "明細への反映待ち"
        store.update_order(order["id"], hold_code="UNIDENTIFIED",
                           hold_reason=f"送信者を特定できない ({why})")
        await notify_admin(f"🕵️ **{order['id']}** Kyash受取の送信者を即時に特定できなかった ({why}) "
                           f"{user.mention} {yen(amount)} → 数分おきに明細を見直して自動で決着させる")
        await tell(embed=_e("⏳ 受け取った。送信者を確認中",
                            f"注文 `{order['id']}` ({yen(amount)})\n"
                            "Kyashの明細への反映が遅れていて、送信者の最終確認がまだ終わっていない。\n"
                            "**確認でき次第、自動で払い出して結果をDMで知らせる。** このまま待って。",
                            "warn"))


async def apply_k2p_sender(oid: str, sender: dict, tell) -> str:
    """Kyash→PayPay の受取後、明細の送信者 (publicId) で裏を取って決着させる"""
    o = store.get_order(oid)
    if not o or o.get("status") != "HOLD" or o.get("hold_code") not in ("IDENTIFYING",
                                                                         "UNIDENTIFIED"):
        return "SKIP"
    cfg = store.load_config()
    uid = int(o["user_id"])
    ident = aml.kyash_identity(sender)
    kin = dict(o.get("kyash_in") or {})
    kin.update({"tx": sender.get("tx", ""), "breakdown": sender.get("breakdown") or {},
                "sender": {"public_id": sender.get("public_id"),
                           "user_name": sender.get("user_name", ""),
                           "name_full": sender.get("name_full", "")}})
    store.update_order(oid, kyash_in=kin)

    pv = aml.check_kyash_received(uid, ident)
    if pv["action"] == "allow":
        if pv.get("renamed"):
            old, new = pv["renamed"]
            store.update_binding(uid, "kyash", event="RENAMED", note=f"{old} → {new}",
                                 user_name=ident.get("user_name") or new,
                                 name=ident.get("name") or "")
        dest = store.binding(uid, "paypay")
        reasons = []
        if not cfg.get("auto_send", True):
            reasons.append("自動払い出しOFF")
        if not dest:
            reasons.append("PayPayの登録が解除されている")
        store.update_order(oid, dest=({"service": "paypay", "id": dest["id"],
                                       "name": dest.get("name", "")} if dest else {}))
        if reasons:
            store.update_order(oid, hold_code="REVIEW", hold_reason="; ".join(reasons))
            await tell(embed=_e("📥 受け取った (確認待ち)",
                                f"注文 `{oid}` ({yen(o['in_amount'])})\n"
                                "管理者の確認が終わり次第、PayPayで払い出す。", "warn"))
            await log_event("⏸ 払い出しを保留", f"<@{uid}> の {oid} を確認待ちにした",
                            fields=[("理由", "; ".join(reasons), False),
                                    ("対応", f"`/再送 {oid}` で払い出す", False)])
            return "HOLD"
        await payout(oid, tell)
        return "PAID"

    if pv["action"] == "return":
        # 受取前の Kyash ID は一致したのに、明細の publicId が登録と違った
        store.update_order(oid, hold_code="MISMATCH",
                           hold_reason=f"送信者が登録と違う ({aml.reason_text(pv.get('reason'))})")
        store.log_detection(uid, o.get("user_name", ""), "AML", "RETURN", o["in_amount"],
                            note=f"K→P 受取後の照合: {aml.reason_text(pv.get('reason'))}",
                            sender=aml.display(ident))
        await log_event("🚩 受取後の照合で名義不一致 → 送信者へ返金",
                        f"<@{uid}> の {oid}: 受取前のKyash IDは一致したが、publicId が登録と違った",
                        kind="detect", color="error",
                        fields=[("登録", aml.display(pv.get("binding")), False),
                                ("実際の送信者", aml.display(ident), False),
                                ("金額", yen(o["in_amount"]), True)])
        r = await return_to_sender(oid)
        await tell(embed=_e("🚫 換金を拒否した",
                            "送られてきたKyashアカウントが、本人確認で登録したものと一致しなかった。\n"
                            + ("受け取った全額を**送信者へそのまま返した**。" if r == "RETURNED"
                               else "受け取った額は管理者が確認して送信者へ返す。"), "error"))
        return r

    store.update_order(oid, hold_code="UNIDENTIFIED", hold_reason="送信者を特定できない")
    return "HOLD"


async def return_to_sender(oid: str, by: int | None = None) -> str:
    """Kyash→PayPay で受け取った額を、送ってきたKyash垢へそのまま返す (手数料は取らない)"""
    o = store.get_order(oid)
    if not o or o.get("direction") != "k2p":
        return "NOT_FOUND"
    sender = (o.get("kyash_in") or {}).get("sender") or {}
    if not sender.get("public_id"):
        return "NO_SENDER"
    request_id = o.get("return_request_id") or str(uuid4())
    o = store.claim_order(oid, ("HOLD", "FAILED"), "RETURNING", return_request_id=request_id,
                          note=f"送信者へ返金{f' by {by}' if by else ''}",
                          pending_action="return", sending_ts=time.time())
    if not o:
        return "BUSY"
    r = await asyncio.to_thread(kyash.send, o["in_amount"], sender["public_id"],
                                sender.get("user_name", ""), request_id=request_id)
    invalidate_balance("kyash")
    st = r.get("status")
    if st == "sent":
        store.claim_order(oid, ("RETURNING",), "RETURNED", note="送信者へ返金済",
                          payout={"method": "return", "group": r.get("group"), "tx": r.get("tx"),
                                  "to": sender, "at": jst_now()})
        await log_event("↩️ 送信者へ返金した",
                        f"{oid} {yen(o['in_amount'])} → @{sender.get('user_name')}",
                        kind="detect", color="ok")
        return "RETURNED"
    if st == "unknown":
        store.claim_order(oid, ("RETURNING",), "UNCERTAIN", note="返金の成否不明",
                          error=r.get("error", ""))
        await notify_admin(f"❔ **{oid}** 送信者への返金の成否が分からない → `/送金確認 {oid}`\n"
                           f"{r.get('error')}")
        return "UNCERTAIN"
    store.claim_order(oid, ("RETURNING",), "HOLD", note="返金失敗", error=r.get("error", ""),
                      hold_code="RETURN_FAILED", hold_reason=f"返金失敗: {r.get('error')}")
    await notify_admin(f"⚠️ **{oid}** 送信者への返金に失敗: {r.get('error')}\n"
                       f"残高を確認して `/送信者へ返金 {oid}` でやり直して。")
    return "RETURN_FAILED"


# ============================================================
# 払い出し (両方向共通)
# ============================================================

async def payout(oid: str, tell=None, by: int | None = None) -> dict:
    """PAID / HOLD / FAILED の注文を払い出す。二重に走らないよう CAS で SENDING を奪う。"""
    cfg = store.load_config()
    o = store.get_order(oid)
    if not o:
        return {"status": "not_found"}
    if o.get("hold_code") in ("RECEIPT_REVIEW", "RETURN_FAILED"):
        return {"status": "review", "error": "着金または返金結果の確認が必要です"}
    if o.get("hold_code") == "MISMATCH":
        # 名義が違った入金は払い出さない (送信者へ返すのが筋)
        return {"status": "mismatch", "error": "送信者が登録と違う注文。/送信者へ返金 を使って"}
    dest = o.get("dest") or {}
    svc = "kyash" if o["direction"] == "p2k" else "paypay"
    if not dest.get("id"):
        b = store.binding(o["user_id"], svc)
        if not b:
            return {"status": "no_dest", "error": f"{SVC_LABEL[svc]}の登録が無い"}
        dest = {"service": svc, "id": b["id"], "user_name": b.get("user_name", ""),
                "name": b.get("name", "")}
        store.update_order(oid, dest=dest)
    method = o.get("payout_method") or cfg.get(f"{svc}_payout", "transfer" if svc == "kyash" else "link")
    request_id = o.get("payout_request_id") or str(uuid4())
    o = store.claim_order(oid, ("PAID", "HOLD", "FAILED"), "SENDING",
                          note=f"払い出し{f' by {by}' if by else ''}", pending_action="payout",
                          sending_ts=time.time(), hold_code="", hold_reason="", payout_method=method,
                          payout_request_id=request_id)
    if not o:
        return {"status": "busy"}
    amount = int(o["out_amount"])
    if o["direction"] == "p2k":
        if method == "link":
            r = await asyncio.to_thread(kyash.send_link, amount, request_id=request_id)
        else:
            r = await asyncio.to_thread(kyash.send, amount, dest["id"], dest.get("user_name", ""),
                                        request_id=request_id)
        invalidate_balance("kyash")
    else:
        if method == "direct":
            r = await asyncio.to_thread(paypay.payout_direct, amount, dest["id"])
        else:
            r = await asyncio.to_thread(paypay.payout_link, amount)
        invalidate_balance("paypay")

    st = r.get("status")
    tell = tell or reply_dm(o["user_id"])
    if st == "sent":
        pay = {"method": method, "at": jst_now()}
        for k in ("group", "tx", "order_id", "link_id"):
            if r.get(k):
                pay[k] = r[k]
        link = r.get("url") or r.get("link") or ""
        if link:
            pay["link"] = link
        o = store.claim_order(oid, ("SENDING",), "SENT", note="払い出し完了", payout=pay,
                              error="") or o
        dst_label = SVC_LABEL[svc]
        e = _e("✅ 換金完了", f"注文 `{oid}` ({DIR_LABEL[o['direction']]})", "ok")
        e.add_field(name="受取", value=yen(o["in_amount"]), inline=True)
        e.add_field(name="手数料", value=f"{yen(o['fee_jpy'])} ({o['fee_percent']}%)", inline=True)
        e.add_field(name="払い出し", value=f"**{yen(amount)}**", inline=True)
        if link:
            e.add_field(name=f"{dst_label}の送金リンク (開いて受け取って)",
                        value=f"{link}\n-# このリンクは他人に見せない。", inline=False)
            await _dm(o["user_id"], embed=e)     # その場の表示が消えても受け取れるよう控えを送る
        else:
            e.add_field(name="送り先", value=f"{dst_label}: {aml.display(dest)}", inline=False)
        await tell(embed=e)
        await log_trade(o)
        print(f"[{jst_now()}] {oid} 払い出し完了 {yen(amount)} ({method})")
        return {"status": "sent", "order": o}
    if st == "unknown":
        store.claim_order(oid, ("SENDING",), "UNCERTAIN", note="払い出しの成否不明",
                          error=r.get("error", ""))
        await tell(embed=_e("⏳ 払い出しを確認中",
                            f"注文 `{oid}` の払い出し結果をすぐに確定できなかった。\n"
                            "二重払いを避けるため管理者が確認してから決着させる。少し待って。", "warn"))
        await notify_admin(f"❔ **{oid}** 払い出しの成否が分からない ({method} {yen(amount)})\n"
                           f"{r.get('error')}\n→ 明細を確認して `/送金確認 {oid}`")
        return {"status": "unknown"}
    store.claim_order(oid, ("SENDING",), "FAILED", note="払い出し失敗", error=r.get("error", ""))
    await tell(embed=_e("⚠️ 払い出しに失敗",
                        f"注文 `{oid}` の払い出しに失敗した。受け取った分は記録されているので、"
                        "管理者が対応する。", "error"))
    await log_event("❌ 払い出し失敗", f"<@{o['user_id']}> の {oid}", kind="error", color="error",
                    fields=[("方式", PAYOUT_LABEL.get(method, method), True),
                            ("額", yen(amount), True), ("理由", r.get("error") or "?", False),
                            ("対応", f"`/再送 {oid}`", False)])
    if r.get("reason") in ("RELOGIN", "NO_TOKEN"):
        await notify_admin_throttled("relogin", "🔑 **PayPayのセッションが切れている** 管理者に連絡してください。")
    return {"status": "failed", "error": r.get("error")}


# ============================================================
# Kyash 送信者の後追い特定 (受け取った直後に明細へ出なかった分)
# ============================================================

@tasks.loop(minutes=2)
async def reidentify_kyash():
    try:
        await _reidentify_once()
    except Exception as e:
        traceback.print_exc()
        print(f"[reidentify] {type(e).__name__}: {e}")


async def _reidentify_once():
    if not kyash.ready():
        return
    now = time.time()
    for o in store.recent_orders(300, status="HOLD"):
        if o.get("direction") != "k2p" or o.get("hold_code") not in ("UNIDENTIFIED", "IDENTIFYING"):
            continue
        kin = o.get("kyash_in") or {}
        claimed = float(kin.get("claimed_at") or o.get("created_ts") or now)
        if now - claimed < 60:
            continue
        if now - claimed > 6 * 3600:
            store.update_order(o["id"], hold_code="GAVE_UP",
                               hold_reason="6時間たっても送信者を特定できない (要手動確認)")
            await notify_admin(f"🖐 **{o['id']}** Kyash受取の送信者を特定できないまま6時間経過。\n"
                               "Kyashアプリで入金と送信者を確認して、本人なら `/再送`、"
                               "違えば手動で返して `/強制状態変更 状態:RETURNED`。")
            continue
        r = await asyncio.to_thread(kyash.identify_later, o["in_amount"], claimed,
                                    store.used_kyash_tx())
        if r.get("status") == "received":
            res = await apply_k2p_sender(o["id"], r["sender"], reply_dm(o["user_id"]))
            print(f"[{jst_now()}] {o['id']} 後追いで送信者を特定 → {res}")
        await asyncio.sleep(1)

    # 本人確認 (Kyash) の後追い
    for key, rec in list((store.load_used().get("links") or {}).items()):
        uid = rec.get("reg_pending")
        if not uid:
            continue
        claimed = float(rec.get("claimed_at") or now)
        if now - claimed < 60:
            continue
        amount = int(rec.get("amount") or 0)
        if now - claimed > 6 * 3600:
            store.confirm_link(key, rec.get("ref", ""), amount, reg_pending=None,
                               note="送信者を特定できず登録を打ち切り")
            await _dm(int(uid), embed=_e("❌ Kyashの本人確認を完了できなかった",
                                         "送信者を確認できなかった。もう一度最初から登録して。",
                                         "error"))
            continue
        r = await asyncio.to_thread(kyash.identify_later, amount, claimed, store.used_kyash_tx())
        if r.get("status") == "received":
            await finish_kyash_registration(int(uid), rec.get("reg_user_name", ""), key,
                                            r["sender"], amount,
                                            {"user_name": rec.get("page_user_name", "")},
                                            reply_dm(int(uid)))
        await asyncio.sleep(1)


@reidentify_kyash.before_loop
async def _before_reid():
    await bot.wait_until_ready()


# ============================================================
# PayPay の一時保留 (PayPay → Kyash の受取で起きる)
#
# 受取APIが 42007013 を返したら保留。送信者がPayPayアプリで承認すると、BOTが
# 何もしなくても自動で着金し、リンクは受取済みになる。通知は来ないので見に行く。
# ★リンクが受取済みになっても、それだけでは着金とみなさない (他で受け取られても
#   受取済みになる)。BOT自身の入金履歴に同じ orderId の入金があるかで裏を取る。
# ============================================================

HOLD_BACKOFF = ((5 * 60, 30), (15 * 60, 60), (60 * 60, 180), (6 * 3600, 600))
HOLD_BACKOFF_MAX = 3600
HOLD_ERROR_INTERVAL = 300
HOLD_VERIFY_INTERVAL = 120
HOLD_REACCEPT_INTERVAL = 600
HOLD_UNKNOWN_RETRY = 60
HOLD_BUTTON_COOLDOWN = 20
_hold_btn_next: dict[int, float] = {}

HOLD_RESULT_LABEL = {
    "RECEIVED": "✅ 受け取れた", "AUTO_RECEIVED": "✅ 承認されて着金していた",
    "ALREADY": "✅ 処理済み", "STILL_HELD": "⏳ まだ承認待ち", "VERIFYING": "⏳ 入金履歴への反映待ち",
    "RETRYING": "⏳ 確認中", "ERROR": "⚠️ 確認に失敗 (自動で再確認する)",
    "NO_FUNDS": "🈳 払い出し残高の補充待ち", "CLOSED": "❌ リンクが失効した",
    "EXPIRED": "⌛ 自動確認の期限切れ", "TAKEN": "🚫 他で受け取られた可能性",
    "MANUAL": "🖐 管理者の確認待ち", "REJECTED": "⏳ 再確認する", "GONE": "❔ 記録が無い",
}
HOLD_DONE_RESULTS = ("RECEIVED", "AUTO_RECEIVED", "ALREADY")
HOLD_STATE_LABEL = {
    "RECEIVING": "⏳ 受取処理中", "WATCHING": "⏳ 監視中", "MANUAL": "🖐 要手動確認",
    "TAKEN": "🚫 他で受取の疑い", "RECEIVED": "✅ 着金", "DONE": "✅ 完了", "FAILED": "❌ 失敗",
    "CLOSED": "❌ 失効", "EXPIRED": "⌛ 期限切れ",
}


def hold_age_sec(hold: dict) -> float:
    return max(0.0, time.time() - float(hold.get("created_ts") or time.time()))


def hold_interval(age_sec: float) -> int:
    for limit, interval in HOLD_BACKOFF:
        if age_sec < limit:
            return interval
    return HOLD_BACKOFF_MAX


def hold_embed(amount: int) -> discord.Embed:
    return _e("⏳ 受け取りが一時保留になった",
              f"{yen(amount)} のリンクは、PayPayの詐欺防止機能で**受け取りが一時保留**になっている。\n\n"
              "**送信者（あなた）のPayPayアプリに確認画面が出ているはず。**\n"
              "そこで取引を承認すると、そのままBOTに着金する。\n\n"
              "✅ 承認したら下のボタンを押して。すぐに確認して換金を続ける。\n"
              "🔁 ボタンが使えなくなっても、**同じリンクをもう一度貼れば**続きから確認できる。\n"
              "⚠️ リンクを作り直す必要はない（作り直して貼ると、両方が別々に換金される）。\n"
              "-# 押さなくても自動で確認していて、結果はDMで届く。", "warn")


async def start_watching(interaction: discord.Interaction, rec: dict, res: dict):
    held = res.get("status") == "held"
    now = time.time()
    amount = int(rec.get("amount", 0) or 0)
    rc = res.get("result_code") or ""
    store.update_hold(rec["key"], state="WATCHING", result_code=rc,
                      note=(f"{rc} {res.get('result_message') or ''}".strip() if held
                            else f"結果不明: {res.get('error') or res.get('reason') or ''}")[:200],
                      next_check_at=now + (HOLD_BACKOFF[0][1] if held else 15),
                      next_accept_at=now + (HOLD_REACCEPT_INTERVAL if held else HOLD_UNKNOWN_RETRY))
    if store.load_config().get("log_receive_fail", True):
        await log_event("⏳ PayPay受取が一時保留 → 監視開始" if held
                        else "❔ PayPay受取結果が不明 → 監視開始",
                        f"<@{rec['user_id']}> {yen(amount)}", kind="error",
                        fields=[("resultCode", f"`{rc or '-'}`", True),
                                ("詳細", res.get("error") or "-", False)])
    if held:
        return await interaction.followup.send(embed=hold_embed(amount), view=HoldApprovedView(),
                                               ephemeral=True)
    return await interaction.followup.send(
        embed=_e("⏳ 受取結果を確認中",
                 f"{yen(amount)} のリンクを受け取ろうとしたが、PayPayの応答が途切れて結果を確定できなかった。\n"
                 "リンクの状態とBOTの入金履歴で自動で確認を続ける。\n\n"
                 "**着金していれば換金まで進めて、結果をDMで知らせる。**\n"
                 "-# リンクを作り直したりキャンセルしたりせず、そのまま待って。", "warn"),
        ephemeral=True)


def can_resume_hold(rec: dict, user_id: int, info: dict) -> bool:
    if int(rec.get("user_id", 0) or 0) != int(user_id):
        return False
    state = rec.get("state")
    if state in store.HOLD_SETTLED or state == "RECEIVING":
        return False
    if state == "WATCHING":
        return True
    return paypay.classify_status(info.get("order_status")) == "success"


async def resume_hold_from_paste(interaction: discord.Interaction, rec: dict, info: dict):
    key = rec["key"]
    uid = int(rec["user_id"])
    wait = _hold_btn_next.get(uid, 0.0) - time.time()
    if wait > 0:
        return await interaction.followup.send(
            f"⏳ 確認したばかり。あと{wait:.0f}秒待ってからもう一度貼って。", ephemeral=True)
    _hold_btn_next[uid] = time.time() + HOLD_BUTTON_COOLDOWN
    fields = {"code": info.get("code") or rec.get("code", "")}
    if rec.get("state") != "WATCHING":
        fields.update(state="WATCHING", next_accept_at=0, completed_seen_at=0,
                      note=f"貼り直しで再開 (前の状態: {rec.get('state')})")
        store.reserve_link(key, uid, note="保留の再開")
    store.update_hold(key, **fields)
    r = await guarded_check_hold(store.get_hold(key) or rec, force=True, reply=reply_to(interaction))
    await interaction.followup.send(
        embed=_e("🔄 保留中のリンクを確認した",
                 f"{yen(rec.get('amount', 0))} → {HOLD_RESULT_LABEL.get(r, r)}",
                 "ok" if r in HOLD_DONE_RESULTS else "warn"), ephemeral=True)


async def _hold_manual(hold: dict, why: str, tell) -> str:
    amount = int(hold.get("amount", 0) or 0)
    release_reserve(hold["key"])
    store.update_hold(hold["key"], state="MANUAL", note=why[:200])
    await tell(content=f"🖐 {yen(amount)} の入金を自動で確認できないので、管理者が確認してから処理する。")
    await notify_admin(f"🖐 **PayPay受取を自動で照合できない** ({why})\n"
                       f"<@{hold['user_id']}> {yen(amount)} / 送信者 {hold.get('sender') or '?'}\n"
                       "PayPayアプリで入金を確認して、入っていれば "
                       "`/保留確認 対象:<リンク> 操作:手動で着金確定`、無ければ `操作:打ち切り`。")
    return "MANUAL"


async def _hold_verify_completed(hold: dict, info: dict, cfg: dict, tell) -> str:
    key = hold["key"]
    amount = int(hold.get("amount", 0) or 0)
    order_id = str(info.get("order_id") or hold.get("order_id") or "")
    state, item = await asyncio.to_thread(paypay.find_incoming, order_id,
                                          hold_options(cfg)["history_pages"])
    now = time.time()
    if state == "found":
        received = paypay.settled_amount(amount or info.get("amount"), paypay.history_amount(item))
        await settle_p2k(hold, received, order_id, tell, via="hold_history")
        return "AUTO_RECEIVED"
    if state == "no_id":
        return await _hold_manual(hold, "リンクに取引番号が無く、入金履歴と照合できない", tell)
    if state == "error":
        store.update_hold(key, note="入金履歴を読めない (再確認する)",
                          next_check_at=now + HOLD_ERROR_INTERVAL)
        return "ERROR"
    first = float(hold.get("completed_seen_at") or 0) or now
    try:
        grace = max(1.0, float(cfg.get("hold_taken_grace_min", 30) or 30)) * 60
    except (TypeError, ValueError):
        grace = 1800.0
    if now - first < grace:
        store.update_hold(key, completed_seen_at=first, next_check_at=now + HOLD_VERIFY_INTERVAL,
                          note=("入金履歴に載っているが未確定" if state == "pending"
                                else "受取済みだが入金履歴にまだ無い (反映待ち)"))
        return "VERIFYING"
    if state == "pending":
        return await _hold_manual(hold, "入金履歴に載っているが、確定しないまま時間が経った", tell)
    release_reserve(key)
    store.finish_hold(key, "TAKEN", completed_seen_at=first, note="受取済みだがBOTの入金履歴に無い")
    await tell(content=(f"⚠️ {yen(amount)} のリンクは受取済みになっているが、BOTへの入金を確認できない。\n"
                        "別の場所で受け取られた可能性がある。**換金は行っていない。**\n"
                        "心当たりが無い場合は管理者に連絡して。"))
    await log_event("🚫 保留リンクが他で受け取られた可能性",
                    f"<@{hold['user_id']}> のリンクは受取済みだが、BOTの入金履歴に同じ取引が無い",
                    kind="error", color="error",
                    fields=[("金額", yen(amount), True),
                            ("対応", "PayPayアプリの履歴を確認し、入金されていれば "
                                     "`/保留確認 対象:<リンク> 操作:手動で着金確定`", False)])
    return "TAKEN"


async def check_one_hold(hold: dict, force: bool = False, reply=None) -> str:
    store.load_orders()
    cfg = store.load_config()
    key = hold["key"]
    uid = int(hold["user_id"])
    code = str(hold.get("code") or "")
    amount = int(hold.get("amount", 0) or 0)
    tell = reply or reply_dm(uid)
    now = time.time()
    store.update_hold(key, checks=int(hold.get("checks", 0)) + 1,
                      next_check_at=now + hold_interval(hold_age_sec(hold)))
    if not code or "…(" in code:
        return await _hold_manual(hold, "リンクコードが残っていない", tell)
    try:
        chk = await asyncio.to_thread(paypay.inspect_link, code)
    except Exception as e:
        store.update_hold(key, note=f"リンク確認失敗: {type(e).__name__}",
                          next_check_at=now + HOLD_ERROR_INTERVAL)
        return "ERROR"
    info = chk.get("info")
    if not info:
        store.update_hold(key, note=f"リンク確認失敗: {chk.get('error', '?')}"[:200],
                          next_check_at=now + HOLD_ERROR_INTERVAL)
        return "ERROR"
    status = str(info.get("order_status") or "")
    store.update_hold(key, last_status=status)
    kind = paypay.classify_status(status)
    if kind == "success":
        return await _hold_verify_completed(hold, info, cfg, tell)
    if kind == "fail":
        store.release_link(key)
        release_reserve(key)
        store.finish_hold(key, "CLOSED", note=f"リンクが {paypay.status_text(status)}")
        await tell(content=f"❌ {yen(amount)} のリンクは {paypay.status_text(status)} になった。"
                           "換金は行われていない。")
        return "CLOSED"
    try:
        limit_h = float(cfg.get("hold_watch_hours", 24) or 24)
    except (TypeError, ValueError):
        limit_h = 24.0
    if hold_age_sec(hold) / 3600 > limit_h and not force:
        store.release_link(key)
        release_reserve(key)
        store.finish_hold(key, "EXPIRED", note=f"{limit_h:g}時間経過")
        await tell(content=(f"⌛ {yen(amount)} のリンクは保留のまま {limit_h:g}時間経ったので、"
                            "自動の確認を終えた。換金は行われていない。\n"
                            "PayPayアプリで承認した後なら、**同じリンクをもう一度貼れば**続きから処理できる。"
                            "承認しないならキャンセルすれば全額戻る。"))
        return "EXPIRED"
    if not force and now < float(hold.get("next_accept_at") or 0):
        return "STILL_HELD"
    if key not in _reserved and cfg.get("check_balance_before_receive", True):
        out = int(hold.get("reserve_out") or quote(cfg, "p2k", amount,
                                                    float(hold.get("extra_fee", 0) or 0))["out"])
        async with _stock_lock:
            cap = await capacity(cfg, "kyash", out, exclude_key=key)
            if not cap.get("ok"):
                store.update_hold(key, note=f"払い出し残高不足で受取保留 ({cap.get('why')})")
                await warn_capacity(cap, "kyash", amount, f"<@{uid}> (保留分)")
                return "NO_FUNDS"
            _reserved[key] = ("kyash", out)
    res = await asyncio.to_thread(paypay.receive_link, code, hold.get("passcode") or None, info,
                                  **hold_options(cfg))
    st = res.get("status")
    if st in ("received", "already_received"):
        await settle_p2k(hold, int(res.get("amount") or 0),
                         res.get("order_id") or hold.get("order_id", ""), tell,
                         via="hold_accept" if st == "received" else "hold_history")
        return "RECEIVED" if st == "received" else "AUTO_RECEIVED"
    if st == "held":
        store.update_hold(key, next_accept_at=time.time() + HOLD_REACCEPT_INTERVAL,
                          result_code=res.get("result_code", ""), rejects=0)
        return "STILL_HELD"
    if st == "unknown":
        store.update_hold(key, next_accept_at=time.time() + HOLD_UNKNOWN_RETRY,
                          next_check_at=time.time() + HOLD_BACKOFF[0][1],
                          note=f"結果不明: {res.get('error', '')}"[:200])
        return "RETRYING"
    reason = res.get("reason", "")
    if reason in ("RELOGIN", "NO_TOKEN"):
        store.update_hold(key, note=str(res.get("error", ""))[:200],
                          next_check_at=time.time() + HOLD_ERROR_INTERVAL)
        await notify_admin_throttled("relogin", "🔑 **PayPayのセッションが切れていて、保留中のリンクを"
                                                "確認できない** 管理者に連絡してください。")
        return "ERROR"
    if reason == "NEED_PASSCODE":
        return await _hold_manual(hold, "パスコードが残っておらず受け取り直せない", tell)
    if reason == "NOT_PENDING":
        return "STILL_HELD"
    rejects = int(hold.get("rejects", 0) or 0) + 1
    store.update_hold(key, rejects=rejects, result_code=res.get("result_code", ""),
                      note=str(res.get("error", ""))[:200],
                      next_accept_at=time.time() + HOLD_REACCEPT_INTERVAL * 2 ** min(rejects, 4))
    return "REJECTED"


_hold_locks: dict[str, asyncio.Lock] = {}


async def guarded_check_hold(hold: dict, force: bool = False, reply=None) -> str:
    """同じ保留を触る経路 (自動ループ / 管理者 / 本人のボタン / 貼り直し) を直列化する"""
    key = hold.get("key", "")
    if not key:
        return "ERROR"
    async with _hold_locks.setdefault(key, asyncio.Lock()):
        fresh = store.get_hold(key)
        if not fresh:
            return "GONE"
        if fresh.get("state") in store.HOLD_SETTLED:
            return "ALREADY"
        if fresh.get("state") != "WATCHING":
            return fresh.get("state") or "GONE"
        return await check_one_hold(fresh, force=force, reply=reply)


class HoldApprovedView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="PayPayで承認した → 今すぐ確認", style=discord.ButtonStyle.success,
                       emoji="🔄", custom_id="pk:hold_check")
    async def check_now(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = interaction.user.id
        now = time.time()
        wait = _hold_btn_next.get(uid, 0.0) - now
        if wait > 0:
            return await interaction.response.send_message(
                f"⏳ 連打しないで。あと{wait:.0f}秒待ってから押して。", ephemeral=True)
        _hold_btn_next[uid] = now + HOLD_BUTTON_COOLDOWN
        await interaction.response.defer(ephemeral=True, thinking=True)
        mine = [h for h in store.holds_in("WATCHING") if int(h.get("user_id", 0)) == uid]
        if not mine:
            return await interaction.followup.send(
                "監視中の保留が無い。既に処理が終わっているか、自動の確認期間が過ぎている。\n"
                "-# 承認した後なら、**同じリンクをもう一度貼れば**続きから処理できる。",
                ephemeral=True)
        lines = []
        for h in mine[:5]:
            try:
                r = await guarded_check_hold(h, force=True, reply=reply_to(interaction))
            except Exception:
                traceback.print_exc()
                r = "ERROR"
            lines.append(f"{yen(h.get('amount', 0))} → {HOLD_RESULT_LABEL.get(r, r)}")
        done = any(ln.split(" → ", 1)[-1].startswith("✅") for ln in lines)
        await interaction.followup.send(
            embed=_e("🔄 確認した" if done else "🔄 確認したが、まだ完了していない", "\n".join(lines),
                     "ok" if done else "warn"), ephemeral=True)


@tasks.loop(seconds=30)
async def watch_holds():
    now = time.time()
    try:
        holds = [h for h in store.holds_in("WATCHING") if float(h.get("next_check_at") or 0) <= now]
    except Exception as e:
        print(f"[watch_holds] 読込失敗: {e}")
        return
    for h in holds:
        try:
            result = await guarded_check_hold(h)
            if result not in ("STILL_HELD", "VERIFYING", "ALREADY", "GONE"):
                print(f"[{jst_now()}] 保留 {h['key'][:18]}… → {result}")
        except Exception as e:
            traceback.print_exc()
            print(f"[watch_holds] {h.get('key')}: {type(e).__name__}: {e}")
        await asyncio.sleep(1)


@watch_holds.before_loop
async def _before_watch():
    await bot.wait_until_ready()


async def recover_receiving_holds():
    """受取APIを投げている最中に落ちた記録を監視に回し、監視中の分の残高確保を戻す"""
    cfg = store.load_config()
    for h in store.holds_in("RECEIVING"):
        store.update_hold(h["key"], state="WATCHING", next_check_at=time.time() + 10,
                          next_accept_at=time.time() + HOLD_UNKNOWN_RETRY,
                          note="BOT再起動時に受取処理中のまま残っていた")
        print(f"[起動] 受取途中の記録を監視へ回した: {h['key'][:18]}…")
    for h in store.holds_in("WATCHING"):
        out = int(h.get("reserve_out") or quote(cfg, "p2k", int(h.get("amount") or 0),
                                                 float(h.get("extra_fee", 0) or 0))["out"])
        _reserved[h["key"]] = ("kyash", out)


# ============================================================
# コマンド共通
# ============================================================

def allowed_guilds() -> list[int]:
    out = []
    for g in (store.load_config().get("allowed_guild_ids") or []):
        try:
            out.append(int(g))
        except (TypeError, ValueError):
            continue
    return out


def admin_only():
    async def pred(interaction: discord.Interaction) -> bool:
        if is_admin(interaction.user.id):
            return True
        print(f"[guard] 管理コマンドを拒否: user={interaction.user.id} "
              f"cmd={getattr(interaction.command, 'name', '?')}")
        await interaction.response.send_message("管理者専用。", ephemeral=True)
        return False
    return app_commands.check(pred)


DIR_CHOICES = [app_commands.Choice(name=DIR_LABEL[d], value=d) for d in store.DIRECTIONS]
SVC_CHOICES = [app_commands.Choice(name=SVC_LABEL[s], value=s) for s in store.SERVICES]


def order_line(o: dict) -> str:
    return (f"`{o['id']}` {o['created_at'][5:16]} <@{o['user_id']}> {DIR_LABEL[o['direction']]} "
            f"{yen(o['in_amount'])}→{yen(o['out_amount'])} **{o['status']}**"
            + (f" — {o['hold_reason'][:60]}" if o.get("hold_reason") else ""))


def order_embed(o: dict) -> discord.Embed:
    e = _e(f"🧾 {o['id']} ({DIR_LABEL[o['direction']]})", f"状態: **{o['status']}** "
           f"({STATUS_JP.get(o['status'], '')})", "info")
    e.add_field(name="ユーザー", value=f"<@{o['user_id']}> `{o['user_id']}`", inline=True)
    e.add_field(name="受取 → 払い出し", value=f"{yen(o['in_amount'])} → {yen(o['out_amount'])}",
                inline=True)
    e.add_field(name="手数料", value=f"{yen(o['fee_jpy'])} ({o['fee_percent']}%)", inline=True)
    if o.get("hold_reason"):
        e.add_field(name="保留理由", value=f"{o['hold_reason']} (`{o.get('hold_code')}`)",
                    inline=False)
    if o.get("error"):
        e.add_field(name="エラー", value=str(o["error"])[:500], inline=False)
    if o.get("paypay_in"):
        p = o["paypay_in"]
        e.add_field(name="PayPay入金",
                    value=f"送信者 {p.get('sender') or '?'} / ID `{p.get('sender_id') or '-'}`\n"
                          f"{p.get('detect') or ''}", inline=False)
    if o.get("kyash_in"):
        k = o["kyash_in"]
        s = k.get("sender") or {}
        e.add_field(name="Kyash入金",
                    value=(f"ページのID @{k.get('page_user_name') or '?'} / "
                           f"明細の送信者 @{s.get('user_name') or '?'} (publicId `{s.get('public_id') or '-'}`)"),
                    inline=False)
    if o.get("dest"):
        e.add_field(name="払い出し先", value=aml.display(o["dest"]), inline=False)
    pay = o.get("payout") or {}
    if pay:
        e.add_field(name="払い出し",
                    value=(f"{PAYOUT_LABEL.get(pay.get('method'), pay.get('method'))} {pay.get('at', '')}"
                           + (f"\nリンク: ||{pay['link']}||" if pay.get("link") else "")),
                    inline=False)
    hist = o.get("history") or []
    if hist:
        e.add_field(name="経緯", value="\n".join(f"{h['at'][5:16]} {h['status']} {h.get('note', '')}"
                                                for h in hist[-8:])[:1024], inline=False)
    return e


# ============================================================
# 利用者コマンド
# ============================================================

@bot.tree.command(name="換金", description="PayPay ⇄ Kyash の換金を始める")
@app_commands.describe(方向="換金する向き")
@app_commands.choices(方向=DIR_CHOICES)
async def exchange_cmd(interaction: discord.Interaction, 方向: app_commands.Choice[str]):
    await open_exchange(interaction, 方向.value)


@bot.tree.command(name="本人確認", description="PayPay / Kyash の登録状況。未登録ならここから登録できる")
async def reg_cmd(interaction: discord.Interaction):
    cfg = store.load_config()
    g = aml.gate(interaction.user.id)
    if g["state"] == "blocked":
        return await interaction.response.send_message(embed=blocked_embed(), ephemeral=True)
    await interaction.response.send_message(
        embed=reg_intro_embed(cfg, interaction.user.id, g["missing"]), view=RegStartView(),
        ephemeral=True)


@bot.tree.command(name="履歴", description="自分の換金履歴と登録状況")
async def history_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(embed=user_status_embed(interaction.user),
                                            ephemeral=True)


# ============================================================
# 管理: 設置・設定
# ============================================================

@bot.tree.command(name="パネル", description="換金パネルをこのチャンネルに設置 (管理者)")
@admin_only()
async def panel_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await interaction.channel.send(embed=panel_embed(store.load_config()), view=PanelView())
    except Exception as e:
        return await interaction.followup.send(f"❌ 設置できない: `{type(e).__name__}: {e}`",
                                               ephemeral=True)
    await interaction.followup.send("✅ パネルを設置した (再起動後もボタンは使える)", ephemeral=True)


def _cfg_save(fn):
    try:
        return fn()
    except RuntimeError as e:
        return str(e)


@bot.tree.command(name="設定", description="全体の運用パラメータ (管理者)")
@app_commands.describe(受付="False で全面停止", 自動払い出し="False なら全件確認待ち (/再送で払う)",
                       日次上限="1ユーザー1日あたりの上限 (円・両方向合計)",
                       メンテ文="停止中に表示する文", 予備残高="払い出し側に常に残す額 (円)",
                       残高確認="受け取る前に払い出し側の残高を確認する")
@admin_only()
async def config_cmd(interaction: discord.Interaction, 受付: bool | None = None,
                     自動払い出し: bool | None = None, 日次上限: int | None = None,
                     メンテ文: str | None = None, 予備残高: int | None = None,
                     残高確認: bool | None = None):
    fields = {}
    if 受付 is not None:
        fields["enabled"] = 受付
    if 自動払い出し is not None:
        fields["auto_send"] = 自動払い出し
    if 日次上限 is not None:
        fields["daily_limit_jpy"] = max(0, 日次上限)
    if メンテ文 is not None:
        fields["maintenance_note"] = メンテ文[:300]
    if 予備残高 is not None:
        fields["payout_reserve_jpy"] = max(0, 予備残高)
    if 残高確認 is not None:
        fields["check_balance_before_receive"] = 残高確認
    if fields:
        r = _cfg_save(lambda: store.update_config(**fields))
        if isinstance(r, str):
            return await interaction.response.send_message(f"❌ {r}", ephemeral=True)
    cfg = store.load_config()
    e = _e("⚙️ 設定", "変更した。" if fields else "現在の値。", "info")
    e.add_field(name="受付", value="ON" if cfg["enabled"] else "OFF", inline=True)
    e.add_field(name="自動払い出し", value="ON" if cfg["auto_send"] else "OFF", inline=True)
    e.add_field(name="日次上限", value=yen(cfg["daily_limit_jpy"]), inline=True)
    e.add_field(name="予備残高", value=yen(cfg.get("payout_reserve_jpy", 0)), inline=True)
    e.add_field(name="受取前の残高確認", value="ON" if cfg.get("check_balance_before_receive") else "OFF",
                inline=True)
    for d in store.DIRECTIONS:
        dc = store.direction_cfg(cfg, d)
        e.add_field(name=DIR_LABEL[d],
                    value=(f"{'受付中' if dc['enabled'] else '停止'} / {dc['fee_percent']}%"
                           f"+{yen(dc['fee_fixed_jpy'])} / {yen(dc['min_jpy'])}〜{yen(dc['max_jpy'])}"),
                    inline=False)
    e.add_field(name="払い出し方式",
                value=(f"PayPay: {PAYOUT_LABEL.get(cfg.get('paypay_payout'), cfg.get('paypay_payout'))}"
                       f" / Kyash: {PAYOUT_LABEL.get(cfg.get('kyash_payout'), cfg.get('kyash_payout'))}"),
                inline=False)
    if cfg.get("maintenance_note"):
        e.add_field(name="メンテ文", value=cfg["maintenance_note"], inline=False)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="方向設定", description="方向ごとの手数料と受付範囲 (管理者)")
@app_commands.describe(方向="設定する向き", 受付="この方向を受け付けるか", 手数料="%",
                       固定手数料="1回ごとに足す額 (円)", 最低額="円", 上限額="1回あたり (円)")
@app_commands.choices(方向=DIR_CHOICES)
@admin_only()
async def direction_cmd(interaction: discord.Interaction, 方向: app_commands.Choice[str],
                        受付: bool | None = None, 手数料: float | None = None,
                        固定手数料: int | None = None, 最低額: int | None = None,
                        上限額: int | None = None):
    if 手数料 is not None and not (0 <= 手数料 < 100):
        return await interaction.response.send_message("手数料は 0〜99.9 %", ephemeral=True)
    if 最低額 is not None and 最低額 < 1:
        return await interaction.response.send_message("最低額は1円以上", ephemeral=True)
    r = _cfg_save(lambda: store.set_direction(
        方向.value, enabled=受付, fee_percent=手数料,
        fee_fixed_jpy=max(0, 固定手数料) if 固定手数料 is not None else None,
        min_jpy=最低額, max_jpy=上限額))
    if isinstance(r, str):
        return await interaction.response.send_message(f"❌ {r}", ephemeral=True)
    dc = store.direction_cfg(store.load_config(), 方向.value)
    if dc["max_jpy"] < dc["min_jpy"]:
        return await interaction.response.send_message(
            f"⚠️ 上限額 {yen(dc['max_jpy'])} が最低額 {yen(dc['min_jpy'])} より小さい。直して。",
            ephemeral=True)
    await interaction.response.send_message(
        f"✅ **{方向.name}**: {'受付中' if dc['enabled'] else '停止'} / 手数料 {dc['fee_percent']}% "
        f"+ {yen(dc['fee_fixed_jpy'])} / {yen(dc['min_jpy'])}〜{yen(dc['max_jpy'])}\n"
        "-# 設置済みパネルの表示は `/パネル` で置き直すと更新される。", ephemeral=True)


@bot.tree.command(name="払い出し設定", description="払い出しの方式 (管理者)")
@app_commands.describe(paypay方式="Kyash→PayPay の払い出し", kyash方式="PayPay→Kyash の払い出し")
@app_commands.choices(
    paypay方式=[app_commands.Choice(name="送金リンクを作って本人に渡す", value="link"),
              app_commands.Choice(name="登録済みPayPay垢へ直接送る", value="direct")],
    kyash方式=[app_commands.Choice(name="登録済みKyash垢へ直接送る (推奨)", value="transfer"),
             app_commands.Choice(name="送金リンクを作って本人に渡す", value="link")])
@admin_only()
async def payout_cfg_cmd(interaction: discord.Interaction,
                         paypay方式: app_commands.Choice[str] | None = None,
                         kyash方式: app_commands.Choice[str] | None = None):
    fields = {}
    if paypay方式:
        fields["paypay_payout"] = paypay方式.value
    if kyash方式:
        fields["kyash_payout"] = kyash方式.value
    if fields:
        r = _cfg_save(lambda: store.update_config(**fields))
        if isinstance(r, str):
            return await interaction.response.send_message(f"❌ {r}", ephemeral=True)
    cfg = store.load_config()
    await interaction.response.send_message(
        f"PayPay払い出し: **{PAYOUT_LABEL.get(cfg['paypay_payout'])}**\n"
        f"Kyash払い出し: **{PAYOUT_LABEL.get(cfg['kyash_payout'])}**\n"
        "-# 直接送金は登録済みの垢にしか出ないので横流しされない。送金リンクは受け取った人の物になる。",
        ephemeral=True)


@bot.tree.command(name="ログ設定", description="ログの出力先 (管理者)")
@app_commands.describe(取引ログ="換金成立", 内部ログ="失敗・保留・例外 (未設定なら取引ログへ)",
                       検知ログ="本人確認・名義不一致 (未設定なら内部ログへ)", 受取失敗="受取失敗も流す")
@admin_only()
async def log_cfg_cmd(interaction: discord.Interaction,
                      取引ログ: discord.TextChannel | None = None,
                      内部ログ: discord.TextChannel | None = None,
                      検知ログ: discord.TextChannel | None = None,
                      受取失敗: bool | None = None):
    fields = {}
    if 取引ログ:
        fields["log_channel_id"] = 取引ログ.id
    if 内部ログ:
        fields["error_channel_id"] = 内部ログ.id
    if 検知ログ:
        fields["detect_channel_id"] = 検知ログ.id
    if 受取失敗 is not None:
        fields["log_receive_fail"] = 受取失敗
    if fields:
        r = _cfg_save(lambda: store.update_config(**fields))
        if isinstance(r, str):
            return await interaction.response.send_message(f"❌ {r}", ephemeral=True)
    cfg = store.load_config()

    def ch(v):
        return f"<#{v}>" if v else "未設定"
    await interaction.response.send_message(
        f"取引ログ: {ch(cfg['log_channel_id'])}\n内部ログ: {ch(cfg['error_channel_id'])}\n"
        f"検知ログ: {ch(cfg['detect_channel_id'])}\n受取失敗: {'流す' if cfg['log_receive_fail'] else '流さない'}",
        ephemeral=True)


@bot.tree.command(name="残高種別設定", description="PayPayマネー/マネーライトの扱い (PayPay→Kyash) (管理者)")
@app_commands.describe(種別="対象", 扱い="allow=通す / deny=受け取らずに拒否 / hold=受け取って確認待ち",
                       上乗せ="通常手数料に足す %", 混在の計算="flat=一律 / prorated=マネラ比率で按分",
                       マネラ比率上限="混在でこれを超えるとマネラのみ扱い (0〜1)")
@app_commands.choices(
    種別=[app_commands.Choice(name=wallet_detect.LABELS[k], value=k)
        for k in ("money_only", "mixed", "light_only", "unknown")],
    扱い=[app_commands.Choice(name=n, value=v) for n, v in
        (("通す", "allow"), ("受け取らずに拒否", "deny"), ("受け取って確認待ち", "hold"))],
    混在の計算=[app_commands.Choice(name="一律", value="flat"),
           app_commands.Choice(name="マネラ比率で按分", value="prorated")])
@admin_only()
async def wallet_cfg_cmd(interaction: discord.Interaction,
                         種別: app_commands.Choice[str] | None = None,
                         扱い: app_commands.Choice[str] | None = None,
                         上乗せ: float | None = None,
                         混在の計算: app_commands.Choice[str] | None = None,
                         マネラ比率上限: float | None = None):
    cfg = store.load_config()
    pol = cfg.setdefault("wallet_policy", {})
    if 種別 and (扱い or 上乗せ is not None):
        cur = pol.setdefault(種別.value, {"mode": "allow", "extra_fee_percent": 0.0})
        if 扱い:
            cur["mode"] = 扱い.value
        if 上乗せ is not None:
            cur["extra_fee_percent"] = max(0.0, min(50.0, 上乗せ))
    if 混在の計算:
        cfg["mixed_fee_mode"] = 混在の計算.value
    if マネラ比率上限 is not None:
        cfg["mixed_max_light_ratio"] = max(0.0, min(1.0, マネラ比率上限))
    r = _cfg_save(lambda: store.save_config(cfg))
    if isinstance(r, str):
        return await interaction.response.send_message(f"❌ {r}", ephemeral=True)
    lines = [f"{wallet_detect.LABELS[k]}: **{v.get('mode')}** +{v.get('extra_fee_percent', 0)}%"
             for k, v in cfg["wallet_policy"].items() if k in wallet_detect.LABELS]
    lines.append(f"混在の計算: {cfg['mixed_fee_mode']} / マネラ比率上限: {cfg['mixed_max_light_ratio']}")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="本人確認設定", description="本人確認 (登録リンク) の条件 (管理者)")
@app_commands.describe(金額="登録リンクの金額 (円・既定1)", ランダム上限="金額より大きくするとその範囲でランダム指定",
                       制限時間="金額を出してから貼るまで (秒)", 貼り直し回数="1回の登録で貼り直せる回数",
                       付け替え許可="登録済みの人が自分で別の垢に付け替えられるか",
                       paypay受取="PayPayの登録リンクを受け取るか (False=見るだけ)")
@admin_only()
async def reg_cfg_cmd(interaction: discord.Interaction, 金額: int | None = None,
                      ランダム上限: int | None = None, 制限時間: int | None = None,
                      貼り直し回数: int | None = None, 付け替え許可: bool | None = None,
                      paypay受取: bool | None = None):
    fields = {}
    if 金額 is not None:
        fields["reg_amount"] = max(1, min(100, 金額))
        if ランダム上限 is None:
            fields["reg_amount_max"] = fields["reg_amount"]
    if ランダム上限 is not None:
        fields["reg_amount_max"] = max(1, min(100, ランダム上限))
    if 制限時間 is not None:
        fields["reg_window_sec"] = max(30, min(1800, 制限時間))
    if 貼り直し回数 is not None:
        fields["reg_max_attempts"] = max(1, min(10, 貼り直し回数))
    if 付け替え許可 is not None:
        fields["reg_allow_rebind"] = 付け替え許可
    if paypay受取 is not None:
        fields["reg_receive_paypay"] = paypay受取
    if fields:
        r = _cfg_save(lambda: store.update_config(**fields))
        if isinstance(r, str):
            return await interaction.response.send_message(f"❌ {r}", ephemeral=True)
    cfg = store.load_config()
    lo, hi = aml.amount_range(cfg)
    await interaction.response.send_message(
        f"登録リンク: **{yen(lo)}**" + (f"〜{yen(hi)} (ランダム)" if hi > lo else " (固定)")
        + f"\n制限時間: {aml.window_sec(cfg)}秒 / 貼り直し: {aml.max_attempts(cfg)}回"
        + f"\n付け替え: {'本人が可' if cfg.get('reg_allow_rebind') else '管理者の解除が必要'}"
        + f"\nPayPay登録リンク: {'受け取る' if cfg.get('reg_receive_paypay', True) else '見るだけ'}"
        + "\n-# 登録と違う垢からの送金は、設定に関わらず受け取らずに拒否する。", ephemeral=True)


# ============================================================
# 管理: 本人確認
# ============================================================

def account_embed(acc: dict) -> discord.Embed:
    e = _e(f"🪪 {acc.get('user_name') or '?'}",
           f"<@{acc['user_id']}> `{acc['user_id']}` / 状態 **{acc.get('state')}**", "aml")
    for svc in store.SERVICES:
        b = acc.get(svc) or {}
        if not b:
            e.add_field(name=SVC_LABEL[svc], value="未登録", inline=False)
            continue
        e.add_field(name=f"{SVC_LABEL[svc]} ({b.get('state')})",
                    value=f"{aml.display(b)}\n登録 {b.get('verified_at', '?')}"
                          + (f"\n前の垢: {aml.display(b['previous'])}" if b.get("previous") else ""),
                    inline=False)
    e.add_field(name="利用", value=f"{acc.get('count', 0)}回 / {yen(acc.get('volume_jpy', 0))}",
                inline=True)
    ev = acc.get("events") or []
    if ev:
        e.add_field(name="経緯", value="\n".join(f"{x['at'][5:16]} {x['event']} {x.get('note', '')}"[:120]
                                                for x in ev[-8:])[:1024], inline=False)
    return e


@bot.tree.command(name="本人確認一覧", description="紐付けの一覧 / 詳細 / 検索 (管理者)")
@app_commands.describe(ユーザー="詳細を見る相手", 検索="PayPay名 / Kyash ID / ID の部分一致")
@admin_only()
async def reg_list_cmd(interaction: discord.Interaction, ユーザー: discord.User | None = None,
                       検索: str | None = None):
    if ユーザー:
        acc = store.get_account(ユーザー.id)
        if not acc:
            return await interaction.response.send_message("登録が無い。", ephemeral=True)
        return await interaction.response.send_message(embed=account_embed(acc), ephemeral=True)
    items = store.search_accounts(検索, 15) if 検索 else store.list_accounts(20)
    rows = []
    for a in items:
        pp = (a.get("paypay") or {})
        ky = (a.get("kyash") or {})
        rows.append(f"<@{a['user_id']}> {a.get('state')} | PP: {pp.get('name') or '-'}"
                    f"{'' if pp.get('state') in (None, 'VERIFIED') else '(解除)'} | "
                    f"KY: @{ky.get('user_name') or '-'}"
                    f"{'' if ky.get('state') in (None, 'VERIFIED') else '(解除)'}")
    await interaction.response.send_message(
        embed=_e("🪪 本人確認一覧" + (f" 「{検索}」" if 検索 else ""), "\n".join(rows) or "該当なし", "aml"),
        ephemeral=True)


@bot.tree.command(name="本人確認解除", description="紐付けを解除して登録し直させる (管理者)")
@app_commands.describe(ユーザー="対象", サービス="解除する方", 理由="記録に残す")
@app_commands.choices(サービス=SVC_CHOICES + [app_commands.Choice(name="両方", value="both")])
@admin_only()
async def reg_revoke_cmd(interaction: discord.Interaction, ユーザー: discord.User,
                         サービス: app_commands.Choice[str], 理由: str = ""):
    svcs = store.SERVICES if サービス.value == "both" else (サービス.value,)
    done = [s for s in svcs if store.revoke(ユーザー.id, s, interaction.user.id, 理由)]
    if not done:
        return await interaction.response.send_message("解除できる紐付けが無い。", ephemeral=True)
    await interaction.response.send_message(
        f"✅ {ユーザー.mention} の {' / '.join(SVC_LABEL[s] for s in done)} を解除した。"
        "次の換金で登録し直しになる。", ephemeral=True)
    await log_event("🪪 紐付けを解除", f"{interaction.user.mention} が {ユーザー.mention} の "
                    f"{' / '.join(SVC_LABEL[s] for s in done)} を解除", kind="detect",
                    fields=[("理由", 理由 or "-", False)])


@bot.tree.command(name="利用停止", description="利用停止にする。紐付いている垢も他の誰も登録できなくなる (管理者)")
@app_commands.describe(ユーザー="対象", 理由="記録に残す")
@admin_only()
async def block_cmd(interaction: discord.Interaction, ユーザー: discord.User, 理由: str = ""):
    store.set_user_state(ユーザー.id, "BLOCKED", interaction.user.id, 理由, str(ユーザー))
    await interaction.response.send_message(f"⛔ {ユーザー.mention} を利用停止にした。", ephemeral=True)
    await log_event("⛔ 利用停止", f"{interaction.user.mention} → {ユーザー.mention}", kind="detect",
                    color="error", fields=[("理由", 理由 or "-", False)])


@bot.tree.command(name="利用再開", description="利用停止を解く (管理者)")
@admin_only()
async def unblock_cmd(interaction: discord.Interaction, ユーザー: discord.User):
    store.set_user_state(ユーザー.id, "ACTIVE", interaction.user.id, "利用再開", str(ユーザー))
    await interaction.response.send_message(f"✅ {ユーザー.mention} の利用停止を解いた。", ephemeral=True)


@bot.tree.command(name="検知ログ", description="拒否・返金・登録の記録 (管理者)")
@app_commands.describe(件数="最大25", ユーザー="絞り込み")
@admin_only()
async def detections_cmd(interaction: discord.Interaction, 件数: int = 15,
                         ユーザー: discord.User | None = None):
    items = store.recent_detections(max(1, min(25, 件数)), ユーザー.id if ユーザー else None)
    rows = [f"{i['at'][5:16]} <@{i['user_id']}> {i['kind']}/{i['action']} {yen(i.get('amount', 0))} "
            f"{i.get('note', '')[:60]}" for i in items]
    await interaction.response.send_message(
        embed=_e("🔎 検知ログ", "\n".join(rows) or "記録なし", "aml"), ephemeral=True)


@bot.tree.command(name="リンク判定", description="送金リンクを受け取らずに検査する (管理者)")
@admin_only()
async def inspect_cmd(interaction: discord.Interaction, リンク: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    if kyash.extract_code(リンク):
        chk = await asyncio.to_thread(kyash.inspect_link, リンク)
        e = _e("🔎 Kyashリンク (受け取っていない)", chk.get("error") or "受け取れる状態",
               "ok" if chk.get("ok") else "warn")
        e.add_field(name="金額", value=yen(chk.get("amount")), inline=True)
        e.add_field(name="送信者", value=f"{chk.get('sender_name') or '?'} / "
                                        f"@{chk.get('sender_user_name') or '?'}", inline=True)
        owner = store.find_kyash_owner_by_user_name(chk.get("sender_user_name") or "")
        e.add_field(name="登録している人", value=f"<@{owner['user_id']}>" if owner else "なし",
                    inline=True)
        return await interaction.followup.send(embed=e, ephemeral=True)
    chk = await asyncio.to_thread(paypay.inspect_link, リンク)
    info = chk.get("info") or {}
    e = _e("🔎 PayPayリンク (受け取っていない)", chk.get("error") or "受け取れる状態",
           "ok" if chk.get("ok") else "warn")
    if info:
        ident = aml.paypay_identity(info)
        raw, m, l = wallet_detect.detect(info)
        e.add_field(name="金額", value=yen(info.get("amount")), inline=True)
        e.add_field(name="状態", value=paypay.status_text(info.get("order_status")), inline=True)
        e.add_field(name="パスコード", value="あり" if info.get("has_password") else "なし", inline=True)
        e.add_field(name="送信者", value=aml.display(ident), inline=False)
        e.add_field(name="残高種別", value=wallet_detect.summary_line(raw, m, l), inline=False)
        owner = store.find_owner("paypay", ident.get("id"))
        e.add_field(name="登録している人", value=f"<@{owner['user_id']}>" if owner else "なし",
                    inline=True)
    await interaction.followup.send(embed=e, ephemeral=True)


# ============================================================
# 管理: 残高・注文
# ============================================================

@bot.tree.command(name="残高", description="PayPay / Kyash の残高と未払い (管理者)")
@admin_only()
async def balance_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    cfg = store.load_config()
    pb = await asyncio.to_thread(paypay.get_balance) if (paypay.account_info() or {}).get(
        "access_token") else None
    kb = await asyncio.to_thread(kyash.balance_detail) if kyash.ready() else None
    st = store.stats()
    e = _e("💰 残高", "", "info")
    if pb:
        e.add_field(name="🟥 PayPay (払い出し元: Kyash→PayPay)",
                    value=(f"マネー {yen(pb.get('money'))} / マネーライト {yen(pb.get('money_light'))}\n"
                           f"送れる額 {yen((pb.get('money') or 0) + (pb.get('money_light') or 0))}\n"
                           f"未払い {yen(store.unpaid_out('paypay'))}"), inline=False)
    else:
        e.add_field(name="🟥 PayPay", value="取得できない (未ログイン / 期限切れ)", inline=False)
    if kb:
        e.add_field(name="🟦 Kyash (払い出し元: PayPay→Kyash)",
                    value=(f"残高 {yen(kb.get('amount'))} (マネー {yen(kb.get('kyashMoney'))} / "
                           f"バリュー {yen(kb.get('kyashValue'))})\n"
                           f"未払い {yen(store.unpaid_out('kyash'))}"), inline=False)
    else:
        e.add_field(name="🟦 Kyash", value="取得できない (未ログイン / 期限切れ)", inline=False)
    e.add_field(name="予備", value=yen(cfg.get("payout_reserve_jpy", 0)), inline=True)
    held = {s: sum(a for (sd, a) in _reserved.values() if sd == s) for s in store.SERVICES}
    e.add_field(name="受取中の確保", value=f"PayPay {yen(held['paypay'])} / Kyash {yen(held['kyash'])}",
                inline=True)
    for d in store.DIRECTIONS:
        e.add_field(name=f"累計 {DIR_LABEL[d]}",
                    value=f"{st[f'{d}_count']}件 / 受取 {yen(st[f'{d}_in'])} / 手数料 {yen(st[f'{d}_fee'])}",
                    inline=False)
    e.add_field(name="要対応", value=(f"HOLD {st['HOLD']} / FAILED {st['FAILED']} / "
                                      f"UNCERTAIN {st['UNCERTAIN']}"), inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


ORDER_STATES = ("RECEIVING", "PAID", "HOLD", "SENDING", "SENT", "UNCERTAIN", "FAILED", "RETURNING", "RETURNED",
                "REFUNDED", "CANCELED")


@bot.tree.command(name="注文一覧", description="直近の注文 (管理者)")
@app_commands.describe(件数="最大25", 状態="絞り込み", ユーザー="絞り込み")
@app_commands.choices(状態=[app_commands.Choice(name=s, value=s) for s in ORDER_STATES])
@admin_only()
async def orders_cmd(interaction: discord.Interaction, 件数: int = 15,
                     状態: app_commands.Choice[str] | None = None,
                     ユーザー: discord.User | None = None):
    n = max(1, min(25, 件数))
    if ユーザー:
        items = store.user_orders(ユーザー.id, n)
        if 状態:
            items = [o for o in items if o["status"] == 状態.value]
    else:
        items = store.recent_orders(n, 状態.value if 状態 else None)
    await interaction.response.send_message(
        embed=_e("🧾 注文一覧", "\n".join(order_line(o) for o in items)[:4000] or "なし", "info"),
        ephemeral=True)


@bot.tree.command(name="注文詳細", description="注文1件の詳細 (管理者)")
@admin_only()
async def order_cmd(interaction: discord.Interaction, 注文id: str):
    o = store.get_order(注文id)
    if not o:
        return await interaction.response.send_message("その注文は無い。", ephemeral=True)
    await interaction.response.send_message(embed=order_embed(o), ephemeral=True)


@bot.tree.command(name="再送", description="PAID / HOLD / FAILED の注文を払い出す (管理者)")
@admin_only()
async def resend_cmd(interaction: discord.Interaction, 注文id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    o = store.get_order(注文id)
    if not o:
        return await interaction.followup.send("その注文は無い。", ephemeral=True)
    if o["status"] not in ("PAID", "HOLD", "FAILED"):
        return await interaction.followup.send(
            f"`{o['id']}` は **{o['status']}**。再送できるのは PAID / HOLD / FAILED だけ。"
            + ("\n-# UNCERTAIN は二重払いの恐れがあるので `/送金確認` で決着させて。"
               if o["status"] == "UNCERTAIN" else ""), ephemeral=True)
    note = ""
    if o.get("hold_code") in ("UNIDENTIFIED", "IDENTIFYING", "GAVE_UP"):
        note = "\n-# ⚠️ 送信者を明細で確認できていない注文を、管理者判断で払い出した。"
    r = await payout(o["id"], reply_dm(o["user_id"]), by=interaction.user.id)
    st = r.get("status")
    msg = {"sent": "✅ 払い出した", "unknown": "❔ 成否不明 → UNCERTAIN", "failed": f"❌ 失敗: {r.get('error')}",
           "busy": "⏳ 他で処理中", "no_dest": f"❌ {r.get('error')}", "review": f"確認待ち: {r.get('error')}", "mismatch": f"🚫 {r.get('error')}"}
    await interaction.followup.send(f"`{o['id']}` → {msg.get(st, st)}{note}", ephemeral=True)
    if st == "sent":
        await notify_admin(f"🔁 {interaction.user.mention} が `{o['id']}` を手動で払い出した{note}")


@bot.tree.command(name="送信者へ返金", description="Kyash→PayPay の受取額を送信者へ返す (管理者)")
@admin_only()
async def return_cmd(interaction: discord.Interaction, 注文id: str):
    await interaction.response.defer(ephemeral=True, thinking=True)
    o = store.get_order(注文id)
    if not o or o.get("direction") != "k2p":
        return await interaction.followup.send("Kyash→PayPay の注文ではない。", ephemeral=True)
    r = await return_to_sender(o["id"], by=interaction.user.id)
    msg = {"RETURNED": "✅ 送信者へ返した", "UNCERTAIN": "❔ 成否不明 → `/送金確認`",
           "RETURN_FAILED": "❌ 返金に失敗 (HOLDに戻した)", "BUSY": "⏳ 状態が HOLD / FAILED ではない",
           "NO_SENDER": "❌ 送信者が特定できていない。Kyashアプリで返して `/強制状態変更 状態:RETURNED`"}
    await interaction.followup.send(f"`{o['id']}` → {msg.get(r, r)}", ephemeral=True)


@bot.tree.command(name="送金確認", description="UNCERTAIN の注文を決着させる (管理者)")
@app_commands.describe(判定="自動=Kyash明細で確認 / 手で確かめた結果を入れる")
@app_commands.choices(判定=[app_commands.Choice(name="自動で確認 (Kyash直接送金のみ)", value="auto"),
                          app_commands.Choice(name="送れていた", value="sent"),
                          app_commands.Choice(name="送れていない", value="not_sent")])
@admin_only()
async def confirm_cmd(interaction: discord.Interaction, 注文id: str,
                      判定: app_commands.Choice[str] | None = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    o = store.get_order(注文id)
    if not o or o["status"] != "UNCERTAIN":
        return await interaction.followup.send("UNCERTAIN の注文ではない。", ephemeral=True)
    is_return = o.get("pending_action") == "return"
    mode = 判定.value if 判定 else "auto"
    who = f"by {interaction.user.id}"
    if mode == "auto":
        if is_return:
            s = (o.get("kyash_in") or {}).get("sender") or {}
            amt, to = o["in_amount"], s.get("public_id")
        elif o["direction"] == "p2k" and o.get("payout_method") == "transfer":
            amt, to = o["out_amount"], (o.get("dest") or {}).get("id")
        else:
            return await interaction.followup.send(
                "この注文は自動で確認できない (送金リンク / PayPay)。アプリの履歴を見て、"
                "`判定:送れていた` か `判定:送れていない` で決着させて。", ephemeral=True)
        found = await asyncio.to_thread(kyash.find_outgoing, amt, to,
                                        o.get("return_request_id" if is_return else "payout_request_id", ""))
        if not found:
            return await interaction.followup.send(
                "Kyashの明細に該当する出金が見当たらない。もう少し待つか、アプリで確認して手で判定して。",
                ephemeral=True)
        mode = "sent"
    if mode == "sent":
        new = "RETURNED" if is_return else "SENT"
        fields = {"confirmed_tx": found["tx"]} if 判定 is None or 判定.value == "auto" else {}
        if not store.claim_order(o["id"], ("UNCERTAIN",), new, note=f"送金確認: 送れていた {who}", **fields):
            return await interaction.followup.send("この注文は既に処理されています。", ephemeral=True)
        await interaction.followup.send(f"✅ `{o['id']}` → {new}", ephemeral=True)
        if new == "SENT":
            await _dm(o["user_id"], embed=_e("✅ 換金完了", f"注文 `{o['id']}` の払い出しを確認した。", "ok"))
            await log_trade(store.get_order(o["id"]))
        return
    new = "HOLD" if is_return else "FAILED"
    updated = store.claim_order(o["id"], ("UNCERTAIN",), new, note=f"送金確認: 送れていない {who}",
                      **({"hold_code": "RETURN_FAILED", "hold_reason": "返金が送れていなかった"}
                         if is_return else {}))
    if not updated:
        return await interaction.followup.send("この注文は既に処理されています。", ephemeral=True)
    await interaction.followup.send(
        f"`{o['id']}` → {new}。" + ("`/送信者へ返金` でやり直せる。" if is_return else "`/再送` で払い出せる。"),
        ephemeral=True)


@bot.tree.command(name="強制状態変更", description="注文の状態を手で書き換える (管理者・最終手段)")
@app_commands.describe(状態="新しい状態", 理由="記録に残す (必須)")
@app_commands.choices(状態=[app_commands.Choice(name=f"{s} ({STATUS_JP[s]})", value=s)
                          for s in ("SENT", "FAILED", "HOLD", "REFUNDED", "RETURNED", "CANCELED")])
@admin_only()
async def force_cmd(interaction: discord.Interaction, 注文id: str, 状態: app_commands.Choice[str],
                    理由: str):
    o = store.get_order(注文id)
    if not o:
        return await interaction.response.send_message("その注文は無い。", ephemeral=True)
    expect = tuple(s for s in ORDER_STATES if s not in ("RECEIVING", "SENDING", "RETURNING"))
    extra = {"hold_code": "REVIEW", "hold_reason": f"手動: {理由}"} if 状態.value == "HOLD" else {}
    r = store.claim_order(o["id"], expect, 状態.value,
                          note=f"強制変更 by {interaction.user.id}: {理由}", **extra)
    if not r:
        return await interaction.response.send_message(
            f"変更できない (今 **{o['status']}**。送金中・返金中は触れない)。", ephemeral=True)
    await interaction.response.send_message(f"✅ `{o['id']}` {o['status']} → **{状態.value}**",
                                            ephemeral=True)
    await notify_admin(f"✍️ {interaction.user.mention} が `{o['id']}` を {o['status']} → "
                       f"{状態.value} に変更: {理由}")


# ============================================================
# 管理: PayPay の保留
# ============================================================

@bot.tree.command(name="保留一覧", description="PayPayの一時保留・要確認の受取 (管理者)")
@app_commands.describe(すべて="完了・失敗したものも出す")
@admin_only()
async def holds_cmd(interaction: discord.Interaction, すべて: bool = False):
    items = store.all_holds(40)
    if not すべて:
        items = [h for h in items if h.get("state") in ("RECEIVING", "WATCHING", "MANUAL", "TAKEN")]
    rows = [f"`{h['key'][:22]}` {h.get('created_at', '')[5:16]} <@{h.get('user_id')}> "
            f"{yen(h.get('amount', 0))} {HOLD_STATE_LABEL.get(h.get('state'), h.get('state'))} "
            f"{(h.get('note') or '')[:40]}" for h in items[:25]]
    await interaction.response.send_message(
        embed=_e("⏳ 保留一覧", "\n".join(rows) or "なし", "warn"), ephemeral=True)


def _find_hold(target: str) -> dict | None:
    t = str(target or "").strip()
    if not t:
        return None
    code = paypay.extract_code(t) if "paypay" in t or "/" in t else t
    for h in store.all_holds(500):
        if h["key"] == t or h["key"].startswith(t) or (code and h.get("code") == code) \
                or (t and str(h.get("order_id") or "") == t):
            return h
    return None


@bot.tree.command(name="保留確認", description="PayPayの保留を今すぐ確認 / 手動で決着 (管理者)")
@app_commands.describe(対象="リンク / 保留キー (/保留一覧) / 取引番号", 操作="何をするか")
@app_commands.choices(操作=[app_commands.Choice(name="今すぐ確認", value="check"),
                          app_commands.Choice(name="手動で着金確定 (入金を確かめた後)", value="settle"),
                          app_commands.Choice(name="打ち切り (着金していない)", value="close")])
@admin_only()
async def hold_check_cmd(interaction: discord.Interaction, 対象: str,
                         操作: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True, thinking=True)
    h = _find_hold(対象)
    if not h:
        return await interaction.followup.send("該当する保留が無い。", ephemeral=True)
    key = h["key"]
    if h.get("state") in store.HOLD_SETTLED:
        return await interaction.followup.send("この入金は処理済みです。", ephemeral=True)
    if 操作.value == "check":
        if h.get("state") not in ("WATCHING",):
            store.update_hold(key, state="WATCHING", next_accept_at=0)
        r = await guarded_check_hold(store.get_hold(key), force=True, reply=reply_dm(int(h["user_id"])))
        return await interaction.followup.send(f"→ {HOLD_RESULT_LABEL.get(r, r)}", ephemeral=True)
    if 操作.value == "close":
        release_reserve(key)
        store.release_link(key)
        store.finish_hold(key, "CLOSED", note=f"管理者が打ち切り by {interaction.user.id}")
        return await interaction.followup.send("打ち切った (換金はしていない)。", ephemeral=True)
    # settle: リンクが受取済みになっていること (= PayPay側で着金していること) が条件
    code = str(h.get("code") or "")
    if not code or "…(" in code:
        return await interaction.followup.send(
            "リンクコードが残っていない。`対象` にリンクそのものを貼って。", ephemeral=True)
    chk = await asyncio.to_thread(paypay.inspect_link, code)
    info = chk.get("info") or {}
    if paypay.classify_status(info.get("order_status")) != "success":
        return await interaction.followup.send(
            f"リンクが受取済みになっていない ({paypay.status_text(info.get('order_status'))})。"
            "着金していないので確定できない。", ephemeral=True)
    async with _hold_locks.setdefault(key, asyncio.Lock()):
        fresh = store.get_hold(key) or h
        if fresh.get("state") in store.HOLD_SETTLED:
            return await interaction.followup.send("既に処理済み。", ephemeral=True)
        r = await settle_p2k(fresh, int(info.get("amount") or fresh.get("amount") or 0),
                             info.get("order_id") or fresh.get("order_id", ""),
                             reply_dm(int(h["user_id"])), via="manual")
    await interaction.followup.send(f"手動で着金確定 → {r}", ephemeral=True)
    await notify_admin(f"✍️ {interaction.user.mention} が保留 `{key[:22]}` を手動で着金確定した")


# ============================================================
# 管理: 疎通
# ============================================================

@bot.tree.command(name="ヘルス", description="PayPay / Kyash の疎通確認 (管理者)")
@admin_only()
async def health_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    cfg = store.load_config()
    e = _e("🩺 ヘルス", "", "info")
    acc = paypay.account_info() or {}
    if acc.get("access_token"):
        ok = await asyncio.to_thread(paypay.alive)
        e.add_field(name="🟥 PayPay", value=(f"{'✅ OK' if ok else '❌ NG'}\n"
                                            f"{paypay.mask_phone(acc.get('phone', ''))}\n"
                                            f"UDID `{acc.get('device_uuid') or '-'}`"), inline=False)
    else:
        e.add_field(name="🟥 PayPay", value="❌ 未ログイン → 管理者に連絡してください。", inline=False)
    if kyash.ready():
        ok, why = await asyncio.to_thread(kyash.keepalive)
        a = kyash.account()
        e.add_field(name="🟦 Kyash", value=(f"{'✅ OK' if ok else '❌ NG (/kyashログイン)'} ({why})\n"
                                           f"{a['email']} / @{a['user_name'] or '?'} "
                                           f"(publicId {a['public_id']})"), inline=False)
    else:
        e.add_field(name="🟦 Kyash", value="❌ 未ログイン → `/kyashログイン`", inline=False)
    e.add_field(name="ログ", value=("取引 " + ("✅" if cfg.get("log_channel_id") else "❌未設定")
                                   + " / 内部 " + ("✅" if cfg.get("error_channel_id") else "↪取引")
                                   + " / 検知 " + ("✅" if cfg.get("detect_channel_id") else "↪内部")),
                inline=False)
    if store.config_is_broken():
        e.add_field(name="🚨 config.json", value="壊れている → 受付と自動払い出しを停止中", inline=False)
    await interaction.followup.send(embed=e, ephemeral=True)


@bot.tree.command(name="ログテスト", description="各ログチャンネルへテスト送信 (管理者)")
@admin_only()
async def log_test_cmd(interaction: discord.Interaction):
    for kind, label in (("tx", "取引"), ("error", "内部"), ("detect", "検知")):
        await log_event(f"🧪 ログテスト ({label})", f"{interaction.user.mention} が送信", kind=kind,
                        color="info")
    await interaction.response.send_message("送った。届いているか確認して。", ephemeral=True)


# ============================================================
@bot.tree.command(name="paypayログアウト", description="保存済みPayPayトークンを削除 (管理者)")
@admin_only()
async def paypay_logout_cmd(interaction: discord.Interaction, 確認: bool = False):
    if not 確認:
        return await interaction.response.send_message(
            "削除するなら `確認:True`。\n-# 削除すると両方向とも止まる。", ephemeral=True)
    removed = await asyncio.to_thread(paypay.logout)
    await interaction.response.send_message(
        f"🗝️ 削除した: `{', '.join(removed) or 'なし'}`", ephemeral=True)


# ============================================================
# 管理: Kyash ログイン (手順は kyash_bridge.login_step1/2 に一本化)
# ============================================================

_kyash_login_state: dict[int, dict] = {}


def _kyash_done_embed(a: dict) -> discord.Embed:
    return _e("✅ Kyashログイン完了",
              f"{a.get('email')} / @{a.get('user_name') or '?'} (publicId {a.get('public_id')})\n"
              f"トークン残り {a.get('remaining_h', 0):.0f} 時間。期限が近づくと自動で更新する。", "ok")


class KyashLinkModal(discord.ui.Modal, title="Kyash 認証リンクの入力"):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.pasted = discord.ui.TextInput(label="SMS / メールに届いたリンク (丸ごと)",
                                           max_length=600, required=True)
        self.add_item(self.pasted)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        st = _kyash_login_state.pop(self.owner_id, None)
        if not st:
            return await interaction.followup.send("⏰ 時間切れ。`/kyashログイン` からやり直して。",
                                                   ephemeral=True)
        res = await asyncio.to_thread(kyash.login_step2, st, str(self.pasted.value))
        if res.get("status") != "ok":
            return await interaction.followup.send(
                embed=_e("❌ 認証に失敗", f"{res.get('error')}\n`/kyashログイン` からやり直して。", "error"),
                ephemeral=True)
        await interaction.followup.send(embed=_kyash_done_embed(res), ephemeral=True)
        await notify_admin(f"🔑 {interaction.user.mention} がKyashにログインした")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[kyashログイン] link: {type(error).__name__}")


class KyashLinkView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("実行した本人しか進められない。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="届いたリンクを貼る", style=discord.ButtonStyle.success, emoji="📩")
    async def paste(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(KyashLinkModal(self.owner_id))


class KyashLoginModal(discord.ui.Modal, title="Kyashログイン"):
    """⚠️ 伏せ字入力は無い。嫌なら `py system\\kyash_login.py` (画面に出ない)。"""

    def __init__(self):
        super().__init__(timeout=600)
        self.email = discord.ui.TextInput(label="メールアドレス", max_length=200)
        self.password = discord.ui.TextInput(label="Kyashのパスワード",
                                             placeholder="保存もログ出力もしない", max_length=128)
        self.add_item(self.email)
        self.add_item(self.password)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        pw = str(self.password.value)
        try:
            st = await asyncio.to_thread(kyash.login_step1, str(self.email.value), pw)
        finally:
            pw = ""
        if st.get("status") == "ok":
            await interaction.followup.send(embed=_kyash_done_embed(st), ephemeral=True)
            return await notify_admin(f"🔑 {interaction.user.mention} がKyashにログインした")
        if st.get("status") != "link_required":
            return await interaction.followup.send(
                embed=_e("❌ ログインできなかった", str(st.get("error")), "error"), ephemeral=True)
        _kyash_login_state[interaction.user.id] = st
        await interaction.followup.send(
            embed=_e("📩 認証リンクが届く",
                     "KyashからSMS（またはメール）でログイン用のリンクが届く。\n"
                     "**リンクを開かずに**長押しでコピーして、下のボタンから丸ごと貼って。\n"
                     "-# 次回からは同じ端末として扱われ、リンク認証は省かれる。", "info"),
            view=KyashLinkView(interaction.user.id), ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        print(f"[kyashログイン] {type(error).__name__}")


@bot.tree.command(name="kyashログイン", description="BOT側のKyashにログイン (管理者)")
@admin_only()
async def kyash_login_cmd(interaction: discord.Interaction):
    await interaction.response.send_modal(KyashLoginModal())


@bot.tree.command(name="kyashログアウト", description="保存済みKyashトークンを削除 (管理者)")
@admin_only()
async def kyash_logout_cmd(interaction: discord.Interaction, 確認: bool = False):
    if not 確認:
        return await interaction.response.send_message(
            "削除するなら `確認:True`。\n-# 削除すると両方向とも止まる。", ephemeral=True)
    removed = await asyncio.to_thread(kyash.logout)
    await interaction.response.send_message(f"🗝️ 削除した: `{', '.join(removed) or 'なし'}`",
                                            ephemeral=True)


# ============================================================
# 定期処理
# ============================================================

@tasks.loop(minutes=25)
async def keep_session():
    """PayPayトークンを生かし続ける"""
    try:
        if not (paypay.account_info() or {}).get("access_token"):
            return
        if not await asyncio.to_thread(paypay.alive):
            await notify_admin_throttled("pp_dead", "⚠️ PayPayセッションが切れた。管理者に連絡してください。",
                                         interval=3600)
    except Exception as e:
        print(f"[keep_session] {type(e).__name__}: {e}")


@keep_session.before_loop
async def _before_keep():
    await bot.wait_until_ready()


@tasks.loop(hours=6)
async def keep_kyash():
    """Kyashトークン (約30日) を期限の3日前から更新する"""
    try:
        if not kyash.ready():
            return
        ok, why = await asyncio.to_thread(kyash.keepalive)
        print(f"[{jst_now()}] Kyashセッション: {'OK' if ok else 'NG'} ({why})")
        if not ok:
            await notify_admin_throttled("ky_dead", f"⚠️ Kyashセッションが使えない ({why})。`/kyashログイン`",
                                         interval=3600)
    except Exception as e:
        print(f"[keep_kyash] {type(e).__name__}: {e}")


@keep_kyash.before_loop
async def _before_keep_kyash():
    await bot.wait_until_ready()


_reminded: dict[str, float] = {}


@tasks.loop(hours=1)
async def remind_stuck_orders():
    cfg = store.load_config()
    if not cfg.get("remind_stuck", True):
        return
    try:
        hours = max(1.0, float(cfg.get("stuck_remind_hours", 6) or 6))
    except (TypeError, ValueError):
        hours = 6.0
    now = time.time()
    stuck = [o for o in store.open_orders()
             if o.get("status") in ("PAID", "HOLD", "FAILED", "UNCERTAIN")
             and now - float(o.get("created_ts") or now) > hours * 3600
             and now - _reminded.get(o["id"], 0) > hours * 3600]
    if not stuck:
        return
    for o in stuck:
        _reminded[o["id"]] = now
    await notify_admin("⏰ **決着していない注文**\n" + "\n".join(order_line(o) for o in stuck[:15])
                       + ("\n…" if len(stuck) > 15 else ""))


@remind_stuck_orders.before_loop
async def _before_remind():
    await bot.wait_until_ready()


# ============================================================
# 起動
# ============================================================

async def tree_guard(interaction: discord.Interaction) -> bool:
    """スラッシュコマンドは、許可したサーバーの中でだけ受け付ける"""
    if interaction.guild_id is None:
        try:
            await interaction.response.send_message("DMでは使えない。サーバー内で実行して。",
                                                    ephemeral=True)
        except Exception:
            pass
        return False
    allow = allowed_guilds()
    if allow and interaction.guild_id not in allow:
        try:
            await interaction.response.send_message("このサーバーでは使えない。", ephemeral=True)
        except Exception:
            pass
        return False
    return True


bot.tree.interaction_check = tree_guard


@bot.event
async def on_guild_join(guild: discord.Guild):
    allow = allowed_guilds()
    if allow and guild.id not in allow:
        print(f"[guard] 未許可サーバーに招待された → 退出: {guild.name} ({guild.id})")
        await notify_admin(f"🚪 未許可サーバー **{guild.name}** (`{guild.id}`) に招待されたので退出した")
        try:
            await guild.leave()
        except Exception as e:
            print(f"[guard] 退出失敗: {e}")


async def pin_allowed_guilds():
    cfg = store.load_config()
    if cfg.get("allowed_guild_ids"):
        return
    ids = [g.id for g in bot.guilds]
    if not ids:
        return
    try:
        store.set_config("allowed_guild_ids", ids)
    except RuntimeError:
        return
    names = ", ".join(f"{g.name}({g.id})" for g in bot.guilds)
    print(f"[guard] 許可サーバーを固定: {names}")
    await notify_admin(f"🔒 使用可能サーバーを固定した: {names}\n"
                       "-# 増やす時は system/config.json の allowed_guild_ids に追記して再起動")


async def recover_stuck_orders():
    """払い出し中 / 返金中のまま落ちた注文は、投げた直後かもしれないので UNCERTAIN に倒す"""
    # 受取結果を台帳に保存した直後の停止では、リンク側への反映だけが残る。
    orders = store.load_orders()["orders"]
    for key, link in store.load_used()["links"].items():
        order = orders.get(link.get("order_id"))
        if link.get("state") != "RESERVED" or not order or order.get("receipt_key") != key:
            continue
        if order["status"] == "CANCELED" and order.get("receipt_rejected"):
            store.release_link(key)
        elif order["status"] != "RECEIVING":
            store.confirm_link(key, order["id"], order["in_amount"])
    for o in store.open_orders():
        if o.get("receipt_key") and (o.get("kyash_in") or {}).get("receipt") in ("received", "unidentified"):
            store.record_usage(o["user_id"], o["in_amount"], o["id"])
        if o["status"] == "RECEIVING":
            store.confirm_link(o["receipt_key"], o["id"], o["in_amount"])
            store.claim_order(o["id"], ("RECEIVING",), "HOLD", hold_code="RECEIPT_REVIEW",
                              hold_reason="受取中に停止。Kyashの入金明細を確認してください")
            await notify_admin(f"{o['id']} のKyash着金を確認してください。")
        elif o.get("status") in ("SENDING", "RETURNING"):
            store.claim_order(o["id"], ("SENDING", "RETURNING"), "UNCERTAIN",
                              note="BOT再起動時に処理中のまま残っていた")
            await notify_admin(f"🛑 **{o['id']}** が{'払い出し' if o['status'] == 'SENDING' else '返金'}"
                               f"中のまま落ちていた → UNCERTAIN。明細を確認して `/送金確認 {o['id']}`")


async def setup_hook():
    # 永続ビュー (timeout=None + 固定 custom_id)。再起動後も古いメッセージのボタンが動く。
    # View はイベントループの中でしか作れないのでここで作る。
    for svc in store.SERVICES:
        REG_SUBMIT_VIEWS[svc] = RegSubmitView(svc)
        bot.add_view(REG_SUBMIT_VIEWS[svc])
    for v in (PanelView(), RegStartView(), HoldApprovedView()):
        bot.add_view(v)


bot.setup_hook = setup_hook
_ready_done = False


@bot.event
async def on_ready():
    global _ready_done
    if _ready_done:
        print(f"[{jst_now()}] 再接続 (初期化はスキップ)")
        return
    _ready_done = True
    await pin_allowed_guilds()
    await recover_stuck_orders()
    await recover_receiving_holds()
    try:
        synced = await bot.tree.sync()
        print(f"[+] スラッシュコマンド同期: {len(synced)}件")
    except Exception as e:
        print(f"[!] 同期失敗: {e}")
    for loop in (keep_session, keep_kyash, watch_holds, reidentify_kyash, remind_stuck_orders):
        if not loop.is_running():
            loop.start()
    cfg = store.load_config()
    print("=" * 60)
    print(f" ✅ ログイン: {bot.user} ({bot.user.id})")
    for d in store.DIRECTIONS:
        dc = store.direction_cfg(cfg, d)
        print(f" {DIR_LABEL[d]}: {'受付' if dc['enabled'] else '停止'} {dc['fee_percent']}% "
              f"{yen(dc['min_jpy'])}〜{yen(dc['max_jpy'])}")
    print(f" PayPay: {'ログイン済' if (paypay.account_info() or {}).get('access_token') else '未ログイン'}"
          f" / Kyash: {'ログイン済' if kyash.ready() else '未ログイン'}")
    print(f" 管理者: {', '.join(str(a) for a in admin_ids())} (secrets.json で固定)")
    if not cfg.get("log_channel_id"):
        print(" ⚠️ ログチャンネル未設定 = 失敗や保留の通知が誰にも届かない。/ログ設定 で設定して。")
    if store.config_is_broken():
        print(" 🚨 config.json が壊れている → 受付と自動払い出しを停止中")
    print("=" * 60)


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        return
    traceback.print_exc()
    msg = f"❌ エラー: `{type(error).__name__}`"
    try:
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


def acquire_single_instance_lock():
    """二重起動を防ぐ (同じ注文ファイル・同じ残高を2プロセスで触らせない)"""
    path = os.path.join(BASE, "bot.lock")
    f = open(path, "a+")
    try:
        import fcntl
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        print("=" * 60)
        print(" ❌ 既にBOTが起動している (bot.lock を確保できない)")
        print(f"    ロックファイル: {path}")
        print("=" * 60)
        sys.exit(1)
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    return f


if __name__ == "__main__":
    token = load_token()
    admins = require_admin_ids()
    print(f"[起動] 管理者 {len(admins)}人: " + ", ".join(str(a) for a in admins))
    _instance_lock = acquire_single_instance_lock()
    store.load_orders()
    if not os.path.exists(store.CONFIG_FILE):
        store.save_config(store.load_config())   # 初回は既定値で生成
    bot.run(token)
