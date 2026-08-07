"""结构化导出：Release → Medium → Track（稳定 ID，供 BOT / 歌词站）。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from imas_hub import __version__
from imas_hub.db.database import rows_to_dicts
from imas_hub.normalize.cover import find_cover_path


def _ms_to_mmss(ms: int | None) -> str | None:
    if ms is None or ms < 0:
        return None
    total = int(ms) // 1000
    return f"{total // 60}:{total % 60:02d}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def export_release(conn: sqlite3.Connection, release_id: int) -> dict[str, Any] | None:
    """导出单张专辑的完整包（含 media/tracks/covers）。"""
    row = conn.execute(
        """
        SELECT r.*, s.code AS series_code, s.folder_name AS series_folder,
               s.title AS series_title
        FROM release r
        JOIN series s ON s.id = r.series_id
        WHERE r.id = ?
        """,
        (release_id,),
    ).fetchone()
    if not row:
        return None
    r = dict(row)

    media_rows = rows_to_dicts(
        conn.execute(
            "SELECT * FROM medium WHERE release_id=? ORDER BY position",
            (release_id,),
        ).fetchall()
    )
    track_rows = rows_to_dicts(
        conn.execute(
            """
            SELECT t.id, t.medium_id, t.position, t.title, t.artist,
                   t.composer, t.lyricist,
                   COALESCE(t.duration_ms, lf.duration_ms) AS duration_ms,
                   t.mb_recording_id, t.match_status, t.local_file_id,
                   m.position AS medium_position,
                   lf.path AS file_path, lf.rel_path AS file_rel_path,
                   lf.hash_sha256, lf.integrity, lf.codec,
                   lf.sample_rate, lf.bits, lf.channels, lf.filename
            FROM track t
            JOIN medium m ON m.id = t.medium_id
            LEFT JOIN local_file lf ON lf.id = t.local_file_id
            WHERE m.release_id = ?
            ORDER BY m.position, t.position
            """,
            (release_id,),
        ).fetchall()
    )
    cover_rows = rows_to_dicts(
        conn.execute(
            "SELECT id, path, preferred, file_id FROM cover_art "
            "WHERE release_id=? ORDER BY preferred DESC, id",
            (release_id,),
        ).fetchall()
    )

    from imas_hub.normalize.cover import find_back_cover_path

    cover_path = find_cover_path(conn, release_id)
    preferred_cover = None
    if cover_path and cover_path.is_file():
        preferred_cover = {
            "path": str(cover_path),
            "filename": cover_path.name,
            "exists": True,
        }
    back_candidates = []
    back_path = find_back_cover_path(conn, release_id)
    if back_path and back_path.is_file():
        back_candidates.append({"path": str(back_path), "filename": back_path.name})

    tracks_by_medium: dict[int, list[dict]] = {}
    tracks_flat: list[dict] = []
    for t in track_rows:
        mid = int(t["medium_id"])
        duration_ms = t.get("duration_ms")
        item = {
            "id": t["id"],
            "medium_id": mid,
            "medium_position": t.get("medium_position"),
            "position": t.get("position"),
            "title": t.get("title"),
            "artist": t.get("artist"),
            "composer": t.get("composer"),
            "lyricist": t.get("lyricist"),
            "duration_ms": duration_ms,
            "duration": _ms_to_mmss(duration_ms),
            "mb_recording_id": t.get("mb_recording_id"),
            "match_status": t.get("match_status"),
            "file": {
                "id": t.get("local_file_id"),
                "path": t.get("file_path"),
                "rel_path": t.get("file_rel_path"),
                "filename": t.get("filename"),
                "hash_sha256": t.get("hash_sha256"),
                "integrity": t.get("integrity"),
                "codec": t.get("codec"),
                "sample_rate": t.get("sample_rate"),
                "bits": t.get("bits"),
                "channels": t.get("channels"),
            }
            if t.get("local_file_id") or t.get("file_path")
            else None,
            # wikimas {{Track}} 友好字段（渲染细节见 imas_hub.wiki.render）
            "wiki": {
                "title": t.get("title"),
                "length": _ms_to_mmss(duration_ms),
                "artist": t.get("artist"),
                "nolink": False,
            },
        }
        tracks_by_medium.setdefault(mid, []).append(item)
        tracks_flat.append(item)

    media_out = []
    for m in media_rows:
        mid = int(m["id"])
        media_out.append(
            {
                "id": mid,
                "position": m.get("position"),
                "format": m.get("format"),
                "title": m.get("title"),
                "path": m.get("path"),
                "tracks": tracks_by_medium.get(mid, []),
            }
        )

    return {
        "schema": "imas_hub.release_export/v1",
        "exported_at": _now_iso(),
        "hub_version": __version__,
        "release": {
            "id": r["id"],
            "title": r.get("title"),
            "folder_name": r.get("folder_name"),
            "path": r.get("path"),
            "catalog_no": r.get("catalog_no"),
            "date": r.get("date_guess"),
            "barcode": r.get("barcode"),
            "label": r.get("label_hint"),
            "genre": r.get("genre"),
            "mb_release_id": r.get("mb_release_id"),
            "match_status": r.get("match_status"),
            "match_confidence": r.get("match_confidence"),
            "medium_count": r.get("medium_count"),
            "track_count": r.get("track_count_local"),
            "integrity_status": r.get("integrity_status"),
            "has_cover": bool(r.get("has_cover")),
            "has_scan": bool(r.get("has_scan")),
            "has_dvd": bool(r.get("has_dvd")),
            "has_log": bool(r.get("has_log")),
            "notes": r.get("notes"),
            "series": {
                "code": r.get("series_code"),
                "folder_name": r.get("series_folder"),
                "title": r.get("series_title"),
            },
            # wikimas Album info 友好字段
            "wiki": {
                "title": r.get("title"),
                "catalog": r.get("catalog_no"),
                "barcode": r.get("barcode"),
                "release": r.get("date_guess"),
                "label": r.get("label_hint"),
                "brand": "765as",
            },
        },
        "media": media_out,
        "tracks": tracks_flat,
        "covers": {
            "preferred": preferred_cover,
            "back": back_candidates[0] if back_candidates else None,
            "db": cover_rows,
        },
        "links": {
            "musicbrainz": (
                f"https://musicbrainz.org/release/{r['mb_release_id']}"
                if r.get("mb_release_id")
                else None
            ),
            "hub": f"/release/{r['id']}",
            "api": f"/api/release/{r['id']}/export",
        },
    }


def export_releases(
    conn: sqlite3.Connection,
    *,
    series_code: str | None = None,
    match_status: str | None = "confirmed",
    release_ids: list[int] | None = None,
) -> dict[str, Any]:
    """批量导出。默认仅 confirmed（可推 Wiki / 歌词站的最小集合）。"""
    sql = """
        SELECT r.id
        FROM release r
        JOIN series s ON s.id = r.series_id
        WHERE 1=1
    """
    params: list[Any] = []
    if release_ids:
        placeholders = ",".join("?" * len(release_ids))
        sql += f" AND r.id IN ({placeholders})"
        params.extend(release_ids)
    if series_code:
        sql += " AND s.code = ?"
        params.append(series_code.zfill(2))
    if match_status:
        sql += " AND r.match_status = ?"
        params.append(match_status)
    sql += " ORDER BY s.code, (r.date_guess IS NULL), r.date_guess, r.folder_name"

    ids = [int(row["id"]) for row in conn.execute(sql, params).fetchall()]
    items = []
    for rid in ids:
        payload = export_release(conn, rid)
        if payload:
            items.append(payload)

    return {
        "schema": "imas_hub.library_export/v1",
        "exported_at": _now_iso(),
        "hub_version": __version__,
        "filter": {
            "series_code": series_code.zfill(2) if series_code else None,
            "match_status": match_status,
            "release_ids": release_ids,
        },
        "count": len(items),
        "releases": items,
    }


def write_export(payload: dict[str, Any], out_path: Path) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out_path
