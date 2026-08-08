"""Hub 结构化署名 → wikimas {{Track}} / Album info 用声优名（ADR 0002）。

Hub: track.artists = [{"seiyuu": "中村繪里子", "character": "天海春香", "display_text": null}]
Wiki: 中村繪里子、長谷優里奈（声优顿号串，走 {{cv}} 位置参数）

非声优（制作人等 display_text 行）应走 Track 的命名参数 |artist=，避免 {{cv}} 误解析。
"""

from __future__ import annotations

from dataclasses import dataclass

# wiki 用顿号分隔声优
WIKI_ARTIST_SEP = "、"


def seiyuu_list_from_artists(artists: list[dict] | None) -> list[str]:
    """结构化署名 → 声优名列表（去重保序；display_text 行天然排除）。"""
    out: list[str] = []
    seen: set[str] = set()
    for a in artists or []:
        cv = a.get("seiyuu")
        if not cv or cv in seen:
            continue
        seen.add(cv)
        out.append(cv)
    return out


@dataclass
class WikiArtistCredit:
    """Track 艺人字段。"""

    text: str  # 显示串
    as_named_artist: bool  # True → |artist= 命名参数（非声优，不走 {{cv}}）
    is_empty: bool = False


def track_artist_credit(artists: list[dict] | None) -> WikiArtistCredit:
    """生成 Track 用艺人参数。

    - 有声优行 → 声优顿号串，位置参数（走 {{cv}}）
    - 纯制作人 / 团体 → display_text 顿号串，|artist= 命名参数
    - 空 → 空
    """
    seiyuu = seiyuu_list_from_artists(artists)
    if seiyuu:
        return WikiArtistCredit(
            text=WIKI_ARTIST_SEP.join(seiyuu),
            as_named_artist=False,
            is_empty=False,
        )

    texts = [a.get("display_text") for a in (artists or []) if a.get("display_text")]
    if not texts:
        return WikiArtistCredit(text="", as_named_artist=False, is_empty=True)
    return WikiArtistCredit(
        text=WIKI_ARTIST_SEP.join(texts),
        as_named_artist=True,
        is_empty=False,
    )


def merge_album_artists(track_artists: list[list[dict] | None]) -> str:
    """多轨声优合并为 Album info |artist=（仅声优，去重保序）。"""
    out: list[str] = []
    seen: set[str] = set()
    for artists in track_artists:
        for cv in seiyuu_list_from_artists(artists):
            if cv not in seen:
                seen.add(cv)
                out.append(cv)
    return WIKI_ARTIST_SEP.join(out)
