"""曲名拆分：基名 + ver= + karaoke/drama/BGM + wiki 安全页名。

wikimas {{Track}}} 中 {{{1}}} 既是显示基名也是歌曲页链接 / SMW「歌曲」属性。
因此 {{{1}}} 不得含 MediaWiki 非法标题字符（尤其是 ASCII <>）。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


def _nfc(s: str) -> str:
    """仅 NFC。禁止 NFKC（会把 ＜＞ 压成 <>，破坏 wiki/SMW）。"""
    return unicodedata.normalize("NFC", s).strip()


# MediaWiki 标题非法 / 危险字符 → 全角
_MW_TITLE_SAFE = str.maketrans(
    {
        "#": "＃",
        "<": "＜",
        ">": "＞",
        "[": "［",
        "]": "］",
        "{": "｛",
        "}": "｝",
        "|": "｜",
        # 不可见/异常空白
        "\u00a0": " ",
    }
)

_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uf900-\ufaff]")

# 末尾无意义连字符
_TRAIL_DASH = re.compile(r"[\s\u3000]*[-–—−_]+[\s\u3000]*$")

# 【方括号标签】
_SQ_TAG = re.compile(r"【([^】]*)】")

# 末尾圆括号（半角/全角）
_TRAIL_PAREN = re.compile(r"[\s\u3000]*[（(]([^）]*)[）)][\s\u3000]*$")

# 波折后缀 ～『曲』より～
_WAVE_SUFFIX = re.compile(r"([～~].+)$")

# 括号外 karaoke / instrumental
_TRAIL_KARAOKE = re.compile(
    r"(?:"
    r"[\s\u3000]*(?:オリジナル[・･·]?カラオケ|オリジナルカラオケ|カラオケ)"
    r"|[\s\-–—]*(?:Instrumental|Off\s*Vocal|off\s*vocal|off-vocal|インスト(?:ゥルメンタル)?)"
    r")[\s\u3000]*$",
    re.IGNORECASE,
)

# 明确的「版本/编曲」括号内容
_VER_HINT = re.compile(
    r"(?:"
    r"\bver(?:sion)?\b"
    r"|\bver\.\s*\d*"
    r"|\bmix\b|remix|re-?mix"
    r"|リミックス|ミックス"
    r"|カラオケ|karaoke|instrumental|off[\s-]*vocal|インスト"
    r"|new\s*song"
    r"|ファミソン|8\s*bit|8bit"
    r"|best\s*album"
    r"|extra"
    r"|オリジナル"
    r"|ソロ|\bsolo\b"
    r"|m@ster"
    r"|hyr\s*version"
    r"|オフボーカル"
    r")",
    re.IGNORECASE,
)

# 元数据括号：应剥掉并 nolink，不进 ver
_META_PAREN = re.compile(
    r"^(?:"
    r"ドラマ|Drama|ドラマパート"
    r"|トーク|Talk"
    r"|ラジオ|Radio"
    r"|スキット|Skit"
    r"|ナレーション|Narration"
    r")$",
    re.IGNORECASE,
)

_TALK_HINT = re.compile(
    r"(?i)(?:トーク|^Talk\b|ドラマ|Drama|ラジオ|Radio|スキット|skit|ナレーション)"
)
_BGM_HINT = re.compile(
    r"(?i)(?:"
    r"BGM|ドラマ用|効果音|\bSE\b|ジングル|jingle"
    r"|オープニング|フィナーレ|エンディング"
    r"|アトラクション"
    r"|インスト(?!ゥルメンタルを)"  # soft
    r")"
)


@dataclass
class WikiTrackTitle:
    title: str
    ver: str | None = None
    karaoke: bool = False
    talk: bool = False
    instrumental: bool = False
    bgm: bool = False
    tags: list[str] | None = None
    outro: str | None = None

    @property
    def nolink(self) -> bool:
        return (
            self.karaoke
            or self.talk
            or self.instrumental
            or self.bgm
            or not self.title
        )

    @property
    def empty_artist(self) -> bool:
        """仅 karaoke / 纯器乐清空艺人；drama 仍保留声优。"""
        if self.karaoke:
            return True
        if self.instrumental and not self.talk:
            return True
        return False


def sanitize_wiki_page_title(title: str) -> str:
    """歌曲页 / SMW 用标题：去掉非法字符，CJK 曲名统一全角 !?。"""
    s = _nfc(title)
    s = s.translate(_MW_TITLE_SAFE)
    # 再次确保无 ASCII 尖括号
    s = s.replace("<", "＜").replace(">", "＞")
    if _CJK.search(s):
        s = s.replace("!", "！").replace("?", "？")
    # 压缩空白
    s = re.sub(r"[\s\u3000]+", " ", s).strip()
    s = _TRAIL_DASH.sub("", s).strip()
    return s


def escape_wiki_template_value(s: str) -> str:
    """模板参数值转义（曲名/艺人/ver 通用）。"""
    if not s:
        return s
    out = _nfc(s)
    out = out.replace("|", "{{!}}")
    out = out.replace("{{", "&#123;&#123;").replace("}}", "&#125;&#125;")
    out = out.replace("<", "＜").replace(">", "＞")
    return out


def escape_track_title_param(s: str) -> str:
    """{{Track|{{{1}}}}} 专用：页名安全 + 模板转义。"""
    return escape_wiki_template_value(sanitize_wiki_page_title(s))


def _is_version_inner(inner: str) -> bool:
    i = inner.strip()
    if not i:
        return False
    if _META_PAREN.match(i):
        return False
    if _VER_HINT.search(i):
        return True
    if re.fullmatch(r"\d{1,2}", i):
        return True
    return False


def _is_meta_inner(inner: str) -> bool:
    return bool(_META_PAREN.match(inner.strip()))


def _paren_is_karaoke(inner: str) -> bool:
    return bool(
        re.search(
            r"(?i)カラオケ|karaoke|instrumental|off[\s-]*vocal|インスト",
            inner,
        )
    )


def _paren_is_instrumental(inner: str) -> bool:
    return bool(
        re.search(r"(?i)instrumental|off[\s-]*vocal|インスト|オフボーカル", inner)
    )


def split_track_title(raw: str | None) -> WikiTrackTitle:
    """拆分 hub 曲名 → wiki Track 参数。"""
    if not raw:
        return WikiTrackTitle(title="")
    s = _nfc(str(raw))
    tags: list[str] = []
    outro: str | None = None
    karaoke = False
    instrumental = False
    talk = False
    bgm = False

    # 1) 尾部 "-"
    s = _TRAIL_DASH.sub("", s).strip()

    # 2) 【标签】
    for m in list(_SQ_TAG.finditer(s)):
        tags.append(m.group(1).strip())
    s = _SQ_TAG.sub("", s)
    s = re.sub(r"[\s\u3000]{2,}", " ", s).strip()
    s = _TRAIL_DASH.sub("", s).strip()

    tag_blob = " ".join(tags)
    if _TALK_HINT.search(tag_blob):
        talk = True
    if _BGM_HINT.search(tag_blob):
        bgm = True
        instrumental = True

    # 3) 波折后缀（先剥，便于识别 ver.01）
    m_w = _WAVE_SUFFIX.search(s)
    if m_w and m_w.start() > 0:
        suf = m_w.group(1).strip()
        if "より" in suf or "『" in suf or "「" in suf or len(suf) <= 48:
            outro = suf
            s = s[: m_w.start()].rstrip(" \u3000")

    # 4) 括号外 karaoke
    m_k = _TRAIL_KARAOKE.search(s)
    if m_k:
        karaoke = True
        if re.search(r"(?i)instrumental|インスト|off", m_k.group(0)):
            instrumental = True
        s = s[: m_k.start()].rstrip(" \u3000")
        s = _TRAIL_DASH.sub("", s).strip()

    # 5) 从右剥括号：meta → talk；version → ver；karaoke 标记
    ver_parts: list[str] = []
    for _ in range(4):
        m = _TRAIL_PAREN.search(s)
        if not m:
            break
        inner = m.group(1).strip()
        if _is_meta_inner(inner):
            talk = True
            tags.append(inner)
            s = s[: m.start()].rstrip(" \u3000")
            s = _TRAIL_DASH.sub("", s).strip()
            continue
        if not _is_version_inner(inner):
            break
        if _paren_is_karaoke(inner):
            karaoke = True
        if _paren_is_instrumental(inner):
            instrumental = True
            karaoke = True
        ver_parts.append(inner)
        s = s[: m.start()].rstrip(" \u3000")
        s = _TRAIL_DASH.sub("", s).strip()

    ver: str | None = None
    if ver_parts:
        ordered = list(reversed(ver_parts))
        ver = (
            f"({ordered[0]})"
            if len(ordered) == 1
            else " ".join(f"({p})" for p in ordered)
        )

    # 6) 标题内关键词
    if _TALK_HINT.search(s):
        talk = True
    # 开场/终场/attraction/BGM：nolink，但不一定清空艺人
    if _BGM_HINT.search(s) or re.search(
        r"(オープニング|フィナーレ|エンディング|アトラクション)", s
    ):
        bgm = True

    if re.search(r"(?i)カラオケ|karaoke", s) and not karaoke:
        karaoke = True
    if re.search(r"(?i)\binstrumental\b|オフボーカル|オフヴォーカル", s):
        instrumental = True
        karaoke = True

    if re.match(r"^(トーク|Talk)\b", s, re.I):
        talk = True

    title = s.strip(" \u3000") or _nfc(str(raw))
    title = _TRAIL_DASH.sub("", title).strip()
    # 歌曲页安全化（L＜＞R、CJK 感叹号等）
    title = sanitize_wiki_page_title(title)

    if outro:
        outro = _nfc(outro)

    return WikiTrackTitle(
        title=title,
        ver=ver,
        karaoke=karaoke,
        talk=talk,
        instrumental=instrumental,
        bgm=bgm,
        tags=tags or None,
        outro=outro,
    )


def format_duration(ms: int | None = None, text: str | None = None) -> str:
    """mm:ss，分钟至少两位。"""
    if text:
        t = text.strip()
        if re.fullmatch(r"\d+:\d{2}", t):
            m, sec = t.split(":")
            return f"{int(m):02d}:{int(sec):02d}"
        if re.fullmatch(r"\d+:\d{2}:\d{2}", t):
            return t
    if ms is None or ms < 0:
        return "00:00"
    total = int(ms) // 1000
    return f"{total // 60:02d}:{total % 60:02d}"
