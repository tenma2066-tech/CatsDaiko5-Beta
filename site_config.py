"""お店ごとに変わる設定（店名・問い合わせ先など）を1か所にまとめたもの。

画面（templates/*.html）と Q&A（qa_bot.py）は、ここの値だけを使う。環境変数で上書きできるので、
別の人がこの仕組みを使うときは、コードを書き換えずに .env を変えるだけでよい（docs/SETUP.md）。
販売用のソース一式（source_pack.py）には、このファイルの「持ち主の既定値」を空にしたものを入れる。
"""
from __future__ import annotations

import os
import re


def _env(name: str, default: str) -> str:
    """環境変数の値。未設定・空のときは既定値（docker-compose が空のまま渡してきても既定値になる）。"""
    return (os.environ.get(name) or "").strip() or default


# 店名（画面のタイトル・フッター・Q&A の名乗り）
SHOP_NAME = _env("SHOP_NAME", "TN SHOP") or "SHOP"
# 問い合わせ先の X（旧Twitter）のID。@ は付けても付けなくてもよい。空なら問い合わせ先を出さない。
SHOP_X_ID = re.sub(r"[^A-Za-z0-9_]", "", _env("SHOP_X_ID", "paysms1212"))[:15]
# Google Search Console の所有確認（<meta name="google-site-verification">）。空なら出さない。
GOOGLE_SITE_VERIFICATION = re.sub(r"[^A-Za-z0-9_-]", "", _env(
    "GOOGLE_SITE_VERIFICATION", "HOaFZOzV5t4jyEXdAr2bMg6f2OVwvdpedlAosg1FXT0"))[:100]
# アプリ内の実績を始める前に、感想を集めていた X のポストのID。「今までの実績を見る」のリンク先になる。空なら出さない。
REVIEW_POST_ID = re.sub(r"\D", "", _env("REVIEW_POST_ID", "2119246650996932810"))[:25]


# 実績（完了後に星と感想を書いてもらう）を使うか。0 にすると、入力のお願いも /reviews も出さない。
REVIEWS_ENABLED = _env("REVIEWS_ENABLED", "1") != "0"
# ミニゲーム「ダイナマイト野球」（別コンテナの dya-proxy を Caddy が /dya/ に回す。deploy/vps/）。
# サーバーに dya-proxy と暗証番号がそろったときだけ remote-setup.sh が DYA_ENABLED=1 にする。0 ならトップにカードを出さない。
DYA_ENABLED = _env("DYA_ENABLED", "0") == "1"
DYA_PATH = "/dya/"

REVIEW_MIN_CHARS = 20            # 感想の最小文字数
REVIEW_MAX_CHARS = 400


def past_reviews_url() -> str:
    """アプリ内の実績を始める前の実績（X のポスト）へのリンク。REVIEW_POST_ID が空なら出さない。"""
    return f"https://x.com/{SHOP_X_ID or 'i'}/status/{REVIEW_POST_ID}" if REVIEW_POST_ID else ""


def contact_text() -> str:
    """文章の中で使う問い合わせ先（例：「X @paysms1212」）。未設定なら「お店」。"""
    return f"X @{SHOP_X_ID}" if SHOP_X_ID else "お店"


def contact_url() -> str:
    return f"https://x.com/{SHOP_X_ID}" if SHOP_X_ID else ""


_STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")


def flow_images() -> dict:
    """「ご利用の流れ」の図解があるか（static/flow.jpg・tsum-flow.jpg）。
    図には店名や問い合わせ先が描き込まれるので、お店ごとに作って置く。無ければその欄は出さない。"""
    return {"nyanko": os.path.isfile(os.path.join(_STATIC, "flow.jpg")),
            "tsum": os.path.isfile(os.path.join(_STATIC, "tsum-flow.jpg"))}


def og_images() -> dict:
    """リンクを貼ったときに出る画像（にゃんこ・ツムツム）。お店用の ogp-*.jpg（1200×630・店名入り）があればそれ、
    無ければ店名の入っていないゲームの絵（game-*.jpg）を使う。"""
    out = {}
    for name in ("nyanko", "tsum"):
        if os.path.isfile(os.path.join(_STATIC, f"ogp-{name}.jpg")):
            out[name] = {"src": f"/static/ogp-{name}.jpg?v=1", "w": 1200, "h": 630}
        else:
            out[name] = {"src": f"/static/game-{name}.jpg?v=1", "w": 1024, "h": 512}
    return out


def template_context() -> dict:
    """すべての画面に渡す値。"""
    return {"shop_name": SHOP_NAME, "shop_x": SHOP_X_ID, "contact": contact_text(), "contact_url": contact_url(),
            "google_verification": GOOGLE_SITE_VERIFICATION, "review_post_id": REVIEW_POST_ID,
            "flow_images": flow_images(), "og": og_images(),
            "reviews_on": REVIEWS_ENABLED, "dya_enabled": DYA_ENABLED, "dya_path": DYA_PATH, "past_reviews_url": past_reviews_url(),
            "review_min": REVIEW_MIN_CHARS, "review_max": REVIEW_MAX_CHARS}
