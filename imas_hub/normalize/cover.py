"""主库封面：写入 data/covers，不依赖本地 FLAC 目录。"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from imas_hub.config import CD_ROOT, COVER_NAMES, COVERS_ROOT
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

# 车间目录里可识别的旧规范名（抽取用，不嵌入 hub 逻辑）
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
    """优先主库封面目录，其次 cover_art.path，最后车间 release.path（未迁移时）。"""
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

    row = conn.execute("SELECT path FROM release WHERE id=?", (release_id,)).fetchone()
    if not row or not row["path"]:
        return None
    release_dir = Path(row["path"])
    if not release_dir.is_dir():
        return None
    found = _first_existing(release_dir, FRONT_CANDIDATES)
    if found is not None:
        return found
    for f in release_dir.iterdir():
        if f.is_file() and f.name.lower() in COVER_NAMES:
            return f
    return None


def find_back_cover_path(conn: sqlite3.Connection, release_id: int) -> Path | None:
    """背面：hub → 车间目录 Cover.back.*。"""
    hub = find_hub_back_cover_path(release_id)
    if hub is not None:
        return hub
    row = conn.execute("SELECT path FROM release WHERE id=?", (release_id,)).fetchone()
    if not row or not row["path"]:
        return None
    return _first_existing(Path(row["path"]), BACK_CANDIDATES)


def _register_hub_cover(
    conn: sqlite3.Connection,
    release_id: int,
    dest: Path,
    *,
    preferred: bool = True,
) -> None:
    """把 hub 封面路径写入 cover_art；不绑定 local_file。"""
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
                "UPDATE cover_art SET preferred=1, file_id=NULL WHERE id=?",
                (int(existing["id"]),),
            )
        else:
            # 清掉指向车间的旧 preferred 行可保留作历史，但只保留一条 preferred
            conn.execute(
                """
                INSERT INTO cover_art(release_id, file_id, path, preferred)
                VALUES (?, NULL, ?, 1)
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
                INSERT INTO cover_art(release_id, file_id, path, preferred)
                VALUES (?, NULL, ?, 0)
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
    cd_root: Path | None = None,  # 保留签名兼容；不再写入 CD 目录
    covers_root: Path | None = None,
) -> CoverResult:
    """将图片写入主库封面目录 Cover.jpg / Cover.png，更新 cover_art / has_cover。"""
    _ = cd_root  # unused; covers are hub-owned
    row = conn.execute(
        "SELECT id, path, folder_name FROM release WHERE id=?",
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

    # 清掉指向非 hub 的 preferred 封面记录，避免 find 误选车间路径
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


def extract_release_cover(
    conn: sqlite3.Connection,
    release_id: int,
    *,
    covers_root: Path | None = None,
    overwrite: bool = False,
) -> dict:
    """从车间目录 / 旧 cover_art 复制主封面（及背面）到 hub。

    返回 {release_id, front, back, skipped, source}。
    """
    root = Path(covers_root or COVERS_ROOT)
    dest_dir = hub_cover_dir(release_id, root)
    result: dict = {
        "release_id": release_id,
        "front": None,
        "back": None,
        "skipped": False,
        "source": None,
    }

    existing = find_hub_cover_path(release_id, root)
    if existing is not None and not overwrite:
        _register_hub_cover(conn, release_id, existing, preferred=True)
        result["front"] = str(existing)
        result["skipped"] = True
        result["source"] = "hub"
        # 仍尝试补背面
        if find_hub_back_cover_path(release_id, root) is None:
            src_back = _workshop_back_source(conn, release_id)
            if src_back:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest_b = dest_dir / f"Cover.back{src_back.suffix.lower().replace('jpeg', 'jpg')}"
                if dest_b.suffix.lower() == ".jpeg":
                    dest_b = dest_b.with_suffix(".jpg")
                # normalize ext
                ext = src_back.suffix.lower()
                if ext == ".jpeg":
                    ext = ".jpg"
                dest_b = dest_dir / f"Cover.back{ext}"
                shutil.copy2(src_back, dest_b)
                _register_hub_cover(conn, release_id, dest_b, preferred=False)
                result["back"] = str(dest_b)
        else:
            result["back"] = str(find_hub_back_cover_path(release_id, root))
        return result

    # 找正面源：优先 cover_art 已有文件，再车间
    src_front: Path | None = None
    source_kind = None
    for r in conn.execute(
        "SELECT path FROM cover_art WHERE release_id=? ORDER BY preferred DESC",
        (release_id,),
    ):
        if not r["path"]:
            continue
        p = Path(r["path"])
        if p.is_file() and not is_hub_cover_path(p, root):
            src_front = p
            source_kind = "cover_art"
            break
        if p.is_file() and is_hub_cover_path(p, root):
            src_front = p
            source_kind = "hub"
            break

    if src_front is None:
        row = conn.execute("SELECT path FROM release WHERE id=?", (release_id,)).fetchone()
        if row and row["path"]:
            release_dir = Path(row["path"])
            src_front = _first_existing(release_dir, FRONT_CANDIDATES)
            if src_front is None and release_dir.is_dir():
                for f in release_dir.iterdir():
                    if f.is_file() and f.name.lower() in COVER_NAMES:
                        src_front = f
                        break
            if src_front is not None:
                source_kind = "workshop"

    if src_front is None or not src_front.is_file():
        result["skipped"] = True
        result["source"] = None
        return result

    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = src_front.suffix.lower()
    if ext == ".jpeg":
        ext = ".jpg"
    if ext not in (".jpg", ".png"):
        # 尝试 sniff
        try:
            data = src_front.read_bytes()
            _, ext = sniff_image(data, filename=src_front.name)
        except ValueError:
            result["skipped"] = True
            result["source"] = source_kind
            return result
    dest = dest_dir / f"{CANONICAL_STEM}{ext}"
    if src_front.resolve() != dest.resolve():
        shutil.copy2(src_front, dest)

    # 清理旧 cover_art 中的车间路径，登记 hub
    for r in conn.execute(
        "SELECT id, path FROM cover_art WHERE release_id=?",
        (release_id,),
    ).fetchall():
        p = r["path"] or ""
        if not is_hub_cover_path(p, root):
            conn.execute("DELETE FROM cover_art WHERE id=?", (int(r["id"]),))

    _register_hub_cover(conn, release_id, dest, preferred=True)
    result["front"] = str(dest)
    result["source"] = source_kind

    src_back = _workshop_back_source(conn, release_id)
    if src_back and src_back.is_file():
        ext_b = src_back.suffix.lower()
        if ext_b == ".jpeg":
            ext_b = ".jpg"
        dest_b = dest_dir / f"Cover.back{ext_b}"
        if src_back.resolve() != dest_b.resolve():
            shutil.copy2(src_back, dest_b)
        _register_hub_cover(conn, release_id, dest_b, preferred=False)
        result["back"] = str(dest_b)

    return result


def _workshop_back_source(conn: sqlite3.Connection, release_id: int) -> Path | None:
    hub = find_hub_back_cover_path(release_id)
    if hub is not None:
        return None  # already have
    row = conn.execute("SELECT path FROM release WHERE id=?", (release_id,)).fetchone()
    if not row or not row["path"]:
        return None
    return _first_existing(Path(row["path"]), BACK_CANDIDATES)


def extract_all_covers(
    conn: sqlite3.Connection,
    *,
    covers_root: Path | None = None,
    overwrite: bool = False,
) -> list[dict]:
    """全库抽取封面到 hub。"""
    ids = [
        int(r["id"])
        for r in conn.execute("SELECT id FROM release ORDER BY id").fetchall()
    ]
    return [
        extract_release_cover(
            conn, rid, covers_root=covers_root, overwrite=overwrite
        )
        for rid in ids
    ]


def release_has_hub_cover(release_id: int, covers_root: Path | None = None) -> bool:
    return find_hub_cover_path(release_id, covers_root) is not None
