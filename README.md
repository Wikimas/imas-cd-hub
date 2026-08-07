# 765PRO Catalog

偶像大师 CD 元数据标准目录。品牌 → 系列 → 专辑 → 曲目，在网页上浏览和人工维护。

目标是让 CD 信息干净、统一、可对照；它不是私人音乐库，也不做媒体中心。

> 现状：本机开发中，未上线。

## 能做什么

- **浏览**：品牌 → 系列 → 专辑 → 曲目
- **维护**：网页上新建 / 编辑品牌、系列、专辑、曲目。品番有则全局唯一，无则留空不伪造；MusicBrainz 只作外链 ID，不覆盖正式字段
- **封面**：网页上传，存在主库封面库
- **导出**：单张 / 系列 JSON
- **Wiki**：渲染并推送专辑页（开发暂停，代码保留）

专辑有三态：**未人工审核**（数据齐、待审）、**需人工填充**（缺数据、待填）、**审核完**（定稿）。机器导入的数据默认「未人工审核」。

## 快速开始

```bash
pip install -r requirements.txt
python -m imas_hub init-db
python -m imas_hub serve
```

浏览器打开 http://127.0.0.1:8765

## 维护命令

```bash
python -m imas_hub init-db     # 初始化 / 迁移 schema（安全，可重复跑）
python -m imas_hub status      # 终端摘要：系列 / 专辑 / 状态数
python -m imas_hub serve       # 启动 Web UI
python -m imas_hub export --series 01   # 按系列导出 JSON
python -m imas_hub export --release-id 1
python -m imas_hub wiki-render --release-id 1
python -m imas_hub wiki-push --release-id 1 --apply
```

全部命令在项目根目录运行。详细操作见 `docs/OPS.md`（仅本机）。

## 环境

- Python 3.11+；依赖：`pip install -r requirements.txt`
- 数据库默认 `data/hub.db`（可用环境变量 `IMAS_DB_PATH` 覆盖）
- 封面默认 `data/covers/`；Wiki 推送凭据见 `.env.example`

## 文档

方向与治理文档在本机 `docs/`（不随代码推送到 GitHub）：`PRINCIPLES.md`（原则）、`ROADMAP.md`（路线）、`OPS.md`（操作）、`CONTEXT.md`（术语）、`adr/`（决策记录）。
