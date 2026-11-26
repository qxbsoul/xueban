# 数据库结构与数据语义

> v1.5.10 已更新：`expires_at` 实际为 INTEGER；题表区分真题/材料；新增映射历史、方法、依赖图、错误标签、闸门留痕、账号审计、迁移记录等表。完整清单见 `V1.5.10_CHANGES.md`。

## 0. 总览

系统使用两个 SQLite 数据库：

| 数据库 | 路径 | 主要用途 |
| --- | --- | --- |
| Python 主库 | `xueban-backend/.xueban/xueban.sqlite` | 账号/会话、教师任务、原始作答、题目版本、判题、评分点、学情分析 |
| Node 应用库 | `xueban-backend/server/.xueban/app.sqlite` | 学习中心课程、教材、练习、聊天、审批、学生记忆、运行/团队等 |
| 运行库 | `xueban-backend/.xueban/runtime.sqlite` | 运行时事件、快照、Checkpoint（按代码创建/使用） |

SQLite 当前没有显式外键（`PRAGMA foreign_key_list` 为空）。表之间是**逻辑外键**，删除/迁移必须手工保证一致性。

## 1. 主库（Python）核心表

### 1.1 认证与用户

#### `xueban_users`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| id | TEXT PK | 用户 ID |
| email | TEXT UNIQUE | 登录邮箱 |
| password_hash | TEXT | 密码哈希 |
| password_salt | TEXT | 盐 |
| display_name | TEXT NOT NULL | 显示名 |
| auth_type | TEXT NOT NULL | 通常 `password` |
| role | TEXT | `teacher` / `student` |
| created_at | TEXT | 创建时间 |
| stage / grade / grade_set_at | TEXT/INTEGER/TEXT | 学段/年级设置（学生引导） |

#### `xueban_sessions`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| token_hash | TEXT PK | 会话令牌哈希 |
| user_id | TEXT | 逻辑关联 xueban_users.id |
| expires_at | **INTEGER** | 过期时间（epoch 秒；不是 TEXT） |
| created_at | TEXT | 创建时间 |

### 1.2 教师任务与原始数据

#### `teacher_batches`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| id | TEXT PK | 任务 ID，形如 `batch_305316cdd0` |
| filename | TEXT | 上传文件名拼接 |
| status | TEXT | 通常 `committed` |
| counts_json | TEXT | 导入计数：students/classes/courses/standards/requirements/questions/exams/answers/skipped |
| errors_json | TEXT | 导入错误数组 |
| created_at | TEXT | 创建时间 |
| preprocess_status | TEXT | `review` / `confirmed` |
| preprocess_report_json | TEXT | 预处理报告与 issue 快照 |

**一份教师上传文件集合 = 一个 batch。** batch 是任务隔离的主键。

#### `teacher_import_files`

`id, batch_id, filename, rows, fields_json, errors_json`。记录每个上传文件的行数和字段。

#### `teacher_classes`

`id, code, name, grade, created_at`

**没有 batch_id**。班级按 `class_{code}` 全局复用。当前 DC01/DC02 属于唯一真实任务，但代码层面并未按 batch 隔离。

#### `teacher_students`

`id, user_id, code, class_id, name, grade, created_at`

**没有 batch_id**。学生按 `student_{code}` 全局复用；同一学生跨任务时是同一行。

#### `teacher_answers`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| id | TEXT PK | 作答 ID；新导入为 `{batch_id}:{event_id}-r{revision}`，旧数据为 `{event_id}-r{revision}` |
| student_id | TEXT | 逻辑关联 teacher_students.id |
| question_id | TEXT | 题目业务编号（Q001 等） |
| event_id | TEXT | 作答事件编号 |
| revision | INTEGER | 作答修订版本 |
| submit_time | TEXT | 提交时间 |
| original_answer | TEXT | 学生最终答案原文 |
| solution_process | TEXT | 学生解题过程原文 |
| original_score | REAL | 原始得分（赛题 CSV 自带） |
| max_score | REAL | 原始满分 |
| score_status | TEXT | 原始评分状态（如未评分/已评分） |
| raw_json | TEXT | 原始 CSV 行 JSON |
| import_batch_id | TEXT | 任务 ID |
| exam_node_id | TEXT | 测验节点（N01/N02） |
| course_id / standard_id | TEXT | 兼容字段，当前多为空 |
| grading_status | TEXT | `pending` / `graded` / `needs_review` / `failed` |
| question_version_id | TEXT | 逻辑关联 teacher_question_versions.version_id |

唯一约束：`UNIQUE(import_batch_id, event_id, revision)`。
索引：`idx_teacher_answers_event`、`idx_teacher_answers_student`。

**语义：**

- `original_answer` 为空 ≠ 未作答；必须结合 issue 状态/CSV 原始记录判断；
- `grading_status='pending'` 才进入批量判题候选；
- `original_score` 是赛题自带分数，不等同于 AI 判题分；
- `max_score` 是 CSV 行上的满分，题目版本另有 max_score。

#### `teacher_questions`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| id | TEXT PK | 题目业务编号 |
| topic_id | TEXT | 主题编号 |
| material_type | TEXT | 材料类型 |
| question_type | TEXT | 题型 |
| stem | TEXT | 题干 |
| options | TEXT | 选项 |
| reference_answer | TEXT | 参考答案 |
| reference_solution | TEXT | 参考解析 |
| max_score | REAL | 满分 |
| scoring_points | TEXT | 原始评分点文本 |
| indicator_id | TEXT | 观察指标编号 |
| source | TEXT | 来源 |
| standard_id / course_id | TEXT | 兼容字段 |

**这是全局“最新题目”兼容表。** 导入使用 `INSERT OR REPLACE`，同一 question_id 再导入会覆盖。跨批次权威定义应读 `teacher_question_versions`。

**真题与材料同表**：65 行里 B01–B08（教材）、L01–L03（学习卡）是材料行（`question_type` 空、满分 0、无评分点），真题只有 54 道且全部有作答。`summary`/`audit` 通过 `question_stats()` 分别返回 `questions=54` 与 `materials=11`，不要把材料算作题目。

#### `teacher_question_versions`

| 字段 | 类型 | 含义 |
| --- | --- | --- |
| version_id | TEXT PK | `qv:{batch_id}:{question_id}` |
| batch_id | TEXT | 任务 |
| question_id | TEXT | 题目业务编号 |
| topic_id / standard_id / course_id | TEXT | 主题/标准/课程 |
| material_type / question_type | TEXT | 类型 |
| stem | TEXT | 题干 |
| options | TEXT | 选项 |
| reference_answer | TEXT | 参考答案 |
| reference_solution | TEXT | 参考解析 |
| max_score | REAL | 本批次满分 |
| scoring_points | TEXT | 本批次原始评分点 |
| indicator_id | TEXT | 观察指标 |
| source | TEXT | 来源 |
| source_order | INTEGER | 顺序 |
| created_at | TEXT | 创建时间 |

唯一约束：`UNIQUE(batch_id, question_id)`。

**当前真实数据：54 个版本，65 行题表（54 真题 + 11 材料）。** 差集 `B01..B08`、`L01..L03` 是材料行，不是“没有作答的题”，因此本来就不应生成题目版本。

#### `teacher_exams`

`id, exam_date, topics, description, available_questions, course_id, grading_rule_version, scoring_rule_version, mapping_version`

**没有 batch_id。** 当前 N01/N02 是全局节点；导入用 `INSERT OR REPLACE`。趋势版本字段目前全为 NULL。

#### `teacher_topics` / `teacher_indicators`

- `teacher_topics(id, code, title, goal, course_id, standard_id, created_at)`
- `teacher_indicators(id, code, topic_id, description)`

全局知识主题/观察指标；不按 batch 隔离。

#### `teacher_courses` / `teacher_standards`

- `teacher_courses(id, code, title, grade, created_at)`
- `teacher_standards(id, code, title, version, course_id, source_pages, content_json, created_at)`

**Python 侧遗留表**。当前学生课程/教师学案课程使用 Node `learning_courses`；`teacher_courses` 当前 0 行。不要把它误认为当前课程权威表。

### 1.3 判题任务

#### `teacher_grade_jobs`

| 字段 | 含义 |
| --- | --- |
| id | job ID |
| status | queued/running/pausing/paused/completed/partially_failed/failed |
| total/processed/completed/failed/needs_review | 计数 |
| filters_json | class_id/exam_node_id 过滤条件 |
| error | 最后错误 |
| created_at/updated_at/finished_at | 时间 |
| batch_id | 任务 ID |
| textbook_course_id / textbook_unit_id / textbook_context | 教材上下文 |
| provider / model / prompt_version | 模型配置 |
| pause_requested / cancellation_requested | 控制位 |
| worker_id | 领取 worker |
| started_at / paused_at / resumed_at / last_checkpoint_at / heartbeat_at | 运行状态 |
| retry_of | 预留来源 job（当前 retry 同 job_id） |

#### `teacher_grade_job_items`

`id, job_id, answer_id, status, grade_id, error, updated_at, attempt_count, current_attempt, started_at, completed_at, last_error, grading_version, checkpoint_status`

status：`pending` / `running` / `completed` / `failed`。

#### `teacher_grade_runs`

| 字段 | 含义 |
| --- | --- |
| id | 判题结果 ID |
| answer_id/student_id/question_id/event_id/revision | 来源 |
| grading_version | 同一 answer 的判题版本，递增 |
| grading_model / grading_prompt_version | 模型/提示词版本 |
| score/max_score | 分数 |
| is_correct | correct/incorrect/partial/failed |
| step_results_json | 步骤、操作单元、错误类型、理由、评分点链接 |
| error_type / feedback / judgment_reason | 结论 |
| status | graded/needs_review/manual_confirmed/manual_corrected/failed |
| raw_response | 模型原始返回 |
| grader_version / course_id / standard_id / standard_version / scoring_rule_version | 版本 |
| needs_review | 是否需人工复核 |
| textbook_* / step_analysis_status | 教材上下文/步骤分析状态 |
| question_version_id | 题目版本 |

同一 `answer_id` 可有多条，`effective_grade()` 取 `grading_version DESC, created_at DESC` 的第一条非 failed。

#### `teacher_grade_attempts`

`id, task_id(job_id), answer_id, student_code, class_code, question_id, event_id, revision, attempt_number, model, prompt_version, start_time, end_time, failure_stage, error_type, http_status, error_message, retryable, result_id, batch_id, student_id, duration_ms`

**当前真实数据 0 行**：当前暂停任务运行时的旧代码尚未写入 attempt；新代码已补记录，但没有历史回填。

### 1.4 数据问题与人工修正

#### `teacher_data_issues`

| 字段 | 含义 |
| --- | --- |
| id/task_id/issue_key | 问题标识；`UNIQUE(task_id, issue_key)` |
| issue_type/category/severity | 类型/分类/严重度 |
| source_file/source_row_number | 来源 |
| student_id/class_id/question_id/event_id/revision | 关联 |
| raw_value/expected_value | 原值/期望 |
| reason/detection_rule | 原因/规则 |
| handling_status | detected/pending_review/accepted/auto_normalized/resolved/excluded |
| handling_result/resolved_at/resolved_by | 处理结果 |

`issue_key` 由业务键生成，避免重复。

#### `teacher_corrections`

`id, grade_run_id, before_json, after_json, note, teacher_id, created_at`。**当前 0 行**（还没有真实人工修正历史）。

#### `teacher_scoring_point_confirmations`

`id, grade_run_id, answer_id, scoring_point_id, original_ai_score, confirmed_score, max_score, confirmation_status, teacher_id, note, scoring_rule_version, created_at, updated_at`
唯一约束：`UNIQUE(grade_run_id, scoring_point_id)`。

**当前 0 行**（接口/UI 已实现，但真实数据未确认）。

### 1.5 评分点与知识点映射

#### `scoring_points`

`id, code, batch_id, question_version_id, name, description, created_at`

- `code` 是原始评分点编号，如 `Q001P1`；
- 全局兼容行 `batch_id=NULL`；
- 批次行 `batch_id=qv...` 的对应任务，`question_version_id` 指向版本；
- **当前 180 行**：90 条全局 legacy + 90 条批次 scoped。

#### `scoring_point_knowledge_links`

`id, scoring_point_id, knowledge_point_id, weight, source, confidence, status, created_at, updated_at, batch_id`

- `knowledge_point_id` 形如 `kp:I1`；
- 一个评分点可多条链接 → 多对多；
- `source` 当前主要为 `legacy_indicator`；
- `status` 当前全部 `legacy`；
- **当前 180 行**，与 180 个评分点一一对应。

#### 映射语义

```text
评分点 -> 知识点：scoring_point_knowledge_links
每题每知识点权重 = 1 / 可用评分点数 × link.weight
```

同一题同一知识点总权重有上限；低质量/未确认映射不应被当作人工确认。

### 1.6 学情分析运行记录

#### `teacher_analysis_runs`

`id, task_id, scope, scope_id, algorithm_version, parameter_json, result_json, created_at`

- 班级/学生分析会写；
- 当前算法版本默认 `beta-v1`（`save_analysis`）；
- 7 行历史记录。

## 2. Node 应用库核心表

### 2.1 学习课程

| 表 | 关键字段 | 含义 |
| --- | --- | --- |
| `learning_courses` | id, owner_id, title, subject, grade, source_type, source_name, status, stage, progress, error, parse_quality, quality_json | 课程/学案主表 |
| `learning_course_units` | id, owner_id, course_id, parent_id, level, ordinal, title, summary, status | 章/节树 |
| `learning_lessons` | id, owner_id, course_id, unit_id, ordinal, title, objective, content, key_points_json, source_refs_json, status, version, concepts_json, formulas_json, methods_json, teaching_units_json | 学案正文/教案 |
| `learning_materials` | id, owner_id, course_id, filename, mime, size, extraction_status, text_content, original_blob, sha256, format, page_count, parse_audit_json, material_role | 课程标准/教材/原始文件 |
| `learning_source_blocks` | id, owner_id, course_id, material_id, block_type, ordinal, text, markdown, page, line_start, line_end, bbox_json, status, heading_level, original_text, page_status, extraction_method, raw_text, confidence, repair_history_json, table_status | 原文块/证据 |
| `learning_knowledge_points` | id, owner_id, course_id, unit_id, lesson_id, ordinal, title, explanation, concepts_json, formulas_json, methods_json, source_block_ids_json, status, confidence, prerequisites_json, origin | 课程知识点与先修关系 |
| `learning_exercises` | id, owner_id, course_id, unit_id, lesson_id, phase, ordinal, stem, answer, explanation, difficulty, source_type, validation_status, source_ref_json | 课上/课后练习 |
| `learning_exercise_attempts` | id, owner_id, exercise_id, course_id, lesson_id, answer, status, feedback, created_at | 练习作答 |
| `learning_lesson_progress` | owner_id, course_id, lesson_id, status, completed_at, updated_at | 学习进度 |
| `learning_textbook_shares` | course_id, teacher_owner_id, student_owner_id, created_at | 教师教材共享 |
| `learning_generation_jobs` | id, owner_id, course_id, status, stage, error, attempts, batch_count, details_json | 课程生成任务 |
| `learning_generation_events` | id, job_id, owner_id, course_id, kind, message, payload_json, created_at | 生成过程事件 |
| `learning_diagnoses` | id, owner_id, course_id, status, summary, mastery_json, error | 课程诊断 |

**当前真实数据（v1.5.10 复核）：**

- `learning_courses=1`（活课程）
- 活课程：`learning_course_units=26`、`learning_lessons=53`（38 ready / 15 draft）、`learning_materials=1`、`learning_knowledge_points=0`、`learning_exercises=0`
- 全表计数另含已删除 guest 课程残留：2 单元 + 1 教案 + 2 知识点 + 4 练习（`course_id=8f432f22…`，`owner_id=guest_5a1f6c91f3`）
- `learning_generation_jobs=0`、`learning_generation_events=0`、`learning_source_blocks=0`、`learning_exercise_attempts=0`、`learning_lesson_progress=0`
- `learning_textbook_shares=40`

> 不要把 `learning_knowledge_points=2`、`learning_exercises=4` 当作活课程内容；它们是删除课程后的残留。D14 的结论是“统计隔离 + 明确材料口径”，未擅自删除数据。

**关键语义：**

- 学案正文在 `learning_lessons.content`；
- 课程目录在 `learning_course_units`；
- `learning_knowledge_points.prerequisites_json` 是知识依赖候选，但当前没有完整图谱/前端依赖图验证；
- `learning_source_blocks` 当前 0 行，说明当前课程没有完整源块级追溯链；原文主要保存在 `learning_materials.text_content`。

### 2.2 教师/学生账号（Node 自有）

Node 也有 `xueban_chat_*`、`auth_sessions`、`auth_profiles` 等聊天/账号表；当前教师/学生登录主链路使用 Python `xueban_users/xueban_sessions`，Node 的聊天鉴权使用 `requireAccount()`。

### 2.3 聊天/记忆/运行

| 表 | 含义 |
| --- | --- |
| `xueban_chat_conversations` | 学情工作台会话 |
| `xueban_chat_messages` | 消息 |
| `xueban_chat_checkpoints` | Checkpoint |
| `xueban_chat_approvals` | 人工审批 |
| `student_memories` / `student_memory_versions` / `student_memory_evidence` / `student_memory_candidates` | 学生记忆与证据 |
| `student_learning_events` | 学习事件 |
| `memory_lessons` / `memory_profile_*` | 经验记忆 |
| `runs` / `run_index` / `run_artifacts` / `agent_hops` | Agent 运行与产物 |
| `token_usage` | 模型用量与成本估算 |
| `conversations` / `messages` / `groups` / `group_messages` / `agents` | 旧工作台/群聊 |
| `learning_*` | 学习中心 |

### 2.4 历史表

主库仍存在旧公司/订单/商品/团队表：

```text
companies, departments, employees, employee_config_versions,
projects, products, orders, order_events, ledger, approvals,
task_executions, xueban_projects, ...
```

README 已声明商品/订单模块整体下线。这些表可能是旧数据残留，不应作为当前产品能力依据。

## 3. 数据关系图（逻辑）

```text
xueban_users 1─n xueban_sessions

teacher_batches 1─n teacher_answers
teacher_batches 1─n teacher_question_versions
teacher_question_versions 1─n teacher_answers (question_version_id)
teacher_questions 1─n teacher_question_versions (question_id, 全局兼容)

teacher_batches 1─n teacher_grade_jobs
teacher_grade_jobs 1─n teacher_grade_job_items
teacher_grade_job_items n─1 teacher_answers
teacher_grade_jobs 1─n teacher_grade_runs (逻辑，实际按 answer)

teacher_answers 1─n teacher_grade_runs
teacher_grade_runs 1─n teacher_corrections
teacher_grade_runs 1─n teacher_scoring_point_confirmations

teacher_questions 1─n scoring_points (code 解析)
scoring_points 1─n scoring_point_knowledge_links
teacher_indicators 1─n teacher_topics
scoring_point_knowledge_links n─1 knowledge_point (kp:{indicator.code})

teacher_batches 1─n teacher_data_issues
teacher_batches 1─n teacher_exams?  （实际无 batch_id，逻辑共享）

learning_courses 1─n learning_course_units
learning_course_units 1─n learning_lessons
learning_lessons 1─n learning_exercises
learning_courses 1─n learning_materials
learning_materials 1─n learning_source_blocks
learning_courses 1─n learning_knowledge_points
learning_courses 1─n learning_generation_jobs
learning_generation_jobs 1─n learning_generation_events
```

## 4. 数据隔离机制

### 已按 batch 隔离

| 表/字段 | 隔离方式 |
| --- | --- |
| `teacher_answers.import_batch_id` | 判题/分析/删除按 batch |
| `teacher_grade_jobs.batch_id` | 任务 |
| `teacher_question_versions.batch_id` | 题目版本 |
| `scoring_points.batch_id` | 评分点映射 |
| `scoring_point_knowledge_links.batch_id` | 映射 |
| `teacher_data_issues.task_id` | 数据问题 |
| `teacher_analysis_runs.task_id` | 学情快照 |

### 未按 batch 隔离

| 表 | 问题 |
| --- | --- |
| `teacher_classes` | 班级全局；跨任务同 code 共用 |
| `teacher_students` | 学生全局；跨任务同 code 共用 |
| `teacher_questions` | 全局题面，导入覆盖 |
| `teacher_exams` | 全局节点，导入覆盖；无 batch_id |
| `teacher_topics` / `teacher_indicators` | 全局主题/指标 |
| `teacher_courses` / `teacher_standards` | 全局兼容表 |

**结论：任务隔离是“作答/版本/判题/问题/分析”级，不是“班级/学生/题目全局表”级。**

## 5. 迁移历史

代码中可验证的迁移：

1. `teacher_answers` 旧约束 `UNIQUE(event_id, revision)` → 新约束 `UNIQUE(import_batch_id, event_id, revision)`。
   - 实现：`migrate_answer_scope()`（`xueban_teacher.py` 约 132 行）。
   - 只在检测到旧约束时重建表；重建时保留全部列。

2. 新增 `teacher_question_versions`，并把已有作答/判题关联到版本。
   - 实现：`ensure_question_versions()`（约 151 行）。
   - 只对“有作答的题目”回填；当前 54 个版本。

3. `scoring_points` 去全局 `code UNIQUE`，增加 `batch_id/question_version_id`。
   - `migrate_scoring_points_scope()`（`learning_analysis.py` 约 59 行）。
   - 评分点从 90 增至 180（全局 + 批次）。

4. `teacher_grade_attempts` 增加 `batch_id/student_id/duration_ms`。
   - `init()` 中增量 `ALTER TABLE`。

5. `teacher_exams` 增加 `grading_rule_version/scoring_rule_version/mapping_version`。
   - `init()` 中增量 `ALTER TABLE`。

6. `teacher_scoring_point_confirmations` 新建。

> 迁移没有版本号表，也没有 migration ledger；依赖 `PRAGMA table_info` / `sqlite_master` 做幂等检查。

## 6. 重要字段真实含义

- `teacher_answers.original_answer`：学生最终答案原文；空字符串可能是未作答/录入缺失，需 issue 区分。
- `teacher_answers.solution_process`：学生原始解题过程；为空不代表未作答。
- `teacher_answers.original_score`：赛题 CSV 分数，不是 AI 分。
- `teacher_answers.max_score`：CSV 行满分；权威题目满分优先 `question_version.max_score`。
- `teacher_grade_runs.score`：判题总分；`step_results_json` 里分数是步骤分。
- `teacher_grade_runs.needs_review`：需人工复核；不等于失败。
- `manual_confirmed`：整题确认；不等于逐评分点已确认。
- `manual_corrected`：整题分数被人工改；旧评分点证据不能直接视为已同步。
- `teacher_scoring_point_confirmations.confirmed_score`：教师逐评分点确认值；分析优先使用。
- `scoring_points.batch_id=NULL`：旧全局映射；不是当前批次权威。
- `learning_lessons.content`：学案正文；不是 `learning_materials.text_content` 的复制品。
- `learning_knowledge_points.prerequisites_json`：候选先修关系；当前没有完整验证/图谱消费。
