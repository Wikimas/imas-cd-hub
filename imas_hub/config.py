"""路径与运行参数。"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

# 项目根目录（平铺后 imas_hub/ 与 data/ 同层）
HUB_ROOT = Path(__file__).resolve().parents[1]


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

DB_PATH = Path(os.environ.get("IMAS_DB_PATH", HUB_ROOT / "data" / "hub.db"))
# 主库封面资产（独立于本地 FLAC 目录）
COVERS_ROOT = Path(os.environ.get("IMAS_COVERS_ROOT", HUB_ROOT / "data" / "covers"))


def _load_secret_key() -> str:
    """会话签名密钥：优先 IMAS_SECRET_KEY；否则持久化到 data/secret_key（重启不失效）。

    生产（4B 部署）在服务器 .env 显式配 IMAS_SECRET_KEY。
    """
    env_key = os.environ.get("IMAS_SECRET_KEY")
    if env_key:
        return env_key
    path = HUB_ROOT / "data" / "secret_key"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            key = path.read_text(encoding="utf-8").strip()
            if key:
                return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        path.write_text(key, encoding="utf-8")
    except OSError:
        pass
    return key


# 会话签名密钥（登录 cookie 用）
SECRET_KEY = _load_secret_key()

# HTTPS 部署时置 1（服务器 4B 阶段；本机 http 保持 False）
COOKIE_SECURE = os.environ.get("IMAS_COOKIE_SECURE") == "1"

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
