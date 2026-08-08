-- 765PRO 中枢 schema（脱钩后）
-- 主数据：Brand(Shelf) → Series → Release → Medium → Track；CoverArt 走主库封面目录。
-- 本地文件 / 匹配体系（local_file / file_link / match_job / recording / v_bad_files）已随
-- ADR 0001 数据脱钩迁移删除；主库不绑本地 path，维护只走 Web。

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 品牌（浏览用，非 discography 线）
-- 例：MAIN 本家主系列、00B 本家动画、MILLION …
CREATE TABLE IF NOT EXISTS shelf (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,  -- "MAIN" / "00B"
    title       TEXT,
    sort_order  INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS series (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,  -- "01"…"18" 或 "00B-01"（品牌子系列）
    title       TEXT,
    shelf_id    INTEGER REFERENCES shelf(id) ON DELETE SET NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS release (
    id            INTEGER PRIMARY KEY,
    series_id     INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    title         TEXT,
    catalog_no    TEXT,
    date_guess    TEXT,              -- YYYY-MM-DD 或 NULL
    barcode       TEXT,
    label_hint    TEXT,
    genre         TEXT,              -- 自 MB release-group genres，如 J-Pop
    mb_release_id TEXT,              -- 仅外链 ID，不覆盖正式字段
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

CREATE INDEX IF NOT EXISTS idx_release_series ON release(series_id);
CREATE INDEX IF NOT EXISTS idx_release_catalog ON release(catalog_no);
-- idx_release_review 由 database.migrate 创建（脱钩迁移重建表后）
-- 品番部分 UNIQUE：见 database._migrate_catalog_unique（有重复时跳过）

CREATE TABLE IF NOT EXISTS medium (
    id          INTEGER PRIMARY KEY,
    release_id  INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 1,
    format      TEXT,
    title       TEXT,
    UNIQUE (release_id, position)
);

CREATE TABLE IF NOT EXISTS track (
    id               INTEGER PRIMARY KEY,
    medium_id        INTEGER NOT NULL REFERENCES medium(id) ON DELETE CASCADE,
    position         INTEGER,
    title            TEXT,
    artist           TEXT,              -- 规范格式: 角色 (CV:声优) / …
    composer         TEXT,              -- 多人 " / "
    lyricist         TEXT,
    duration_ms      INTEGER,
    mb_recording_id  TEXT,              -- 仅外链 ID
    archived         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (medium_id, position)
);

CREATE INDEX IF NOT EXISTS idx_track_medium ON track(medium_id);

CREATE TABLE IF NOT EXISTS cover_art (
    id          INTEGER PRIMARY KEY,
    release_id  INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,
    preferred   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS wiki_sync (
    id          INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER NOT NULL,
    page_title  TEXT,
    last_hash   TEXT,
    synced_at   TEXT
);

-- 视图由 database.migrate → _refresh_catalog_views 创建/重建（三态计数）
