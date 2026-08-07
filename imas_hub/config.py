"""路径与运行参数。"""

from __future__ import annotations

import os
from pathlib import Path

# 项目根目录（平铺后 imas_hub/ 与 data/ 同层）
HUB_ROOT = Path(__file__).resolve().parents[1]
# 仓库根（含 本家CD / HiRes…）；平铺后与 HUB_ROOT 相同
REPO_ROOT = HUB_ROOT


def _load_dotenv(path: Path | None = None) -> None:
    """轻量加载 .env（不覆盖已有环境变量；无第三方依赖）。"""
    env_path = path or (HUB_ROOT / ".env")
    if not env_path.is_file():
        return
    try:
        text = env_path.read_text(encoding="utf-8")
    except OSError:
        return
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key or key in os.environ:
            continue
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        os.environ[key] = val


_load_dotenv()

CD_ROOT = Path(os.environ.get("IMAS_CD_ROOT", REPO_ROOT / "本家CD"))
DB_PATH = Path(os.environ.get("IMAS_DB_PATH", HUB_ROOT / "data" / "hub.db"))
# 主库封面资产（独立于本地 FLAC 目录）
COVERS_ROOT = Path(os.environ.get("IMAS_COVERS_ROOT", HUB_ROOT / "data" / "covers"))

# wikimas MediaWiki（阶段 3）— 先本机 WSL localhost:8080
WIKI_URL = os.environ.get("IMAS_WIKI_URL", "http://localhost:8080").rstrip("/")
WIKI_USER = os.environ.get("IMAS_WIKI_USER") or os.environ.get("IMAS_WIKI_BOT_USER")
WIKI_PASS = os.environ.get("IMAS_WIKI_PASS") or os.environ.get("IMAS_WIKI_BOT_PASS")
WIKI_UA = os.environ.get(
    "IMAS_WIKI_UA",
    "765PRO-Hub-WikiBot/0.3 (local; contact: local-dev)",
)

COVER_NAMES = {
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
