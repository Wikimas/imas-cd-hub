"""765 本家艺人署名规范（wiki 渲染共用；歌手库建成后由实体模型取代）。

目标格式（Lantis 风）:
  天海春香 (CV:中村繪里子) / 星井美希 (CV:長谷川明子)

专辑艺人（阶段 1 本家系列）:
  765PRO ALLSTARS

分隔: 「 / 」

特别注意 — 萩原雪歩:
  - 早年 CV: 落合祐里香 ＝ 長谷優里奈（同一人改艺名，MB 写哪个就保留哪个，禁止互转）
  - 后期 CV: 浅倉杏美（换声优，不是改名）
  - 解析时务必识别三种写法，不得把 長谷優里奈 强行改成 落合
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ALBUMARTIST_765 = "765PRO ALLSTARS"
ARTIST_SEP = " / "

# 角色 → 默认声优（仅在来源完全没写 CV 时使用）
# 雪歩默认早年落合；有 release_date 且 ≥2010 则用浅倉（见 resolve_yukiho_cv）
CHAR_TO_CV: dict[str, str] = {
    # 765 初期 9+1
    "天海春香": "中村繪里子",
    "如月千早": "今井麻美",
    "萩原雪歩": "落合祐里香",  # 默认早年；见雪歩专节
    "高槻やよい": "仁後真耶子",
    "秋月律子": "若林直美",
    "三浦あずさ": "たかはし智秋",
    "水瀬伊織": "釘宮理恵",
    "菊地真": "平田宏美",
    "双海亜美": "下田麻美",
    "双海真美": "下田麻美",
    "星井美希": "長谷川明子",
    # 后续加入 765
    "我那覇響": "沼倉愛美",
    "四条貴音": "原由実",
    # 事务员等偶见
    "音無小鳥": "滝田樹里",
}

# 角色别名 → 正名
CHAR_ALIASES: dict[str, str] = {
    "やよい": "高槻やよい",
    "あずさ": "三浦あずさ",
    "伊織": "水瀬伊織",
    "亜美": "双海亜美",
    "真美": "双海真美",
    "響": "我那覇響",
    "貴音": "四条貴音",
    "春香": "天海春香",
    "千早": "如月千早",
    "雪歩": "萩原雪歩",
    "律子": "秋月律子",
    "真": "菊地真",
    "美希": "星井美希",
    "みき": "星井美希",
    "双海亜美・真美": "双海亜美・真美",
    "双海亜美·真美": "双海亜美・真美",
    "双海亜美/真美": "双海亜美・真美",
}

# ---------------------------------------------------------------------------
# 萩原雪歩 CV 专节
# ---------------------------------------------------------------------------
# 早年同一人、两个艺名（禁止互相改写；MB / 本地写哪个就留哪个）
YUKIHO_EARLY_CVS = frozenset({"落合祐里香", "長谷優里奈"})
# 后期换声优
YUKIHO_LATER_CVS = frozenset({"浅倉杏美"})
YUKIHO_ALL_CVS = YUKIHO_EARLY_CVS | YUKIHO_LATER_CVS
# 本地乱标常见异体 → 仍映射到「合法雪歩 CV 集合」内的一种，但不在 落合/長谷 之间挑
YUKIHO_CV_ORTHOGRAPHY: dict[str, str] = {
    "浅仓杏美": "浅倉杏美",
    "淺倉杏美": "浅倉杏美",
    "落合祐里香": "落合祐里香",
    "長谷優里奈": "長谷優里奈",
    "长谷优里奈": "長谷優里奈",
    "長谷優里奈": "長谷優里奈",
}
# 浅倉接任约 2010 年起（无精确日时用年份）
YUKIHO_LATER_FROM = "2010-01-01"

# 仅「异体字 / 简繁」级归一；禁止艺名互转（尤其落合 ↔ 長谷）
SEIYUU_ORTHOGRAPHY: dict[str, str] = {
    "中村绘里子": "中村繪里子",
    "中村絵里子": "中村繪里子",
    "钉宫理恵": "釘宮理恵",
    "釘宮理惠": "釘宮理恵",
    "高桥智秋": "たかはし智秋",
    "高橋智秋": "たかはし智秋",
    "仁后真耶子": "仁後真耶子",
    "浅仓杏美": "浅倉杏美",
    "长谷川明子": "長谷川明子",
    "長谷川明子": "長谷川明子",
    "沼仓爱美": "沼倉愛美",
    **YUKIHO_CV_ORTHOGRAPHY,
}

# 声优 → 可能的角色（反查「声优 (角色)」或仅声优名）
CV_TO_CHARS: dict[str, list[str]] = {
    "中村繪里子": ["天海春香"],
    "今井麻美": ["如月千早"],
    "落合祐里香": ["萩原雪歩"],
    "長谷優里奈": ["萩原雪歩"],
    "浅倉杏美": ["萩原雪歩"],
    "仁後真耶子": ["高槻やよい"],
    "若林直美": ["秋月律子"],
    "たかはし智秋": ["三浦あずさ"],
    "釘宮理恵": ["水瀬伊織"],
    "平田宏美": ["菊地真"],
    "下田麻美": ["双海亜美", "双海真美"],
    "長谷川明子": ["星井美希"],
    "沼倉愛美": ["我那覇響"],
    "原由実": ["四条貴音"],
    "滝田樹里": ["音無小鳥"],
}

GROUP_NAMES = {
    "765PRO ALLSTARS": "765PRO ALLSTARS",
    "765PRO": "765PRO ALLSTARS",
    "765プロ": "765PRO ALLSTARS",
    "IM@S": "765PRO ALLSTARS",
    "THE IDOLM@STER": "765PRO ALLSTARS",
    "アイドルマスター": "765PRO ALLSTARS",
}


@dataclass
class ArtistUnit:
    character: str
    cv: str | None = None

    def format(self) -> str:
        if self.cv:
            return f"{self.character} (CV:{self.cv})"
        return self.character


def nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s).strip()


def canon_char(name: str) -> str:
    n = nfkc(name)
    n = n.strip(" 　")
    if n in CHAR_ALIASES:
        n = CHAR_ALIASES[n]
    if n in CHAR_TO_CV:
        return n
    n2 = re.sub(r"[ 　]+", "", n)
    if n2 in CHAR_TO_CV:
        return n2
    return n


def canon_seiyuu(name: str | None) -> str | None:
    """仅做异体字修正，不做艺名互转。"""
    if not name:
        return None
    n = nfkc(name)
    return SEIYUU_ORTHOGRAPHY.get(n, n)


def is_yukiho_cv(name: str | None) -> bool:
    if not name:
        return False
    return canon_seiyuu(name) in YUKIHO_ALL_CVS


def resolve_yukiho_cv(
    preferred_cv: str | None = None,
    *,
    release_date: str | None = None,
) -> str:
    """雪歩 CV 决议。

    1. 来源已写 CV（落合 / 長谷 / 浅倉）→ 原样保留（仅异体字）
    2. 未写 CV → 按发售日：≥2010 用浅倉，否则默认落合祐里香
       （注意：默认「落合」不等于把 MB 的「長谷」改成落合）
    """
    if preferred_cv:
        cv = canon_seiyuu(preferred_cv)
        assert cv is not None
        # 若写成未知异体但仍像雪歩声优，尽量归入已知集合
        if cv in YUKIHO_ALL_CVS:
            return cv
        # 模糊：含「落合」「長谷優」「浅倉/浅仓」
        raw = nfkc(preferred_cv)
        if "浅倉" in raw or "浅仓" in raw:
            return "浅倉杏美"
        if "長谷優" in raw or "长谷优" in raw:
            return "長谷優里奈"  # 保留長谷艺名
        if "落合" in raw:
            return "落合祐里香"
        return cv

    if release_date and release_date[:10] >= YUKIHO_LATER_FROM:
        return "浅倉杏美"
    return "落合祐里香"


def cv_for_character(
    character: str,
    *,
    preferred_cv: str | None = None,
    release_date: str | None = None,
) -> str | None:
    char = canon_char(character)
    if char == "萩原雪歩":
        return resolve_yukiho_cv(preferred_cv, release_date=release_date)
    if preferred_cv:
        return canon_seiyuu(preferred_cv)
    return CHAR_TO_CV.get(char)


def format_artist_line(units: list[ArtistUnit]) -> str:
    seen: set[str] = set()
    parts: list[str] = []
    for u in units:
        key = f"{u.character}|{u.cv or ''}"
        if key in seen:
            continue
        seen.add(key)
        parts.append(u.format())
    return ARTIST_SEP.join(parts)


def _expand_ami_mami(token: str) -> list[str] | None:
    t = nfkc(token).replace("·", "・").replace("/", "・")
    if re.match(r"^双海亜美\s*・\s*真美$", t) or t in {
        "双海亜美・真美",
        "双海亜美・双海真美",
        "双海亜美真美",
    }:
        return ["双海亜美", "双海真美"]
    if re.match(r"^双海\s*亜美\s*・\s*真美$", t):
        return ["双海亜美", "双海真美"]
    return None


def _is_character_name(name: str) -> bool:
    n = canon_char(name)
    return n in CHAR_TO_CV or name in CHAR_TO_CV


def _is_known_seiyuu(name: str) -> bool:
    n = canon_seiyuu(name) or name
    return n in CV_TO_CHARS or n in YUKIHO_ALL_CVS


def parse_mb_artist_credit(
    credit: list[dict] | None,
    *,
    release_date: str | None = None,
) -> list[ArtistUnit]:
    """解析 MusicBrainz artist-credit → ArtistUnit。

    雪歩：MB 写 長谷優里奈 / 落合祐里香 / 浅倉杏美 均原样进入 CV 字段。
    """
    if not credit:
        return []
    units: list[ArtistUnit] = []
    i = 0
    while i < len(credit):
        item = credit[i] or {}
        artist = item.get("artist") or {}
        name = nfkc(item.get("name") or artist.get("name") or "")
        joinphrase = item.get("joinphrase") or ""
        atype = (artist.get("type") or "").lower()
        disamb = (artist.get("disambiguation") or "").lower()

        is_char = (
            atype == "character"
            or "character" in disamb
            or _is_character_name(name)
        )

        # 团体
        if name in GROUP_NAMES:
            units.append(ArtistUnit(GROUP_NAMES[name], None))
            i += 1
            continue

        # Character [(CV: Person)]
        if is_char:
            char = canon_char(name)
            expanded = _expand_ami_mami(name) or _expand_ami_mami(char)
            jp = joinphrase.replace(" ", "")
            cv_raw = None
            if "(CV:" in jp or jp.startswith("(CV") or "CV:" in jp.upper():
                if i + 1 < len(credit):
                    nxt = credit[i + 1] or {}
                    na = nxt.get("artist") or {}
                    cv_raw = nxt.get("name") or na.get("name")
                    i += 2
                else:
                    i += 1
            else:
                i += 1

            chars = expanded or [char]
            for ch in chars:
                ch = canon_char(ch)
                units.append(
                    ArtistUnit(
                        ch,
                        cv_for_character(
                            ch, preferred_cv=cv_raw, release_date=release_date
                        ),
                    )
                )
            continue

        # 孤立 Person：若是已知声优，反查角色（少见）
        if atype == "person" or _is_known_seiyuu(name):
            cv = canon_seiyuu(name)
            chars = CV_TO_CHARS.get(cv or "", [])
            if chars:
                for ch in chars:
                    units.append(
                        ArtistUnit(
                            ch,
                            cv_for_character(
                                ch, preferred_cv=cv, release_date=release_date
                            ),
                        )
                    )
            else:
                # 作曲家等：保留人名、无角色包装
                units.append(ArtistUnit(name, None))
            i += 1
            continue

        units.append(ArtistUnit(name, None))
        i += 1

    return units


_UNIT_RE = re.compile(
    r"""
    (?P<name>[^,/、／()（）]+)
    (?:
        \s*
        [(（]
        \s*(?:CV\s*[:：]\s*)?
        (?P<paren>[^)）]+)
        [)）]
    )?
    """,
    re.VERBOSE,
)


def normalize_artist_string(
    raw: str | None,
    *,
    release_date: str | None = None,
) -> str:
    """哥伦比亚乱标等 →「角色 (CV:声优) / …」。

    雪歩括号内若是 落合/長谷/浅倉，原样保留，不互转。
    """
    if not raw or not str(raw).strip():
        return ""
    text = nfkc(str(raw))
    text = text.replace("／", "/").replace("、", ",").replace("，", ",")
    chunks = re.split(r"\s*[,/]\s*|\s+&\s+", text)
    units: list[ArtistUnit] = []

    for chunk in chunks:
        chunk = chunk.strip(" 　;；")
        if not chunk:
            continue

        head = chunk.split("(")[0].split("（")[0].strip()
        expanded = _expand_ami_mami(head)
        if expanded and "(" not in chunk and "（" not in chunk:
            for ch in expanded:
                units.append(
                    ArtistUnit(
                        ch, cv_for_character(ch, release_date=release_date)
                    )
                )
            continue

        m = _UNIT_RE.search(chunk)
        if not m:
            if _is_character_name(chunk):
                units.append(
                    ArtistUnit(
                        canon_char(chunk),
                        cv_for_character(chunk, release_date=release_date),
                    )
                )
            else:
                units.append(ArtistUnit(chunk, None))
            continue

        name = m.group("name").strip()
        paren = (m.group("paren") or "").strip() or None

        exp2 = _expand_ami_mami(name)
        if exp2:
            cv = None
            if paren and not _is_character_name(paren):
                cv = canon_seiyuu(paren)
            for ch in exp2:
                units.append(
                    ArtistUnit(
                        ch,
                        cv_for_character(
                            ch, preferred_cv=cv, release_date=release_date
                        ),
                    )
                )
            continue

        name_c = canon_char(name)
        paren_c = canon_char(paren) if paren else None

        # 角色 (声优?)
        if _is_character_name(name):
            if paren and _is_character_name(paren) and paren_c != name_c:
                units.append(
                    ArtistUnit(
                        name_c,
                        cv_for_character(name_c, release_date=release_date),
                    )
                )
                units.append(
                    ArtistUnit(
                        paren_c or paren,
                        cv_for_character(
                            paren_c or paren, release_date=release_date
                        ),
                    )
                )
                continue
            cv = paren if paren and not _is_character_name(paren) else None
            units.append(
                ArtistUnit(
                    name_c,
                    cv_for_character(
                        name_c, preferred_cv=cv, release_date=release_date
                    ),
                )
            )
            continue

        # 声优 (角色)
        if paren and _is_character_name(paren):
            units.append(
                ArtistUnit(
                    paren_c or paren,
                    cv_for_character(
                        paren_c or paren,
                        preferred_cv=name,
                        release_date=release_date,
                    ),
                )
            )
            continue

        # 仅声优名
        if _is_known_seiyuu(name):
            cv = canon_seiyuu(name)
            for ch in CV_TO_CHARS.get(cv or "", [name]):
                if ch in CHAR_TO_CV:
                    units.append(
                        ArtistUnit(
                            ch,
                            cv_for_character(
                                ch, preferred_cv=cv, release_date=release_date
                            ),
                        )
                    )
                else:
                    units.append(ArtistUnit(name, None))
            continue

        if name in GROUP_NAMES:
            units.append(ArtistUnit(GROUP_NAMES[name], None))
        elif paren:
            units.append(ArtistUnit(name, canon_seiyuu(paren)))
        else:
            units.append(ArtistUnit(name_c, cv_for_character(name_c, release_date=release_date)))

    return format_artist_line(units)


def artist_from_mb_or_local(
    mb_credit: list[dict] | None,
    local_artist: str | None = None,
    *,
    release_date: str | None = None,
) -> str:
    """优先 MB credit（保留落合/長谷原文），否则规范化本地 ARTIST。"""
    units = parse_mb_artist_credit(mb_credit, release_date=release_date)
    if units:
        line = format_artist_line(units)
        if line:
            return line
    if local_artist:
        return normalize_artist_string(local_artist, release_date=release_date)
    return ""
