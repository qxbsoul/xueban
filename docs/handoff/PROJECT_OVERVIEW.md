# 智慧学伴 · 项目总览

> 本文件基于 `D:\capital lion\xueban` 当前 Git 工作区实际代码与数据库审计，不依赖旧聊天结论。
> 初次审计：v1.5.9（`85000f5`）；**当前版本 v1.5.11**，变更见 `V1.5.10_CHANGES.md` 与 `V1.5.10_ACCEPTANCE.md`。

## 1. 项目定位

“智慧学伴”是面向“学情诊断与个性化学习”赛题的全栈工作台。

当前产品同时存在两条主业务线：

1. **教师端学情闭环**
   - 教师上传一次赛题 CSV 数据 → 创建一个 `teacher_batches` 任务；
   - 原始学生作答进入 `teacher_answers`；
   - 题目定义按任务批次进入 `teacher_question_versions`；
   - 教师发起批量判题，生成 `teacher_grade_jobs` / `teacher_grade_job_items`；
   - 每题判题结果进入 `teacher_grade_runs`，保留步骤、评分点、原始 API 响应；
   - 评分点作为学情分析的最小证据单元；
   - 输出班级/学生/任务学情、评分点统计、趋势、纵向数据；
   - 支持人工修正总分和逐评分点确认。

2. **学生端学习与 Agent 工作台**
   - 上传课程标准 + 教材，生成“学案课程”（课程、章节、小节、教案、练习）；
   - 学习中心：我的课程、教材管理、练习中心、我的学情；
   - 学情工作台：单聊/群聊、AI 教研组、人工审批、运行日志、学习计划、学情成果、知识库、学生记忆等；
   - 这些能力大量由 Node 网关和 Python 旧 Agent 框架共同承载。

## 2. 当前最重要的产品原则

以下原则是代码和数据库已经部分体现、但并非全部完全落实的设计方向：

- 学案就是课程，不再另起一套“内置课程体系”。
- 一份教师上传文件对应一个任务（batch）。
- 暂停、继续、失败重试、重新判题不应无故新建业务任务。
- 判题保留原始作答、评分点、步骤、错误类型、模型依据和人工修正历史。
- 未作答、错误作答、缺失记录、未评分必须区分。
- 评分点是学情分析的基础证据；没有证据时不能显示为 0 分。
- 不同任务之间的数据必须隔离。
- 统计结果必须能追溯到原始作答和评分点。

> 现状不是“全部原则已经完成”。参见 `FEATURE_STATUS.md`、`KNOWN_ISSUES.md`。

## 3. 技术栈

### 前端

- React 18.3.1 + TypeScript 5.6
- Vite 5.4
- `react-markdown`、`remark-gfm`、`remark-math`、`rehype-katex`
- `pdfjs-dist`
- 入口：`xueban-web/src/main.tsx`；应用路由在 `xueban-web/src/App.tsx`
- 教师端单独壳：`xueban-web/src/components/TeacherApp.tsx`
- 学生/工作台壳：`xueban-web/src/App.tsx` + `Sidebar` + 各 View

### 网关

- Node.js 22.16.0（必须用工作区 `.tools/node22`）
- Express 4
- `better-sqlite3` 12
- TypeScript + `tsx`
- 入口：`xueban-backend/server/src/index.ts`
- 默认端口：`8002`
- 对没有专门路由的 `/api/**` 使用 `proxyFallback` 转发 Python。

### Python 业务后端

- Python 3.12.16
- FastAPI（项目元数据版本 `0.3.0`）
- Uvicorn
- SQLite
- 入口：`xueban-backend/xueban/server.py`
- 默认端口：`8000`；`start-dev.mjs` 会复用/启动 `8010`、`8011` 等备用端口
- 教师路由：`xueban-backend/xueban/api/xueban_teacher.py`
- 认证路由：`xueban-backend/xueban/api/xueban_auth.py`
- 学情算法：`xueban-backend/xueban/services/learning_analysis.py`

### 数据

- 主业务/教师/认证：`xueban-backend/.xueban/xueban.sqlite`
- Node 会话/学习中心/聊天/记忆：`xueban-backend/server/.xueban/app.sqlite`
- 运行事件/快照：`xueban-backend/.xueban/runtime.sqlite`（按运行时创建）
- 计划凭据：`xueban-backend/.xueban/schedules/`（Fernet 加密）
- 知识包：`xueban-backend/.xueban/knowledge/`

## 4. 目录职责

```text
D:\capital lion\xueban\
├─ xueban-web\                 React 工作台
│  └─ src\components\
│     ├─ TeacherApp.tsx        教师端壳：工作台/学案课程/数据管理/判题中心/学情分析
│     ├─ BatchGrading.tsx      教师批量判题、任务、暂停/继续/重试、质量闸门
│     ├─ LearningAnalytics.tsx 教师班级/任务/学生/对比/评分点确认
│     ├─ StudentAnalytics.tsx  学生“我的学情”
│     ├─ LearningCenterView.tsx 学生课程/教材/练习/学情
│     ├─ WorkbenchView.tsx     学情工作台/Agent 对话
│     └─ ...                   审批、日志、组织、计划、成果、知识、设置
├─ xueban-backend\
│  ├─ xueban\                  Python FastAPI 业务后端
│  │  ├─ api\xueban_teacher.py 教师/导入/判题/学情 API
│  │  ├─ api\xueban_auth.py    教师/学生账号与会话
│  │  ├─ services\learning_analysis.py 评分点→知识点→Beta 学情算法
│  │  └─ ...
│  └─ server\                  Node 网关 + 学习中心/聊天/记忆
│     └─ src\
│        ├─ index.ts           路由挂载、代理回退
│        ├─ routes\learning.ts 课程生成、教材、练习、诊断
│        ├─ routes\xueban-chat.ts 学情工作台对话/审批/Checkpoint
│        ├─ routes\student-memory.ts 学生记忆
│        └─ services\learning-agent.ts 教材解析/课程生成的模型调用链
├─ docs\handoff\               本交接文档
├─ verify-artifacts\           验收截图与报告
├─ start-dev.mjs               一键启动
└─ package.json                工作区脚本
```

## 5. 启动方式

### 首次安装（摘要）

```powershell
cd D:\capital lion\xueban\xueban-backend
.\.venv\Scripts\python.exe -m pip install -e ".[server,dev]"

cd D:\capital lion\xueban
.\.tools\node22\npm.cmd --prefix .\xueban-backend\server ci
.\.tools\node22\npm.cmd --prefix .\xueban-web ci
```

### 开发启动

```powershell
cd D:\capital lion\xueban
npm run dev
```

或已有一个 Python 实例时：

```powershell
npm run dev -- --no-python
```

启动后：

```text
前端          http://localhost:3007/app
Node 健康检查 http://localhost:8002/health
Python 文档   http://localhost:8000/docs（或启动器选中的 8010/8011/8012）
```

`start-dev.mjs` 会探测已有 Python 端口，并要求其支持 BYOK embedding 与 OCR 能力，否则自动改用空闲端口。

## 6. 角色与入口

- 角色字段：`xueban_users.role`，取值 `teacher` / `student`。
- 教师登录后，前端直接渲染 `TeacherApp`，不渲染学生 Sidebar。
- 学生/普通工作台账号渲染 `App` 的 Sidebar + View。
- 认证接口前缀：`/api/xueban/auth`（注册、登录、guest、me、profile、logout）。

## 7. 当前版本与状态

- 版本：`v1.5.11`。
- 工作区：包含 v1.5.11 代码、测试、脚本与交接文档更新。
- 当前真实任务 `batch_305316cdd0`：
  - 40 名学生，2 个班（DC01/DC02）；
  - 65 行题表 = 54 道真题 + 11 条教材/学习卡材料；
  - 806 条作答；
  - 当前判题任务 `job_22f2c027b2` 状态 `paused`；
  - 805 个 job item：103 completed、1 failed、701 pending；
  - 108 条 grade run：104 graded、2 needs_review、2 failed；
  - 547 条数据问题：538 accepted、9 pending_review；
  - 全部已判作答集中在 DC01，DC02 仍 0 条已判；
  - 180 个评分点映射全部 legacy，尚未教师确认。

> 这是交接时点的真实状态。不要把“代码已实现”误认为“真实数据已经全量判完”，也不要把 65 行题表当成 65 道真题。

## 8. 本目录文档索引

- `ARCHITECTURE.md`：系统依赖、调用链、异步任务。
- `DATABASE.md`：表、字段、关系、隔离与迁移。
- `FEATURE_STATUS.md`：已完成/部分/未完成/缺陷。
- `DECISIONS.md`：有效、废弃、未决设计。
- `KNOWN_ISSUES.md`：已知问题与复现条件。
- `TEST_GUIDE.md`：测试与验证方式。
- `CURRENT_STATE.md`：Git、运行状态、待办顺序。
- `START_HERE.md`：新窗口快速接管摘要。
