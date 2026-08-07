"""阶段 3：wikimas BOT — 渲染 wikitext + 推送 MediaWiki。"""

from imas_hub.wiki.render import render_album_page, RenderedPage, wiki_cover_dest_name
from imas_hub.wiki.client import WikiClient, WikiConfig

__all__ = [
    "render_album_page",
    "RenderedPage",
    "wiki_cover_dest_name",
    "WikiClient",
    "WikiConfig",
]
