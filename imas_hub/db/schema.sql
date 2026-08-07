-- 765PRO 中枢 schema（阶段 0）
-- 主数据：Release → Medium → Track；LocalFile + FileLink

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 货架 / 大区（浏览用，非 discography 线）
-- 例：00B 动画相关；本家主线 [01]–[18] 通常无 shelf
CREATE TABLE IF NOT EXISTS shelf (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,  -- "00B" / "00A"
    title       TEXT,
    folder_name TEXT,
    path        TEXT UNIQUE,           -- 本地品牌根（可空）
    sort_order  INTEGER NOT NULL DEFAULT 0,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT,
    updated_at  TEXT
);

-- 注意：旧库若已有 series 表，IF NOT EXISTS 不会加 shelf_id。
-- shelf_id 与索引由 database.migrate 增量添加。
CREATE TABLE IF NOT EXISTS series (
    id          INTEGER PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,  -- "01"…"18" 或 "00B-01"（品牌子系列）
    folder_name TEXT,                  -- 本地文件夹名（可空）
    title       TEXT,
    path        TEXT UNIQUE,           -- 本地系列根（可空）
    shelf_id    INTEGER REFERENCES shelf(id) ON DELETE SET NULL,
    archived    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS release (
    id                 INTEGER PRIMARY KEY,
    series_id          INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    folder_name        TEXT,           -- 本地文件夹名（可空）
    path               TEXT UNIQUE,    -- 本地碟根路径（可空：主库可无盘）
    title              TEXT,
    catalog_no         TEXT,
    date_guess         TEXT,              -- YYYY-MM-DD 或 NULL
    barcode            TEXT,
    label_hint         TEXT,
    genre              TEXT,              -- 自 MB release-group genres，如 J-Pop
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
    archived           INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_release_series ON release(series_id);
CREATE INDEX IF NOT EXISTS idx_release_match ON release(match_status);
CREATE INDEX IF NOT EXISTS idx_release_catalog ON release(catalog_no);
CREATE INDEX IF NOT EXISTS idx_release_integrity ON release(integrity_status);
-- 品番部分 UNIQUE：见 database._migrate_catalog_unique（有重复时跳过）

CREATE TABLE IF NOT EXISTS medium (
    id          INTEGER PRIMARY KEY,
    release_id  INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
    position    INTEGER NOT NULL DEFAULT 1,
    format      TEXT,
    path        TEXT,
    title       TEXT,
    UNIQUE (release_id, position)
);

CREATE TABLE IF NOT EXISTS track (
    id               INTEGER PRIMARY KEY,
    medium_id        INTEGER NOT NULL REFERENCES medium(id) ON DELETE CASCADE,
    position         INTEGER,
    title            TEXT,
    artist           TEXT,              -- 规范格式: 角色 (CV:声优) / …
    composer         TEXT,              -- MB work→composer，多人 " / "
    lyricist         TEXT,              -- MB work→lyricist（可选写入）
    duration_ms      INTEGER,
    mb_recording_id  TEXT,
    match_status     TEXT NOT NULL DEFAULT 'unmatched',
    local_file_id    INTEGER,  -- 主音频（角色 cd / 逐轨）
    archived         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (medium_id, position)
);

CREATE INDEX IF NOT EXISTS idx_track_medium ON track(medium_id);

CREATE TABLE IF NOT EXISTS recording (
    id               INTEGER PRIMARY KEY,
    mb_recording_id  TEXT UNIQUE,
    title            TEXT
);

CREATE TABLE IF NOT EXISTS local_file (
    id                    INTEGER PRIMARY KEY,
    path                  TEXT NOT NULL UNIQUE,
    rel_path              TEXT,                 -- 相对 CD_ROOT
    filename              TEXT NOT NULL,
    extension             TEXT,
    codec                 TEXT,
    sample_rate           INTEGER,
    bits                  INTEGER,
    channels              INTEGER,
    duration_ms           INTEGER,
    size_bytes            INTEGER,
    hash_sha256           TEXT,
    integrity             TEXT NOT NULL DEFAULT 'unknown'
                          CHECK (integrity IN (
                              'unknown', 'ok', 'bad', 'missing', 'skipped'
                          )),
    integrity_detail      TEXT,
    integrity_checked_at  TEXT,
    role_hint             TEXT NOT NULL DEFAULT 'other',
    scanned_at            TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_local_file_integrity ON local_file(integrity);
CREATE INDEX IF NOT EXISTS idx_local_file_role ON local_file(role_hint);

CREATE TABLE IF NOT EXISTS file_link (
    id          INTEGER PRIMARY KEY,
    file_id     INTEGER NOT NULL REFERENCES local_file(id) ON DELETE CASCADE,
    release_id  INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
    track_id    INTEGER REFERENCES track(id) ON DELETE SET NULL,
    medium_id   INTEGER REFERENCES medium(id) ON DELETE SET NULL,
    role        TEXT NOT NULL
                CHECK (role IN (
                    'cd', 'ort', 'lossy', 'scan', 'dvd', 'log',
                    'cover', 'cue', 'wav_image', 'other'
                )),
    UNIQUE (file_id, release_id, role)
);

CREATE INDEX IF NOT EXISTS idx_file_link_release ON file_link(release_id);
CREATE INDEX IF NOT EXISTS idx_file_link_track ON file_link(track_id);

CREATE TABLE IF NOT EXISTS cover_art (
    id          INTEGER PRIMARY KEY,
    release_id  INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
    file_id     INTEGER REFERENCES local_file(id) ON DELETE SET NULL,
    path        TEXT NOT NULL,
    preferred   INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS match_job (
    id               INTEGER PRIMARY KEY,
    release_id       INTEGER NOT NULL REFERENCES release(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'queued',
    confidence       REAL,
    candidates_json  TEXT,
    strategy         TEXT,
    raw_json         TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- 阶段 4+ 占位
CREATE TABLE IF NOT EXISTS lyric (
    id            INTEGER PRIMARY KEY,
    track_id      INTEGER REFERENCES track(id) ON DELETE SET NULL,
    recording_id  INTEGER REFERENCES recording(id) ON DELETE SET NULL,
    lang          TEXT NOT NULL DEFAULT 'zh',
    status        TEXT NOT NULL DEFAULT 'draft',
    body          TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS wiki_sync (
    id          INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id   INTEGER NOT NULL,
    page_title  TEXT,
    last_hash   TEXT,
    synced_at   TEXT
);

-- 视图由 database.migrate → _refresh_catalog_views 在列齐全后创建/重建
-- （旧库 CREATE TABLE IF NOT EXISTS 不会加 shelf_id，故不在此写死含 shelf 的 VIEW）

CREATE VIEW IF NOT EXISTS v_bad_files AS
SELECT
    lf.id AS file_id,
    lf.path,
    lf.rel_path,
    lf.filename,
    lf.codec,
    lf.integrity,
    lf.integrity_detail,
    lf.integrity_checked_at,
    r.id AS release_id,
    r.folder_name AS release_folder,
    s.code AS series_code,
    s.folder_name AS series_folder
FROM local_file lf
JOIN file_link fl ON fl.file_id = lf.id
JOIN release r ON r.id = fl.release_id
JOIN series s ON s.id = r.series_id
WHERE lf.integrity = 'bad'
  AND fl.role IN ('cd', 'wav_image', 'other');
