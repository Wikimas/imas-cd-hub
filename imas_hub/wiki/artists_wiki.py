"""Hub 艺人标签 → wikimas {{Track}} / Album info 用声优名。

Hub:  天海春香 (CV:中村繪里子) / 萩原雪歩 (CV:長谷優里奈)
Wiki: 中村繪里子、長谷優里奈

非声优（制作人等）应走 Track 的命名参数 |artist=，避免 {{cv}} 误解析。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from imas_hub.wiki.artists_765 import (
    ARTIST_SEP,
    CV_TO_CHARS,
    YUKIHO_ALL_CVS,
    canon_seiyuu,
    nfkc,
)

# wiki 用顿号分隔声优
WIKI_ARTIST_SEP = "、"

_CV_IN_PAREN = re.compile(
    r"^(?P<char>.+?)\s*[（(]\s*(?:CV\s*[:：]\s*)?(?P<cv>[^）)]+)\s*[）)]\s*$",
    re.IGNORECASE,
)


def _is_known_seiyuu_name(name: str) -> bool:
    n = canon_seiyuu(name) or name
    return n in CV_TO_CHARS or n in YUKIHO_ALL_CVS


def seiyuu_list_from_hub_artist(artist: str | None) -> list[str]:
    """从 hub 规范艺人串抽出声优名列表（去重保序；跳过制作人）。"""
    if not artist or not str(artist).strip():
        return []
    raw = nfkc(str(artist))
    parts = re.split(r"\s*/\s*|" + re.escape(ARTIST_SEP), raw)
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _CV_IN_PAREN.match(part)
        if m:
            cv = canon_seiyuu(m.group("cv").strip()) or m.group("cv").strip()
        else:
            cv = canon_seiyuu(part) or part
            # 无 CV 括号：仅当是已知声优名才收
            if not _is_known_seiyuu_name(cv):
                continue
        if not cv or cv in seen:
            continue
        if cv.upper() in {"765PRO ALLSTARS", "765PRO", "IM@S"}:
            continue
        seen.add(cv)
        out.append(cv)
    return out


def format_wiki_artists(artist: str | None) -> str:
    """Hub artist → wiki 顿号分隔声优串；空则返回空串。"""
    return WIKI_ARTIST_SEP.join(seiyuu_list_from_hub_artist(artist))


@dataclass
class WikiArtistCredit:
    """Track 艺人字段。"""

    text: str  # 显示串
    as_named_artist: bool  # True → |artist= 命名参数（非声优，不走 {{cv}}）
    is_empty: bool = False


def track_artist_credit(artist: str | None) -> WikiArtistCredit:
    """生成 Track 用艺人参数。

    - 角色 (CV:声优) → 声优顿号串，位置参数（走 {{cv}}）
    - 纯制作人 / 编曲者 → |artist= 命名参数
    - 空 → 空
    """
    if not artist or not str(artist).strip():
        return WikiArtistCredit(text="", as_named_artist=False, is_empty=True)

    seiyuu = seiyuu_list_from_hub_artist(artist)
    if seiyuu:
        return WikiArtistCredit(
            text=WIKI_ARTIST_SEP.join(seiyuu),
            as_named_artist=False,
            is_empty=False,
        )

    # 无声优：整段作为 literal artist（制作人）
    raw = nfkc(str(artist)).strip()
    # 多人 / 分隔时改顿号
    raw = re.sub(r"\s*/\s*", WIKI_ARTIST_SEP, raw)
    if not raw:
        return WikiArtistCredit(text="", as_named_artist=False, is_empty=True)
    return WikiArtistCredit(text=raw, as_named_artist=True, is_empty=False)


def merge_album_artists(track_artists: list[str | None]) -> str:
    """多轨声优合并为 Album info |artist=（仅声优，去重保序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for a in track_artists:
        for cv in seiyuu_list_from_hub_artist(a):
            if cv not in seen:
                seen.add(cv)
                out.append(cv)
    return WIKI_ARTIST_SEP.join(out)
