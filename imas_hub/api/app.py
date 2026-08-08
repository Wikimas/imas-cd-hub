"""Web 前端：偶像大师 CD 元数据目录（品牌 / 系列 / 专辑 / 曲目 / 封面 / Wiki / 导出）。"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from imas_hub import __version__
from imas_hub.auth import (
    SESSION_COOKIE,
    csrf_origin_ok,
    record_audit,
    require_login,
    user_from_request,
)
from imas_hub.config import DB_PATH, WIKI_PASS, WIKI_URL, WIKI_USER
from imas_hub.db.database import connect, init_db, rows_to_dicts

TEMPLATES_DIR = Path(__file__).with_name("templates")
STATIC_DIR = Path(__file__).with_name("static")

app = FastAPI(title="IMAS CD Hub", version=__version__)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _session_middleware(request: Request, call_next):
    """解析登录 cookie → request.state.user（None = 路人）；写请求做同源校验。

    CSRF：SameSite=Lax cookie（跨站 POST 不带 cookie）+ Origin/Referer 同源校验
    （浏览器跨站请求必带 Origin；两者都缺的 curl 等非浏览器客户端放行）。
    """
    request.state.user = None
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        conn = connect()
        try:
            request.state.user = user_from_request(conn, request)
        finally:
            conn.close()
    if not csrf_origin_ok(request):
        return JSONResponse({"detail": "跨站来源请求被拒绝"}, status_code=403)
    return await call_next(request)


from imas_hub.api.auth_routes import router as auth_router  # noqa: E402

app.include_router(auth_router)


@app.on_event("startup")
def _startup() -> None:
    # 每次启动跑迁移（含 shelf MAIN / 本家动画）
    init_db()


def _db():
    return connect()


def _norm_series_code(code: str) -> str:
    """``01`` 补零；``00B-01`` 原样（勿 zfill 破坏）。"""
    c = (code or "").strip()
    if c.isdigit():
        return c.zfill(2)
    return c


def _norm_shelf_code(code: str) -> str:
    return (code or "").strip().upper()


_SHELF_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_SERIES_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")


def _validate_shelf_code(code: str) -> str:
    c = _norm_shelf_code(code)
    if not c or not _SHELF_CODE_RE.match(c):
        raise HTTPException(
            400,
            "品牌编码格式无效（字母/数字/下划线/连字符，1–32 字符）",
        )
    return c


def _validate_series_code(code: str) -> str:
    c = _norm_series_code(code)
    if not c or not _SERIES_CODE_RE.match(c):
        raise HTTPException(
            400,
            "系列编码格式无效（字母/数字/下划线/连字符，1–32 字符）",
        )
    return c


def _load_shelves(conn) -> list[dict]:
    """首页品牌列表；视图异常时回退直查 shelf。"""
    try:
        rows = conn.execute(
            "SELECT * FROM v_shelf_summary ORDER BY sort_order, code"
        ).fetchall()
        if rows:
            return rows_to_dicts(rows)
    except Exception:  # noqa: BLE001
        pass
    try:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT id, code, title, sort_order,
                       0 AS series_count, 0 AS release_count,
                       0 AS unreviewed_count, 0 AS needs_fill_count,
                       0 AS reviewed_count
                FROM shelf WHERE archived=0 ORDER BY sort_order, code
                """
            ).fetchall()
        )
    except Exception:  # noqa: BLE001
        return []


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    # 读页前 ensure：改库/迁移后少踩「旧进程」坑
    init_db()
    conn = _db()
    try:
        shelves = _load_shelves(conn)
        if not shelves:
            from imas_hub.db.database import _ensure_home_shelves, _refresh_catalog_views

            n_series = conn.execute("SELECT COUNT(*) FROM series").fetchone()[0]
            if n_series:
                _ensure_home_shelves(conn)
                _refresh_catalog_views(conn)
                conn.commit()
                shelves = _load_shelves(conn)
        totals = dict(
            conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM shelf WHERE archived=0) AS shelves,
                    (SELECT COUNT(*) FROM series WHERE archived=0) AS series,
                    (SELECT COUNT(*) FROM release WHERE archived=0) AS releases,
                    (SELECT COUNT(*) FROM track WHERE archived=0) AS tracks,
                    (SELECT COUNT(*) FROM release WHERE archived=0 AND review_status='unreviewed') AS unreviewed,
                    (SELECT COUNT(*) FROM release WHERE archived=0 AND review_status='needs_fill') AS needs_fill,
                    (SELECT COUNT(*) FROM release WHERE archived=0 AND review_status='reviewed') AS reviewed
                """
            ).fetchone()
        )
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "shelves": shelves,
            "totals": totals,
            "version": __version__,
        },
    )


# —— 列表页表单分离：新建/编辑进独立页面 ——
# 注意固定路径（/shelf/new 等）必须声明在 /shelf/{code} 之前，避免被动态段吞掉。


@app.get("/shelf/new", response_class=HTMLResponse)
def shelf_new_page(request: Request):
    return templates.TemplateResponse(
        request,
        "brand_form.html",
        {"brand": None, "version": __version__},
    )


@app.get("/shelf/{code}/edit", response_class=HTMLResponse)
def shelf_edit_page(request: Request, code: str):
    code = _norm_shelf_code(code)
    conn = _db()
    try:
        sh = conn.execute(
            "SELECT * FROM shelf WHERE code=? AND archived=0", (code,)
        ).fetchone()
        if not sh:
            raise HTTPException(404, f"shelf {code} not found")
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "brand_form.html",
        {"brand": dict(sh), "version": __version__},
    )


@app.get("/shelf/{code}/series/new", response_class=HTMLResponse)
def series_new_page(request: Request, code: str):
    code = _norm_shelf_code(code)
    conn = _db()
    try:
        sh = conn.execute(
            "SELECT * FROM shelf WHERE code=? AND archived=0", (code,)
        ).fetchone()
        if not sh:
            raise HTTPException(404, f"shelf {code} not found")
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "series_form.html",
        {"series": None, "shelf": dict(sh), "version": __version__},
    )


@app.get("/series/{code}/edit", response_class=HTMLResponse)
def series_edit_page(request: Request, code: str):
    code = _norm_series_code(code)
    conn = _db()
    try:
        s = conn.execute(
            """
            SELECT s.*, sh.code AS shelf_code, sh.title AS shelf_title
            FROM series s
            LEFT JOIN shelf sh ON sh.id = s.shelf_id
            WHERE s.code=? AND s.archived=0
            """,
            (code,),
        ).fetchone()
        if not s:
            raise HTTPException(404, f"series {code} not found")
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "series_form.html",
        {"series": dict(s), "shelf": None, "version": __version__},
    )


@app.get("/series/{code}/releases/new", response_class=HTMLResponse)
def release_new_page(request: Request, code: str):
    code = _norm_series_code(code)
    conn = _db()
    try:
        s = conn.execute(
            """
            SELECT s.*, sh.code AS shelf_code, sh.title AS shelf_title
            FROM series s
            LEFT JOIN shelf sh ON sh.id = s.shelf_id
            WHERE s.code=? AND s.archived=0
            """,
            (code,),
        ).fetchone()
        if not s:
            raise HTTPException(404, f"series {code} not found")
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "release_new.html",
        {"series": dict(s), "version": __version__},
    )


@app.get("/shelf/{code}", response_class=HTMLResponse)
def shelf_page(request: Request, code: str):
    code = _norm_shelf_code(code)
    conn = _db()
    try:
        sh = conn.execute("SELECT * FROM shelf WHERE code=? AND archived=0", (code,)).fetchone()
        if not sh:
            raise HTTPException(404, f"shelf {code} not found")
        # 品牌内系列：按系列内首张专辑发行日排序（无日期垫底），不必靠编码序号
        series = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM v_series_summary
                WHERE shelf_id=?
                ORDER BY (first_release_date IS NULL), first_release_date, code
                """,
                (int(sh["id"]),),
            ).fetchall()
        )
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "shelf.html",
        {
            "shelf": dict(sh),
            "series": series,
            "version": __version__,
        },
    )


@app.get("/series/{code}", response_class=HTMLResponse)
def series_page(request: Request, code: str):
    code = _norm_series_code(code)
    conn = _db()
    try:
        s = conn.execute(
            """
            SELECT s.*, sh.code AS shelf_code, sh.title AS shelf_title
            FROM series s
            LEFT JOIN shelf sh ON sh.id = s.shelf_id
            WHERE s.code=? AND s.archived=0
            """,
            (code,),
        ).fetchone()
        if not s:
            raise HTTPException(404, f"series {code} not found")
        releases = rows_to_dicts(
            conn.execute(
                """
                SELECT r.*,
                       (SELECT COUNT(*) FROM track t
                        JOIN medium m ON m.id = t.medium_id
                        WHERE m.release_id = r.id AND t.archived = 0) AS track_count
                FROM release r
                WHERE r.series_id=? AND r.archived=0
                ORDER BY (date_guess IS NULL), date_guess,
                         (title IS NULL), title, id
                """,
                (s["id"],),
            ).fetchall()
        )
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "series.html",
        {
            "series": dict(s),
            "releases": releases,
            "wiki_url": WIKI_URL,
            "wiki_ready": bool(WIKI_USER and WIKI_PASS),
            "version": __version__,
        },
    )


@app.get("/release/{release_id}", response_class=HTMLResponse)
def release_page(request: Request, release_id: int):
    conn = _db()
    try:
        r = conn.execute(
            """
            SELECT r.*, s.code AS series_code, s.title AS series_title,
                   sh.code AS shelf_code, sh.title AS shelf_title
            FROM release r
            JOIN series s ON s.id = r.series_id AND s.archived=0
            LEFT JOIN shelf sh ON sh.id = s.shelf_id AND sh.archived=0
            WHERE r.id=? AND r.archived=0
            """,
            (release_id,),
        ).fetchone()
        if not r:
            raise HTTPException(404, "release not found")
        media = rows_to_dicts(
            conn.execute(
                "SELECT * FROM medium WHERE release_id=? ORDER BY position",
                (release_id,),
            ).fetchall()
        )
        from imas_hub.artists.parse import tracks_with_artist

        tracks = tracks_with_artist(conn, release_id)
        covers = rows_to_dicts(
            conn.execute(
                "SELECT * FROM cover_art WHERE release_id=? ORDER BY preferred DESC",
                (release_id,),
            ).fetchall()
        )
        from imas_hub.normalize.cover import find_cover_path

        cover_path = find_cover_path(conn, release_id)
        cover_exists = bool(cover_path and cover_path.is_file())
        wiki_sync = conn.execute(
            """
            SELECT page_title, last_hash, synced_at
            FROM wiki_sync
            WHERE entity_type='release' AND entity_id=?
            """,
            (release_id,),
        ).fetchone()
        wiki_sync = dict(wiki_sync) if wiki_sync else None
    finally:
        conn.close()
    rel = dict(r)
    return templates.TemplateResponse(
        request,
        "release.html",
        {
            "release": rel,
            "media": media,
            "tracks": tracks,
            "covers": covers,
            "cover_exists": cover_exists,
            "cover_filename": cover_path.name if cover_exists and cover_path else None,
            "wiki_sync": wiki_sync,
            "wiki_url": WIKI_URL,
            "wiki_ready": bool(WIKI_USER and WIKI_PASS),
            "version": __version__,
        },
    )


@app.get("/queue", response_class=HTMLResponse)
def queue_page(request: Request, status: str = "unreviewed"):
    """审核工作队列：未人工审核 / 需人工填充 清单页（只读，审核动作在专辑页）。"""
    if status not in ("all", "unreviewed", "needs_fill", "reviewed"):
        status = "unreviewed"
    conn = _db()
    try:
        counts = dict(
            conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM release WHERE archived=0 AND review_status='unreviewed') AS unreviewed,
                    (SELECT COUNT(*) FROM release WHERE archived=0 AND review_status='needs_fill') AS needs_fill,
                    (SELECT COUNT(*) FROM release WHERE archived=0 AND review_status='reviewed') AS reviewed
                """
            ).fetchone()
        )
        sql = """
            SELECT r.id, r.title, r.catalog_no, r.date_guess, r.review_status,
                   s.code AS series_code, s.title AS series_title,
                   (SELECT COUNT(*) FROM track t
                    JOIN medium m ON m.id = t.medium_id
                    WHERE m.release_id = r.id AND t.archived = 0) AS track_count
            FROM release r
            JOIN series s ON s.id = r.series_id AND s.archived = 0
            WHERE r.archived = 0
        """
        params: list = []
        if status != "all":
            sql += " AND r.review_status = ?"
            params.append(status)
        sql += (
            " ORDER BY (r.date_guess IS NULL), r.date_guess,"
            " (r.title IS NULL), r.title, r.id"
        )
        releases = rows_to_dicts(conn.execute(sql, params).fetchall())
    finally:
        conn.close()
    return templates.TemplateResponse(
        request,
        "queue.html",
        {
            "status": status,
            "counts": counts,
            "releases": releases,
            "version": __version__,
        },
    )


@app.get("/progress", response_class=HTMLResponse)
def progress_page(request: Request):
    """审核进度：三态环形图 + 协作者贡献榜 + 我的修改记录（登录可见）。"""
    conn = _db()
    try:
        counts = dict(
            conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM release WHERE archived=0 AND review_status='unreviewed') AS unreviewed,
                    (SELECT COUNT(*) FROM release WHERE archived=0 AND review_status='needs_fill') AS needs_fill,
                    (SELECT COUNT(*) FROM release WHERE archived=0 AND review_status='reviewed') AS reviewed
                """
            ).fetchone()
        )
        # 贡献榜：只统计数据类实体（排除 auth/login 等非数据动作）
        contributors = [
            dict(r)
            for r in conn.execute(
                """
                SELECT u.username,
                       COUNT(*) AS changes,
                       MAX(a.created_at) AS last_at
                FROM audit_log a
                JOIN user u ON u.id = a.user_id
                WHERE a.entity IN ('shelf', 'series', 'release', 'track', 'cover')
                GROUP BY a.user_id
                ORDER BY changes DESC, last_at DESC
                """
            ).fetchall()
        ]
        user = getattr(request.state, "user", None)
        my_logs: list = []
        if user:
            my_logs = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT a.action, a.entity, a.entity_id, a.detail, a.created_at
                    FROM audit_log a
                    WHERE a.user_id=?
                    ORDER BY a.created_at DESC
                    LIMIT 20
                    """,
                    (user["id"],),
                ).fetchall()
            ]
    finally:
        conn.close()
    total = sum(v or 0 for v in counts.values())
    return templates.TemplateResponse(
        request,
        "progress.html",
        {
            "counts": counts,
            "total": total,
            "contributors": contributors,
            "my_logs": my_logs,
            "version": __version__,
        },
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(request: Request, q: str = ""):
    query = (q or "").strip()
    results = api_search(query) if query else {"q": "", "releases": [], "tracks": [], "artists": []}
    return templates.TemplateResponse(
        request,
        "search.html",
        {
            "q": query,
            "results": results,
            "version": __version__,
        },
    )


@app.get("/api/release/{release_id}/cover")
def api_get_cover(release_id: int):
    """返回当前封面图片（无则 404）。"""
    from imas_hub.normalize.cover import find_cover_path

    conn = _db()
    try:
        path = find_cover_path(conn, release_id)
    finally:
        conn.close()
    if not path or not path.is_file():
        raise HTTPException(404, "no cover")
    media = "image/jpeg"
    suf = path.suffix.lower()
    if suf == ".png":
        media = "image/png"
    elif suf in (".jpg", ".jpeg"):
        media = "image/jpeg"
    elif suf == ".webp":
        media = "image/webp"
    return FileResponse(
        path,
        media_type=media,
        filename=path.name,
        headers={"Cache-Control": "no-cache"},
    )


@app.post("/api/release/{release_id}/cover")
async def api_set_cover(
    release_id: int,
    file: UploadFile = File(...),
    user: dict = Depends(require_login),
):
    """拖拽/选择图片 → 写入封面库 Cover.jpg|png（与本地文件无关）。"""
    from imas_hub.normalize.cover import set_release_cover

    data = await file.read()
    if not data:
        raise HTTPException(400, "empty file")

    conn = _db()
    try:
        result = set_release_cover(
            conn,
            release_id,
            data,
            filename=file.filename,
            content_type=file.content_type,
        )
        record_audit(
            conn,
            user["id"],
            "cover.set",
            "release",
            release_id,
            f"filename={result.filename}",
        )
        conn.commit()
        return {
            "ok": True,
            "release_id": result.release_id,
            "filename": result.filename,
            "path": result.path,
            "mime": result.mime,
            "size_bytes": result.size_bytes,
            "replaced": result.replaced,
            "url": f"/api/release/{release_id}/cover",
        }
    except LookupError as e:
        conn.rollback()
        raise HTTPException(404, str(e)) from e
    except FileNotFoundError as e:
        conn.rollback()
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        conn.rollback()
        raise HTTPException(400, str(e)) from e
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


# --- JSON API（给后续阶段 / 调试） ---


@app.get("/api/series")
def api_series():
    conn = _db()
    try:
        return rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM v_series_summary
                ORDER BY (first_release_date IS NULL), first_release_date, code
                """
            ).fetchall()
        )
    finally:
        conn.close()


@app.get("/api/releases")
def api_releases(series: str | None = None, review_status: str | None = None):
    conn = _db()
    try:
        sql = """
            SELECT r.*, s.code AS series_code
            FROM release r
            JOIN series s ON s.id = r.series_id
            WHERE r.archived = 0 AND s.archived = 0
        """
        params: list = []
        if series:
            sql += " AND s.code = ?"
            params.append(_norm_series_code(series))
        if review_status:
            sql += " AND r.review_status = ?"
            params.append(review_status)
        sql += " ORDER BY s.code, (r.date_guess IS NULL), r.date_guess, r.title"
        return rows_to_dicts(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


@app.get("/api/search")
def api_search(q: str | None = None):
    """全局搜索：专辑 / 曲目 / 品番 / 艺人（三组分段返回，各 LIMIT 50）。

    数据量小（千级专辑 / 万级曲目），LIKE 全表扫描足够，不建 FTS。
    """
    query = (q or "").strip()
    if not query:
        return {"q": "", "releases": [], "tracks": [], "artists": []}
    like = f"%{query}%"
    conn = _db()
    try:
        releases = rows_to_dicts(
            conn.execute(
                """
                SELECT r.id, r.title, r.catalog_no, r.date_guess, r.review_status,
                       s.code AS series_code, s.title AS series_title
                FROM release r
                JOIN series s ON s.id = r.series_id AND s.archived = 0
                WHERE r.archived = 0
                  AND (r.title LIKE ? OR r.catalog_no LIKE ?)
                ORDER BY (r.date_guess IS NULL), r.date_guess,
                         (r.title IS NULL), r.title, r.id
                LIMIT 50
                """,
                (like, like),
            ).fetchall()
        )
        tracks = rows_to_dicts(
            conn.execute(
                """
                SELECT t.id, t.title, t.position, m.position AS medium_position,
                       r.id AS release_id, r.title AS release_title, r.review_status,
                       s.code AS series_code, s.title AS series_title
                FROM track t
                JOIN medium m ON m.id = t.medium_id
                JOIN release r ON r.id = m.release_id AND r.archived = 0
                JOIN series s ON s.id = r.series_id AND s.archived = 0
                WHERE t.archived = 0 AND t.title LIKE ?
                ORDER BY (r.date_guess IS NULL), r.date_guess, r.title, t.position
                LIMIT 50
                """,
                (like,),
            ).fetchall()
        )
        from imas_hub.artists.parse import artist_display

        for tr in tracks:  # 曲目搜索结果补派生艺人显示
            tr["artist"] = artist_display(conn, int(tr["id"]))
        # 艺人：署名行命中（display_text 含「角色 (CV:声优)」派生串；正名命中覆盖实体行）
        from imas_hub.artists.parse import SEP

        artist_rows = conn.execute(
            """
            SELECT ta.track_id, ta.display_text, ta.position AS artist_pos,
                   s.name AS seiyuu, c.name AS character,
                   t.title AS track_title, t.position, m.position AS medium_position,
                   r.id AS release_id, r.title AS release_title, r.review_status,
                   ser.code AS series_code, ser.title AS series_title
            FROM track_artist ta
            JOIN track t ON t.id = ta.track_id AND t.archived = 0
            JOIN medium m ON m.id = t.medium_id
            JOIN release r ON r.id = m.release_id AND r.archived = 0
            JOIN series ser ON ser.id = r.series_id AND ser.archived = 0
            LEFT JOIN seiyuu s ON s.id = ta.seiyuu_id
            LEFT JOIN character c ON c.id = ta.character_id
            WHERE ta.display_text LIKE ?
               OR s.name LIKE ?
               OR c.name LIKE ?
            ORDER BY (r.date_guess IS NULL), r.date_guess, r.title,
                     t.position, ta.position
            LIMIT 50
            """,
            (like, like, like),
        ).fetchall()
        artists: list[dict] = []
        cur: dict | None = None
        for r in artist_rows:
            if r["display_text"] is not None:
                disp = r["display_text"]
            elif r["seiyuu"] and r["character"]:
                disp = f"{r['character']} (CV:{r['seiyuu']})"
            elif r["seiyuu"]:
                disp = r["seiyuu"]
            else:
                disp = ""
            if cur is None or cur["track_id"] != r["track_id"]:
                cur = {
                    "track_id": r["track_id"],
                    "track_title": r["track_title"],
                    "position": r["position"],
                    "medium_position": r["medium_position"],
                    "release_id": r["release_id"],
                    "release_title": r["release_title"],
                    "release_status": r["review_status"],
                    "series_code": r["series_code"],
                    "series_title": r["series_title"],
                    "artist": disp,
                }
                artists.append(cur)
            elif disp:
                cur["artist"] += SEP + disp
    finally:
        conn.close()
    return {"q": query, "releases": releases, "tracks": tracks, "artists": artists}


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _audit_detail(data: dict, max_len: int = 200) -> str:
    """审计 detail：变化字段摘要（None 跳过），截断防撑爆日志。"""
    parts = [f"{k}={v}" for k, v in data.items() if v is not None]
    return ", ".join(parts)[:max_len]


def _empty_to_none(v: str | None) -> str | None:
    if v is None:
        return None
    s = v.strip()
    return s or None


class NotesBody(BaseModel):
    notes: str | None = Field(default=None, description="人工备注；传 null 或空串清空")


class CreateShelfBody(BaseModel):
    code: str
    title: str
    sort_order: int = 100


class ShelfEditBody(BaseModel):
    code: str | None = None
    title: str | None = None
    sort_order: int | None = None


class CreateSeriesBody(BaseModel):
    code: str
    title: str


class SeriesEditBody(BaseModel):
    code: str | None = None
    title: str | None = None
    shelf_code: str | None = None  # 改挂品牌；空串表示取消挂靠


class ReleaseEditBody(BaseModel):
    """编辑专辑元数据。"""

    title: str | None = None
    catalog_no: str | None = None
    date_guess: str | None = None
    barcode: str | None = None
    label_hint: str | None = None
    genre: str | None = None
    notes: str | None = None
    mb_release_id: str | None = None
    review_status: str | None = None  # 仅允许 unreviewed/needs_fill/reviewed
    series_code: str | None = None  # 改挂系列


class CreateReleaseBody(BaseModel):
    """在浏览器创建目录条目。"""

    title: str
    catalog_no: str | None = None
    date_guess: str | None = None
    barcode: str | None = None
    label_hint: str | None = None
    genre: str | None = None
    notes: str | None = None
    review_status: str = "needs_fill"
    track_titles: list[str] = Field(default_factory=list)
    """若提供，按顺序建轨；否则可用 track_count 建空轨。"""
    track_count: int = 0


class AddTrackBody(BaseModel):
    title: str | None = None
    artist: str | None = None
    composer: str | None = None
    lyricist: str | None = None
    medium_position: int = 1
    position: int | None = None  # 默认追加到末尾


class TrackEditItem(BaseModel):
    id: int
    title: str | None = None
    artist: str | None = None
    composer: str | None = None
    lyricist: str | None = None
    duration_ms: int | None = None


class MediumEditItem(BaseModel):
    id: int
    format: str | None = None
    title: str | None = None


class TracksEditBody(BaseModel):
    tracks: list[TrackEditItem]
    media: list[MediumEditItem] | None = None


class WikiPushBody(BaseModel):
    """Wiki 预览 / 推送（默认 dry-run）。"""

    apply: bool = False
    force: bool = False
    allow_overwrite: bool = False
    upload_cover: bool = True


class SeriesWikiPushBody(BaseModel):
    apply: bool = False
    force: bool = False
    allow_overwrite: bool = False
    upload_cover: bool = True
    limit: int | None = None
    review_status: str = "reviewed"


def _wiki_push_result_dict(r) -> dict:
    return {
        "release_id": r.release_id,
        "page_title": r.page_title,
        "action": r.action,
        "url": r.url,
        "content_hash": r.content_hash,
        "message": r.message,
        "warnings": r.warnings,
        "covers": [
            {
                "side": c.side,
                "local_path": c.local_path,
                "wiki_name": c.wiki_name,
                "result": c.result,
                "message": c.message,
            }
            for c in (r.covers or [])
        ],
    }


@app.get("/api/wiki/status")
def api_wiki_status():
    """本机/目标 wiki 配置（不含密码）。"""
    return {
        "ok": True,
        "wiki_url": WIKI_URL,
        "has_credentials": bool(WIKI_USER and WIKI_PASS),
        "user": WIKI_USER or None,
    }


@app.get("/api/release/{release_id}/wiki")
def api_wiki_preview_release(release_id: int):
    """渲染专辑 wikitext + 同步状态 + 封面计划（不推送）。"""
    from imas_hub.wiki.push import _local_cover_paths, last_sync_hash, render_release

    conn = _db()
    try:
        try:
            payload, page = render_release(conn, release_id)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e
        sync = conn.execute(
            """
            SELECT page_title, last_hash, synced_at
            FROM wiki_sync
            WHERE entity_type='release' AND entity_id=?
            """,
            (release_id,),
        ).fetchone()
        cover_plan = [
            {"side": side, "local_path": str(path), "wiki_name": dest}
            for side, path, dest in _local_cover_paths(payload)
        ]
        prev_hash = last_sync_hash(conn, "release", release_id)
        return {
            "ok": True,
            "release_id": release_id,
            "page_title": page.page_title,
            "catalog": page.catalog,
            "brand": page.brand,
            "review_status": page.review_status,
            "track_count": page.track_count,
            "content_hash": page.content_hash,
            "hash_unchanged": bool(prev_hash and prev_hash == page.content_hash),
            "warnings": page.warnings,
            "wikitext": page.wikitext,
            "cover_plan": cover_plan,
            "wiki_sync": dict(sync) if sync else None,
            "wiki_url": WIKI_URL,
            "wiki_page_url": (
                f"{WIKI_URL}/wiki/{page.page_title.replace(' ', '_')}"
                if page.page_title
                else None
            ),
            "has_credentials": bool(WIKI_USER and WIKI_PASS),
            "can_push": (page.review_status or "") == "reviewed",
        }
    finally:
        conn.close()


@app.post("/api/release/{release_id}/wiki")
def api_wiki_push_release(
    release_id: int, body: WikiPushBody, user: dict = Depends(require_login)
):
    """预览或推送单张专辑到 MediaWiki（默认 dry-run）。"""
    from imas_hub.wiki.client import WikiClient, WikiConfig, WikiError
    from imas_hub.wiki.push import push_release, render_release

    conn = _db()
    try:
        # 始终附带渲染结果，便于前端展示
        try:
            _payload, page = render_release(conn, release_id)
        except ValueError as e:
            raise HTTPException(404, str(e)) from e

        if body.apply and not (WIKI_USER and WIKI_PASS):
            raise HTTPException(
                400,
                "缺少 Wiki 凭据：启动 serve 前设置 IMAS_WIKI_USER / IMAS_WIKI_PASS",
            )

        client = None
        try:
            if body.apply:
                client = WikiClient(WikiConfig.from_env())
                try:
                    client.login()
                except WikiError as e:
                    raise HTTPException(401, f"Wiki 登录失败: {e}") from e

            result = push_release(
                conn,
                release_id,
                client,
                apply=body.apply,
                create_only=not body.allow_overwrite,
                force=body.force,
                upload_cover=body.upload_cover,
            )
        finally:
            if client:
                client.close()

        out = _wiki_push_result_dict(result)
        out["ok"] = result.action != "error"
        out["apply"] = body.apply
        out["wikitext"] = page.wikitext
        out["track_count"] = page.track_count
        out["wiki_url"] = WIKI_URL
        out["has_credentials"] = bool(WIKI_USER and WIKI_PASS)
        if body.apply:
            record_audit(
                conn,
                user["id"],
                "wiki.push",
                "release",
                release_id,
                f"action={result.action}",
            )
            conn.commit()
        return out
    finally:
        conn.close()


@app.post("/api/series/{code}/wiki")
def api_wiki_push_series(
    code: str, body: SeriesWikiPushBody, user: dict = Depends(require_login)
):
    """系列批量预览 / 推送 Wiki。"""
    from imas_hub.wiki.client import WikiConfig
    from imas_hub.wiki.push import push_many, select_release_ids

    code = _norm_series_code(code)
    if body.apply and not (WIKI_USER and WIKI_PASS):
        raise HTTPException(
            400,
            "缺少 Wiki 凭据：启动 serve 前设置 IMAS_WIKI_USER / IMAS_WIKI_PASS",
        )

    conn = _db()
    try:
        ids = select_release_ids(
            conn,
            series_code=code,
            limit=body.limit,
            review_status=body.review_status or "reviewed",
        )
        # 空结果不是错误：返回 count=0，前端给中文提示（避免「no releases」英文 404）
        if not ids:
            return {
                "ok": True,
                "apply": body.apply,
                "series": code,
                "count": 0,
                "summary": {},
                "results": [],
                "error_count": 0,
                "wiki_url": WIKI_URL,
                "has_credentials": bool(WIKI_USER and WIKI_PASS),
            }

        results = push_many(
            conn,
            ids,
            apply=body.apply,
            wiki_config=WikiConfig.from_env() if body.apply else None,
            create_only=not body.allow_overwrite,
            force=body.force,
            upload_cover=body.upload_cover,
        )
        counts: dict[str, int] = {}
        for r in results:
            counts[r.action] = counts.get(r.action, 0) + 1
        errors = [r for r in results if r.action == "error"]
        if body.apply:
            record_audit(
                conn,
                user["id"],
                "wiki.push",
                "series",
                None,
                f"code={code} count={len(results)} {_audit_detail(counts)}",
            )
            conn.commit()
        return {
            "ok": len(errors) == 0,
            "apply": body.apply,
            "series": code,
            "count": len(results),
            "summary": counts,
            "wiki_url": WIKI_URL,
            "has_credentials": bool(WIKI_USER and WIKI_PASS),
            "results": [_wiki_push_result_dict(r) for r in results],
            "error_count": len(errors),
        }
    finally:
        conn.close()


@app.patch("/api/release/{release_id}/notes")
@app.put("/api/release/{release_id}/notes")
def api_set_notes(
    release_id: int, body: NotesBody, user: dict = Depends(require_login)
):
    """写入 / 清空 Release 人工备注（不参与匹配逻辑）。"""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT id FROM release WHERE id=?", (release_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "release not found")
        text = _empty_to_none(body.notes)
        now = _now()
        conn.execute(
            "UPDATE release SET notes=?, updated_at=? WHERE id=?",
            (text, now, release_id),
        )
        record_audit(
            conn,
            user["id"],
            "release.notes",
            "release",
            release_id,
            f"notes_len={len(text) if text else 0}",
        )
        conn.commit()
        return {"ok": True, "release_id": release_id, "notes": text, "updated_at": now}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.post("/api/shelves")
def api_create_shelf(body: CreateShelfBody, user: dict = Depends(require_login)):
    """新建品牌。"""
    code = _validate_shelf_code(body.code)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    sort_order = int(body.sort_order if body.sort_order is not None else 100)
    now = _now()
    conn = _db()
    try:
        try:
            conn.execute(
                """
                INSERT INTO shelf(code, title, sort_order, created_at, updated_at)
                VALUES (?,?,?,?,?)
                """,
                (code, title, sort_order, now, now),
            )
            record_audit(
                conn, user["id"], "shelf.create", "shelf", None, f"code={code}"
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise HTTPException(409, f"品牌编码已存在: {code}") from e
        row = dict(conn.execute("SELECT * FROM shelf WHERE code=?", (code,)).fetchone())
        return {
            "ok": True,
            "shelf": row,
            "url": f"/shelf/{code}",
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.put("/api/shelf/{code}")
@app.patch("/api/shelf/{code}")
def api_edit_shelf(code: str, body: ShelfEditBody, user: dict = Depends(require_login)):
    """编辑品牌编码 / 标题 / 排序。"""
    code = _norm_shelf_code(code)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "no fields to update")
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM shelf WHERE code=?", (code,)).fetchone()
        if not row:
            raise HTTPException(404, f"shelf {code} not found")
        new_code = code
        sets: list[str] = []
        params: list = []
        if "code" in data and data["code"] is not None:
            new_code = _validate_shelf_code(str(data["code"]))
            if new_code != code:
                dup = conn.execute(
                    "SELECT id FROM shelf WHERE code=?", (new_code,)
                ).fetchone()
                if dup:
                    raise HTTPException(409, f"品牌编码已存在: {new_code}")
                sets.append("code=?")
                params.append(new_code)
        if "title" in data:
            title = (data["title"] or "").strip() if data["title"] is not None else ""
            if not title:
                raise HTTPException(400, "title required")
            sets.append("title=?")
            params.append(title)
        if "sort_order" in data and data["sort_order"] is not None:
            sets.append("sort_order=?")
            params.append(int(data["sort_order"]))
        if not sets:
            raise HTTPException(400, "no valid fields")
        now = _now()
        sets.append("updated_at=?")
        params.append(now)
        params.append(int(row["id"]))
        try:
            conn.execute(
                f"UPDATE shelf SET {', '.join(sets)} WHERE id=?",
                params,
            )
            record_audit(
                conn,
                user["id"],
                "shelf.edit",
                "shelf",
                int(row["id"]),
                _audit_detail(data),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise HTTPException(409, f"编码冲突: {e}") from e
        updated = dict(
            conn.execute("SELECT * FROM shelf WHERE id=?", (int(row["id"]),)).fetchone()
        )
        return {
            "ok": True,
            "shelf": updated,
            "url": f"/shelf/{updated['code']}",
            "updated_at": now,
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.post("/api/shelf/{code}/series")
def api_create_series(code: str, body: CreateSeriesBody, user: dict = Depends(require_login)):
    """在品牌下新建系列。"""
    shelf_code = _norm_shelf_code(code)
    series_code = _validate_series_code(body.code)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    conn = _db()
    try:
        sh = conn.execute(
            "SELECT id, code FROM shelf WHERE code=?", (shelf_code,)
        ).fetchone()
        if not sh:
            raise HTTPException(404, f"shelf {shelf_code} not found")
        try:
            conn.execute(
                """
                INSERT INTO series(code, title, shelf_id)
                VALUES (?,?,?)
                """,
                (series_code, title, int(sh["id"])),
            )
            record_audit(
                conn,
                user["id"],
                "series.create",
                "series",
                None,
                f"code={series_code} shelf={shelf_code}",
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise HTTPException(409, f"系列编码已存在: {series_code}") from e
        row = dict(
            conn.execute("SELECT * FROM series WHERE code=?", (series_code,)).fetchone()
        )
        return {
            "ok": True,
            "series": row,
            "url": f"/series/{series_code}",
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.put("/api/series/{code}")
@app.patch("/api/series/{code}")
def api_edit_series(code: str, body: SeriesEditBody, user: dict = Depends(require_login)):
    """编辑系列编码 / 标题 / 所属品牌。"""
    code = _norm_series_code(code)
    data = body.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(400, "no fields to update")
    conn = _db()
    try:
        row = conn.execute("SELECT * FROM series WHERE code=?", (code,)).fetchone()
        if not row:
            raise HTTPException(404, f"series {code} not found")
        sets: list[str] = []
        params: list = []
        new_code = code
        if "code" in data and data["code"] is not None:
            new_code = _validate_series_code(str(data["code"]))
            if new_code != code:
                dup = conn.execute(
                    "SELECT id FROM series WHERE code=?", (new_code,)
                ).fetchone()
                if dup:
                    raise HTTPException(409, f"系列编码已存在: {new_code}")
                sets.append("code=?")
                params.append(new_code)
        if "title" in data:
            title = (data["title"] or "").strip() if data["title"] is not None else ""
            if not title:
                raise HTTPException(400, "title required")
            sets.append("title=?")
            params.append(title)
        if "shelf_code" in data:
            sc = data["shelf_code"]
            if sc is None or str(sc).strip() == "":
                sets.append("shelf_id=?")
                params.append(None)
            else:
                shelf_code = _norm_shelf_code(str(sc))
                sh = conn.execute(
                    "SELECT id FROM shelf WHERE code=?", (shelf_code,)
                ).fetchone()
                if not sh:
                    raise HTTPException(404, f"shelf {shelf_code} not found")
                sets.append("shelf_id=?")
                params.append(int(sh["id"]))
        if not sets:
            raise HTTPException(400, "no valid fields")
        params.append(int(row["id"]))
        try:
            conn.execute(
                f"UPDATE series SET {', '.join(sets)} WHERE id=?",
                params,
            )
            record_audit(
                conn,
                user["id"],
                "series.edit",
                "series",
                int(row["id"]),
                _audit_detail(data),
            )
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise HTTPException(409, f"编码冲突: {e}") from e
        updated = dict(
            conn.execute("SELECT * FROM series WHERE id=?", (int(row["id"]),)).fetchone()
        )
        return {
            "ok": True,
            "series": updated,
            "url": f"/series/{updated['code']}",
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.put("/api/release/{release_id}")
@app.patch("/api/release/{release_id}")
def api_edit_release(
    release_id: int, body: ReleaseEditBody, user: dict = Depends(require_login)
):
    """更新专辑元数据。"""
    conn = _db()
    try:
        row = conn.execute(
            "SELECT * FROM release WHERE id=?", (release_id,)
        ).fetchone()
        if not row:
            raise HTTPException(404, "release not found")
        data = body.model_dump(exclude_unset=True)
        if not data:
            raise HTTPException(400, "no fields to update")
        allowed = {
            "title",
            "catalog_no",
            "date_guess",
            "barcode",
            "label_hint",
            "genre",
            "notes",
            "mb_release_id",
            "review_status",
            "series_code",
        }
        sets = []
        params: list = []
        for key, val in data.items():
            if key not in allowed:
                continue
            if key == "series_code":
                sc = _validate_series_code(str(val or ""))
                srow = conn.execute(
                    "SELECT id FROM series WHERE code=?", (sc,)
                ).fetchone()
                if not srow:
                    raise HTTPException(404, f"series {sc} not found")
                sets.append("series_id=?")
                params.append(int(srow["id"]))
                continue
            if key == "review_status":
                st = (val or "").strip()
                if st not in ("unreviewed", "needs_fill", "reviewed"):
                    raise HTTPException(400, f"invalid review_status: {st}")
                sets.append("review_status=?")
                params.append(st)
                continue
            sets.append(f"{key}=?")
            params.append(_empty_to_none(val) if isinstance(val, str) or val is None else val)
        if not sets:
            raise HTTPException(400, "no valid fields")
        # 品番冲突（有则唯一）
        if "catalog_no" in data:
            cat = _empty_to_none(
                data["catalog_no"] if isinstance(data["catalog_no"], str) else data["catalog_no"]
            )
            if cat:
                dup = conn.execute(
                    "SELECT id, title FROM release WHERE catalog_no=? AND id!=?",
                    (cat, release_id),
                ).fetchone()
                if dup:
                    raise HTTPException(
                        409,
                        f"品番 {cat} 已存在于 release id={dup['id']} "
                        f"({dup['title'] or '无标题'})",
                    )
        now = _now()
        sets.append("updated_at=?")
        params.append(now)
        params.append(release_id)
        try:
            conn.execute(
                f"UPDATE release SET {', '.join(sets)} WHERE id=?",
                params,
            )
            record_audit(
                conn,
                user["id"],
                "release.edit",
                "release",
                release_id,
                _audit_detail(data),
            )
            # mb_release_id 只作外链 ID 保存；不再自动从 MB 建轨（批量匹配已退出项目）
            conn.commit()
        except sqlite3.IntegrityError as e:
            conn.rollback()
            raise HTTPException(409, f"品番冲突: {e}") from e
        updated = dict(
            conn.execute("SELECT * FROM release WHERE id=?", (release_id,)).fetchone()
        )
        return {"ok": True, "release_id": release_id, "release": updated, "updated_at": now}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.post("/api/series/{code}/releases")
def api_create_release(
    code: str, body: CreateReleaseBody, user: dict = Depends(require_login)
):
    """创建无本地 path 的主库 Release（浏览器建目）。"""
    code = _norm_series_code(code)
    title = (body.title or "").strip()
    if not title:
        raise HTTPException(400, "title required")
    status = (body.review_status or "needs_fill").strip()
    if status not in ("unreviewed", "needs_fill", "reviewed"):
        raise HTTPException(400, f"invalid review_status: {status}")

    conn = _db()
    try:
        s = conn.execute("SELECT id, code FROM series WHERE code=?", (code,)).fetchone()
        if not s:
            raise HTTPException(
                404,
                f"series {code} not found — 请先在对应品牌下新建系列 {code}",
            )
        catalog = _empty_to_none(body.catalog_no)
        if catalog:
            dup = conn.execute(
                "SELECT id, title FROM release WHERE catalog_no=?",
                (catalog,),
            ).fetchone()
            if dup:
                raise HTTPException(
                    409,
                    f"品番 {catalog} 已存在于 release id={dup['id']} "
                    f"({dup['title'] or '无标题'})",
                )
        now = _now()
        titles = [t.strip() for t in (body.track_titles or []) if (t or "").strip()]
        n_tracks = len(titles) if titles else max(0, int(body.track_count or 0))
        conn.execute(
            """
            INSERT INTO release(
                series_id, title, catalog_no, date_guess,
                barcode, label_hint, genre, notes, review_status,
                medium_count, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,1,?,?)
            """,
            (
                int(s["id"]),
                title,
                catalog,
                _empty_to_none(body.date_guess),
                _empty_to_none(body.barcode),
                _empty_to_none(body.label_hint),
                _empty_to_none(body.genre),
                _empty_to_none(body.notes),
                status,
                now,
                now,
            ),
        )
        rid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        conn.execute(
            """
            INSERT INTO medium(release_id, position, format, title)
            VALUES (?, 1, 'CD', NULL)
            """,
            (rid,),
        )
        mid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        for i in range(n_tracks):
            ttitle = titles[i] if i < len(titles) else None
            conn.execute(
                """
                INSERT INTO track(
                    medium_id, position, title
                ) VALUES (?, ?, ?)
                """,
                (mid, i + 1, ttitle),
            )
        record_audit(
            conn,
            user["id"],
            "release.create",
            "release",
            rid,
            f"title={title} catalog={catalog} tracks={n_tracks}",
        )
        conn.commit()
        return {
            "ok": True,
            "release_id": rid,
            "series_code": code,
            "title": title,
            "track_count": n_tracks,
            "url": f"/release/{rid}",
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.post("/api/release/{release_id}/tracks")
def api_add_track(
    release_id: int, body: AddTrackBody, user: dict = Depends(require_login)
):
    """追加一条曲目（无文件亦可；主库编辑）。"""
    conn = _db()
    try:
        rel = conn.execute(
            "SELECT id FROM release WHERE id=?", (release_id,)
        ).fetchone()
        if not rel:
            raise HTTPException(404, "release not found")
        mpos = int(body.medium_position or 1)
        med = conn.execute(
            "SELECT id FROM medium WHERE release_id=? AND position=?",
            (release_id, mpos),
        ).fetchone()
        if not med:
            conn.execute(
                """
                INSERT INTO medium(release_id, position, format, title)
                VALUES (?, ?, 'CD', NULL)
                """,
                (release_id, mpos),
            )
            mid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute(
                "UPDATE release SET medium_count=(SELECT COUNT(*) FROM medium WHERE release_id=?) WHERE id=?",
                (release_id, release_id),
            )
        else:
            mid = int(med["id"])
        if body.position is not None:
            pos = int(body.position)
        else:
            row = conn.execute(
                "SELECT COALESCE(MAX(position), 0) AS m FROM track WHERE medium_id=?",
                (mid,),
            ).fetchone()
            pos = int(row["m"] or 0) + 1
        conn.execute(
            """
            INSERT INTO track(medium_id, position, title, composer, lyricist)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                mid,
                pos,
                _empty_to_none(body.title),
                _empty_to_none(body.composer),
                _empty_to_none(body.lyricist),
            ),
        )
        tid = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        from imas_hub.artists.parse import (
            artist_display,
            load_entity_index,
            rebuild_track_artists,
        )

        rel_row = conn.execute(
            "SELECT date_guess FROM release WHERE id=?", (release_id,)
        ).fetchone()
        rebuild_track_artists(
            conn, load_entity_index(conn), tid, body.artist, rel_row["date_guess"]
        )
        artist = artist_display(conn, tid)
        now = _now()
        conn.execute(
            """
            UPDATE release SET updated_at=? WHERE id=?
            """,
            (now, release_id),
        )
        record_audit(
            conn,
            user["id"],
            "track.add",
            "track",
            tid,
            f"title={body.title or ''}",
        )
        conn.commit()
        return {
            "ok": True,
            "release_id": release_id,
            "track_id": tid,
            "position": pos,
            "medium_position": mpos,
            "artist": artist,
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.put("/api/release/{release_id}/tracks")
def api_edit_tracks(
    release_id: int, body: TracksEditBody, user: dict = Depends(require_login)
):
    """批量更新曲目元数据（DB only）。"""
    if not body.tracks:
        raise HTTPException(400, "tracks empty")
    conn = _db()
    try:
        rel = conn.execute(
            "SELECT id FROM release WHERE id=?", (release_id,)
        ).fetchone()
        if not rel:
            raise HTTPException(404, "release not found")
        updated = 0
        for item in body.tracks:
            row = conn.execute(
                """
                SELECT t.id FROM track t
                JOIN medium m ON m.id = t.medium_id
                WHERE t.id=? AND m.release_id=?
                """,
                (item.id, release_id),
            ).fetchone()
            if not row:
                raise HTTPException(400, f"track {item.id} not in release {release_id}")
            conn.execute(
                """
                UPDATE track SET title=?, composer=?, lyricist=?, duration_ms=?
                WHERE id=?
                """,
                (
                    _empty_to_none(item.title),
                    _empty_to_none(item.composer),
                    _empty_to_none(item.lyricist),
                    item.duration_ms,
                    item.id,
                ),
            )
            updated += 1
        if body.media:
            for m in body.media:
                med = conn.execute(
                    "SELECT id FROM medium WHERE id=? AND release_id=?",
                    (m.id, release_id),
                ).fetchone()
                if not med:
                    raise HTTPException(
                        400, f"medium {m.id} not in release {release_id}"
                    )
                conn.execute(
                    "UPDATE medium SET format=?, title=? WHERE id=?",
                    (
                        _empty_to_none(m.format),
                        _empty_to_none(m.title),
                        m.id,
                    ),
                )
        from imas_hub.artists.parse import (
            load_entity_index,
            rebuild_track_artists,
            tracks_with_artist,
        )

        rel_row = conn.execute(
            "SELECT date_guess FROM release WHERE id=?", (release_id,)
        ).fetchone()
        idx = load_entity_index(conn)
        for item in body.tracks:
            rebuild_track_artists(
                conn, idx, item.id, item.artist, rel_row["date_guess"]
            )
        now = _now()
        conn.execute(
            "UPDATE release SET updated_at=? WHERE id=?", (now, release_id)
        )
        record_audit(
            conn,
            user["id"],
            "track.edit",
            "release",
            release_id,
            f"tracks={len(body.tracks)}",
        )
        conn.commit()
        return {
            "ok": True,
            "release_id": release_id,
            "updated_tracks": updated,
            "updated_at": now,
            "tracks": [
                {"id": t["id"], "artist": t["artist"]}
                for t in tracks_with_artist(conn, release_id)
            ],
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.get("/api/artist-suggest")
def api_artist_suggest(
    q: str = "",
    release_id: int | None = Query(default=None, description="按发行日期给默认声优建议"),
):
    """演唱者自动补全：角色 / 声优按「正名 + 别名」模糊命中。

    返回 [{display, seiyuu, character, kind}]：
    - character：`角色 (CV:默认声优)`（多 portrayal 按 release 日期粗判时期）
    - seiyuu：裸声优名
    - display：历史 display_text（团体 / 工作人员等，解析不了的原文）
    """
    query = (q or "").strip()
    if not query:
        return []
    conn = _db()
    try:
        from imas_hub.artists.parse import load_entity_index, pick_default_seiyuu

        idx = load_entity_index(conn)
        release_date = None
        if release_id:
            r = conn.execute(
                "SELECT date_guess FROM release WHERE id=?", (release_id,)
            ).fetchone()
            release_date = r["date_guess"] if r else None
        out: list[dict] = []
        seen: set[tuple] = set()

        def push(kind: str, display: str, seiyuu: str | None, character: str | None) -> None:
            key = (kind, seiyuu, character)
            if key in seen:
                return
            seen.add(key)
            out.append(
                {"display": display, "seiyuu": seiyuu, "character": character, "kind": kind}
            )

        # 角色：正名 + 别名
        for name, cid in idx.char_by_name.items():
            if query in name:
                sid = pick_default_seiyuu(idx, cid, release_date)
                sname = idx.name_by_seiyuu.get(sid) if sid else None
                push("character", f"{name} (CV:{sname})" if sname else name, sname, name)
        for alias, cid in idx.char_by_alias.items():
            if query in alias:
                name = idx.name_by_char[cid]
                sid = pick_default_seiyuu(idx, cid, release_date)
                sname = idx.name_by_seiyuu.get(sid) if sid else None
                push("character", f"{name} (CV:{sname})" if sname else name, sname, name)
        # 声优：正名 + 别名
        for name, sid in idx.seiyuu_by_name.items():
            if query in name:
                push("seiyuu", name, name, None)
        for alias, sid in idx.seiyuu_by_alias.items():
            if query in alias:
                name = idx.name_by_seiyuu[sid]
                push("seiyuu", name, name, None)
        # 历史 display_text（团体等解析不了的原文）
        for (dt,) in conn.execute(
            """
            SELECT DISTINCT display_text FROM track_artist
            WHERE display_text IS NOT NULL AND display_text LIKE ?
            ORDER BY display_text LIMIT 12
            """,
            (f"%{query}%",),
        ):
            push("display", dt, None, None)
        out.sort(key=lambda x: (0 if x["display"].startswith(query) else 1, x["display"]))
        return out[:20]
    finally:
        conn.close()


# ---- DELETE：软删除（archived=1）----
# 软删不放宽品番唯一约束——归档的 release 也占品番，避免复活时撞车。
# 需要彻底让出版号才硬删（不在前端范围，走数据库直接操作或备份）。


@app.delete("/api/shelf/{code}")
def api_delete_shelf(code: str, user: dict = Depends(require_login)):
    """归档品牌；品牌下仍有未归档系列时拒删（409）。"""
    code = _norm_shelf_code(code)
    conn = _db()
    try:
        sh = conn.execute(
            "SELECT id, title FROM shelf WHERE code=? AND archived=0", (code,)
        ).fetchone()
        if not sh:
            raise HTTPException(404, f"品牌 {code} 不存在或已归档")
        n_series = conn.execute(
            "SELECT COUNT(*) FROM series WHERE shelf_id=? AND archived=0",
            (int(sh["id"]),),
        ).fetchone()[0]
        if n_series:
            raise HTTPException(
                409,
                f"品牌 {code} 下仍有 {n_series} 个未归档系列，请先处理它们再删除品牌",
            )
        now = _now()
        conn.execute(
            "UPDATE shelf SET archived=1, updated_at=? WHERE id=?",
            (now, int(sh["id"])),
        )
        record_audit(
            conn, user["id"], "shelf.archive", "shelf", int(sh["id"]), f"code={code}"
        )
        conn.commit()
        return {"ok": True, "code": code, "archived": True}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.delete("/api/series/{code}")
def api_delete_series(code: str, user: dict = Depends(require_login)):
    """归档系列；系列下仍有未归档专辑时拒删（409）。"""
    code = _norm_series_code(code)
    conn = _db()
    try:
        s = conn.execute(
            "SELECT id, title FROM series WHERE code=? AND archived=0", (code,)
        ).fetchone()
        if not s:
            raise HTTPException(404, f"系列 {code} 不存在或已归档")
        n_rel = conn.execute(
            "SELECT COUNT(*) FROM release WHERE series_id=? AND archived=0",
            (int(s["id"]),),
        ).fetchone()[0]
        if n_rel:
            raise HTTPException(
                409,
                f"系列 {code} 下仍有 {n_rel} 张未归档专辑，请先处理它们再删除系列",
            )
        conn.execute(
            "UPDATE series SET archived=1 WHERE id=?",
            (int(s["id"]),),
        )
        record_audit(
            conn, user["id"], "series.archive", "series", int(s["id"]), f"code={code}"
        )
        conn.commit()
        return {"ok": True, "code": code, "archived": True}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.delete("/api/release/{release_id}")
def api_delete_release(release_id: int, user: dict = Depends(require_login)):
    """归档专辑；连带归档其曲目（同库事务）。

    软删保留元数据与封面、品番仍占唯一位。
    """
    conn = _db()
    try:
        r = conn.execute(
            "SELECT id, title FROM release WHERE id=? AND archived=0",
            (release_id,),
        ).fetchone()
        if not r:
            raise HTTPException(404, f"专辑 {release_id} 不存在或已归档")
        now = _now()
        conn.execute(
            "UPDATE release SET archived=1, updated_at=? WHERE id=?",
            (now, release_id),
        )
        conn.execute(
            """
            UPDATE track SET archived=1
            WHERE medium_id IN (SELECT id FROM medium WHERE release_id=?)
            """,
            (release_id,),
        )
        record_audit(
            conn,
            user["id"],
            "release.archive",
            "release",
            release_id,
            f"title={r['title'] or ''}",
        )
        conn.commit()
        return {
            "ok": True,
            "release_id": release_id,
            "archived": True,
        }
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.delete("/api/release/{release_id}/tracks/{track_id}")
def api_delete_track(
    release_id: int, track_id: int, user: dict = Depends(require_login)
):
    """归档单曲（保留行数据，列表过滤掉）。"""
    conn = _db()
    try:
        row = conn.execute(
            """
            SELECT t.id FROM track t
            JOIN medium m ON m.id = t.medium_id
            WHERE t.id=? AND m.release_id=? AND t.archived=0
            """,
            (track_id, release_id),
        ).fetchone()
        if not row:
            raise HTTPException(404, f"曲目 {track_id} 不在专辑 {release_id} 下或已归档")
        now = _now()
        conn.execute(
            "UPDATE track SET archived=1 WHERE id=?", (track_id,)
        )
        conn.execute(
            "UPDATE release SET updated_at=? WHERE id=?", (now, release_id)
        )
        record_audit(
            conn,
            user["id"],
            "track.archive",
            "track",
            track_id,
            f"release={release_id}",
        )
        conn.commit()
        return {"ok": True, "track_id": track_id, "archived": True}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        raise HTTPException(500, str(e)) from e
    finally:
        conn.close()


@app.get("/api/release/{release_id}/export")
def api_export_release(release_id: int):
    """单张专辑完整导出（BOT / 歌词站）。"""
    from imas_hub.export import export_release

    conn = _db()
    try:
        payload = export_release(conn, release_id)
        if not payload:
            raise HTTPException(404, "release not found")
        return JSONResponse(
            payload,
            headers={
                "Content-Disposition": (
                    f'inline; filename="release_{release_id}_export.json"'
                )
            },
        )
    finally:
        conn.close()


@app.get("/api/export")
def api_export_library(
    series: str = Query(..., description="系列 code，必填（不做全库导出）"),
    review_status: str | None = Query(
        "reviewed",
        description="审核状态过滤；传空字符串表示不过滤",
    ),
):
    """按系列批量导出（全库导出入口已移除）。默认仅 reviewed。"""
    from imas_hub.export import export_releases

    status = review_status
    if status is not None and status.strip() == "":
        status = None
    conn = _db()
    try:
        payload = export_releases(
            conn,
            series_code=_norm_series_code(series),
            review_status=status,
        )
        tag = _norm_series_code(series)
        return JSONResponse(
            payload,
            headers={
                "Content-Disposition": (
                    f'inline; filename="export_{tag}_{status or "any"}.json"'
                )
            },
        )
    finally:
        conn.close()
