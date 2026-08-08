"""export JSON / hub payload → wikimas 专辑页 wikitext。"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from imas_hub.wiki.artists_wiki import merge_album_artists, track_artist_credit
from imas_hub.wiki.track_title import (
    escape_track_title_param,
    escape_wiki_template_value,
    format_duration,
    split_track_title,
)

# 厂牌显示名（wiki 范例多用现行名）
LABEL_WIKI: dict[str, str] = {
    "Columbia Music Entertainment": "Nippon Columbia",
    "Columbia": "Nippon Columbia",
    "Nippon Columbia": "Nippon Columbia",
    "Columbia Marketing": "Nippon Columbia",
    "Lantis": "Lantis",
    "Bandai Namco Arts": "Lantis",
    "Bandai Namco Music Live": "Lantis",
}

DEFAULT_BRAND = "765as"
DEFAULT_TYPE = "专辑"


@dataclass
class RenderedPage:
    page_title: str
    wikitext: str
    release_id: int | None = None
    catalog: str | None = None
    brand: str = DEFAULT_BRAND
    review_status: str | None = None
    content_hash: str = ""
    warnings: list[str] = field(default_factory=list)
    track_count: int = 0

    def __post_init__(self) -> None:
        if not self.content_hash and self.wikitext:
            self.content_hash = hashlib.sha256(
                self.wikitext.encode("utf-8")
            ).hexdigest()


def _wiki_label(raw: str | None) -> str | None:
    if not raw:
        return None
    s = raw.strip()
    return LABEL_WIKI.get(s, s)


def _escape_template_value(s: str) -> str:
    """模板参数值转义（委托 track_title，统一 | <> {{ }}）。"""
    return escape_wiki_template_value(s)


def _album_intro(title: str, brand: str) -> str:
    """简短导语；本家 brand=765as。"""
    if brand == "765as":
        series = "「[[偶像大师 (本家)|偶像大师]]」"
    else:
        series = "偶像大师"
    return f"'''{_escape_template_value(title)}''' 是{series}系列相关专辑。"


# 与站内 {{Notice *}} 一致，走 {{Msg}} 提示框
BOT_NOTICE = """{{Msg
|lv=1
|icon=material-symbols:smart-toy-outline
|title=本页面由自动化程序编写
|content=本页内容由程序根据本地曲库与 MusicBrainz 数据自动生成。如发现错误，请直接编辑本页或联系维护者。
}}"""

BOT_CATEGORY = "[[分类:自动化程序编写的页面]]"


def wiki_cover_dest_name(
    release: dict[str, Any],
    covers: dict[str, Any] | None = None,
    *,
    side: str = "front",
) -> str | None:
    """Wiki 目标文件名：Cover {catalog} front.jpg|.png（随本地扩展名）。"""
    ext = ".jpg"
    preferred = (covers or {}).get("preferred") if side == "front" else (covers or {}).get("back")
    if preferred:
        fn = preferred.get("filename") or preferred.get("path") or ""
        e = Path(str(fn)).suffix.lower()
        if e in (".jpg", ".jpeg"):
            ext = ".jpg"
        elif e == ".png":
            ext = ".png"
    cat = (release.get("catalog_no") or release.get("wiki", {}).get("catalog") or "").strip()
    if cat:
        return f"Cover {cat} {side}{ext}"
    title = (release.get("title") or "").strip()
    if title:
        safe = re.sub(r'[\\/:*?"<>|]+', "", title)[:60]
        return f"Cover {safe} {side}{ext}"
    return None


def _cover_filename(release: dict[str, Any], covers: dict[str, Any] | None) -> str | None:
    return wiki_cover_dest_name(release, covers, side="front")


def _render_track_line(track: dict[str, Any]) -> tuple[str, list[str]]:
    """单轨 → {{Track|...}} 行。返回 (wikitext, warnings)。"""
    warns: list[str] = []
    raw_title = track.get("title") or ""
    split = split_track_title(raw_title)
    credit = track_artist_credit(track.get("artists"))
    if split.empty_artist:
        credit = track_artist_credit(None)
    length = format_duration(
        track.get("duration_ms"),
        track.get("duration") or (track.get("wiki") or {}).get("length"),
    )
    nolink = split.nolink or bool((track.get("wiki") or {}).get("nolink"))
    if not split.title:
        warns.append(f"empty title position={track.get('position')}")
    # 歌曲页链接用标题：必须页名安全（无 ASCII <> 等）
    safe_title = escape_track_title_param(split.title)

    # {{Track|title|ver=...|artist|length|nolink=1|artist=...|outro=...}}
    # {{{1}}}=title {{{2}}}=seiyuu {{{3}}}=length；非声优用命名 artist=
    parts: list[str] = [f"{{{{Track|{safe_title}"]
    if split.ver:
        parts.append(f"|ver={_escape_template_value(split.ver)}")

    if credit.as_named_artist and credit.text:
        # 位置 artist 留空，避免 {{cv}} 误吃制作人
        parts.append("|")
        parts.append(f"|{length}")
        parts.append(f"|artist={_escape_template_value(credit.text)}")
    else:
        parts.append(f"|{_escape_template_value(credit.text)}")
        parts.append(f"|{length}")

    if nolink:
        parts.append("|nolink=1")
    if split.outro:
        parts.append(f"|outro={_escape_template_value(split.outro)}")
    line = "".join(parts) + "}}"
    return line, warns


def _render_credits_section(tracks: list[dict[str, Any]]) -> str | None:
    """有作词/作曲时生成制作人员草稿。"""
    blocks: list[str] = []
    for t in tracks:
        lyricist = (t.get("lyricist") or "").strip()
        composer = (t.get("composer") or "").strip()
        if not lyricist and not composer:
            continue
        split = split_track_title(t.get("title"))
        display = split.title
        if split.ver:
            # ver 可能已含括号，如 (M@STER VERSION)
            v = split.ver
            display = f"{display} {v}" if v.startswith("(") else f"{display} ({v})"
        if split.karaoke:
            continue  # karaoke 通常不写制作
        lines = [f"'''{_escape_template_value(display)}'''", "{{Credits"]
        if lyricist:
            lines.append(f"|作詞={_escape_template_value(lyricist)}")
        if composer:
            lines.append(f"|作曲={_escape_template_value(composer)}")
        lines.append("}}")
        blocks.append("\n".join(lines))
    if not blocks:
        return None
    return "{{col|\n" + "\n\n".join(blocks) + "\n}}"


def render_album_page(
    payload: dict[str, Any],
    *,
    brand: str | None = None,
    include_intro: bool = True,
    include_credits: bool = True,
    include_scan_stub: bool = True,
    missing_scan: bool | None = None,
) -> RenderedPage:
    """从 imas_hub.release_export/v1 渲染专辑页。"""
    warnings: list[str] = []
    release = payload.get("release") or {}
    if not release and "title" in payload:
        # 允许直接传 release 字典
        release = payload

    title = (release.get("title") or release.get("wiki", {}).get("title") or "").strip()
    if not title:
        raise ValueError("release.title is required")

    review_status = release.get("review_status")
    if review_status and review_status != "reviewed":
        warnings.append(
            f"review_status={review_status!r} — 仅 reviewed 可推；渲染仍生成草稿"
        )

    brand = brand or (release.get("wiki") or {}).get("brand") or DEFAULT_BRAND
    catalog = release.get("catalog_no") or (release.get("wiki") or {}).get("catalog")
    barcode = release.get("barcode") or (release.get("wiki") or {}).get("barcode")
    date = release.get("date") or release.get("date_guess") or (release.get("wiki") or {}).get(
        "release"
    )
    label = _wiki_label(
        release.get("label") or release.get("label_hint") or (release.get("wiki") or {}).get("label")
    )
    media = payload.get("media") or []
    tracks_flat = payload.get("tracks") or []
    if not tracks_flat and media:
        for m in media:
            tracks_flat.extend(m.get("tracks") or [])

    medium_count = int(release.get("medium_count") or len(media) or 1)
    spec = f"{medium_count} CD"

    # 专辑级 artist：仅非 karaoke/instrumental 轨
    vocal_artists: list[list[dict] | None] = []
    for t in tracks_flat:
        st = split_track_title(t.get("title"))
        if st.empty_artist:
            continue
        vocal_artists.append(t.get("artists"))
    album_artist = merge_album_artists(vocal_artists)
    if not album_artist:
        warnings.append("album artist empty after CV extraction")

    cover_name = _cover_filename(release, payload.get("covers"))
    # 脱钩后主库无本地扫描，图库恒为缺失占位
    if missing_scan is None:
        missing_scan = True

    # --- Album info ---
    info_lines = [
        "{{Album info",
        f"|title={_escape_template_value(title)}",
    ]
    if cover_name:
        info_lines.append(f"|image={_escape_template_value(cover_name)}")
    info_lines.append(f"|type={DEFAULT_TYPE}")
    info_lines.append(f"|brand={brand}")
    if album_artist:
        info_lines.append(f"|artist={_escape_template_value(album_artist)}")
    if catalog:
        info_lines.append(f"|catalog={_escape_template_value(str(catalog))}")
    if barcode:
        info_lines.append(f"|barcode={_escape_template_value(str(barcode))}")
    info_lines.append(f"|spec={spec}")
    if label:
        info_lines.append(f"|label={_escape_template_value(label)}")
    if date:
        # 保证 yyyy-mm-dd
        d = str(date).replace(".", "-")[:10]
        info_lines.append(f"|release={d}")
    mb = release.get("mb_release_id")
    if mb:
        # 用完整 URL（本机 interwiki musicbrainz 前缀未带 /release/）
        mb_url = f"https://musicbrainz.org/release/{mb}"
        info_lines.append(
            f"|link={{{{Exlink|{_escape_template_value(mb_url)}|name=MusicBrainz}}}}"
        )
    info_lines.append("}}")

    parts: list[str] = ["\n".join(info_lines), ""]

    # 提示框放在导语「是…系列相关专辑。」之前
    parts.append(BOT_NOTICE)
    parts.append("")
    if include_intro:
        parts.append(_album_intro(title, brand))
        parts.append("")

    # --- Tracklist ---
    parts.append("== 收录曲目 ==")
    if media:
        for mi, m in enumerate(media):
            head_label = catalog or m.get("title") or f"Disc {m.get('position') or mi + 1}"
            if len(media) > 1:
                # 多碟：每碟一个 Track head，head 用 品番 Disc N 或 medium title
                disc_title = m.get("title") or f"Disc {m.get('position') or mi + 1}"
                head_label = f"{catalog} {disc_title}" if catalog else disc_title
            parts.append(f"{{{{Track head|{_escape_template_value(str(head_label))}}}}}")
            mtracks = m.get("tracks") or []
            for t in mtracks:
                line, tw = _render_track_line(t)
                warnings.extend(tw)
                parts.append("|-")
                parts.append(line)
            parts.append("|-")
            parts.append("{{Track_end}}")
            if mi < len(media) - 1:
                parts.append("")
    else:
        parts.append(f"{{{{Track head|{_escape_template_value(str(catalog or title))}}}}}")
        for t in tracks_flat:
            line, tw = _render_track_line(t)
            warnings.extend(tw)
            parts.append("|-")
            parts.append(line)
        parts.append("|-")
        parts.append("{{Track_end}}")

    parts.append("")

    # --- Credits ---
    if include_credits:
        cred = _render_credits_section(tracks_flat)
        parts.append("== 制作人员 ==")
        if cred:
            parts.append(cred)
        else:
            parts.append("<!-- 以 Booklet 为准；hub 暂无完整 credits -->")
        parts.append("")

    # --- Scan ---
    if include_scan_stub:
        parts.append("== 图库 ==")
        if missing_scan:
            parts.append("{{Scan")
            parts.append("|missing=1")
            parts.append("}}")
        else:
            parts.append("{{Scan")
            parts.append("<!-- 本地有 Scan/，文件名需人工或后续 BOT 上传后填写 -->")
            parts.append("}}")
        parts.append("")

    # --- Nav ---
    parts.append("== 注释与链接 ==")
    parts.append(f"{{{{Navbox album|{brand}}}}}")
    parts.append("")
    # 分类 + hub 溯源注释
    rid = release.get("id")
    parts.append(BOT_CATEGORY)
    parts.append(
        f"<!-- imas_hub release_id={rid} catalog={catalog} "
        f"mb={mb or ''} hash_src=v1 -->"
    )

    wikitext = "\n".join(parts)
    if not wikitext.endswith("\n"):
        wikitext += "\n"

    return RenderedPage(
        page_title=title,
        wikitext=wikitext,
        release_id=int(rid) if rid is not None else None,
        catalog=str(catalog) if catalog else None,
        brand=brand,
        review_status=review_status,
        track_count=len(tracks_flat),
        warnings=warnings,
    )
