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

-- —— 账户与审计（ADR 0003）：邀请码自助注册 + 签名会话（无服务端 session 表）——

CREATE TABLE IF NOT EXISTS user (
    id            INTEGER PRIMARY KEY,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,  -- 登录名，不区分大小写
    password_hash TEXT NOT NULL,                        -- pbkdf2$sha256$iter$salt$hash
    role          TEXT NOT NULL DEFAULT 'editor' CHECK (role IN ('admin', 'editor')),
    active        INTEGER NOT NULL DEFAULT 1,           -- 停用后登录/会话立即失效
    last_login_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- 一次性邀请码（管理员发号）：active=0 即已用/作废
CREATE TABLE IF NOT EXISTS invite (
    id         INTEGER PRIMARY KEY,
    code       TEXT NOT NULL UNIQUE,
    created_by INTEGER NOT NULL REFERENCES user(id) ON DELETE CASCADE,
    active     INTEGER NOT NULL DEFAULT 1,
    used_by    INTEGER REFERENCES user(id) ON DELETE SET NULL,
    used_at    TEXT,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 操作日志：谁改了什么（圈子 3-10 人，轻量记录即可）
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY,
    user_id    INTEGER REFERENCES user(id) ON DELETE SET NULL,
    action     TEXT NOT NULL,          -- shelf.create / release.edit / invite.create …
    entity     TEXT NOT NULL,          -- shelf / series / release / track / cover / user / invite / auth
    entity_id  INTEGER,
    detail     TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_log(user_id);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_log(entity, entity_id);

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
    composer         TEXT,              -- 多人 " / "
    lyricist         TEXT,
    duration_ms      INTEGER,
    mb_recording_id  TEXT,              -- 仅外链 ID
    archived         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (medium_id, position)
);

-- 演唱者实体模型（ADR 0002）：
-- 曲目署名存 track_artist；「角色 (CV:声优)」是派生显示，不再存文本列。
-- seiyuu/character 建实体；改名（艺名变更）记别名不拆实体；一角色多声优记 portrayal。
-- 团体名/工作人员等无实体署名走 track_artist.display_text 兜底。
CREATE TABLE IF NOT EXISTS seiyuu (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,   -- 正名（如 落合祐里香 / 愛美）
    note        TEXT,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS seiyuu_alias (
    id          INTEGER PRIMARY KEY,
    seiyuu_id   INTEGER NOT NULL REFERENCES seiyuu(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL UNIQUE,   -- 旧艺名 / 异体 / 简繁（長谷優里奈、寺川愛美）
    UNIQUE (seiyuu_id, alias)
);

CREATE TABLE IF NOT EXISTS character (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,   -- 日文正名（如 萩原雪歩）
    note        TEXT,
    created_at  TEXT,
    updated_at  TEXT
);

CREATE TABLE IF NOT EXISTS character_alias (
    id          INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE,
    alias       TEXT NOT NULL UNIQUE,   -- 中文译名 / 昵称（萩原雪步、雪歩、やよい）
    UNIQUE (character_id, alias)
);

CREATE TABLE IF NOT EXISTS portrayal (
    id          INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL REFERENCES character(id) ON DELETE CASCADE,
    seiyuu_id   INTEGER NOT NULL REFERENCES seiyuu(id) ON DELETE CASCADE,
    period_note TEXT,                   -- 时期备注（"2011–" 等，只做建议不裁决）
    UNIQUE (character_id, seiyuu_id)
);

CREATE TABLE IF NOT EXISTS track_artist (
    id          INTEGER PRIMARY KEY,
    track_id    INTEGER NOT NULL REFERENCES track(id) ON DELETE CASCADE,
    seiyuu_id   INTEGER REFERENCES seiyuu(id) ON DELETE RESTRICT,
    character_id INTEGER REFERENCES character(id) ON DELETE RESTRICT,
    display_text TEXT,                  -- 无实体兜底（团体/工作人员/未解析串）
    position    INTEGER NOT NULL DEFAULT 0,
    CHECK (seiyuu_id IS NOT NULL OR display_text IS NOT NULL),
    CHECK (display_text IS NULL OR (seiyuu_id IS NULL AND character_id IS NULL)),
    UNIQUE (track_id, position)
);

CREATE INDEX IF NOT EXISTS idx_track_medium ON track(medium_id);
CREATE INDEX IF NOT EXISTS idx_track_artist_track ON track_artist(track_id);
CREATE INDEX IF NOT EXISTS idx_track_artist_seiyuu ON track_artist(seiyuu_id);
CREATE INDEX IF NOT EXISTS idx_portrayal_char ON portrayal(character_id);

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
