# 系统架构

## v1.5.11 架构补充

- 所有 `/api/teacher/**`（除 `grade/config`）经中间件校验登录与资源归属；任务以 `teacher_batches.owner_id` 隔离，历史 NULL 为共享兼容。
- 角色严格白名单：teacher/student/guest；游客不进入教师端真实数据。
- 任务内学生身份由 `teacher_enrollments` 记录，默认 unverified；跨任务趋势返回身份警告。
- 方法覆盖率分母来自 `teacher_method_opportunities`；无机会数据返回 null。
- 趋势比较版本值 + 算法参数版本；跨版本仅允许 `teacher_version_compat` 显式兼容。
- 映射确认/拒绝递增 `teacher_mapping_versions`，判题与学情快照记录版本，影响面可查询。


> v1.5.10 已更新：质量闸门改为服务端强制、题目版本成为所有任务内查询的权威、分析快照去重、初始化拆分为 bootstrap、新增映射/方法/依赖图/错误分类/推荐。完整清单见 `V1.5.10_CHANGES.md`。

## 1. 总体拓扑

```text
浏览器
  │
  ├─ /app 教师端 / 学生工作台（React 18 + Vite，:3007）
  │
  └─ /api/**
        │
        ▼
  Node Express 网关（:8002）
        │
        ├─ 自有路由：/api/learning、/api/xueban-chat、/api/xueban-knowledge、
        │            /api/student-memory、/api/groups、/api/runs、/api/auth 等
        │
        └─ proxyFallback（无匹配路由）
                │
                ▼
        Python FastAPI（默认 :8000，启动器可改 :8010/8011/8012）
                ├─ /api/xueban/auth
                ├─ /api/teacher/**
                ├─ /api/xueban/**
                ├─ Agent/runtime/session/memory/knowledge 等
                └─ SQLite .xueban/xueban.sqlite
```

关键文件：

- 前端入口：`xueban-web/src/App.tsx`
- 教师壳：`xueban-web/src/components/TeacherApp.tsx`
- Node 路由与回退：`xueban-backend/server/src/index.ts`
- Node 回退实现：`xueban-backend/server/src/proxy-fallback.ts`
- Python 应用：`xueban-backend/xueban/server.py`
- 教师 API：`xueban-backend/xueban/api/xueban_teacher.py`
- 认证 API：`xueban-backend/xueban/api/xueban_auth.py`

## 2. 前端架构

### 2.1 壳层

`App.tsx` 先判断路径是否 `/app` 或 `#/app`：

- 不是 → `LandingPage`；
- 是 → `WorkbenchApp`。

`WorkbenchApp` 根据 `store.authStatus` 和 `store.authUser.role`：

- `role === 'teacher'` → `TeacherApp`；
- 其他 → Sidebar + Topbar + View 的学生/工作台壳。

### 2.2 教师端

`TeacherApp.tsx` 固定 Tab：

```text
workbench  教师工作台
textbooks  学案课程
data       数据管理
grading    判题中心
analytics  学情分析
```

组件职责：

| 组件 | 职责 |
| --- | --- |
| `TeacherApp.tsx` | 汇总、导入、课程、判题、学情入口 |
| `BatchGrading.tsx` | 任务列表、预处理、质量闸门、批量判题、暂停/继续/重试、任务详情 |
| `LearningAnalytics.tsx` | 班级学情、知识点、任务分析、评分点表、学生列表、多班对比、单题纵向、评分点确认 |
| `StudentAnalytics.tsx` | 学生“我的学情”，按评分点证据展示知识点 |
| `LearningCenterView.tsx` | 学生课程/教材/练习/学情 |

### 2.3 学生工作台

`App.tsx` View 包括：

```text
workbench / learning / memory / approvals / logs / org / tasks /
deliverables / dashboard / knowledge / settings / studentRecords
```

其中：

- `LearningCenterView` 是当前主学习界面；
- `WorkbenchView` / `xueban-chat` 是 Agent 对话与教研协作；
- 其余 View 是历史运行时/组织/审批/记忆/计划能力。

## 3. 教师端业务数据流

### 3.1 导入

```text
TeacherApp.DataManager
  → POST /api/teacher/imports/preview
  → POST /api/teacher/imports
  → Python upload_import()
  → read_csvs() 按关键词识别：
       模拟学生 / 课程要求 / 题目与材料 / 测验节点 / 作答记录
  → validate()
  → import_rows()
  → teacher_batches + teacher_students/classes/questions/exams/answers
  → 生成 teacher_question_versions
  → _build_issues() 生成 teacher_data_issues
```

要点：

- 一份上传文件集合对应一个 `teacher_batches.id`；
- 题目定义写入全局 `teacher_questions`，同时写批次版本 `teacher_question_versions`；
- 作答的 `question_version_id = qv:{batch_id}:{question_id}`；
- 作答 ID 新格式为 `{batch_id}:{event_id}-r{revision}`；
- 旧数据作答 ID 仍是 `{event_id}-r{revision}`。

### 3.2 预处理确认

```text
POST /api/teacher/batches/{batch_id}/preprocessing/confirm
→ teacher_batches.preprocess_status = confirmed
```

`_build_issues()` 已生成：

- 缺失解题过程；
- 缺失最终答案；
- 原始得分越界；
- 评分点分数之和与题满分不一致；
- 重复/冲突；
- 其他结构问题。

任务当前真实数据：547 条 issue，其中 538 条 `accepted`（缺失解题过程，非阻断），9 条 `pending_review`。

### 3.3 批量判题

```text
BatchGrading
  → POST /api/teacher/grade/batch {batch_id, class_id?, exam_node_id?, force?, force_reason?}
  → Python batch()
  → 校验 preprocess_status == confirmed
  → 服务端 quality_gate()；blocking 且未 force → 409 QUALITY_GATE_BLOCKED
  → force 必须填理由，写 teacher_quality_gate_overrides（教师/时间/问题/理由）
  → 选出 grading_status='pending' 的答案
  → 建 teacher_grade_jobs + teacher_grade_job_items（pending）
  → BackgroundTasks.run_job(job_id)
```

`run_job()` 当前逻辑：

1. 读取 `DEEPSEEK_API_KEY`；缺失时任务置 failed 并给出明确错误（不伪造成绩）；
2. 原子地把 job 从 `queued/paused` 改成 `running`，写 `worker_id/heartbeat_at`；
3. 循环检查 `pause_requested` / `cancellation_requested`；
4. 每次原子领取一条 pending item（`WHERE status='pending'`，rowcount 校验，避免双 worker 重复判题）；
5. 调 `grade_answer()`；
6. 成功置 completed，失败置 failed；
7. `processed = completed + failed`（不是 item 总数）；
8. 循环结束按失败数置 completed/partially_failed/failed；进程级异常也会落 failed，不会永久停在 running。

暂停：

```text
POST /grade/jobs/{id}/pause
→ pause_requested=1
→ 有活跃心跳且 running → pausing，由 worker 在下一轮置 paused
→ 无心跳/排队中 → 直接 paused，并把 running item 还原 pending
```

继续：

```text
POST /grade/jobs/{id}/resume
→ 仅允许非 running/queued/pausing 的任务
→ pause_requested=0；queued；再次后台 run_job
```

失败重试：

```text
POST /grade/jobs/{id}/retry
→ 同一 job_id 的 failed items 重置为 pending（运行中任务拒绝重试）
→ 不创建新业务 job
```

> v1.5.10：`batch()` 已强制 quality gate；前端禁用只是提示，不是唯一防线。直接调 API 在 blocking 时会得到 409，除非显式 force 且留痕。

### 3.4 单题判题

`grade_answer()`：

- 读取 `teacher_meta(answer)`，优先 `teacher_question_versions`；
- 拆分学生 `solution_process`；
- 调 DeepSeek `llm_grade()`；
- `validate_llm_result()` 校验分数、步骤、评分点 ID、重复评分点、分配上限、操作单元来源；
- 写 `teacher_grade_runs`，保存：
  - score/max_score
  - step_results_json（步骤、操作单元、错误类型、判题理由、评分点链接）
  - raw_response（模型原始 JSON）
  - question_version_id
  - needs_review/status
- 写 `teacher_grade_attempts`（包括成功与失败）；
- 没有模型 key 时直接 503 `GRADER_NOT_CONFIGURED`，**不写成绩、不做规则猜测**；v1.5.10 已删除不可达的 rule-assisted 死代码。

### 3.5 人工修正

- `POST /grades/{id}/manual`：改整题分数、状态 `manual_corrected`，写 `teacher_corrections`；
- `POST /grades/{id}/confirm`：只确认整题结果，状态 `manual_confirmed`，写 correction；
- `POST /grades/{id}/scoring-points`：逐评分点确认，写 `teacher_scoring_point_confirmations`；
  - 总分与整题分数不一致 → `needs_review`；
  - 总分一致 → `manual_confirmed`。

> `manual_confirmed` 只代表整题确认，不自动代表逐评分点已确认。学情分析对两者的使用有区别。

## 4. 学情分析调用链

```text
GET /api/teacher/analysis/me                 学生个人学情
GET /api/teacher/analysis/classes            班级列表
GET /api/teacher/analysis/classes/{id}       班级详情 + 趋势 + 任务 + 高频错误
GET /api/teacher/analysis/students/{id}      教师看学生
GET /api/teacher/analysis/classes/{id}/questions/{qid}
GET /api/teacher/analysis/classes/{id}/questions/{qid}/longitudinal
GET /api/teacher/analysis/mapping-audit?batch_id=   评分点→知识点/方法映射审计（真实路由）
GET /api/teacher/analysis/comparability?nodes=...  节点可比性签名与原因
GET /api/teacher/analysis/compare-detailed?...     多班详细对比（成绩/覆盖/状态分布/错误）
GET /api/teacher/analysis/trend/student/{id}       学生跨任务知识点趋势 + EWMA
GET /api/teacher/analysis/trend/class/{id}         班级跨任务知识点趋势 + EWMA
GET /api/teacher/analysis/students/{id}/knowledge/{kp}  知识点证据下钻
GET /api/teacher/batches/{id}/mapping              评分点映射（含 legacy/confirmed）
POST /api/teacher/scoring-points/{id}/knowledge-links  教师确认映射（权重归一化 + 历史）
POST /api/teacher/scoring-points/{id}/methods      方法映射
GET/POST /api/teacher/methods                      方法目录
GET /api/teacher/knowledge-graph                   依赖图
POST /api/teacher/knowledge-graph/candidates       按主题顺序生成候选边
POST/DELETE /api/teacher/knowledge-graph/edges     确认/删除依赖（环检测）
GET /api/teacher/knowledge-graph/{kp}/dependencies 直接/间接前置
GET /api/teacher/knowledge-graph/students/{id}/gaps 前置薄弱诊断
POST /api/teacher/answers/{id}/error-labels/classify 规则分类（用已有判题证据）
POST /api/teacher/batches/{id}/error-labels/classify 批量规则分类
GET  /api/teacher/batches/{id}/error-labels         错误类型汇总
GET  /api/teacher/students/{id}/recommendations    可解释练习推荐
GET  /api/teacher/grade/config                     判题是否已配置（Key 状态）
```

服务层：`xueban-backend/xueban/services/learning_analysis.py`

```text
teacher_answers
  → teacher_grade_runs（每题最新有效 grade）
  → step_results_json.scoring_point_links
  → scoring_points（按 batch 优先）
  → scoring_point_knowledge_links
  → knowledge point evidence
  → Beta(2,2) 加权证据
  → 学生/班级/任务统计
```

统计单位必须区分：

- **事件**：学生的一次作答事件（`answer_id`）；
- **学生**：同一学生在同一题上的最新有效作答；
- **知识点**：评分点映射后的证据聚合。

## 5. 教材/课程生成调用链

教师端“学案课程”和学生端“学习中心”实际调用 Node：

```text
POST /api/learning/courses/joint
  或 POST /api/learning/courses
→ routes/learning.ts
→ 读取课程标准和教材（learning_materials）
→ runJointPreparation() / runCourseGeneration()
→ services/learning.ts
→ services/learning-agent.ts
→ transport/api-clients.callProvider()
→ DeepSeek / 自定义 OpenAI 兼容 provider
→ 生成 learning_course_units / learning_lessons / learning_exercises
→ learning_generation_jobs / learning_generation_events
```

关键特点：

- 课程是 `learning_courses`；
- 章节/小节是 `learning_course_units`；
- 学案正文是 `learning_lessons`；
- 练习是 `learning_exercises`；
- 课程标准与教材是 `learning_materials`；
- 源块是 `learning_source_blocks`；
- 生成事件是 `learning_generation_events`；
- Agent 调用有重试（`maxAttempts` 默认 2，最大 3）；生成课程时部分阶段显式限制为 1 次，避免重复写库；
- 失败会写 course.status=error、job.error，并保留可重试入口。

模型 provider：

- 默认 `deepseek`，默认模型 `deepseek-flash`；
- 也支持 OpenAI 兼容自定义 provider；
- 前端通过请求头携带用户 BYOK key（`x-deepseek-key` 等），不写服务端数据库；
- 课程生成与教师批量判题不是同一条模型配置链：教师判题用 Python 端 `DEEPSEEK_API_KEY`。

## 6. Node/Python 依赖边界

### Node 自有

- 学习中心课程、教材、练习、诊断；
- 学情工作台聊天、审批、Checkpoint、成果；
- 学生记忆、经验记忆；
- 知识包；
- 运行/团队/技能/CLI/ACP 等。

### Python 自有

- 教师 CSV 导入；
- 教师预处理/问题；
- 教师单题/批量判题；
- 教师/学生学情分析；
- 账号与会话；
- 旧 Agent runtime / projects / approvals / schedules 等。

### 代理回退

`server/src/index.ts` 中：

```ts
app.use('/api', proxyFallback);
```

Node 自有路由必须挂在该行之前。未匹配的 `/api/**` 原样转发到 Python，因此：

- `/api/teacher/**` 实际由 Python 处理；
- `/api/learning/**` 由 Node 处理；
- `/api/xueban/auth/**` 转发 Python；
- 新增 API 时必须确认是否需要在 Node 挂载，否则会落到 Python 并 404。

## 7. 认证与权限

- 登录/注册：`POST /api/xueban/auth/login|register`；
- 会话令牌：`xueban_sessions.token_hash`；
- 前端 `Authorization: Bearer <token>`；
- 角色：`xueban_users.role`；
- 教师 API 统一 `require_teacher()`；
- 学生个人学情用 `student_for(user)`，学生只能看自己的 `teacher_students` 记录；
- 教师可以看所有学生/班级数据。

> 任务级隔离不是统一的：`teacher_answers`、`teacher_grade_jobs`、`teacher_question_versions`、`scoring_points` 有 batch 字段；`teacher_classes/students/exams/questions` 仍是全局表。详见 `DATABASE.md` 与 `KNOWN_ISSUES.md`。

## 8. 异步任务与持久化

### 教师判题

- 持久化：`teacher_grade_jobs`、`teacher_grade_job_items`、`teacher_grade_runs`；
- 执行：FastAPI `BackgroundTasks`，进程内后台线程；
- 断点：item 粒度 pending/running/completed/failed；
- 暂停：pause_requested + 状态机；
- 重试：失败项重置，同一 job_id；
- 心跳：job.heartbeat_at、last_checkpoint_at；
- 缺点：没有独立队列/worker 集群；服务重启后需要重新 resume；同一进程并发控制依赖状态条件更新。

### 课程生成

- 持久化：`learning_generation_jobs`、`learning_generation_events`；
- 执行：Node 进程内异步函数（`void runCourseGeneration(...)`）；
- 重试：课程接口 `POST /courses/:id/retry`；
- 失败：course.status=failed，job.error；不会自动无限重试。

### Agent 对话

- Node `/api/xueban-chat` 负责会话/消息/审批/Checkpoint；
- 运行事件与产物走 `/api/runs`、`/api/run-sessions`；
- 底层模型通过 `transport/api-clients`、BYOK key；部分任务经 Python 旧 runtime；
- 客户端以 SSE 接收文本增量、思考增量、工具事件和 usage。

## 9. 模型与提示词版本

代码中出现的版本标识：

- 学情分析：`teacher_analysis_runs.algorithm_version`，当前为 **`sp-beta-v2`**（`coverage` 分母改为知识点可作答题目数，门槛可配置）；历史 v1.5.9 记录为 `sp-beta-v1`，保留可追溯。
- 教师判题：job 的 `prompt_version=teacher-grade-v3`，run 的 `grading_prompt_version=teacher-grade-v3`、`grader_version=v1.5.10`（v1.5.9 及以前的 run 为 v2/v1.4.1，属历史数据）。
- 趋势：`teacher_exams` 有 grading_rule_version / scoring_rule_version / mapping_version；可比性同时校验规则签名，缺元数据时为 `unverifiable`。

## 10. 关键架构判断

- 当前是“双后端”架构，不是纯 FastAPI，也不是纯 Node。
- 教师学情分析的权威数据在 Python SQLite；学生课程/工作台的大量数据在 Node SQLite。
- `teacher_questions` 是全局兼容表，不是严格的批次隔离表；批次权威题目定义应读 `teacher_question_versions`。
- 教师批量判题不是消息队列系统，而是数据库持久化 + 进程内 BackgroundTasks；不要把 `teacher_grade_jobs` 当成可靠分布式队列。

## 11. v1.5.10 的关键实现约束

1. **题目版本是任务内唯一权威**：`QUESTION_JOIN/QUESTION_COLUMNS` 统一以 `a.question_version_id` 关联版本，缺失才回退全局题表，并在返回值里标记 `question_source=version|global_fallback`。问题检测、质量闸门、成绩、字典、results、分析全部使用同一套 join。
2. **有效成绩/证据统一口径**：`learning_analysis.grade_usage()` 是唯一判定入口。`failed`/`needs_review` 不参与成绩与证据；`manual_corrected` 成绩有效、评分点仅在逐点确认后有效；`graded`/`manual_confirmed` 成绩与评分点均有效。
3. **初始化拆分**：`init()` 只建表（进程内一次）；`bootstrap()` 按 `schema_migrations` 执行迁移/回填，由 FastAPI startup 调用。
4. **分析快照去重**：`save_analysis()` 用 `result_hash` 复用同内容快照，按 scope 只保留最近 `XUEBAN_ANALYSIS_RETENTION`（默认 5）份。
5. **质量闸门留痕**：绕过写入 `teacher_quality_gate_overrides`，不提供无痕绕过。
6. **删除任务先子后父**：确认/修正/attempt/grade/job/版本/作答按依赖顺序清理，删除后自检 residual。
