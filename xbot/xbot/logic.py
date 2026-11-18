"""ブラウザを使わない判断部分（テストしやすいよう分けてある）。"""
import hashlib
import time
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
KINDS = {"always": "すべてのDMに返す", "keyword": "キーワードが入っていたら返す", "first_time": "はじめての相手にだけ返す"}


def norm(text: str) -> str:
    """全角・半角、大文字・小文字の違いを無視して比べるための形にする。"""
    return unicodedata.normalize("NFKC", text or "").casefold().strip()


def keywords_of(rule) -> list:
    raw = (rule["keywords"] or "").replace("、", ",").replace("\n", ",")
    return [norm(k) for k in raw.split(",") if norm(k)]


def rule_matches(rule, message: str, first_time: bool) -> bool:
    if not rule["enabled"]:
        return False
    kind = rule["kind"]
    if kind == "always":
        return True
    if kind == "first_time":
        return first_time
    if kind == "keyword":
        kws = keywords_of(rule)
        if not kws:
            return False
        m = norm(message)
        hits = [k in m for k in kws]
        return all(hits) if rule["match"] == "all" else any(hits)
    return False


def pick_rule(rules, message: str, first_time: bool):
    """優先度（数字が小さいほど先）→ 作成順で、最初に当てはまったルールを返す。"""
    for r in sorted(rules, key=lambda r: (int(r["priority"]), int(r["id"]))):
        if rule_matches(r, message, first_time):
            return r
    return None


def msg_hash(conv: str, message: str) -> str:
    return hashlib.sha256((conv + "\n" + norm(message)).encode()).hexdigest()[:24]


def jst_day_start(now: float) -> float:
    d = datetime.fromtimestamp(now, JST)
    return d.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def can_post(posted_times, now: float, max_per_day: int) -> bool:
    start = jst_day_start(now)
    return sum(1 for t in posted_times if t >= start) < max_per_day


def can_reply(reply_times, now: float, max_per_hour: int) -> bool:
    return sum(1 for t in reply_times if t >= now - 3600) < max_per_hour


def in_cooldown(last_reply_at, now: float, hours: float) -> bool:
    return last_reply_at is not None and now - last_reply_at < hours * 3600


def parse_jst(value: str) -> float:
    """iPhone の日時入力（2026-11-20T21:30）を日本時間として読む。"""
    d = datetime.strptime(value.strip()[:16], "%Y-%m-%dT%H:%M")
    return d.replace(tzinfo=JST).timestamp()


def fmt_jst(ts) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(float(ts), JST).strftime("%m/%d %H:%M")


def is_own_preview(preview: str) -> bool:
    """会話一覧のプレビューが自分の送った文なら、返信しない。"""
    p = norm(preview)
    return p.startswith(("you:", "あなた:", "you sent", "あなたが送信", "あなたが"))


MAX_POST_LEN = 280   # 全角は2として数える（Xの数え方に近い簡易版）


def post_length(text: str) -> int:
    n = 0
    for ch in text or "":
        n += 1 if ord(ch) < 0x1100 else 2
    return n


WEEK = ["月", "火", "水", "木", "金", "土", "日"]


def next_time(rep: dict, after: float):
    """定期投稿の次の時刻（after より後）。rep = {type: daily|weekly|hours, time: "HH:MM", days: [0..6], hours: N}"""
    if not rep or rep.get("type") not in ("daily", "weekly", "hours"):
        return None
    if rep["type"] == "hours":
        step = max(1, int(rep.get("hours") or 1)) * 3600
        base = float(rep.get("anchor") or after)
        n = int((after - base) // step) + 1
        return base + max(1, n) * step
    hh, mm = [int(x) for x in str(rep.get("time") or "09:00").split(":")[:2]]
    d = datetime.fromtimestamp(after, JST).replace(hour=hh, minute=mm, second=0, microsecond=0)
    from datetime import timedelta
    days = set(int(x) for x in rep.get("days") or []) if rep["type"] == "weekly" else set(range(7))
    if not days:
        return None
    for i in range(0, 8):
        c = d + timedelta(days=i)
        if c.timestamp() > after and c.weekday() in days:
            return c.timestamp()
    return None


def describe_repeat(rep) -> str:
    if not rep:
        return ""
    if rep.get("type") == "daily":
        return f"毎日 {rep.get('time')}"
    if rep.get("type") == "weekly":
        return "毎週 " + "・".join(WEEK[int(d)] for d in sorted(rep.get("days") or [])) + f" {rep.get('time')}"
    if rep.get("type") == "hours":
        return f"{rep.get('hours')}時間ごと"
    return ""


def render_text(text: str, ts: float) -> str:
    """本文の {日付} {曜日} {時刻} を、投稿するときの日本時間に置きかえる（同じ文の連続投稿を避けるのにも使える）。"""
    d = datetime.fromtimestamp(ts, JST)
    return (text or "").replace("{日付}", f"{d.month}/{d.day}").replace("{曜日}", WEEK[d.weekday()]).replace("{時刻}", d.strftime("%H:%M"))


def now() -> float:
    return time.time()
