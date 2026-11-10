# 智慧学伴 · 学情诊断与个性化学习

面向「智慧学伴」赛题的本地全栈工作台：老师提出学情需求，AI 教学主管拆解任务并调度各学科教研员，完成**学习内容与要求梳理**、**学生学习情况分析**、**个性化学习建议**三项核心任务，最后由教学主管汇总成可核对、可交付的学情成果。

```text
React 工作台 :3007  →  同源 /api/**  →  Node 网关 :8002  →  Python FastAPI :8000
```

## 1. 目录结构

```text
D:\capital lion\
├─ xueban-web\            React + TypeScript + Vite 工作台（/app），介绍页在 src\landing
├─ xueban-backend\
│  ├─ xueban\               Python FastAPI 业务后端（账号、教研组、教研员、项目、审批、学情数据）
│  ├─ server\                   Node 22 网关、真实模型会话、学伴编排
│  └─ .venv\                    Python 虚拟环境（本机重建，勿提交）
├─ .tools\node22\               Windows 便携 Node 22（网关必须用它）
├─ start-dev.mjs                一键启动三项服务
├─ verify-app.mjs               浏览器验收脚本（产物写到 verify-artifacts\）
└─ package.json                 根启动脚本
```

## 2. 端口

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| React 工作台 | http://localhost:3007 | 日常使用（`/app`）；`/` 是介绍页 |
| Node 网关 | http://localhost:8002/health | 所有 `/api/**` 的统一入口 |
| Python API | http://localhost:8000/docs | 业务接口文档（启动器可能改用 8010） |

## 3. 环境要求

- Windows 10/11 + PowerShell
- **Python 3.12**（`xueban-backend\.venv`）
- **Node.js 22.x**：用工作区自带的 `.tools\node22`，不要用 Node 24 安装网关依赖（`better-sqlite3` 原生模块与大版本绑定）
- 可用端口 `3007`、`8002`、`8000`（被占用时启动器会用 8010）

## 4. 首次安装

```powershell
cd D:\capital lion\xueban-backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -U pip
.\.venv\Scripts\python.exe -m pip install -e ".[server,dev]"

cd D:\capital lion
.\.tools\node22\npm.cmd --prefix .\xueban-backend\server ci
.\.tools\node22\npm.cmd --prefix .\xueban-web ci
```

## 5. 启动

```powershell
cd D:\capital lion
npm run dev
```

看到「全部服务已就绪」后打开 http://localhost:3007 ，注册、登录或用「游客一键体验」进入。停止时在终端按 `Ctrl+C`。

已有 Python 实例时可跳过重复启动：

```powershell
npm run dev -- --no-python
```

修改 Python 或 Node 代码后要停止旧进程再启动，否则会复用旧代码。

## 6. 模型与密钥

点左下角「设置」→「模型与 API」选择 DeepSeek（Base URL 固定 `https://api.deepseek.com/v1`），模型选 `deepseek-flash`（DeepSeek V4.1 Flash），填入自己的 API Key。

- Key 只存在当前浏览器 `localStorage`（`sf_deepseek_key`），请求时用 `X-DeepSeek-Key` 头即用即弃，不写服务端数据库。自定义模型可以保存多个，也能在设置页逐个删除。
- 知识库向量化是另一套配置（默认通义 `qwen3.7-text-embedding-flash`），未配置时回退本地关键词检索。
- 缺 Key 或上游失败会明确报错，不会伪造成功结果。

## 7. 功能地图（`/app`）

| 视图 | 能力 |
| --- | --- |
| 学情工作台 | 单聊／群聊，老师 ↔ 教学主管 ↔ AI 教研员；流式思考、DAG 分工、Checkpoint 重做、最近发言编辑 |
| 人工审批 | 需审批的任务节点（导出真实学生数据、删除数据、对外发布、资金动作等） |
| 运行日志 | 对话与运行记录 |
| 学习计划 | 周期性的学情巡检、周报任务 |
| 学情成果 | 以对话为单位：最终学情报告 + 教研员中间产出 |
| 教研团队 | 15 个组：5 个职能组 + 语文／数学／英语／物理／化学／生物／历史／地理／道德与法治／信息技术 10 个学科教研组 |
| 学情看板 | 模型用量与刊例成本估算 |
| 课程资源库 | 课程标准、题目、学情数据的上传、解析与检索；支持 PDF、Word、PPT、Excel、CSV、JSON、HTML、Markdown、TXT 与图片；图片默认用内置离线 OCR（RapidOCR，中英文）提取文字，配置视觉模型后还能补充语义描述 |
| 设置 | 模型、Embedding、工具与扩展 |

商品与订单模块已整体下线（前端入口、店铺工具、商品种子数据均已移除）。

## 8. 数据位置

| 路径 | 内容 |
| --- | --- |
| `xueban-backend\.xueban\xueban.sqlite` | 账号、教研组、部门、教研员、项目、任务、审批 |
| `xueban-backend\.xueban\runtime.sqlite` | 运行事件、快照与检查点 |
| `xueban-backend\.xueban\schedules\` | 学习计划与执行历史（凭据 Fernet 加密） |
| `xueban-backend\.xueban\.schedule-secret-key` | 计划凭据的本地加密密钥，必须保密 |
| `xueban-backend\server\.xueban\app.sqlite` | 会话、消息、Checkpoint、知识包归属 |
| `xueban-backend\.xueban\knowledge\` | 知识包原始文件、索引与检索库 |

SQLite 运行时会产生 `-wal`、`-shm`，服务运行时不要手工移动数据库。

## 9. 构建与测试

```powershell
cd D:\capital lion
npm run build                                              # 前端 tsc + vite build
npm run check:gateway                                      # 网关 tsc

cd xueban-backend
.\.venv\Scripts\python.exe -m pytest -m "not requires_api_key" --tb=short -q

cd xueban-backend\server
D:\capital lion\.tools\node22\node.exe node_modules\vitest\vitest.mjs run --maxWorkers=2
```

真实浏览器验收（需先启动服务）：

```powershell
cd D:\capital lion
npm run dev        # 另开一个终端
D:\capital lion\.tools\node22\node.exe verify-app.mjs
```

截图与结果 JSON 会写到 `verify-artifacts\`。

## 10. 常见问题

- **Node 22 / better-sqlite3 报错**：用 `.tools\node22\npm.cmd` 重装网关依赖。
- **端口占用**：`netstat -ano | findstr :3007`（同理 8002、8000），只停本工作区的进程。
- **页面显示演示数据**：说明链路没通，依次检查 `:8000`、`:8002/health`，再看网关日志。
- **`NO_API_KEY`**：当前模型缺 Key，去设置里补，不要连续重试。

## 11. 约定

- 前端只请求同源 `/api/**`，不直连 Python `:8000`。
- 请求统一走 `xueban-web\src\api.ts`，状态与流式事件走 `src\store.ts`，组件里不写裸 `fetch`。
- 所有业务数据按教研组隔离；跨组访问必须被拒绝。
- 高风险能力（Shell、文件写入、对外发布、导出真实学生数据）不默认开放。
- 模型、Embedding、搜索工具是三套独立配置，不要混用凭据。
