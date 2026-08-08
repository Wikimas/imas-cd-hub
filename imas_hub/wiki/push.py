"""把 reviewed Release 渲染并推送到 MediaWiki。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from imas_hub.export.bundle import export_release
from imas_hub.wiki.client import WikiClient, WikiConfig, WikiError
from imas_hub.wiki.render import RenderedPage, render_album_page, wiki_cover_dest_name


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class CoverUploadResult:
    side: str  # front / back
    local_path: str
    wiki_name: str
    result: str  # uploaded / exists / skipped / error
    message: str = ""


@dataclass
class PushResult:
    release_id: int
    page_title: str
    action: str  # skipped / dry-run / created / updated / unchanged / error
    url: str | None = None
    content_hash: str | None = None
    message: str = ""
    warnings: list[str] = field(default_factory=list)
    covers: list[CoverUploadResult] = field(default_factory=list)


def record_wiki_sync(
    conn: sqlite3.Connection,
    *,
    entity_type: str,
    entity_id: int,
    page_title: str,
    content_hash: str,
) -> None:
    existing = conn.execute(
        "SELECT id FROM wiki_sync WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id),
    ).fetchone()
    if existing:
        conn.execute(
            """
            UPDATE wiki_sync
            SET page_title=?, last_hash=?, synced_at=?
            WHERE id=?
            """,
            (page_title, content_hash, _now_iso(), existing["id"]),
        )
    else:
        conn.execute(
            """
            INSERT INTO wiki_sync (entity_type, entity_id, page_title, last_hash, synced_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_type, entity_id, page_title, content_hash, _now_iso()),
        )
    conn.commit()


def last_sync_hash(conn: sqlite3.Connection, entity_type: str, entity_id: int) -> str | None:
    row = conn.execute(
        "SELECT last_hash FROM wiki_sync WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id),
    ).fetchone()
    return row["last_hash"] if row else None


def render_release(
    conn: sqlite3.Connection,
    release_id: int,
    **render_kw: Any,
) -> tuple[dict[str, Any], RenderedPage]:
    payload = export_release(conn, release_id)
    if not payload:
        raise ValueError(f"release {release_id} not found")
    page = render_album_page(payload, **render_kw)
    return payload, page


def _local_cover_paths(payload: dict[str, Any]) -> list[tuple[str, Path, str]]:
    """返回 (side, local_path, wiki_filename) 列表。"""
    release = payload.get("release") or {}
    covers = payload.get("covers") or {}
    out: list[tuple[str, Path, str]] = []
    for side, key in (("front", "preferred"), ("back", "back")):
        info = covers.get(key)
        if not info:
            continue
        path_s = info.get("path")
        if not path_s:
            continue
        path = Path(path_s)
        if not path.is_file():
            continue
        dest = wiki_cover_dest_name(release, covers, side=side)
        if not dest:
            continue
        out.append((side, path, dest))
    return out


def upload_release_covers(
    payload: dict[str, Any],
    client: WikiClient,
    *,
    include_back: bool = True,
) -> list[CoverUploadResult]:
    """上传正/背面封到 MediaWiki。"""
    results: list[CoverUploadResult] = []
    for side, path, dest in _local_cover_paths(payload):
        if side == "back" and not include_back:
            continue
        try:
            up = client.upload(
                path,
                filename=dest,
                comment=f"hub bot: {side} cover catalog="
                f"{(payload.get('release') or {}).get('catalog_no') or '?'}",
                ignore_warnings=True,
            )
            # MediaWiki: result Success / Warning; duplicate may still Success with filekey
            res = (up.get("result") or "Success").lower()
            if res == "success":
                results.append(
                    CoverUploadResult(
                        side=side,
                        local_path=str(path),
                        wiki_name=dest,
                        result="uploaded",
                        message=up.get("filename") or dest,
                    )
                )
            else:
                results.append(
                    CoverUploadResult(
                        side=side,
                        local_path=str(path),
                        wiki_name=dest,
                        result="uploaded",
                        message=str(up)[:200],
                    )
                )
        except WikiError as e:
            results.append(
                CoverUploadResult(
                    side=side,
                    local_path=str(path),
                    wiki_name=dest,
                    result="error",
                    message=str(e),
                )
            )
    return results


def push_release(
    conn: sqlite3.Connection,
    release_id: int,
    client: WikiClient | None = None,
    *,
    apply: bool = False,
    create_only: bool = False,
    force: bool = False,
    summary: str | None = None,
    out_dir: Path | None = None,
    require_reviewed: bool = True,
    upload_cover: bool = True,
) -> PushResult:
    """渲染并（可选）推送单张专辑。

    默认 dry-run：只写本地 wikitext，不调编辑 API。
    apply 时默认同时上传本地 Cover（正/背）。
    """
    payload, page = render_release(conn, release_id)
    release = payload["release"]
    status = release.get("review_status")
    cover_plan = _local_cover_paths(payload)

    if require_reviewed and status != "reviewed":
        return PushResult(
            release_id=release_id,
            page_title=page.page_title,
            action="skipped",
            content_hash=page.content_hash,
            message=f"review_status={status!r} 未审核，禁止推 Wiki",
            warnings=page.warnings,
        )

    if out_dir:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = f"r{release_id}_" + "".join(
            c if c.isalnum() or c in "-_" else "_" for c in page.page_title
        )[:80]
        (out_dir / f"{safe}.wiki").write_text(page.wikitext, encoding="utf-8")

    prev = last_sync_hash(conn, "release", release_id)
    page_unchanged = bool(prev and prev == page.content_hash and not force)

    if not apply:
        cover_hint = ""
        if upload_cover and cover_plan:
            names = ", ".join(d for _, _, d in cover_plan)
            cover_hint = f"；将上传封面: {names}"
        elif upload_cover:
            cover_hint = "；无本地封面可上传"
        return PushResult(
            release_id=release_id,
            page_title=page.page_title,
            action="dry-run",
            content_hash=page.content_hash,
            message=f"已渲染 {page.track_count} 轨"
            + (f"；写入 {out_dir}" if out_dir else "")
            + cover_hint,
            warnings=page.warnings,
        )

    if client is None:
        raise WikiError("apply 需要 WikiClient")

    cover_results: list[CoverUploadResult] = []
    if upload_cover:
        cover_results = upload_release_covers(payload, client)
        for cr in cover_results:
            if cr.result == "error":
                page.warnings.append(f"cover {cr.side}: {cr.message}")

    # 页面正文未变且非 force：仍可能刚补传了图
    if page_unchanged:
        cover_msg = ""
        if cover_results:
            cover_msg = "；封面 " + ", ".join(
                f"{c.side}={c.result}:{c.wiki_name}" for c in cover_results
            )
        return PushResult(
            release_id=release_id,
            page_title=page.page_title,
            action="unchanged",
            url=client.page_url(page.page_title),
            content_hash=page.content_hash,
            message="content hash 未变，跳过页面" + cover_msg,
            warnings=page.warnings,
            covers=cover_results,
        )

    existing = client.get_page(page.page_title)
    is_new = not existing or existing.get("missing")
    if create_only and not is_new:
        cover_msg = ""
        if cover_results:
            cover_msg = "；封面 " + ", ".join(
                f"{c.side}={c.result}:{c.wiki_name}" for c in cover_results
            )
        return PushResult(
            release_id=release_id,
            page_title=page.page_title,
            action="skipped",
            url=client.page_url(page.page_title),
            content_hash=page.content_hash,
            message="页面已存在且 create_only" + cover_msg,
            warnings=page.warnings,
            covers=cover_results,
        )

    sum_text = summary or (
        f"hub bot: {'创建' if is_new else 'update'} "
        f"catalog={page.catalog or '?'} release_id={release_id}"
    )
    try:
        client.edit(
            page.page_title,
            page.wikitext,
            summary=sum_text,
            create_only=create_only and is_new,
            bot=True,
        )
    except WikiError as e:
        return PushResult(
            release_id=release_id,
            page_title=page.page_title,
            action="error",
            content_hash=page.content_hash,
            message=str(e),
            warnings=page.warnings,
            covers=cover_results,
        )

    record_wiki_sync(
        conn,
        entity_type="release",
        entity_id=release_id,
        page_title=page.page_title,
        content_hash=page.content_hash,
    )
    cover_msg = ""
    if cover_results:
        cover_msg = "；封面 " + ", ".join(
            f"{c.side}={c.result}:{c.wiki_name}" for c in cover_results
        )
    return PushResult(
        release_id=release_id,
        page_title=page.page_title,
        action="created" if is_new else "updated",
        url=client.page_url(page.page_title),
        content_hash=page.content_hash,
        message=sum_text + cover_msg,
        warnings=page.warnings,
        covers=cover_results,
    )


def select_release_ids(
    conn: sqlite3.Connection,
    *,
    release_id: int | None = None,
    series_code: str | None = None,
    limit: int | None = None,
    review_status: str = "reviewed",
) -> list[int]:
    if release_id is not None:
        return [release_id]
    sql = """
        SELECT r.id
        FROM release r
        JOIN series s ON s.id = r.series_id
        WHERE 1=1
    """
    params: list[Any] = []
    if review_status:
        sql += " AND r.review_status = ?"
        params.append(review_status)
    if series_code:
        sql += " AND s.code = ?"
        params.append(series_code.zfill(2))
    sql += " ORDER BY s.code, (r.date_guess IS NULL), r.date_guess, r.title"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [int(r["id"]) for r in conn.execute(sql, params).fetchall()]


def push_many(
    conn: sqlite3.Connection,
    release_ids: list[int],
    *,
    apply: bool = False,
    wiki_config: WikiConfig | None = None,
    create_only: bool = True,
    force: bool = False,
    out_dir: Path | None = None,
    upload_cover: bool = True,
) -> list[PushResult]:
    results: list[PushResult] = []
    client: WikiClient | None = None
    try:
        if apply:
            client = WikiClient(wiki_config or WikiConfig.from_env())
            client.login()
        for rid in release_ids:
            try:
                results.append(
                    push_release(
                        conn,
                        rid,
                        client,
                        apply=apply,
                        create_only=create_only,
                        force=force,
                        out_dir=out_dir,
                        upload_cover=upload_cover,
                    )
                )
            except Exception as e:
                results.append(
                    PushResult(
                        release_id=rid,
                        page_title="",
                        action="error",
                        message=str(e),
                    )
                )
    finally:
        if client:
            client.close()
    return results
