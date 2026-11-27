# 测试与验证指南（v1.5.16）

> v1.5.16 起，回归门禁用 **41 个 Python 模块**（202 个用例），新增：
> `test_grading_reliability`、`test_reparse_history`、`test_evidence_chain`、
> `test_data_quality_model`、`test_analytics_consistency`。
> 本机沙箱下 `tempfile.mkdtemp()` 目录不可写，跑测试时把 `TEMP/TMP` 指到工作区目录，
> 或用仓库外的 `sitecustomize` 垫片把 `mkdtemp` 换成 `os.makedirs`。

> 原则：优先隔离临时数据库；不要在未确认成本前对 806 条真实作答调用模型。
> 所有命令从 `D:\capital lion\xueban` 执行，除非另有说明。

## 1. 环境

| 项 | 要求/当前值 |
| --- | --- |
| OS | Windows 10/11 + PowerShell |
| Node | 22.x；当前 `v22.16.0`（`.tools\node22\node.exe`） |
| Python | 3.12；当前 `3.12.16`（`xueban-backend\.venv`） |
| 前端 | `xueban-web` |
| Node 网关 | `xueban-backend\server` |
| Python 主库 | `xueban-backend\.xueban\xueban.sqlite` |
| Node 应用库 | `xueban-backend\server\.xueban\app.sqlite` |
| pytest | **未安装**（旧测试大量 `import pytest`，全量 discover 会报环境错误；本次统一用 unittest） |

检查：

```powershell
& '.\.tools\node22\node.exe' --version
& '.\xueban-backend\.venv\Scripts\python.exe' --version
```

## 2. 启动

```powershell
cd D:\capital lion\xueban
npm run dev
```

只复用已有 Python：

```powershell
npm run dev -- --no-python
```

预期：

```text
http://localhost:3007/app       前端
http://localhost:8002/health    Node 网关 status=ok
http://localhost:8000/docs      Python（或 8010/8011/8012）
```

注意：

- 必须使用 Node 22；`better-sqlite3` 与 Node 24 不兼容。
- 修改 Python/Node 后要停止旧进程再启动，否则可能复用旧实例。
- 审查/验收时可用数据库副本：设置 `XUEBAN_XUEBAN_DB`、`XUEBAN_HOME`、`XUEBAN_DATA_DIR` 指向 `_demo` 副本，避免写真实库。

## 3. Python 测试（unittest，隔离临时库）

### 3.1 教师/学情核心（22 个模块，推荐每次改动后运行）

```powershell
cd D:\capital lion\xueban\xueban-backend
& '.\.venv\Scripts\python.exe' -m unittest `
  tests.test_cross_batch_isolation tests.test_delete_batch tests.test_manual_confirm `
  tests.test_analysis_scope tests.test_quality_and_attempts tests.test_job_state `
  tests.test_pause_resume tests.test_learning_analysis `
  tests.test_import_scope tests.test_question_version_scope tests.test_delete_scope `
  tests.test_question_metrics tests.test_revision_scope_http tests.test_grading_pipeline_mock `
  tests.test_mapping_management tests.test_coverage_model tests.test_trend_comparability `
  tests.test_quality_gate_enforcement tests.test_analysis_extras tests.test_init_and_snapshot `
  tests.test_student_accounts tests.test_full_flow_http `
  tests.test_permissions_http tests.test_trend_version_gate tests.test_method_coverage `
  tests.test_identity_mapping tests.test_feedback_metrics tests.test_background_reliability tests.test_upgrade_schema tests.test_migration_atomicity tests.test_step_structure tests.test_preflight_parser tests.test_confirmation_semantics
```

覆盖：

- 真实 CSV 导入路径、跨任务同事件、重复导入、同批去重；
- 题目版本隔离（成绩/问题/闸门/results/学生详情/单题分析）；
- 删除任务 5 类场景 + 运行中阻断 + 其他任务完好；
- Q011/Q014/Q015 学生等权回归、人工修正与评分点证据口径；
- revisions 任务作用域（HTTP）；
- mock 模型判题全链路：成功/失败/超时/暂停/继续/重试/重判/attempt；
- 映射确认、权重归一化、历史、方法；覆盖率分层与 Beta 精确分位；
- 趋势三态与 EWMA；闸门强制与留痕；依赖图/环检测/错误分类/推荐/多班对比；
- 初始化零回填、快照去重；学生账号发放/首登改密/重置/绑定；
- HTTP 全链路集成（导入→闸门→映射→判题→学情→分类→推荐→依赖图→删除）；
- v1.5.11：权限矩阵（游客/学生/教师/未登录、跨教师/跨学生）、趋势版本兼容、方法机会覆盖、任务内身份、映射拒绝/版本/影响面、错误分类指标、推荐反馈、后台恢复与双 worker 并发。

### 3.2 迁移与验收脚本

```powershell
# 正式库只读检查
.\xueban-backend\.venv\Scripts\python.exe xueban-backend\scripts\migration_preflight.py --db xueban-backend\.xueban\xueban.sqlite
# 服务未启动时查迁移状态
.\xueban-backend\.venv\Scripts\python.exe xueban-backend\scripts\migration_status.py --db <db>
# 副本迁移 + 失败注入预演（不写源库）
.\xueban-backend\.venv\Scripts\python.exe xueban-backend\scripts\migrate_copy.py --source <源库> --target-dir <副本目录> --fail-test
```

### 3.3 全量 discover（了解现状，不建议作为本次门禁）

```powershell
& '.\.venv\Scripts\python.exe' -m unittest discover -s tests -p "test_*.py"
```

当前约 255 个用例被收集、162 个模块导入失败，原因是旧测试 `import pytest` 而环境未安装 pytest；不要把该结果当作 v1.5.10 回归。

## 4. Node 网关测试（自动隔离，不写真实库）

```powershell
cd D:\capital lion\xueban\xueban-backend\server
& 'D:\capital lion\xueban\.tools\node22\npm.cmd' test
```

隔离机制：

- `vitest.config.ts` 每次运行创建独立临时目录（优先仓库外 `_vitest_tmp`），并设置 `XUEBAN_DATA_DIR`；
- `vitest.setup.ts` 为每个测试文件分配独立子目录；
- `vitest.global-setup.ts` 校验目录不在工作区运行目录，运行完删除；
- `storage/sqlite.ts` 在 `NODE_ENV=test` 且缺少 `XUEBAN_DATA_DIR` 时直接报错，禁止连接真实 `app.sqlite`。

验证真实库未被污染：

```powershell
# 记录前后行数，应完全一致
& '.\xueban-backend\.venv\Scripts\python.exe' -c "import sqlite3;c=sqlite3.connect(r'xueban-backend/server/.xueban/app.sqlite');print([c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0] for t in ('agent_hops','memory_handoffs','token_usage')])"
```

其他脚本：

```text
npm run test:wire
npm run test:fixtures
npm run test:cli
```

其中部分测试依赖环境变量/Python/浏览器，失败需区分环境问题与代码问题。

## 5. 前端验证

### TypeScript

```powershell
cd D:\capital lion\xueban\xueban-web
& 'D:\capital lion\xueban\.tools\node22\node.exe' 'node_modules\typescript\bin\tsc' --noEmit -p 'tsconfig.json'
```

预期：退出码 0，无输出。

### 生产构建

```powershell
& 'D:\capital lion\xueban\.tools\node22\node.exe' 'node_modules\vite\bin\vite.js' build `
  --outDir 'D:\capital lion\.build-check' --emptyOutDir --logLevel error
```

### 浏览器验收（v1.5.10）

```powershell
cd D:\capital lion\xueban
$env:TEMP='D:\capital lion\_browser_tmp'; $env:TMP=$env:TEMP   # 受限 Windows 上 Node 需要可写 TEMP
& 'D:\capital lion\.tools\node22\node.exe' verify-v1.5.10.mjs
```

脚本会：教师登录 → 工作台/数据管理/判题中心/学情分析（班级、单题、映射、依赖图、错误类型）→ 退出 → 学生登录 → 学习中心/我的学情/证据链；同时记录控制台错误。

截图目录：`D:\capital lion\verify-artifacts\v1.5.10\`（仓库外），包含：

```text
01_login.png
02_teacher_workbench.png
03_data_manager.png
04_task_list.png
05_task_detail_and_gate.png
06_analytics_classes.png
07_class_overview.png
08_question_detail.png
09_mapping_management.png
10_knowledge_graph.png
11_error_labels.png
12_student_analytics.png
13_student_knowledge_detail.png
result.json
```

无外网时 Google Fonts 会报 `ERR_CONNECTION_CLOSED`，不计为应用错误。

## 6. 核心业务手工验证

### 6.1 教师导入/预处理

1. 教师账号登录 → 数据管理；
2. 选择 5 个 CSV（学生、课程要求、测验节点、题目、作答）；
3. 预览 → 确认导入；
4. 记录返回的**学生初始账号**（只显示一次）；
5. 打开任务 → 查看预处理报告与问题分类。

### 6.2 质量闸门

```text
GET /api/teacher/batches/{batch_id}/quality-gate
```

- 当前真实任务 `blocking=false`，唯一非阻断项为 missing_process；
- 存在阻断时直接 `POST /grade/batch` 应 409 `QUALITY_GATE_BLOCKED`；
- 强制继续必须 `force=true` + `force_reason`，并在 `/quality-gate-overrides` 可查。

### 6.3 判题（mock 或小批真实）

- 无 Key：`GET /grade/config` 显示未配置，创建判题任务 503；
- 有 Key：先 1 个班/1 个节点/1-3 题；
- 检查 `teacher_grade_runs`（raw_response/step_results/question_version_id）、`teacher_grade_attempts`、`job_items`；
- 暂停→继续→失败重试→单题重判（新增 grading_version，历史保留）。

### 6.4 学情分析

- 教师：班级 → 知识点/任务/趋势（含可比性原因与 EWMA）/错误/学生；
- 单题：事件平均与学生等权平均必须不同且可核对；
- 学生：学习中心 → 我的学情 → 知识点 → 完整证据链；
- 没有证据显示“证据不足”，不显示 0。

### 6.5 映射与依赖图

- 学情分析 → 评分点映射：legacy → 教师确认 → 权重归一化 → 历史可查；
- 知识依赖：生成候选 → 确认边 → 拒绝重复/循环 → 学生前置薄弱。

### 6.6 学生账号

1. 导入后把初始密码发给学生；
2. 学生登录 → 首次必须改密；
3. 教师可重置密码、绑定自注册账号；
4. 学生只能看自己的学情（越权 403）。

### 6.7 教材/课程

- 学生或教师 → 学习中心/学案课程：上传 → 等待 job → 查看章节/教案/练习；
- 注意当前真实课程是 Markdown 导入，活课程没有知识点/练习；历史生成 job 为 0。

## 7. 真实赛题数据验证方式

### 7.1 只读审计

```text
GET /api/teacher/batches/{id}/overview
GET /api/teacher/batches/{id}/preprocessing
GET /api/teacher/batches/{id}/issues?status=pending
GET /api/teacher/batches/{id}/quality-gate
GET /api/teacher/analysis/classes/{id}?batch_id=...
GET /api/teacher/analysis/classes/{id}/questions/{qid}?batch_id=...
```

### 7.2 副本验证（推荐）

复制 `xueban.sqlite` 与 `server/.xueban/app.sqlite` 到 `_demo/`，启动时用环境变量指向副本；不要直接在真实库上做删除/重判实验。

### 7.3 全量前置条件

- quality gate 无 blocking；
- 9 条 pending issue 已处置；
- 映射确认或明确标记 legacy；
- 趋势版本策略明确（否则保持 unverifiable / non_comparable）；
- token 预算确认；
- 已用真实暂停/继续验证 701 pending 的续跑（v1.5.10 仍未执行）。

## 8. 每项测试的预期结果

| 测试 | 预期 |
| --- | --- |
| Python 22 模块 unittest | 全部 OK（见验收报告的具体数字） |
| Node vitest | 174 文件 / 1132 测试通过 |
| tsc（web/server） | 退出码 0 |
| vite build | 退出码 0 |
| 教师登录 | 进入 TeacherApp，不显示学生 Sidebar |
| 学生登录 | 进入学生工作台，可进学习中心 |
| 导入 | 1 batch、计数正确、issue 生成、返回一次性学生凭据 |
| 批量判题 | job/item/grade 状态推进，失败可重试，attempt 落库 |
| 暂停 | 状态 paused，pending 不减少 |
| 评分点确认 | 独立表写入 + 历史，总分一致性决定 status |
| 学情 | 无证据不显示 0；可追溯 evidence |
| 趋势 | 三态 + 具体原因；EWMA 不替代原始值 |
| 课程 | 课程/章节/lesson/exercise 可读 |
| Node 测试 | 运行前后真实 `app.sqlite` 行数不变 |
