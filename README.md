# 智慧学伴 · 学情诊断与个性化学习

面向“学情诊断与个性化学习”赛题的全栈工作台：教师上传赛题数据 → 预处理与质量闸门 → AI 分步判题 → 人工修正/评分点确认 → 学生/班级/任务学情；学生端提供学习中心与“我的学情”。

> 当前版本 v1.5.21：判题 HTTP/错误分类/并发 Worker/每题持久化/重启恢复/前端失败自动更新。验收见 `docs/handoff/V1.5.21_ACCEPTANCE.md`；v1.5.20 见 `docs/handoff/V1.5.20_ACCEPTANCE.md`。

```text
React 工作台 :3007  →  同源 /api/**  →  Node 网关 :8002  →  Python FastAPI :8000
```

## 1. 目录结构

```text
D:\capital lion\xueban\
├─ xueban-web\              React + TypeScript + Vite 工作台（/app）
├─ xueban-backend\
│  ├─ xueban\               Python FastAPI（账号、教师任务、判题、学情）
│  ├─ server\               Node 22 网关（学习中心、聊天、记忆、代理回退）
│  └─ .venv\                Python 虚拟环境（勿提交）
├─ docs\handoff\            交接与验收文档（v1.5.10–v1.5.16）
├─ .tools\node22\           Windows 便携 Node 22
├─ start-dev.mjs            一键启动三项服务
├─ verify-v1.5.10.mjs       浏览器验收截图脚本
└─ package.json             根启动脚本
```

## 2. 端口

| 服务 | 地址 | 用途 |
| --- | --- | --- |
| React 工作台 | http://localhost:3007 | 日常使用（`/app`），`/` 是介绍页 |
| Node 网关 | http://localhost:8002/health | 所有 `/api/**` 的统一入口 |
| Python API | http://localhost:8000/docs | 业务接口（启动器可能改用 8010） |

## 3. 环境要求

- Windows 10/11 + PowerShell
- **Python 3.12**（`xueban-backend\.venv`）
- **Node.js 22.x**：用 `.tools\node22`；不要用 Node 24 安装网关依赖（`better-sqlite3` 原生模块）
- 可用端口 `3007`、`8002`、`8000`（被占用时启动器会用 8010）

## 4. 首次安装

```powershell
cd D:\capital lion\xueban\xueban-backend
.\.venv\Scripts\python.exe -m pip install -e ".[server,dev]"

cd D:\capital lion\xueban
.\.tools\node22\npm.cmd --prefix .\xueban-backend\server ci
.\.tools\node22\npm.cmd --prefix .\xueban-web ci
```

## 5. 启动

```powershell
cd D:\capital lion\xueban
npm run dev
```

稳定后打开 http://localhost:3007 ，注册/登录，或用“游客一键体验”。

已有 Python 实例时可跳过重复启动：

```powershell
npm run dev -- --no-python
```

## 6. 模型与密钥

- 学生对话/课程生成：设置页填 BYOK Key，只存浏览器 localStorage，请求头即用即弃。
- **教师批量判题**：使用服务端 `xueban-backend/.env` 的 `DEEPSEEK_API_KEY`，不是教师 BYOK（是否改为 BYOK 仍未决）。
- 未配置判题 Key 时：`GET /api/teacher/grade/config` 返回 `configured=false`，创建判题任务返回 503，不会伪造分数。
- 不得把 Key 写入 Git、前端代码、日志或测试文件。

## 7. 功能地图

| 端 | 能力 |
| --- | --- |
| 教师端 | 数据导入（返回一次性学生初始账号）、预处理与问题清单、质量闸门（强制/留痕）、批量判题（暂停/继续/重试/单题重判）、人工整题修正、逐评分点确认与历史、班级/任务/学生学情、单题分析与证据下钻、多班对比、趋势与 EWMA、评分点映射管理、知识依赖图、错误类型分类、个性化推荐 |
| 学生端 | 学习中心（课程/教材/练习/我的学情）、首次登录改密、知识点掌握与证据链、解题方法、跨任务趋势、推荐练习、我的判题记录 |
| 工作台 | 学情工作台单聊/群聊、人工审批、运行日志、学习计划、学情成果、教研团队、学情看板、课程资源库 |

商品与订单模块已下线（旧表和旧代码仍在，删除需单独决策）。

## 8. 数据位置

| 路径 | 内容 |
| --- | --- |
| `xueban-backend\.xueban\xueban.sqlite` | 账号、教师任务、作答、判题、评分点、学情、审计 |
| `xueban-backend\server\.xueban\app.sqlite` | 学习课程、聊天、记忆、运行 |
| `xueban-backend\.xueban\runtime.sqlite` | 运行事件/快照 |
| `xueban-backend\.xueban\schedules\` | 学习计划（Fernet 加密） |
| `xueban-backend\.xueban\knowledge\` | 知识包 |

SQLite 运行时会产生 `-wal`/`-shm`，不要手工移动。v1.5.10 已备份并 VACUUM，备份在 `D:\capital lion\backup\v1.5.10`。

## 9. 构建与测试

```powershell
cd D:\capital lion\xueban
npm run build                    # 前端 tsc + vite build
npm run check:gateway            # 网关 tsc

cd xueban-backend
.\.venv\Scripts\python.exe -m unittest `
  tests.test_cross_batch_isolation tests.test_delete_batch tests.test_manual_confirm `
  tests.test_analysis_scope tests.test_quality_and_attempts tests.test_job_state `
  tests.test_pause_resume tests.test_learning_analysis tests.test_import_scope `
  tests.test_question_version_scope tests.test_delete_scope tests.test_question_metrics `
  tests.test_revision_scope_http tests.test_grading_pipeline_mock tests.test_mapping_management `
  tests.test_coverage_model tests.test_trend_comparability tests.test_quality_gate_enforcement `
  tests.test_analysis_extras tests.test_init_and_snapshot tests.test_student_accounts `
  tests.test_full_flow_http tests.test_permissions_http tests.test_trend_version_gate `
  tests.test_method_coverage tests.test_identity_mapping tests.test_feedback_metrics `
  tests.test_background_reliability tests.test_upgrade_schema tests.test_migration_atomicity tests.test_step_structure tests.test_preflight_parser tests.test_confirmation_semantics

cd server
.\.tools\node22\npm.cmd test     # vitest，自动使用隔离临时库
```

> 本环境未安装 pytest；旧测试中的 `import pytest` 会导致 `unittest discover` 大量导入失败，这不是 v1.5.10 回归。

全量判题准入预检（只读，不调用模型）：

```powershell
cd xueban-backend
.\.venv\Scripts\python.exe scripts\full_grading_preflight.py --db .\.xueban\xueban.sqlite --batch batch_305316cdd0
```

迁移预检与副本预演（正式库只读）：

```powershell
cd xueban-backend
.\.venv\Scripts\python.exe scripts\migration_preflight.py --db .\.xueban\xueban.sqlite
.\.venv\Scripts\python.exe scripts\migration_status.py --db .\.xueban\xueban.sqlite
.\.venv\Scripts\python.exe scripts\migrate_copy.py --source .\.xueban\xueban.sqlite --target-dir ..\_migration-copy --fail-test
```

浏览器验收（需先启动服务）：

```powershell
cd D:\capital lion\xueban
$env:TEMP='D:\capital lion\_browser_tmp'; $env:TMP=$env:TEMP
.\.tools\node22\node.exe verify-v1.5.10.mjs
```

截图写到 `D:\capital lion\verify-artifacts\v1.5.10\`。

真实判题小样本（只读，不调用模型）：

```powershell
cd xueban-backend
.\.venv\Scripts\python.exe scripts\export_grading_sample.py --db .\.xueban\xueban.sqlite --batch batch_305316cdd0
```

输出到 `D:\capital lion\verify-artifacts\v1.5.11\`，包含样本 CSV 与人工评价模板；有 Key 后按模板做 10–20 条人工核对。

## 10. 常见问题

- **Node 22 / better-sqlite3 报错**：用 `.tools\node22\npm.cmd` 重装网关依赖。
- **端口占用**：`netstat -ano | findstr :3007`（同理 8002、8000）。
- **判题 503**：服务端未配置 `DEEPSEEK_API_KEY`。
- **测试 EPERM（Windows）**：把 `TEMP/TMP` 指到可写目录；vitest 配置已自动处理数据目录。
- **页面显示演示数据**：链路没通，依次检查 `:8000`、`:8002/health`。

## 11. 约定

- 前端只请求同源 `/api/**`，不直连 Python。
- 请求统一走 `xueban-web\src\api.ts`。
- 任务内题目内容一律读 `question_versions`，不允许回读全局最新题面。
- 高风险能力（Shell、文件写入、对外发布、导出真实学生数据）不默认开放。
