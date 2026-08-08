"""SQLite 连接与初始化。"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from imas_hub.config import DB_PATH
from imas_hub.artists.parse import load_entity_index, migrate_all_tracks

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# 逻辑货架种子（原 scan.signals；扫盘模块已退出项目，保留给迁移/视图刷新）
SHELF_META: dict[str, tuple[str, int]] = {
    "MAIN": ("本家主系列", 10),
    "00B": ("本家动画", 20),
    # 预留：00A / 00C … 推进时再补标题
}

MAIN_SHELF_CODE = "MAIN"


def shelf_display_title(code: str, fallback: str | None = None) -> str:
    c = str(code or "").strip().upper()
    if c in SHELF_META:
        return SHELF_META[c][0]
    return (fallback or c).strip() or c


def shelf_sort_order(code: str) -> int:
    c = str(code or "").strip().upper()
    if c in SHELF_META:
        return SHELF_META[c][1]
    # 未知 00X 货架排在主系列之后、已知动画附近
    if re.fullmatch(r"00[A-Za-z]", c):
        return 50 + (ord(c[-1]) - ord("A"))
    return 100


def is_primary_series_code(code: str | None) -> bool:
    """首期数字系列 ``01``–``18``。"""
    if not code:
        return False
    try:
        n = int(str(code).strip())
    except ValueError:
        return False
    return 1 <= n <= 18


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path or DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _release_has_match_status(conn: sqlite3.Connection) -> bool:
    """旧库特征：release 仍有五态 match_status（脱钩迁移未执行）。"""
    return "match_status" in _existing_columns(conn, "release")


def _migrate_decouple(conn: sqlite3.Connection) -> None:
    """ADR 0001 数据脱钩：一次性把旧五态库迁到三态、删除本地文件/匹配体系。

    - 删表：local_file / file_link / match_job / recording / lyric（+ v_bad_files）
    - release/series/shelf/medium/track/cover_art 重建，去路径、扫描、匹配字段
    - match_status → review_status 三态：confirmed→unreviewed，manual/pending→needs_fill
    - cover_art 去 file_id；mb_release_id / mb_recording_id 保留为外链 ID
    """
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript(
            """
            BEGIN IMMEDIATE;
            DROP VIEW IF EXISTS v_series_summary;
            DROP VIEW IF EXISTS v_shelf_summary;
            DROP VIEW IF EXISTS v_bad_files;

            -- release：去路径/扫描/匹配字段，五态 → 三态
            CREATE TABLE release__new (
                id            INTEGER PRIMARY KEY,
                series_id     INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                title         TEXT,
                catalog_no    TEXT,
                date_guess    TEXT,
                barcode       TEXT,
                label_hint    TEXT,
                genre         TEXT,
                mb_release_id TEXT,
                review_status TEXT NOT NULL DEFAULT 'unreviewed'
                               CHECK (review_status IN (
                                   'unreviewed', 'needs_fill', 'reviewed'
                               )),
                medium_count  INTEGER NOT NULL DEFAULT 1,
                has_cover     INTEGER NOT NULL DEFAULT 0,
                notes         TEXT,
                archived      INTEGER NOT NULL DEFAULT 0,
                created_at    TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            );
            INSERT INTO release__new (
                id, series_id, title, catalog_no, date_guess,
                barcode, label_hint, genre, mb_release_id, review_status,
                medium_count, has_cover, notes, archived, created_at, updated_at
            )
            SELECT
                id, series_id, title, catalog_no, date_guess,
                barcode, label_hint, genre, mb_release_id,
                CASE match_status
                    WHEN 'confirmed' THEN 'unreviewed'
                    WHEN 'manual'    THEN 'needs_fill'
                    WHEN 'pending'   THEN 'needs_fill'
                    ELSE 'needs_fill'
                END,
                medium_count, has_cover, notes, archived, created_at, updated_at
            FROM release;
            DROP TABLE release;
            ALTER TABLE release__new RENAME TO release;
            CREATE INDEX IF NOT EXISTS idx_release_series ON release(series_id);
            CREATE INDEX IF NOT EXISTS idx_release_review ON release(review_status);
            CREATE INDEX IF NOT EXISTS idx_release_catalog ON release(catalog_no);

            -- series / shelf：去本地路径字段
            CREATE TABLE series__new (
                id       INTEGER PRIMARY KEY,
                code     TEXT NOT NULL UNIQUE,
                title    TEXT,
                shelf_id INTEGER REFERENCES shelf(id) ON DELETE SET NULL,
                archived INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO series__new(id, code, title, shelf_id, archived)
            SELECT id, code, title, shelf_id, archived FROM series;
            DROP TABLE series;
            ALTER TABLE series__new RENAME TO series;
            CREATE INDEX IF NOT EXISTS idx_series_shelf ON series(shelf_id);

            CREATE TABLE shelf__new (
                id         INTEGER PRIMARY KEY,
                code       TEXT NOT NULL UNIQUE,
                title      TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                archived   INTEGER NOT NULL DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );
            INSERT INTO shelf__new(id, code, title, sort_order, archived, created_at, updated_at)
            SELECT id, code, title, sort_order, archived, created_at, updated_at FROM shelf;
            DROP TABLE shelf;
            ALTER TABLE shelf__new RENAME TO shelf;

            -- medium / track：去路径与匹配字段
            CREATE TABLE medium__new (
                id          INTEGER PRIMARY KEY,
                release_id  INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                position    INTEGER NOT NULL DEFAULT 1,
                format      TEXT,
                title       TEXT,
                UNIQUE (release_id, position)
            );
            INSERT INTO medium__new(id, release_id, position, format, title)
            SELECT id, release_id, position, format, title FROM medium;
            DROP TABLE medium;
            ALTER TABLE medium__new RENAME TO medium;

            CREATE TABLE track__new (
                id               INTEGER PRIMARY KEY,
                medium_id        INTEGER NOT NULL REFERENCES medium(id) ON DELETE CASCADE,
                position         INTEGER,
                title            TEXT,
                artist           TEXT,
                composer         TEXT,
                lyricist         TEXT,
                duration_ms      INTEGER,
                mb_recording_id  TEXT,
                archived         INTEGER NOT NULL DEFAULT 0,
                UNIQUE (medium_id, position)
            );
            INSERT INTO track__new(
                id, medium_id, position, title, artist, composer, lyricist,
                duration_ms, mb_recording_id, archived
            )
            SELECT
                id, medium_id, position, title, artist, composer, lyricist,
                duration_ms, mb_recording_id, archived
            FROM track;
            DROP TABLE track;
            ALTER TABLE track__new RENAME TO track;
            CREATE INDEX IF NOT EXISTS idx_track_medium ON track(medium_id);

            -- cover_art：去 file_id（封面归主库目录，不绑本地文件）
            CREATE TABLE cover_art__new (
                id          INTEGER PRIMARY KEY,
                release_id  INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
                path        TEXT NOT NULL,
                preferred   INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO cover_art__new(id, release_id, path, preferred)
            SELECT id, release_id, path, preferred FROM cover_art;
            DROP TABLE cover_art;
            ALTER TABLE cover_art__new RENAME TO cover_art;

            -- 本地文件 / 匹配体系整表删除
            DROP TABLE IF EXISTS local_file;
            DROP TABLE IF EXISTS file_link;
            DROP TABLE IF EXISTS match_job;
            DROP TABLE IF EXISTS recording;
            DROP TABLE IF EXISTS lyric;
            COMMIT;
            """
        )
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _refresh_catalog_views(conn: sqlite3.Connection) -> None:
    """重建 catalog 视图（shelf / series 摘要，三态计数）。"""
    conn.execute("DROP VIEW IF EXISTS v_series_summary")
    conn.execute("DROP VIEW IF EXISTS v_shelf_summary")
    conn.executescript(
        """
        CREATE VIEW v_series_summary AS
        SELECT
            s.id, s.code, s.title, s.shelf_id,
            sh.code AS shelf_code, sh.title AS shelf_title,
            COUNT(r.id) AS release_count,
            MIN(r.date_guess) AS first_release_date,
            SUM(CASE WHEN r.review_status = 'unreviewed' THEN 1 ELSE 0 END) AS unreviewed_count,
            SUM(CASE WHEN r.review_status = 'needs_fill' THEN 1 ELSE 0 END) AS needs_fill_count,
            SUM(CASE WHEN r.review_status = 'reviewed' THEN 1 ELSE 0 END) AS reviewed_count
        FROM series s
        LEFT JOIN shelf sh ON sh.id = s.shelf_id
        LEFT JOIN release r ON r.series_id = s.id AND r.archived = 0
        WHERE s.archived = 0
        GROUP BY s.id;

        CREATE VIEW v_shelf_summary AS
        SELECT
            sh.id, sh.code, sh.title, sh.sort_order,
            COUNT(DISTINCT s.id) AS series_count,
            COUNT(r.id) AS release_count,
            SUM(CASE WHEN r.review_status = 'unreviewed' THEN 1 ELSE 0 END) AS unreviewed_count,
            SUM(CASE WHEN r.review_status = 'needs_fill' THEN 1 ELSE 0 END) AS needs_fill_count,
            SUM(CASE WHEN r.review_status = 'reviewed' THEN 1 ELSE 0 END) AS reviewed_count
        FROM shelf sh
        LEFT JOIN series s ON s.shelf_id = sh.id AND s.archived = 0
        LEFT JOIN release r ON r.series_id = s.id AND r.archived = 0
        WHERE sh.archived = 0
        GROUP BY sh.id;
        """
    )


def _migrate_shelf_model(conn: sqlite3.Connection) -> None:
    """shelf 货架 + series 挂 shelf_id（脱钩后无路径，不再按路径拆桶）。"""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shelf (
            id          INTEGER PRIMARY KEY,
            code        TEXT NOT NULL UNIQUE,
            title       TEXT,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            archived    INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT,
            updated_at  TEXT
        )
        """
    )
    series_cols = _existing_columns(conn, "series")
    if "shelf_id" not in series_cols:
        conn.execute(
            "ALTER TABLE series ADD COLUMN shelf_id INTEGER "
            "REFERENCES shelf(id) ON DELETE SET NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_series_shelf ON series(shelf_id)"
        )
    _ensure_home_shelves(conn)
    _refresh_catalog_views(conn)


def _ensure_home_shelves(conn: sqlite3.Connection) -> None:
    """冷启动种子货架（只跑一次）；之后尊重用户对编码/标题的修改。

    - 未 seed 且库内无 shelf → 写入 SHELF_META（MAIN / 00B…）
    - 未 seed 但已有 shelf（含用户改名后的 765MAIN）→ 只打标记，**不**重生 MAIN/00B
    - 仅把 shelf_id 为空的主线系列挂到 **仍存在的** MAIN；已挂靠的不动
    """

    now = utc_now()
    seeded = conn.execute(
        "SELECT value FROM meta WHERE key='home_shelves_seeded'"
    ).fetchone()
    if not seeded:
        n_shelf = int(conn.execute("SELECT COUNT(*) FROM shelf").fetchone()[0])
        if n_shelf == 0:
            for code, (title, sort) in SHELF_META.items():
                conn.execute(
                    """
                    INSERT INTO shelf(code, title, sort_order, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (code, title, sort, now, now),
                )
        conn.execute(
            """
            INSERT INTO meta(key, value) VALUES ('home_shelves_seeded', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (now,),
        )

    main = conn.execute(
        "SELECT id FROM shelf WHERE code=?", (MAIN_SHELF_CODE,)
    ).fetchone()
    if not main:
        return
    main_id = int(main["id"])
    rows = conn.execute(
        "SELECT id, code FROM series WHERE shelf_id IS NULL"
    ).fetchall()
    for r in rows:
        if is_primary_series_code(r["code"]):
            conn.execute(
                "UPDATE series SET shelf_id=? WHERE id=?",
                (main_id, int(r["id"])),
            )


def _migrate_catalog_unique(conn: sqlite3.Connection) -> None:
    """非空品番全局唯一（允许多条无品番）。有重复时跳过，避免卡死启动。"""
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='uq_release_catalog_no'"
    ).fetchone()
    if exists:
        return
    dups = conn.execute(
        """
        SELECT catalog_no, COUNT(*) AS c
        FROM release
        WHERE catalog_no IS NOT NULL AND catalog_no != ''
        GROUP BY catalog_no
        HAVING c > 1
        LIMIT 1
        """
    ).fetchone()
    if dups:
        return
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS uq_release_catalog_no
        ON release(catalog_no)
        WHERE catalog_no IS NOT NULL AND catalog_no != ''
        """
    )


def _migrate_artist_entities(conn: sqlite3.Connection) -> None:
    """ADR 0002 演唱者实体：建 6 表；track.artist 文本迁进 track_artist 后删列。

    依赖顺序：表建好 → 种子导入（meta artist_seed_v1，见 imas_hub.artists.seed）
    → 本函数解析全部曲目 → drop track.artist。
    种子未导入时只建表等待；track_artist 已有数据时只删列。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS seiyuu (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            note        TEXT,
            created_at  TEXT,
            updated_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS seiyuu_alias (
            id          INTEGER PRIMARY KEY,
            seiyuu_id   INTEGER NOT NULL REFERENCES seiyuu(id) ON DELETE CASCADE,
            alias       TEXT NOT NULL UNIQUE,
            UNIQUE (seiyuu_id, alias)
        );
        CREATE TABLE IF NOT EXISTS character (
            id          INTEGER PRIMARY KEY,
            name        TEXT NOT NULL UNIQUE,
            note        TEXT,
            created_at  TEXT,
            updated_at  TEXT
        );
        CREATE TABLE IF NOT EXISTS character_alias (
            id           INTEGER PRIMARY KEY,
            character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE,
            alias        TEXT NOT NULL UNIQUE,
            UNIQUE (character_id, alias)
        );
        CREATE TABLE IF NOT EXISTS portrayal (
            id           INTEGER PRIMARY KEY,
            character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE,
            seiyuu_id    INTEGER NOT NULL REFERENCES seiyuu(id) ON DELETE CASCADE,
            period_note  TEXT,
            UNIQUE (character_id, seiyuu_id)
        );
        CREATE TABLE IF NOT EXISTS track_artist (
            id           INTEGER PRIMARY KEY,
            track_id     INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
            seiyuu_id    INTEGER REFERENCES seiyuu(id) ON DELETE RESTRICT,
            character_id INTEGER REFERENCES character(id) ON DELETE RESTRICT,
            display_text TEXT,
            position     INTEGER NOT NULL DEFAULT 0,
            CHECK (seiyuu_id IS NOT NULL OR display_text IS NOT NULL),
            CHECK (display_text IS NULL OR (seiyuu_id IS NULL AND character_id IS NULL)),
            UNIQUE (track_id, position)
        );
        CREATE INDEX IF NOT EXISTS idx_track_artist_track ON track_artist(track_id);
        CREATE INDEX IF NOT EXISTS idx_track_artist_seiyuu ON track_artist(seiyuu_id);
        CREATE INDEX IF NOT EXISTS idx_portrayal_char ON portrayal(character_id);
        """
    )
    track_cols = _existing_columns(conn, "track")
    if "artist" not in track_cols:
        return  # 已是实体模型库
    n_parsed = int(conn.execute("SELECT COUNT(*) FROM track_artist").fetchone()[0])
    if n_parsed > 0:
        conn.execute("ALTER TABLE track DROP COLUMN artist")
        return
    n_seiyuu = int(conn.execute("SELECT COUNT(*) FROM seiyuu").fetchone()[0])
    if n_seiyuu == 0:
        return  # 种子未导入（python -m imas_hub.artists.seed），等待
    stats = migrate_all_tracks(conn, load_entity_index(conn))
    conn.execute("ALTER TABLE track DROP COLUMN artist")
    print(
        f"[migrate] 演唱者迁移: {stats['tracks']} 曲 / {stats['parts']} 署名，"
        f"实体 {stats['entities']} / display_text {stats['display_text']}"
    )


def migrate(conn: sqlite3.Connection) -> None:
    """增量迁移（幂等）：补列 → 数据脱钩（一次性）→ 货架 → 品番唯一 → 演唱者实体 → 视图。"""
    track_cols = _existing_columns(conn, "track")
    # 注意：track.artist 是历史列（ADR 0002 后不再存在），这里不补——
    # 补回来会被 _migrate_artist_entities 再 DROP，纯空转。
    if "composer" not in track_cols:
        conn.execute("ALTER TABLE track ADD COLUMN composer TEXT")
    if "lyricist" not in track_cols:
        conn.execute("ALTER TABLE track ADD COLUMN lyricist TEXT")
    rel_cols = _existing_columns(conn, "release")
    if "genre" not in rel_cols:
        conn.execute("ALTER TABLE release ADD COLUMN genre TEXT")
    if "archived" not in rel_cols:
        conn.execute("ALTER TABLE release ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    series_cols = _existing_columns(conn, "series")
    if "archived" not in series_cols:
        conn.execute("ALTER TABLE series ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    shelf_cols = _existing_columns(conn, "shelf")
    if "archived" not in shelf_cols:
        conn.execute("ALTER TABLE shelf ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
    track_cols = _existing_columns(conn, "track")
    if "archived" not in track_cols:
        conn.execute("ALTER TABLE track ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")

    if _release_has_match_status(conn):
        _migrate_decouple(conn)
    _migrate_shelf_model(conn)
    _migrate_catalog_unique(conn)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_release_review ON release(review_status)"
    )
    _migrate_artist_entities(conn)
    _refresh_catalog_views(conn)


def init_db(db_path: Path | None = None) -> Path:
    path = Path(db_path or DB_PATH)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(path) as conn:
        conn.executescript(schema)
        migrate(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("schema_version", "0.8.0"),
        )
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("initialized_at", utc_now()),
        )
        conn.commit()
    return path


@contextmanager
def get_db(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]
