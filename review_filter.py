"""実績（感想）に、汚い言葉・宣伝・連絡先が入らないようにする検査。

check(文章, 追加の禁止語) → (通してよいか, 理由のコード, お客さんに見せる文)

考え方:
- 文字の幅や大小をそろえ（NFKC・小文字）、文字のあいだに挟んだ空白・記号を取り除いてから探す
  （「死 ね」「し.ね」「ｆｕｃｋ」「f*u*c*k」のようなすり抜けを拾う）。
- 日本語は「含まれていたら不可」。ただし普通の言葉を巻き込む語は入れない
  （例：「ばか」は「ばかり」、「かす」は「カスタム」、「くそ」は「やくそく」に含まれるので、
  カタカナ・漢字の形や、前後がはっきりした形だけを登録する）。
- 英語は単語の区切りで探す（class の中の ass を拾わない）。
- 低い評価や不満そのものは止めない（「遅かった」「失敗した」は書ける）。止めるのは言葉づかいだけ。
"""
from __future__ import annotations

import re
import unicodedata

# 文字のあいだに挟んでごまかすのに使われる空白・記号（これらは取り除いてから探す）
# 長音「ー」は言葉の一部なので取り除かない（「ばーか」が「ばかり」と区別できなくなる）。
_SEPARATORS = re.compile(r"[\s​-‏⁠﻿.,。、・･*＊_\-‐-―〜~/\\|!！?？'\"`^+=:;()（）\[\]「」『』<>＜＞#＃$＄%％&＆]+")

# 日本語：含まれていたら不可
JA_WORDS = (
    # 暴言・脅し
    "死ね", "しねよ", "しねや", "氏ね", "タヒね", "タヒれ", "殺す", "殺して", "殺したい", "ころすぞ", "コロス", "ぶっ殺",
    "くたばれ", "消えろ", "失せろ", "地獄に落ち", "呪う", "呪って",
    # 侮辱
    "クソ", "糞", "くっそ", "ゴミ", "ごみくず", "クズ", "屑", "カス野郎", "このカス", "ゴミカス", "バカ", "馬鹿", "ばーか", "ばかやろ",
    "アホ", "阿呆", "あほか", "ボケ", "ぼけが", "マヌケ", "間抜け", "ウザ", "うざい", "うぜえ", "うぜー", "キモい", "きもい", "キショ", "きしょい",
    "キチガイ", "気違い", "きちがい", "雑魚", "ハゲ", "デブ", "ブス", "ぶさいく", "クソガキ", "老害", "無能", "低能",
    # 差別・中傷
    "ガイジ", "池沼", "身障", "チョン", "シナ人", "ニガー", "土人", "ホモ野郎", "オカマ野郎",
    # 性的
    "セックス", "エロい", "エロ動画", "エロ画像", "エロサイト", "えろい", "えっち", "エッチ", "ちんこ", "チンコ", "ちんちん", "チンチン", "ちんぽ", "チンポ", "まんこ", "マンコ",
    "おっぱい", "オッパイ", "乳首", "ペニス", "ヴァギナ", "射精", "オナニー", "シコ", "風俗", "レイプ", "強姦", "売春", "援交", "パパ活",
    "アダルト", "わいせつ", "猥褻", "ポルノ",
)
# 英語：単語として出てきたら不可（前後が英字でないとき）
EN_WORDS = (
    "fuck", "fucking", "fucker", "motherfucker", "fuk", "fck", "shit", "bullshit", "bitch", "bastard", "asshole", "dickhead",
    "pussy", "cunt", "whore", "slut", "nigger", "nigga", "faggot", "retard", "retarded", "porn", "sex", "wtf", "stfu", "kys",
)
# 英語：記号を取り除いたあとでも、含まれていたら不可（ほかの単語の一部になりにくいものだけ）
EN_ANYWHERE = ("fuck", "motherfuck", "bullshit", "asshole", "nigger", "faggot")

# 宣伝・連絡先（実績は感想を書く場所。外へ誘導する書き込みは載せない）
_URL = re.compile(r"(https?://|www\.|[a-z0-9-]+\.(com|net|org|jp|io|gg|me|xyz|info|biz|shop|app|link|ly|co)(/|\b))", re.I)
_HANDLE = re.compile(r"(?<![a-z0-9])@[a-z0-9_]{3,}", re.I)
_CONTACT = re.compile(r"(discord|ディスコ|line\s*id|ラインid|ライン交換|telegram|テレグラム|インスタ|dmして|dmください|dm下さい)", re.I)
_PHONE = re.compile(r"(?<!\d)0\d{9,10}(?!\d)")

# 危険・紛らわしい文字：見えない文字（ゼロ幅）、文字の並びを入れ替える制御文字、タグ（<script> など）
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2069\ufeff\u00ad\ufff9-\ufffb]")
_TAG = re.compile(r"<\s*/?\s*[a-z!][^>]{0,200}>|javascript\s*:|&#x?[0-9a-f]+;|\bon[a-z]+\s*=", re.I)
# でたらめな並び（字数を埋めるための入力）：五十音・アルファベット・数字・キーボードの順
_RUNS = ("あいうえお", "かきくけこ", "さしすせそ", "たちつてと", "なにぬねの", "はひふへほ", "まみむめも", "らりるれろ",
         "あかさたな", "はまやらわ", "アイウエオ", "アカサタナ", "いろはにほへと",
         "abcdef", "qwerty", "asdfgh", "zxcvbn", "123456", "012345", "987654")

MSG_WORDS = "不適切な言葉が含まれています。言葉を変えて、もう一度お書きください。"
MSG_CHARS = "使えない文字や記号が含まれています。ふつうの文字だけでお書きください。"
MSG_JUNK = "意味の通る文章でお書きください（文字を並べただけの入力は受け付けられません）。"


def junk(text: str) -> bool:
    """でたらめな入力か（機械的に分かる範囲）。微妙なものは review_ai（AI）が見る。"""
    tight = squeeze(normalize(text))
    if not tight:
        return True
    if sum(tight.count(run) for run in map(normalize, _RUNS)) >= 2:      # 「あかさたなはまやらわ」「あいうえおかきくけこ」など
        return True
    letters = [ch for ch in tight if ch.isalnum()]
    if len(letters) < max(8, len(tight) // 2):                           # 記号・絵文字ばかり
        return True
    if len(set(letters)) < max(6, len(letters) // 6):                    # 少ない種類の文字のくり返し
        return True
    for size in (2, 3, 4, 5, 6):                                         # 同じかたまりのくり返しで字数を埋めたもの（「よかったよかったよかった…」）
        if len(tight) >= size * 4:
            top = max((tight.count(tight[i:i + size]) for i in range(0, min(len(tight) - size, 12))), default=0)
            if top * size >= len(tight) * 0.7:
                return True
    return False
MSG_AD = "URL・ID・連絡先は書けません。感想だけをお書きください。"


def normalize(text: str) -> str:
    """幅・大小をそろえる（全角英数→半角、半角カナ→全角、大文字→小文字）。"""
    return unicodedata.normalize("NFKC", str(text or "")).lower()


def squeeze(text: str) -> str:
    """文字のあいだの空白・記号を取り除く（ごまかしを拾うため）。"""
    return _SEPARATORS.sub("", text)


def clean_words(raw) -> list[str]:
    """管理画面で足す禁止語を整える（1行に1語。2文字以上・40文字まで・最大200語）。"""
    out: list[str] = []
    for line in str(raw or "").splitlines():
        word = squeeze(normalize(line))
        if 2 <= len(word) <= 40 and word not in out:
            out.append(word)
    return out[:200]


def check(text: str, extra_words=()) -> tuple[bool, str, str]:
    """感想を載せてよいか。戻り値は (よいか, 理由のコード, お客さんに見せる文)。"""
    if _INVISIBLE.search(str(text or "")) or _TAG.search(str(text or "")):
        return False, "bad_chars", MSG_CHARS
    plain = normalize(text)
    if _URL.search(plain) or _HANDLE.search(plain) or _CONTACT.search(plain) or _PHONE.search(squeeze(plain)):
        return False, "ad", MSG_AD
    tight = squeeze(plain)
    for word in JA_WORDS:
        if squeeze(normalize(word)) in tight:
            return False, "bad_words", MSG_WORDS
    for word in extra_words or ():
        w = squeeze(normalize(word))
        if len(w) >= 2 and w in tight:
            return False, "bad_words", MSG_WORDS
    for word in EN_WORDS:
        if re.search(r"(?<![a-z])" + re.escape(word) + r"(?![a-z])", plain):
            return False, "bad_words", MSG_WORDS
    letters = re.sub(r"[^a-z]", "", plain)          # f.u.c.k / f u c k / f*ck は別途、英字だけ並べて探す
    if any(word in letters for word in EN_ANYWHERE):
        return False, "bad_words", MSG_WORDS
    if junk(plain):                     # 宣伝・言葉づかいのほうが理由として具体的なので、でたらめの判定は最後
        return False, "gibberish", MSG_JUNK
    return True, "", ""
