"""本机 / 远程 MediaWiki API 客户端（登录、读页、编辑、上传）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import httpx


@dataclass
class WikiConfig:
    base_url: str = "http://localhost:8080"
    username: str | None = None
    password: str | None = None
    user_agent: str = "IMAS-CD-Hub-WikiBot/0.3 (local; contact: local-dev)"
    timeout: float = 60.0

    @classmethod
    def from_env(cls) -> "WikiConfig":
        # 确保 .env 已加载（config 导入时会 load；此处再兜底）
        try:
            from imas_hub.config import WIKI_PASS, WIKI_UA, WIKI_URL, WIKI_USER

            return cls(
                base_url=WIKI_URL,
                username=WIKI_USER,
                password=WIKI_PASS,
                user_agent=WIKI_UA,
            )
        except ImportError:
            return cls(
                base_url=os.environ.get("IMAS_WIKI_URL", "http://localhost:8080").rstrip(
                    "/"
                ),
                username=os.environ.get("IMAS_WIKI_USER")
                or os.environ.get("IMAS_WIKI_BOT_USER"),
                password=os.environ.get("IMAS_WIKI_PASS")
                or os.environ.get("IMAS_WIKI_BOT_PASS"),
                user_agent=os.environ.get(
                    "IMAS_WIKI_UA",
                    "IMAS-CD-Hub-WikiBot/0.3 (local; contact: local-dev)",
                ),
            )


class WikiError(RuntimeError):
    pass


class WikiClient:
    def __init__(self, config: WikiConfig | None = None):
        self.config = config or WikiConfig.from_env()
        self._client = httpx.Client(
            base_url=self.config.base_url,
            headers={"User-Agent": self.config.user_agent},
            timeout=self.config.timeout,
            follow_redirects=True,
        )
        self._logged_in = False

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WikiClient":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    @property
    def api_url(self) -> str:
        return "/api.php"

    def _api(self, method: str = "GET", **params: Any) -> dict[str, Any]:
        params.setdefault("format", "json")
        if method.upper() == "GET":
            r = self._client.get(self.api_url, params=params)
        else:
            # token 等敏感字段走 form body
            r = self._client.post(self.api_url, data=params)
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            raise WikiError(f"{err.get('code')}: {err.get('info')}")
        return data

    def siteinfo(self) -> dict[str, Any]:
        data = self._api(
            "GET",
            action="query",
            meta="siteinfo",
            siprop="general",
        )
        return data.get("query", {}).get("general", {})

    def login(self, username: str | None = None, password: str | None = None) -> None:
        user = username or self.config.username
        passwd = password or self.config.password
        if not user or not passwd:
            raise WikiError(
                "缺少 Wiki 凭据：设置 IMAS_WIKI_USER / IMAS_WIKI_PASS "
                "或传入 --user / --password"
            )
        # login token
        tok = self._api("GET", action="query", meta="tokens", type="login")
        login_token = tok["query"]["tokens"]["logintoken"]
        result = self._api(
            "POST",
            action="login",
            lgname=user,
            lgpassword=passwd,
            lgtoken=login_token,
        )
        login = result.get("login", {})
        if login.get("result") != "Success":
            raise WikiError(f"login failed: {login}")
        self._logged_in = True
        self.config.username = user

    def csrf_token(self) -> str:
        data = self._api("GET", action="query", meta="tokens")
        return data["query"]["tokens"]["csrftoken"]

    def get_page(self, title: str) -> dict[str, Any] | None:
        """返回 {title, pageid, content, missing} 或 None。"""
        data = self._api(
            "GET",
            action="query",
            prop="revisions|info",
            rvprop="content|timestamp|ids",
            rvslots="main",
            titles=title,
            curtimestamp=1,
        )
        pages = data.get("query", {}).get("pages", {})
        for p in pages.values():
            if "missing" in p:
                return {
                    "title": title,
                    "missing": True,
                    "content": None,
                    "pageid": None,
                    "timestamp": None,
                }
            rev = (p.get("revisions") or [{}])[0]
            content = rev.get("slots", {}).get("main", {}).get("*") or rev.get("*")
            return {
                "title": p.get("title", title),
                "missing": False,
                "content": content,
                "pageid": p.get("pageid"),
                "timestamp": rev.get("timestamp"),
                "revid": rev.get("revid"),
            }
        return None

    def edit(
        self,
        title: str,
        text: str,
        *,
        summary: str = "hub bot: sync album",
        create_only: bool = False,
        bot: bool = True,
        minor: bool = False,
    ) -> dict[str, Any]:
        if not self._logged_in:
            self.login()
        token = self.csrf_token()
        params: dict[str, Any] = {
            "action": "edit",
            "title": title,
            "text": text,
            "token": token,
            "summary": summary,
            "format": "json",
        }
        if create_only:
            params["createonly"] = "1"
        if bot:
            params["bot"] = "1"
        if minor:
            params["minor"] = "1"
        data = self._api("POST", **params)
        edit = data.get("edit", {})
        if edit.get("result") != "Success":
            raise WikiError(f"edit failed: {data}")
        return edit

    def upload(
        self,
        file_path: Path,
        *,
        filename: str | None = None,
        comment: str = "hub bot: cover upload",
        ignore_warnings: bool = True,
    ) -> dict[str, Any]:
        if not self._logged_in:
            self.login()
        path = Path(file_path)
        if not path.is_file():
            raise WikiError(f"file not found: {path}")
        dest = filename or path.name
        token = self.csrf_token()
        data_fields = {
            "action": "upload",
            "filename": dest,
            "token": token,
            "comment": comment,
            "format": "json",
        }
        if ignore_warnings:
            data_fields["ignorewarnings"] = "1"
        with path.open("rb") as fh:
            r = self._client.post(
                self.api_url,
                data=data_fields,
                files={"file": (dest, fh)},
            )
        r.raise_for_status()
        data = r.json()
        if "error" in data:
            err = data["error"]
            code = err.get("code") or ""
            # 文件已存在且内容相同：视为成功（幂等）
            if code in ("fileexists-no-change", "fileexists-duplicate"):
                return {
                    "result": "Success",
                    "filename": dest,
                    "warnings": {code: err.get("info")},
                }
            raise WikiError(f"upload {code}: {err.get('info')}")
        return data.get("upload", data)

    def page_url(self, title: str) -> str:
        # articlepath is usually /wiki/$1
        from urllib.parse import quote

        return urljoin(self.config.base_url + "/", "wiki/" + quote(title.replace(" ", "_"), safe="/:@!$&'()*+,;=-._~()"))
