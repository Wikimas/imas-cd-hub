"""CLI: init-db / status / serve / export / wiki-render / wiki-push。

一次性批处理（扫盘、MB 匹配、retag、改名等）已随代码清理退出项目；
日常维护走 Web UI（`python -m imas_hub serve`）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from imas_hub import __version__
from imas_hub.config import DB_PATH


def cmd_init_db(args: argparse.Namespace) -> int:
    from imas_hub.db.database import init_db

    path = init_db(Path(args.db) if args.db else None)
    print(f"DB ready: {path}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from imas_hub.db.database import connect, init_db

    init_db(Path(args.db) if args.db else None)
    conn = connect(Path(args.db) if args.db else None)
    try:
        series = conn.execute(
            """
            SELECT * FROM v_series_summary
            ORDER BY (first_release_date IS NULL), first_release_date, code
            """
        ).fetchall()
        if not series:
            print("No data. 请用 Web UI 新建系列/专辑（python -m imas_hub serve）")
            return 1
        print(
            f"{'code':<4} {'series':<40} {'rel':>4} {'cfm':>4} "
            f"{'pend':>4} {'unm':>4} {'ok':>4} {'bad':>4} {'audio':>6}"
        )
        print("-" * 90)
        for s in series:
            name = s["folder_name"] or s["title"] or s["code"]
            print(
                f"{s['code']:<4} {name[:40]:<40} "
                f"{s['release_count'] or 0:>4} {s['confirmed_count'] or 0:>4} "
                f"{s['pending_count'] or 0:>4} {s['unmatched_count'] or 0:>4} "
                f"{s['integrity_ok_count'] or 0:>4} {s['integrity_bad_count'] or 0:>4} "
                f"{s['audio_file_total'] or 0:>6}"
            )
        totals = conn.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM release) AS releases,
                (SELECT COUNT(*) FROM track) AS tracks,
                (SELECT COUNT(*) FROM local_file) AS files,
                (SELECT COUNT(*) FROM local_file WHERE integrity='bad') AS bad_files
            """
        ).fetchone()
        print("-" * 90)
        print(
            f"Totals: releases={totals['releases']} tracks={totals['tracks']} "
            f"files={totals['files']} bad_files={totals['bad_files']}"
        )
        print(f"DB: {args.db or DB_PATH}")
    finally:
        conn.close()
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run(
        "imas_hub.api.app:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    """导出 JSON 给 wikimas BOT / 歌词站（单张或按系列，不做全库导出）。"""
    from imas_hub.db.database import connect, init_db
    from imas_hub.export import export_release, export_releases, write_export

    if not args.series and not args.release_id:
        print("Need --series or --release-id")
        return 1

    init_db(Path(args.db) if args.db else None)
    conn = connect(Path(args.db) if args.db else None)
    try:
        if args.release_id:
            payload = export_release(conn, args.release_id)
            if not payload:
                print(f"release {args.release_id} not found")
                return 1
            tag = f"r{args.release_id}"
        else:
            status = args.match_status
            if status is not None and status.strip() == "":
                status = None
            payload = export_releases(
                conn,
                series_code=args.series,
                match_status=status,
            )
            tag = f"{args.series}_{status or 'any'}"

        if args.out:
            out = Path(args.out)
        else:
            out = Path("data") / f"export_{tag}.json"
        write_export(payload, out)
        count = payload.get("count")
        if count is None:
            count = 1
        print(f"Exported {count} release(s) → {out.resolve()}")
        return 0
    finally:
        conn.close()


def cmd_wiki_render(args: argparse.Namespace) -> int:
    """渲染 wikimas 专辑页 wikitext（不推送）。"""
    from imas_hub.db.database import connect, init_db
    from imas_hub.wiki.push import render_release, select_release_ids

    init_db(Path(args.db) if args.db else None)
    conn = connect(Path(args.db) if args.db else None)
    out_dir = Path(args.out or "data/wiki_out")
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        ids = select_release_ids(
            conn,
            release_id=args.release_id,
            series_code=args.series,
            limit=args.limit,
            match_status=args.match_status or "confirmed",
        )
        if not ids:
            print("No releases matched")
            return 1
        ok = 0
        for rid in ids:
            try:
                _payload, page = render_release(conn, rid)
            except Exception as e:
                print(f"r{rid}: ERROR {e}")
                continue
            safe = f"r{rid}_" + "".join(
                c if c.isalnum() or c in "-_" else "_" for c in page.page_title
            )[:80]
            path = out_dir / f"{safe}.wiki"
            path.write_text(page.wikitext, encoding="utf-8")
            warn = f" warnings={page.warnings}" if page.warnings else ""
            print(
                f"r{rid}: {page.page_title}  tracks={page.track_count}  "
                f"hash={page.content_hash[:12]}… → {path.name}{warn}"
            )
            ok += 1
        print(f"Rendered {ok}/{len(ids)} → {out_dir.resolve()}")
        return 0 if ok else 1
    finally:
        conn.close()


def cmd_wiki_push(args: argparse.Namespace) -> int:
    """推送专辑页到 MediaWiki。默认 dry-run；--apply 才写入本机/远程 wiki。"""
    from imas_hub.db.database import connect, init_db
    from imas_hub.wiki.client import WikiConfig
    from imas_hub.wiki.push import push_many, select_release_ids

    init_db(Path(args.db) if args.db else None)
    conn = connect(Path(args.db) if args.db else None)
    out_dir = Path(args.out or "data/wiki_out")
    try:
        ids = select_release_ids(
            conn,
            release_id=args.release_id,
            series_code=args.series,
            limit=args.limit,
            match_status=args.match_status or "confirmed",
        )
        if not ids:
            print("No releases matched")
            return 1

        cfg = WikiConfig.from_env()
        if args.wiki_url:
            cfg.base_url = args.wiki_url.rstrip("/")
        if args.user:
            cfg.username = args.user
        if args.password:
            cfg.password = args.password

        mode = "APPLY" if args.apply else "DRY-RUN"
        print(f"[{mode}] target={cfg.base_url}  releases={len(ids)}")
        if args.apply and not (cfg.username and cfg.password):
            print(
                "ERROR: --apply 需要凭据：IMAS_WIKI_USER / IMAS_WIKI_PASS "
                "或 --user / --password"
            )
            return 1

        results = push_many(
            conn,
            ids,
            apply=bool(args.apply),
            wiki_config=cfg,
            create_only=not args.allow_overwrite,
            force=bool(args.force),
            out_dir=out_dir,
            upload_cover=not bool(args.no_upload_cover),
        )
        counts: dict[str, int] = {}
        for r in results:
            counts[r.action] = counts.get(r.action, 0) + 1
            extra = f"  {r.url}" if r.url else ""
            warn = f"  !{r.warnings}" if r.warnings else ""
            cover_extra = ""
            if r.covers:
                cover_extra = "  covers=[" + "; ".join(
                    f"{c.side}:{c.result}:{c.wiki_name}" for c in r.covers
                ) + "]"
            print(
                f"  r{r.release_id} [{r.action}] {r.page_title or '?'}  "
                f"{r.message}{extra}{cover_extra}{warn}"
            )
        print("Summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        errors = counts.get("error", 0)
        return 1 if errors else 0
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="imas_hub",
        description="765PRO 目录主库 CLI（维护用；日常操作走 Web UI）",
    )
    p.add_argument("--version", action="version", version=f"imas_hub {__version__}")
    p.add_argument("--db", default=None, help=f"SQLite 路径（默认 {DB_PATH}）")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init-db", help="初始化数据库 schema（安全；跑迁移/刷新视图）")
    s.set_defaults(func=cmd_init_db)

    s = sub.add_parser("status", help="打印系列/状态摘要")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("serve", help="启动 Web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--reload", action="store_true")
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser(
        "export",
        help="导出 JSON 给 wikimas BOT / 歌词站（单张或按系列）",
    )
    s.add_argument("--series", default=None, help="系列 code，如 01")
    s.add_argument("--release-id", type=int, default=None)
    s.add_argument(
        "--match-status",
        default="confirmed",
        help="匹配状态过滤（默认 confirmed；传空串不过滤）",
    )
    s.add_argument("--out", default=None, help="JSON 输出路径")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser(
        "wiki-render",
        help="渲染 wikimas 专辑 wikitext 到 data/wiki_out（不推送）",
    )
    s.add_argument("--series", default=None, help="系列 code，如 01")
    s.add_argument("--release-id", type=int, default=None)
    s.add_argument("--limit", type=int, default=None, help="最多渲染 N 张")
    s.add_argument(
        "--match-status",
        default="confirmed",
        help="默认 confirmed",
    )
    s.add_argument("--out", default="data/wiki_out", help="输出目录")
    s.set_defaults(func=cmd_wiki_render)

    s = sub.add_parser(
        "wiki-push",
        help="推送专辑页到 MediaWiki（默认 dry-run；--apply 才写入）",
    )
    s.add_argument("--series", default=None, help="系列 code，如 01")
    s.add_argument("--release-id", type=int, default=None)
    s.add_argument("--limit", type=int, default=None, help="最多处理 N 张")
    s.add_argument("--match-status", default="confirmed")
    s.add_argument(
        "--apply",
        action="store_true",
        help="真正调用 MediaWiki edit API（默认只渲染）",
    )
    s.add_argument(
        "--allow-overwrite",
        action="store_true",
        help="允许覆盖已存在页面（默认 createonly，不覆盖）",
    )
    s.add_argument(
        "--force",
        action="store_true",
        help="即使 content hash 未变也推送",
    )
    s.add_argument(
        "--no-upload-cover",
        action="store_true",
        help="不上传本地 Cover（默认 --apply 时上传 front/back）",
    )
    s.add_argument(
        "--wiki-url",
        default=None,
        help="默认 http://localhost:8080 或 IMAS_WIKI_URL",
    )
    s.add_argument("--user", default=None, help="或环境变量 IMAS_WIKI_USER")
    s.add_argument("--password", default=None, help="或环境变量 IMAS_WIKI_PASS")
    s.add_argument("--out", default="data/wiki_out", help="本地 wikitext 备份目录")
    s.set_defaults(func=cmd_wiki_push)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
