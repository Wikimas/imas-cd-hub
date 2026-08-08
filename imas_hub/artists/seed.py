"""歌手库种子导入（ADR 0002）。

seed.json（由 .scratch/seiyuu-seed/make_seed.py 生成，用户本地数据，不入仓库）
→ seiyuu / seiyuu_alias / character / character_alias / portrayal。

幂等：meta 键 ``artist_seed_v1`` 守卫；已导入则跳过。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from imas_hub.db.database import utc_now

SEED_META_KEY = "artist_seed_v1"


def load_seed(conn, seed_path: str | Path) -> dict[str, int]:
    """导入种子（幂等）。返回导入统计；已导入过则原样返回计数 0 记录。"""
    seeded = conn.execute(
        "SELECT value FROM meta WHERE key=?", (SEED_META_KEY,)
    ).fetchone()
    if seeded:
        return {"seiyuu": 0, "characters": 0, "skipped": True}

    data = json.loads(Path(seed_path).read_text(encoding="utf-8"))
    now = utc_now()

    seiyuu_ids: dict[str, int] = {}
    for item in data["seiyuu"]:
        cur = conn.execute(
            "INSERT INTO seiyuu(name, created_at, updated_at) VALUES (?, ?, ?)",
            (item["name"], now, now),
        )
        seiyuu_ids[item["name"]] = int(cur.lastrowid)
        for alias in item.get("aliases", []):
            conn.execute(
                "INSERT INTO seiyuu_alias(seiyuu_id, alias) VALUES (?, ?)",
                (seiyuu_ids[item["name"]], alias),
            )

    char_ids: dict[str, int] = {}
    for item in data["characters"]:
        cur = conn.execute(
            "INSERT INTO character(name, created_at, updated_at) VALUES (?, ?, ?)",
            (item["name"], now, now),
        )
        char_ids[item["name"]] = int(cur.lastrowid)
        for alias in item.get("aliases", []):
            conn.execute(
                "INSERT INTO character_alias(character_id, alias) VALUES (?, ?)",
                (char_ids[item["name"]], alias),
            )
        for p in item.get("portrayals", []):
            conn.execute(
                """
                INSERT INTO portrayal(character_id, seiyuu_id, period_note)
                VALUES (?, ?, ?)
                """,
                (char_ids[item["name"]], seiyuu_ids[p["seiyuu"]], p.get("period")),
            )

    conn.execute(
        "INSERT INTO meta(key, value) VALUES (?, ?)", (SEED_META_KEY, now)
    )
    return {"seiyuu": len(seiyuu_ids), "characters": len(char_ids), "skipped": False}


def require_seed(seed_path: str | Path) -> dict[str, Any]:
    """供一次性调用：python -m imas_hub.artists.seed <seed.json>"""
    from imas_hub.db.database import get_db, init_db

    init_db()  # 建实体表 + 迁移（此时 seiyuu 为空 → 只建表，不解析/不删列）
    with get_db() as conn:
        stats = load_seed(conn, seed_path)
    return stats


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("用法: python -m imas_hub.artists.seed <seed.json>")
    result = require_seed(sys.argv[1])
    if result.get("skipped"):
        print("已导入过（artist_seed_v1 存在），跳过")
    else:
        print(f"种子导入完成：声优 {result['seiyuu']}，角色 {result['characters']}")
