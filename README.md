# IMAS CD Hub

偶像大师（THE IDOLM@STER）CD 元数据**共建目录**。品牌 → 系列 → 专辑 → 曲目，在网页上浏览、搜索和人工维护。

目标：让 CD 信息干净、统一、可对照，作为「按标准写标签」时的数据真源。它不是私人音乐库，也不做媒体中心。

## 能做什么

- **浏览 / 搜索**：品牌 → 系列 → 专辑 → 曲目；全局搜索（专辑 / 曲目 / 品番 / 艺人）
- **维护**：网页上新建 / 编辑品牌、系列、专辑、曲目。品番有则全局唯一，无则留空不伪造；MusicBrainz 只作外链 ID，不覆盖正式字段
- **曲目编辑**：按 Disc 分组展示；曲目拖拽排序、时长（mm:ss）、作曲 / 作词行内编辑；艺人用下拉多选（勾选实体 + 搜索补全 + 自定义）
- **封面**：网页上传，存于主库封面库
- **审核协作**：专辑三态——**未人工审核** / **需人工填充** / **审核完**；审核队列（含「审核完」tab）；审核进度页（三态环形图 + 协作者贡献榜 + 个人修改记录）
- **审计**：admin 可在后台查看修改日志（最近 200 条，按用户 / 对象筛选）
- **账号**：邀请码自助注册（邀请码由管理员发放）；写操作需登录，路人只读（浏览 / 搜索 / 导出保留）
- **导出**：单张 / 系列 JSON
- **Wiki**：渲染并推送专辑页（开发暂停，代码保留）

## 快速开始

```bash
pip install -r requirements.txt
python -m imas_hub init-db
python -m imas_hub serve
```

浏览器打开 http://127.0.0.1:8765

首个管理员用 `python -m imas_hub user bootstrap-admin <用户名>` 创建；此后发邀请码、停用账号、重置密码都在 Web `/admin` 完成。

## 维护命令

```bash
python -m imas_hub init-db          # 初始化 / 迁移 schema（安全，可重复跑）
python -m imas_hub status           # 终端摘要：系列 / 专辑 / 状态数
python -m imas_hub serve            # 启动 Web UI（日常维护的主界面）
python -m imas_hub export --series 01           # 按系列导出 JSON（默认仅审核完）
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
