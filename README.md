# 765PRO Catalog

共建 **偶像大师 CD 元数据标准目录**（Web 主库）。

- **主产品：** 干净、可共建的发行/曲目数据（世界目录，不绑你的私人 FLAC）
- **日常维护：** 全部走 Web UI（品牌 → 系列 → 专辑 → 曲目）；批量导入与本地处理工具已退出项目
- **终点：** 全品牌 CD 标准数据；当前主线：代码清理已收尾 → 账户系统 → 上服务器

**产品原则与当前主线：** [`docs/PRINCIPLES.md`](docs/PRINCIPLES.md)、[`docs/ROADMAP.md`](docs/ROADMAP.md)、[`AGENTS.md`](AGENTS.md)  
**Web 日常操作与检查命令：** [`docs/OPS.md`](docs/OPS.md)

对外进度叙事可写在个人博客；**不以博客为原则真源。**

## 快速运行（Web 共建目录）

```bash
pip install -r requirements.txt
python -m imas_hub init-db
python -m imas_hub serve
# 浏览器打开 http://127.0.0.1:8765
```

所有命令在**项目根目录**（本 README 所在处）运行。

### 在浏览器做（共建目录）

| 在浏览器做 | 说明 |
|------------|------|
| **品牌总览** → 品牌 → 系列 → 专辑 | 创建/编辑品牌编码与名称 |
| 品牌页 → **新建系列**、改系列编码 | 系列可改挂品牌 |
| 系列页 → **新建专辑** | 纯目录条目 |
| 专辑页 → 改标题/品番/系列编码/曲目/状态 → **保存** | 主库真源 |
| 封面上传、Wiki 预览/推送、JSON 导出 | 已在页上 |

**模型：** `shelf` → `series` → `release`（前端叫 **品牌 / 系列 / 专辑**）  
前端**不**展示本地 path / FLAC；也没有扫盘、匹配、写标签入口——那些是一次性导入时代的车间能力，已退出项目。

**写入规则：** 有品番则品番全局唯一（冲突拒保存）；无品番可正常入库；MusicBrainz 只作外链 ID 保留，不再自动匹配/盖写正式字段。

## 环境

- Python 3.11+
- 依赖：`pip install -r requirements.txt`（fastapi / uvicorn / jinja2 / httpx / python-multipart）

## CLI（仅维护命令）

```bash
python -m imas_hub init-db     # 初始化/迁移 schema、刷新视图（安全，可重复跑）
python -m imas_hub status      # 终端摘要：系列 / 专辑 / 确认数
python -m imas_hub serve       # 启动 Web UI（默认 http://127.0.0.1:8765）
python -m imas_hub export --series 01   # 按系列导出 JSON（BOT / 歌词站）
python -m imas_hub export --release-id 1
python -m imas_hub wiki-render --release-id 1   # 渲染 wikitext 到 data/wiki_out（不推送）
python -m imas_hub wiki-push --release-id 1 --apply   # 推送到 MediaWiki
```

> 已删除：scan / match / confirm / retag / rename / dry-run / check-integrity / probe-meta / cue-split / selfcheck 等一次性命令。

## 环境变量

| 变量 | 默认 | 说明 |
|------|------|------|
| `IMAS_DB_PATH` | `data/hub.db` | SQLite |
| `IMAS_WIKI_URL` / `IMAS_WIKI_USER` / `IMAS_WIKI_PASS` | 本机 wiki / 空 | Wiki 推送凭据（`.env`，已 gitignore） |

## 目录

```
765PRO/
  imas_hub/            # 主程序包
    config.py          # 路径与常量
    db/schema.sql      # 中枢 schema
    api/               # FastAPI Web UI
    export/            # 单张 / 系列 JSON 导出
    normalize/cover.py # 封面库（Web 上传）
    wiki/              # Wiki 渲染与推送
    cli.py
  data/                # hub.db、covers/（运行后生成）
  docs/                # PRINCIPLES / ROADMAP / OPS
  .scratch/            # issue、备份与归档
```

## Web UI 运营

### Release 页（核心）

| 操作 | 说明 |
|------|------|
| 编辑专辑字段 | 标题 / 品番 / 日期 / 条码 / 厂牌 / 流派 / MBID（外链）/ 备注 |
| 编辑曲目 | 标题、艺人、作曲、作词（表格内联） |
| **保存到数据库** | `Ctrl+S` 或工具栏 · 只改主库 |
| 拖拽封面 | 写入 **主库封面库** `data/covers/{id}/Cover.*` |
| JSON 导出 | 单张导出 |
| Wiki 预览 / 推送 | confirmed / manual |

### 系列页

- 新建无盘主库条目
- 系列 JSON 导出
- Wiki 预览 / 推送 confirmed

### HTTP API

| 端点 | 说明 |
|------|------|
| `PUT /api/release/{id}` | 改专辑元数据 |
| `PUT /api/release/{id}/tracks` | 批量改曲目 |
| `GET/POST /api/release/{id}/cover` | 读/换主库封面 |
| `GET /api/release/{id}/export` | 单张 JSON |
| `GET /api/export?series={code}` | 按系列 JSON（series 必填，无全库导出） |
| `GET /api/wiki/status` | Wiki 目标 URL / 是否已配凭据 |
| `GET /api/release/{id}/wiki` | 渲染 wikitext 预览 |
| `POST /api/release/{id}/wiki` | `{apply, upload_cover, allow_overwrite, force}` |
| `POST /api/series/{code}/wiki` | 系列批量预览 / 推送 |

Wiki 推送需在启动 `serve` 前设置 `IMAS_WIKI_USER` / `IMAS_WIKI_PASS`（或 `.env`）。

## Wiki BOT（本机 wikimas）

目标：`export` → `Album info` + Tracklist wikitext → 推 **本机** MediaWiki（`http://localhost:8080`），确认后再切 wikimas.org。

仅 `match_status=confirmed`（或 `manual`）的 Release 可推。

### 环境变量 / `.env`

在项目根 `.env` 写凭据即可（启动时自动加载；**已 gitignore**）。可复制 `.env.example`。

| 变量 | 默认 | 说明 |
|------|------|------|
| `IMAS_WIKI_URL` | `http://localhost:8080` | MediaWiki 根 URL |
| `IMAS_WIKI_USER` | （空） | 登录用户（BotPassword：`User@botname`） |
| `IMAS_WIKI_PASS` | （空） | 机器人密码 |
| `IMAS_WIKI_UA` | `765PRO-Hub-WikiBot/0.3 …` | User-Agent |

已有进程环境变量优先于 `.env`。修改 `.env` 后需重启 `serve`。

### 命令

```bash
# 只渲染 wikitext → data/wiki_out/*.wiki（无需凭据）
python -m imas_hub wiki-render --release-id 1
python -m imas_hub wiki-render --series 01 --limit 2

# dry-run：渲染 + 列出将推送的页名（默认）
python -m imas_hub wiki-push --release-id 1

# 真正写入本机 wiki（需凭据；默认不覆盖已有页；默认上传 Cover front/back）
python -m imas_hub wiki-push --release-id 1 --apply --user 'SerinaP@AlbumHubBot' --password '***'

# 允许覆盖已有页 / 强制重推正文
python -m imas_hub wiki-push --release-id 1 --apply --allow-overwrite --force

# 只改文、不传图
python -m imas_hub wiki-push --release-id 1 --apply --no-upload-cover
```

封面会上传为 `Cover {品番} front.jpg`（或 `.png`），与 `|image=` 一致。正文 hash 未变时仍会补传缺失封面。

### 渲染约定（对照本机 Boilerplate:Album）

| 字段 | 规则 |
|------|------|
| `brand` | 本家固定 `765as` |
| `artist` | 主库 `角色 (CV:声优)` → 声优名，顿号 `、` 分隔 |
| `Track` | 拆 `(M@STER VERSION)` 等到 `ver=`；karaoke / トーク → `nolink=1` |
| `label` | `Columbia Music Entertainment` → `Nippon Columbia` |
| 时长 | `mm:ss` 分钟补零（如 `03:30`） |
| 图库 | 无 Scan 时 `{{Scan\|missing=1}}` |

推送成功后写入 `wiki_sync`（page_title / content hash），hash 未变会跳过（`--force` 除外）。

### 建议试推流程

1. `wiki-render --release-id 1` 人工打开 `.wiki` 对照本机已有专辑页
2. 本机 wiki 建 bot 账号或 BotPassword
3. `wiki-push --release-id 1 --apply`（createonly）
4. 浏览器打开 `http://localhost:8080/wiki/…` 核对 brand / Track / SMW
5. 稳定后把 `IMAS_WIKI_URL` 换成 `https://wikimas.org` 再推

## 阶段边界

| 已做 | 未做 |
|------|------|
| Schema / 主库脱钩（无 path 条目） | 歌词站 / ORT 挂靠 |
| 六品牌主系列入库（本家/百万/闪彩/SideM/灰姑娘/动画） | 全品牌所有系列补完（上线后人工逐步补） |
| Web 目录维护（品牌/系列/专辑/曲目） | 账户系统（管理员发号） |
| 封面库 + Web 手动上传 | 服务器部署 / 真库迁服务器 |
| 单张/系列 JSON 导出 | 正式站批量推送 |
| Wiki 渲染 + 本机 push + 封面上传 | 全库 JSON 导出（已移除） |
| 一次性导入/车间代码已清理 | 上线后日常全走 Web 人工维护 |
