"""演唱者文本 ⇄ 实体（ADR 0002）。

「角色 (CV:声优) / …」是派生显示，不再存文本列；解析回 track_artist 实体行。
- 「角色 (CV:声优)」：角色/声优按「正名 + 别名」解析；& 昵称后缀剥除
- 裸角色名 → 默认 portrayal 补 CV（水谷絵理 → (CV:花澤香菜)）
- 裸声优名 → 声优名义行（seiyuu 无 character）
- 解析不了的部分 → display_text 原样保留（团体/工作人员/待人工）
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from typing import Any

# 规范分隔符；历史数据可能有裸 "/"，这里只认带空格的规范分隔，避免误拆
SEP = " / "

_CV_IN_PAREN = re.compile(
    r"^(?P<char>.+?)\s*[（(]\s*(?:CV\s*[:：]\s*)?(?P<cv>[^）)]+)\s*[）)]\s*$",
    re.IGNORECASE,
)
_NICK_SUFFIX = re.compile(r"\s*&\s*.*$")  # 「天海春香 & はるかさん」→ 天海春香
_NESTED_CV = re.compile(r"[（(]\s*CV\s*[:：.].*$")  # 「高坂海美(CV.上田麗奈)」→ 高坂海美
_AMI_MAMI = re.compile(r"双海亜美\s*[・/]\s*真美")
_YEAR_RANGE = re.compile(r"(?P<start>\d{4})\s*(?:[-–—~〜]\s*(?P<end>\d{4}))?")


@dataclass
class EntityIndex:
    """实体查找表（迁移 / 保存解析 / suggest 共用，进程内加载）。"""

    seiyuu_by_name: dict[str, int]
    seiyuu_by_alias: dict[str, int]
    char_by_name: dict[str, int]
    char_by_alias: dict[str, int]
    name_by_seiyuu: dict[int, str]
    name_by_char: dict[int, str]
    portrayals: dict[int, list[tuple[int, str]]] = field(default_factory=dict)
    # character_id -> [(seiyuu_id, period_note)]，按 id 升序（种子导入序）

    def seiyuu_id(self, name: str) -> int | None:
        name = name.strip()
        if name in self.seiyuu_by_name:
            return self.seiyuu_by_name[name]
        return self.seiyuu_by_alias.get(name)

    def char_id(self, name: str) -> int | None:
        name = name.strip()
        if name in self.char_by_name:
            return self.char_by_name[name]
        return self.char_by_alias.get(name)


def load_entity_index(conn) -> EntityIndex:
    idx = EntityIndex(
        seiyuu_by_name={},
        seiyuu_by_alias={},
        char_by_name={},
        char_by_alias={},
        name_by_seiyuu={},
        name_by_char={},
    )
    for r in conn.execute("SELECT id, name FROM seiyuu"):
        idx.seiyuu_by_name[r["name"]] = int(r["id"])
        idx.name_by_seiyuu[int(r["id"])] = r["name"]
    for r in conn.execute("SELECT seiyuu_id, alias FROM seiyuu_alias"):
        idx.seiyuu_by_alias[r["alias"]] = int(r["seiyuu_id"])
    for r in conn.execute("SELECT id, name FROM character"):
        idx.char_by_name[r["name"]] = int(r["id"])
        idx.name_by_char[int(r["id"])] = r["name"]
    for r in conn.execute("SELECT character_id, alias FROM character_alias"):
        idx.char_by_alias[r["alias"]] = int(r["character_id"])
    for r in conn.execute(
        "SELECT character_id, seiyuu_id, period_note FROM portrayal ORDER BY id"
    ):
        idx.portrayals.setdefault(int(r["character_id"]), []).append(
            (int(r["seiyuu_id"]), r["period_note"] or "")
        )
    return idx


def _period_covers(period: str, year: int) -> bool:
    """period_note 形如 「2011–」「–2021.12」「2007–2011」；只按年份粗判。"""
    m = _YEAR_RANGE.search(period)
    if not m:
        return False
    start = int(m.group("start"))
    end = int(m.group("end")) if m.group("end") else None
    if end is None:
        return year >= start
    return start <= year <= end


def pick_default_seiyuu(
    idx: EntityIndex, character_id: int, release_date: str | None
) -> int | None:
    """多 portrayal 时给默认建议（不裁决）：按发行日期命中时期；否则现任（无截止），再否则首条。"""
    portrayals = idx.portrayals.get(character_id) or []
    if not portrayals:
        return None
    if len(portrayals) == 1:
        return portrayals[0][0]
    if release_date and len(release_date) >= 4:
        year = int(release_date[:4])
        for sid, period in portrayals:
            if period and _period_covers(period, year):
                return sid
    for sid, period in portrayals:  # 现任优先（无截止）
        if period and not re.search(r"[-–—~〜]\s*\d{4}", period):
            return sid
    return portrayals[0][0]


def parse_artist_text(text: str) -> list[str]:
    """按规范分隔符切分并剥离空白；双海「亜美/真美」「亜美・真美」组合拆两条。"""
    parts: list[str] = []
    for raw in str(text or "").split(SEP):
        part = raw.strip()
        if not part:
            continue
        if _AMI_MAMI.search(part):
            for twin in ("双海亜美", "双海真美"):
                parts.append(_AMI_MAMI.sub(twin, part))
        else:
            parts.append(part)
    return parts


def resolve_part(
    idx: EntityIndex, part: str, release_date: str | None
) -> tuple[int | None, int | None, str | None]:
    """→ (seiyuu_id, character_id, display_text)；恰好一类非空。"""
    m = _CV_IN_PAREN.match(part)
    if m:
        base = _NICK_SUFFIX.sub("", m.group("char")).strip()
        base = _NESTED_CV.sub("", base).strip()  # 嵌套脏数据容错
        cid = idx.char_id(base) if base else None
        sid = idx.seiyuu_id(m.group("cv").strip())
        if sid and cid:
            return sid, cid, None
        return None, None, part  # 声优或角色未知 → 原文待人工
    cid = idx.char_id(part)
    if cid is not None:
        sid = pick_default_seiyuu(idx, cid, release_date)
        if sid is not None:
            return sid, cid, None
        return None, None, part
    sid = idx.seiyuu_id(part)
    if sid is not None:
        return sid, None, None  # 声优名义
    return None, None, part


def rebuild_track_artists(
    conn, idx: EntityIndex, track_id: int, text: str | None, release_date: str | None
) -> list[dict[str, Any]]:
    """按文本重建 track_artist 行（DELETE + INSERT）；返回解析结果供回显。"""
    conn.execute("DELETE FROM track_artist WHERE track_id=?", (track_id,))
    rows: list[dict[str, Any]] = []
    for position, part in enumerate(parse_artist_text(text)):
        sid, cid, disp = resolve_part(idx, part, release_date)
        if disp is not None:
            conn.execute(
                """
                INSERT INTO track_artist(track_id, seiyuu_id, character_id, display_text, position)
                VALUES (?, NULL, NULL, ?, ?)
                """,
                (track_id, disp, position),
            )
            rows.append({"seiyuu": None, "character": None, "display_text": disp})
        else:
            conn.execute(
                """
                INSERT INTO track_artist(track_id, seiyuu_id, character_id, display_text, position)
                VALUES (?, ?, ?, NULL, ?)
                """,
                (track_id, sid, cid, position),
            )
            rows.append(
                {
                    "seiyuu": idx.name_by_seiyuu.get(sid),
                    "character": idx.name_by_char.get(cid),
                    "display_text": None,
                }
            )
    return rows


def _track_artist_rows(conn, track_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT s.name AS seiyuu, c.name AS character, ta.display_text
        FROM track_artist ta
        LEFT JOIN seiyuu s ON s.id = ta.seiyuu_id
        LEFT JOIN character c ON c.id = ta.character_id
        WHERE ta.track_id = ?
        ORDER BY ta.position
        """,
        (track_id,),
    ).fetchall()


def artist_display(conn, track_id: int) -> str:
    """派生显示：角色 (CV:声优) / 声优名 / display_text，' / ' 连接。"""
    out: list[str] = []
    for r in _track_artist_rows(conn, track_id):
        if r["seiyuu"] and r["character"]:
            out.append(f"{r['character']} (CV:{r['seiyuu']})")
        elif r["seiyuu"]:
            out.append(r["seiyuu"])
        else:
            out.append(r["display_text"] or "")
    return SEP.join(p for p in out if p)


def artists_json(conn, track_id: int) -> list[dict[str, Any]]:
    """结构化署名（导出 / wiki 渲染用）：[{seiyuu, character, display_text}]。"""
    return [
        {
            "seiyuu": r["seiyuu"],
            "character": r["character"],
            "display_text": r["display_text"],
        }
        for r in _track_artist_rows(conn, track_id)
    ]


def tracks_with_artist(
    conn, release_id: int, *, include_archived: bool = False
) -> list[dict[str, Any]]:
    """一张专辑的全部曲目；``artist`` 为派生文本，``artists`` 为结构化署名。

    供 release 页 / 导出共用（track.artist 列已随迁移删除）。
    """
    arch_clause = "" if include_archived else "AND t.archived = 0"
    rows = conn.execute(
        f"""
        SELECT t.id, t.medium_id, t.position, t.title,
               t.composer, t.lyricist, t.duration_ms, t.mb_recording_id,
               m.position AS medium_position,
               s.name AS seiyuu, c.name AS character, ta.display_text
        FROM track t
        JOIN medium m ON m.id = t.medium_id
        LEFT JOIN track_artist ta ON ta.track_id = t.id
        LEFT JOIN seiyuu s ON s.id = ta.seiyuu_id
        LEFT JOIN character c ON c.id = ta.character_id
        WHERE m.release_id = ? {arch_clause}
        ORDER BY m.position, t.position, ta.position
        """,
        (release_id,),
    ).fetchall()
    tracks: dict[int, dict[str, Any]] = {}
    order: list[int] = []
    for r in rows:
        tid = int(r["id"])
        if tid not in tracks:
            tracks[tid] = {
                "id": tid,
                "medium_id": int(r["medium_id"]),
                "position": r["position"],
                "title": r["title"],
                "composer": r["composer"],
                "lyricist": r["lyricist"],
                "duration_ms": r["duration_ms"],
                "mb_recording_id": r["mb_recording_id"],
                "medium_position": r["medium_position"],
                "artists": [],
            }
            order.append(tid)
        tr = tracks[tid]
        if r["seiyuu"] and r["character"]:
            tr["artists"].append(
                {"seiyuu": r["seiyuu"], "character": r["character"], "display_text": None}
            )
        elif r["seiyuu"]:
            tr["artists"].append(
                {"seiyuu": r["seiyuu"], "character": None, "display_text": None}
            )
        else:
            tr["artists"].append(
                {"seiyuu": None, "character": None, "display_text": r["display_text"]}
            )
    out: list[dict[str, Any]] = []
    for tid in order:
        tr = tracks[tid]
        parts: list[str] = []
        for a in tr["artists"]:
            if a["seiyuu"] and a["character"]:
                parts.append(f"{a['character']} (CV:{a['seiyuu']})")
            elif a["seiyuu"]:
                parts.append(a["seiyuu"])
            else:
                parts.append(a["display_text"] or "")
        tr["artist"] = SEP.join(p for p in parts if p)
        out.append(tr)
    return out


def migrate_all_tracks(conn, idx: EntityIndex) -> dict[str, int]:
    """迁移：全部 track.artist 文本 → track_artist（一次性，幂等由调用方守卫）。"""
    tracks = conn.execute(
        """
        SELECT t.id, t.artist, r.date_guess
        FROM track t
        JOIN medium m ON m.id = t.medium_id
        JOIN release r ON r.id = m.release_id
        WHERE t.artist IS NOT NULL AND t.artist != ''
        """
    ).fetchall()
    stats = {"tracks": 0, "parts": 0, "entities": 0, "display_text": 0}
    for t in tracks:
        parts = parse_artist_text(t["artist"])
        if not parts:
            continue
        stats["tracks"] += 1
        stats["parts"] += len(parts)
        for position, part in enumerate(parts):
            sid, cid, disp = resolve_part(idx, part, t["date_guess"])
            if disp is not None:
                conn.execute(
                    """
                    INSERT INTO track_artist(track_id, seiyuu_id, character_id, display_text, position)
                    VALUES (?, NULL, NULL, ?, ?)
                    """,
                    (int(t["id"]), disp, position),
                )
                stats["display_text"] += 1
            else:
                conn.execute(
                    """
                    INSERT INTO track_artist(track_id, seiyuu_id, character_id, display_text, position)
                    VALUES (?, ?, ?, NULL, ?)
                    """,
                    (int(t["id"]), sid, cid, position),
                )
                stats["entities"] += 1
    return stats
