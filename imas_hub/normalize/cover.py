"""主库封面：写入 data/covers，不依赖本地 CD 目录（脱钩后唯一来源）。"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

from imas_hub.config import COVER_NAMES, COVERS_ROOT
from imas_hub.db.database import utc_now

# 写入规范名
CANONICAL_STEM = "Cover"
ALLOWED_MIME = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/pjpeg": ".jpg",
    "image/png": ".png",
}
MAX_COVER_BYTES = 25 * 1024 * 1024  # 25 MB

# 目录里可识别的旧规范名（替换用，不嵌入车间逻辑）
REPLACEABLE_NAMES = {
    "cover.jpg",
    "cover.jpeg",
    "cover.png",
    "folder.jpg",
    "folder.jpeg",
    "folder.png",
    "album.jpg",
    "album.jpeg",
    "album.png",
    "front.jpg",
    "front.jpeg",
    "front.png",
}

BACK_COVER_NAMES = {
    "cover.back.jpg",
    "cover.back.jpeg",
    "cover.back.png",
    "back.jpg",
    "back.jpeg",
    "back.png",
}

FRONT_CANDIDATES = (
    "Cover.jpg",
    "Cover.jpeg",
    "Cover.png",
    "cover.jpg",
    "cover.jpeg",
    "cover.png",
)
BACK_CANDIDATES = (
    "Cover.back.jpg",
    "Cover.back.jpeg",
    "Cover.back.png",
    "cover.back.jpg",
    "cover.back.jpeg",
    "cover.back.png",
)


@dataclass
class CoverResult:
    release_id: int
    path: str
    filename: str
    mime: str
    size_bytes: int
    replaced: list[str]
    has_cover: bool = True


def hub_cover_dir(release_id: int, covers_root: Path | None = None) -> Path:
    return Path(covers_root or COVERS_ROOT) / str(int(release_id))


def is_hub_cover_path(path: str | Path, covers_root: Path | None = None) -> bool:
    """path 是否落在主库封面根下。"""
    root = Path(covers_root or COVERS_ROOT).resolve()
    try:
        Path(path).resolve().relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def sniff_image(
    data: bytes, filename: str | None = None, content_type: str | None = None
) -> tuple[str, str]:
    """返回 (mime, extension)。仅支持 JPEG / PNG。"""
    if not data:
        raise ValueError("empty image data")
    if len(data) > MAX_COVER_BYTES:
        raise ValueError(f"image too large (max {MAX_COVER_BYTES // (1024 * 1024)} MB)")

    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg", ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png", ".png"

    ct = (content_type or "").split(";")[0].strip().lower()
    if ct in ALLOWED_MIME:
        return ("image/jpeg" if ALLOWED_MIME[ct] == ".jpg" else "image/png"), ALLOWED_MIME[ct]
    if filename:
        ext = Path(filename).suffix.lower()
        if ext in (".jpg", ".jpeg"):
            return "image/jpeg", ".jpg"
        if ext == ".png":
            return "image/png", ".png"

    raise ValueError("unsupported image type (use JPEG or PNG)")


def _first_existing(directory: Path, names: tuple[str, ...]) -> Path | None:
    if not directory.is_dir():
        return None
    for name in names:
        p = directory / name
        if p.is_file():
            return p
    return None


def find_hub_cover_path(
    release_id: int, covers_root: Path | None = None
) -> Path | None:
    return _first_existing(hub_cover_dir(release_id, covers_root), FRONT_CANDIDATES)


def find_hub_back_cover_path(
    release_id: int, covers_root: Path | None = None
) -> Path | None:
    return _first_existing(hub_cover_dir(release_id, covers_root), BACK_CANDIDATES)


def find_cover_path(conn: sqlite3.Connection, release_id: int) -> Path | None:
    """优先主库封面目录，其次 cover_art.path（脱钩后无车间目录回退）。"""
    hub = find_hub_cover_path(release_id)
    if hub is not None:
        return hub

    for r in conn.execute(
        "SELECT path FROM cover_art WHERE release_id=? ORDER BY preferred DESC",
        (release_id,),
    ):
        if not r["path"]:
            continue
        p = Path(r["path"])
        if p.is_file():
            return p
    return None


def find_back_cover_path(conn: sqlite3.Connection, release_id: int) -> Path | None:
    """背面：仅主库封面目录。"""
    return find_hub_back_cover_path(release_id)


def _register_hub_cover(
    conn: sqlite3.Connection,
    release_id: int,
    dest: Path,
    *,
    preferred: bool = True,
) -> None:
    """把 hub 封面路径写入 cover_art。"""
    path_s = str(dest)
    if preferred:
        conn.execute(
            "UPDATE cover_art SET preferred=0 WHERE release_id=?",
            (release_id,),
        )
        # 同 path 更新；否则插入
        existing = conn.execute(
            "SELECT id FROM cover_art WHERE release_id=? AND path=?",
            (release_id, path_s),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE cover_art SET preferred=1 WHERE id=?",
                (int(existing["id"]),),
            )
        else:
            conn.execute(
                """
                INSERT INTO cover_art(release_id, path, preferred)
                VALUES (?, ?, 1)
                """,
                (release_id, path_s),
            )
    else:
        existing = conn.execute(
            "SELECT id FROM cover_art WHERE release_id=? AND path=?",
            (release_id, path_s),
        ).fetchone()
        if not existing:
            conn.execute(
                """
                INSERT INTO cover_art(release_id, path, preferred)
                VALUES (?, ?, 0)
                """,
                (release_id, path_s),
            )

    conn.execute(
        "UPDATE release SET has_cover=1, updated_at=? WHERE id=?",
        (utc_now(), release_id),
    )


def set_release_cover(
    conn: sqlite3.Connection,
    release_id: int,
    data: bytes,
    *,
    filename: str | None = None,
    content_type: str | None = None,
    covers_root: Path | None = None,
) -> CoverResult:
    """将图片写入主库封面目录 Cover.jpg / Cover.png，更新 cover_art / has_cover。"""
    row = conn.execute(
        "SELECT id FROM release WHERE id=?",
        (release_id,),
    ).fetchone()
    if not row:
        raise LookupError(f"release {release_id} not found")

    mime, ext = sniff_image(data, filename=filename, content_type=content_type)
    dest_dir = hub_cover_dir(release_id, covers_root)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{CANONICAL_STEM}{ext}"
    replaced: list[str] = []

    # 清理 hub 目录内旧主封面（保留 Cover.back.*）
    for f in list(dest_dir.iterdir()) if dest_dir.is_dir() else []:
        if not f.is_file():
            continue
        low = f.name.lower()
        if low.startswith("cover.back") or low in BACK_COVER_NAMES:
            continue
        if f.resolve() == dest.resolve():
            continue
        if low in REPLACEABLE_NAMES or low in COVER_NAMES or low.startswith("cover."):
            try:
                f.unlink()
                replaced.append(f.name)
            except OSError:
                pass
            # 删除对应 cover_art 行
            conn.execute(
                "DELETE FROM cover_art WHERE release_id=? AND path=?",
                (release_id, str(f)),
            )

    dest.write_bytes(data)
    size = dest.stat().st_size

    # 清掉指向非 hub 的 preferred 封面记录，避免 find 误选外部路径
    for r in conn.execute(
        "SELECT id, path FROM cover_art WHERE release_id=?",
        (release_id,),
    ).fetchall():
        p = r["path"] or ""
        if not is_hub_cover_path(p, covers_root):
            conn.execute("DELETE FROM cover_art WHERE id=?", (int(r["id"]),))

    _register_hub_cover(conn, release_id, dest, preferred=True)

    return CoverResult(
        release_id=release_id,
        path=str(dest),
        filename=dest.name,
        mime=mime,
        size_bytes=size,
        replaced=replaced,
        has_cover=True,
    )
