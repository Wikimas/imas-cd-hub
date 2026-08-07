"""SQLite 连接与初始化。"""

from __future__ import annotations

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from imas_hub.config import DB_PATH

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


def _table_sql(conn: sqlite3.Connection, table: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    return (row[0] or "") if row else ""


def _path_column_not_null(conn: sqlite3.Connection, table: str) -> bool:
    """path 列是否仍为 NOT NULL（脱钩前旧库）。"""
    for r in conn.execute(f"PRAGMA table_info({table})").fetchall():
        # cid, name, type, notnull, dflt, pk
        if str(r[1]) == "path" and int(r[3] or 0) == 1:
            return True
    return False


def _migrate_nullable_paths(conn: sqlite3.Connection) -> None:
    """release/series 的 path、folder_name 改为可空（主库可无本地绑定）。

    SQLite 不能 ALTER 去掉 NOT NULL，需重建表。外键子表靠 id 关联，重建后保留。
    """
    need_release = _path_column_not_null(conn, "release")
    need_series = _path_column_not_null(conn, "series")
    if not need_release and not need_series:
        return

    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        # 先删依赖 path 的视图，避免 DROP TABLE 时视图挂死
        conn.executescript(
            """
            DROP VIEW IF EXISTS v_series_summary;
            DROP VIEW IF EXISTS v_bad_files;
            """
        )
        if need_series:
            conn.executescript(
                """
                CREATE TABLE series__new (
                    id          INTEGER PRIMARY KEY,
                    code        TEXT NOT NULL UNIQUE,
                    folder_name TEXT,
                    title       TEXT,
                    path        TEXT UNIQUE
                );
                INSERT INTO series__new(id, code, folder_name, title, path)
                SELECT id, code, folder_name, title, path FROM series;
                DROP TABLE series;
                ALTER TABLE series__new RENAME TO series;
                """
            )
        if need_release:
            # genre 列可能尚不存在（极旧库）；迁移前 migrate 已加列
            conn.executescript(
                """
                CREATE TABLE release__new (
                    id                 INTEGER PRIMARY KEY,
                    series_id          INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
                    folder_name        TEXT,
                    path               TEXT UNIQUE,
                    title              TEXT,
                    catalog_no         TEXT,
                    date_guess         TEXT,
                    barcode            TEXT,
                    label_hint         TEXT,
                    genre              TEXT,
                    mb_release_id      TEXT,
                    match_status       TEXT NOT NULL DEFAULT 'unmatched'
                                       CHECK (match_status IN (
                                           'unmatched', 'pending', 'confirmed', 'manual', 'error'
                                       )),
                    match_confidence   REAL,
                    medium_count       INTEGER NOT NULL DEFAULT 1,
                    track_count_local  INTEGER NOT NULL DEFAULT 0,
                    audio_file_count   INTEGER NOT NULL DEFAULT 0,
                    has_cue            INTEGER NOT NULL DEFAULT 0,
                    has_cover          INTEGER NOT NULL DEFAULT 0,
                    has_scan           INTEGER NOT NULL DEFAULT 0,
                    has_dvd            INTEGER NOT NULL DEFAULT 0,
                    has_log            INTEGER NOT NULL DEFAULT 0,
                    integrity_status   TEXT NOT NULL DEFAULT 'unknown'
                                       CHECK (integrity_status IN (
                                           'unknown', 'ok', 'partial', 'bad', 'unchecked'
                                       )),
                    bad_file_count     INTEGER NOT NULL DEFAULT 0,
                    notes              TEXT,
                    scanned_at         TEXT,
                    created_at         TEXT NOT NULL,
                    updated_at         TEXT NOT NULL
                );
                INSERT INTO release__new (
                    id, series_id, folder_name, path, title, catalog_no, date_guess,
                    barcode, label_hint, genre, mb_release_id, match_status,
                    match_confidence, medium_count, track_count_local, audio_file_count,
                    has_cue, has_cover, has_scan, has_dvd, has_log,
                    integrity_status, bad_file_count, notes, scanned_at,
                    created_at, updated_at
                )
                SELECT
                    id, series_id, folder_name, path, title, catalog_no, date_guess,
                    barcode, label_hint, genre, mb_release_id, match_status,
                    match_confidence, medium_count, track_count_local, audio_file_count,
                    has_cue, has_cover, has_scan, has_dvd, has_log,
                    integrity_status, bad_file_count, notes, scanned_at,
                    created_at, updated_at
                FROM release;
                DROP TABLE release;
                ALTER TABLE release__new RENAME TO release;
                CREATE INDEX IF NOT EXISTS idx_release_series ON release(series_id);
                CREATE INDEX IF NOT EXISTS idx_release_match ON release(match_status);
                CREATE INDEX IF NOT EXISTS idx_release_catalog ON release(catalog_no);
                CREATE INDEX IF NOT EXISTS idx_release_integrity ON release(integrity_status);
                """
            )
        # 视图依赖旧定义时重建
        conn.executescript(
            """
            """
        )
        _refresh_catalog_views(conn)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def _refresh_catalog_views(conn: sqlite3.Connection) -> None:
    """重建 catalog 相关视图（shelf / series 摘要）。"""
    # shelf_id 可能尚未存在
    series_cols = _existing_columns(conn, "series")
    has_shelf_col = "shelf_id" in series_cols
    has_shelf_table = bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shelf'"
        ).fetchone()
    )
    conn.execute("DROP VIEW IF EXISTS v_series_summary")
    conn.execute("DROP VIEW IF EXISTS v_shelf_summary")
    conn.execute("DROP VIEW IF EXISTS v_bad_files")
    if has_shelf_col and has_shelf_table:
        conn.executescript(
            """
            CREATE VIEW v_series_summary AS
            SELECT
                s.id, s.code, s.folder_name, s.title, s.path, s.shelf_id,
                sh.code AS shelf_code, sh.title AS shelf_title,
                COUNT(r.id) AS release_count,
                MIN(r.date_guess) AS first_release_date,
                SUM(CASE WHEN r.match_status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_count,
                SUM(CASE WHEN r.match_status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN r.match_status = 'unmatched' THEN 1 ELSE 0 END) AS unmatched_count,
                SUM(CASE WHEN r.integrity_status = 'ok' THEN 1 ELSE 0 END) AS integrity_ok_count,
                SUM(CASE WHEN r.integrity_status IN ('bad', 'partial') THEN 1 ELSE 0 END) AS integrity_bad_count,
                SUM(COALESCE(r.bad_file_count, 0)) AS bad_file_total,
                SUM(COALESCE(r.audio_file_count, 0)) AS audio_file_total,
                SUM(CASE WHEN r.path IS NULL OR r.path = '' THEN 1 ELSE 0 END) AS fileless_count
            FROM series s
            LEFT JOIN shelf sh ON sh.id = s.shelf_id
            LEFT JOIN release r ON r.series_id = s.id AND r.archived = 0
            WHERE s.archived = 0
            GROUP BY s.id;

            CREATE VIEW v_shelf_summary AS
            SELECT
                sh.id, sh.code, sh.title, sh.folder_name, sh.path, sh.sort_order,
                COUNT(DISTINCT s.id) AS series_count,
                COUNT(r.id) AS release_count,
                SUM(CASE WHEN r.match_status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_count
            FROM shelf sh
            LEFT JOIN series s ON s.shelf_id = sh.id AND s.archived = 0
            LEFT JOIN release r ON r.series_id = s.id AND r.archived = 0
            WHERE sh.archived = 0
            GROUP BY sh.id;
            """
        )
    else:
        conn.executescript(
            """
            CREATE VIEW v_series_summary AS
            SELECT
                s.id, s.code, s.folder_name, s.title, s.path,
                NULL AS shelf_id, NULL AS shelf_code, NULL AS shelf_title,
                COUNT(r.id) AS release_count,
                MIN(r.date_guess) AS first_release_date,
                SUM(CASE WHEN r.match_status = 'confirmed' THEN 1 ELSE 0 END) AS confirmed_count,
                SUM(CASE WHEN r.match_status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN r.match_status = 'unmatched' THEN 1 ELSE 0 END) AS unmatched_count,
                SUM(CASE WHEN r.integrity_status = 'ok' THEN 1 ELSE 0 END) AS integrity_ok_count,
                SUM(CASE WHEN r.integrity_status IN ('bad', 'partial') THEN 1 ELSE 0 END) AS integrity_bad_count,
                SUM(COALESCE(r.bad_file_count, 0)) AS bad_file_total,
                SUM(COALESCE(r.audio_file_count, 0)) AS audio_file_total,
                SUM(CASE WHEN r.path IS NULL OR r.path = '' THEN 1 ELSE 0 END) AS fileless_count
            FROM series s
            LEFT JOIN release r ON r.series_id = s.id AND r.archived = 0
            WHERE s.archived = 0
            GROUP BY s.id;
            """
        )
    conn.executescript(
        """
        CREATE VIEW v_bad_files AS
        SELECT
            lf.id AS file_id, lf.path, lf.rel_path, lf.filename, lf.codec,
            lf.integrity, lf.integrity_detail, lf.integrity_checked_at,
            r.id AS release_id, r.folder_name AS release_folder,
            s.code AS series_code, s.folder_name AS series_folder
        FROM local_file lf
        JOIN file_link fl ON fl.file_id = lf.id
        JOIN release r ON r.id = fl.release_id
        JOIN series s ON s.id = r.series_id
        WHERE lf.integrity = 'bad'
          AND fl.role IN ('cd', 'wav_image', 'other');
        """
    )


def _migrate_shelf_model(conn: sqlite3.Connection) -> None:
    """方案 A：shelf 货架 + series 挂 shelf_id；拆分扁平 00X 桶。"""
    # shelf 表（schema.sql IF NOT EXISTS 可能已建）
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS shelf (
            id          INTEGER PRIMARY KEY,
            code        TEXT NOT NULL UNIQUE,
            title       TEXT,
            folder_name TEXT,
            path        TEXT UNIQUE,
            sort_order  INTEGER NOT NULL DEFAULT 0,
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

    # 数据：把「整桶当 series」的 00X 拆成 shelf + 子 series
    _split_flat_extended_series(conn)
    # 本家主线 01–18 → shelf MAIN「本家主系列」；统一货架展示名
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
                    INSERT INTO shelf(
                        code, title, folder_name, path, sort_order, created_at, updated_at
                    )
                    VALUES (?, ?, NULL, NULL, ?, ?, ?)
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


def _split_flat_extended_series(conn: sqlite3.Connection) -> None:
    """series.code 为 00A–00Z 且其下 release 的 path 含子分区时，拆成 00B-01 等。"""
    import re
    from pathlib import Path

    now = utc_now()
    buckets = conn.execute(
        """
        SELECT id, code, folder_name, title, path FROM series
        WHERE code GLOB '00[A-Za-z]'
        """
    ).fetchall()
    sub_re = re.compile(r"^\[(?P<sub>\d{2})\]\s*(?P<title>.*)$")

    for b in buckets:
        shelf_code = str(b["code"]).upper()
        # upsert shelf（展示名用逻辑标题，如 00B→本家动画）
        conn.execute(
            """
            INSERT INTO shelf(code, title, folder_name, path, sort_order, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(code) DO UPDATE SET
                title=excluded.title,
                folder_name=COALESCE(excluded.folder_name, shelf.folder_name),
                path=COALESCE(excluded.path, shelf.path),
                sort_order=excluded.sort_order,
                updated_at=excluded.updated_at
            """,
            (
                shelf_code,
                shelf_display_title(shelf_code, b["title"] or shelf_code),
                b["folder_name"],
                b["path"],
                shelf_sort_order(shelf_code),
                now,
                now,
            ),
        )
        shelf_id = int(
            conn.execute(
                "SELECT id FROM shelf WHERE code=?", (shelf_code,)
            ).fetchone()["id"]
        )

        releases = conn.execute(
            "SELECT id, path, folder_name FROM release WHERE series_id=?",
            (int(b["id"]),),
        ).fetchall()
        if not releases:
            # 空桶：删除旧 series（货架已建）
            conn.execute("DELETE FROM series WHERE id=?", (int(b["id"]),))
            continue

        # sub_key -> series_id
        sub_map: dict[str, int] = {}
        for rel in releases:
            path = rel["path"] or ""
            sub_folder = None
            sub_code = "00"
            sub_title = "未分类"
            sub_path = None
            if path:
                p = Path(path)
                parent = p.parent
                # 期望 …/00B货架/子分区/碟
                if parent and parent.name:
                    m = sub_re.match(parent.name)
                    if m:
                        sub_code = m.group("sub")
                        sub_title = (m.group("title") or "").strip() or parent.name
                        sub_folder = parent.name
                        sub_path = str(parent)
                    else:
                        # 碟直接在货架下
                        sub_code = "00"
                        sub_title = "未分类"
                        sub_folder = None
                        sub_path = None
            series_code = f"{shelf_code}-{sub_code}"
            if series_code not in sub_map:
                conn.execute(
                    """
                    INSERT INTO series(code, folder_name, title, path, shelf_id)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(code) DO UPDATE SET
                        folder_name=COALESCE(excluded.folder_name, series.folder_name),
                        title=COALESCE(excluded.title, series.title),
                        path=COALESCE(excluded.path, series.path),
                        shelf_id=excluded.shelf_id
                    """,
                    (series_code, sub_folder, sub_title, sub_path, shelf_id),
                )
                sid = int(
                    conn.execute(
                        "SELECT id FROM series WHERE code=?", (series_code,)
                    ).fetchone()["id"]
                )
                sub_map[series_code] = sid
            conn.execute(
                "UPDATE release SET series_id=? WHERE id=?",
                (sub_map[series_code], int(rel["id"])),
            )

        # 旧 00B series 行若已无 release，删除
        left = conn.execute(
            "SELECT COUNT(*) AS c FROM release WHERE series_id=?",
            (int(b["id"]),),
        ).fetchone()["c"]
        if int(left) == 0:
            conn.execute("DELETE FROM series WHERE id=?", (int(b["id"]),))


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


def migrate(conn: sqlite3.Connection) -> None:
    """增量列迁移（CREATE IF NOT EXISTS 不会加新列）。"""
    track_cols = _existing_columns(conn, "track")
    if "artist" not in track_cols:
        conn.execute("ALTER TABLE track ADD COLUMN artist TEXT")
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
    _migrate_nullable_paths(conn)
    _migrate_shelf_model(conn)
    _migrate_catalog_unique(conn)


def init_db(db_path: Path | None = None) -> Path:
    path = Path(db_path or DB_PATH)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with connect(path) as conn:
        conn.executescript(schema)
        migrate(conn)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("schema_version", "0.6.0"),
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
