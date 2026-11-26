# 功能状态（以代码为准）

> v1.5.10 更新：P0/P1/P2 范围的缺陷已修复并有回归测试；新增账号、映射确认、依赖图、错误分类、推荐、班级对比、趋势 EWMA。逐项状态与证据见 `V1.5.10_CHANGES.md`，最终验收见 `V1.5.10_ACCEPTANCE.md`。

状态定义：

- **已完成**：有真实代码、API/数据落库，并有可复核验证路径。
- **部分完成**：主链路存在，但缺少隔离、校验、UI、失败处理或真实数据验证。
- **仅有入口**：前端按钮/接口壳存在，后端能力不完整。
- **未完成**：没有可信实现。
- **存在缺陷**：已实现但当前代码/数据有明确问题。

## 1. 教师端

### 1.1 登录/角色分流

**已完成**

- `AuthView` → `POST /api/xueban/auth/login|register|guest`；
- `xueban_users.role` 区分 teacher/student；
- `App.tsx` 对 teacher 渲染 `TeacherApp`，学生渲染工作台；
- 教师 API `require_teacher()`。

### 1.2 CSV 数据导入

**已完成（v1.5.10 修复去重作用域）**

实现：

- `TeacherApp.DataManager` 上传多文件；
- `preview()` / `upload_import()`；
- `read_csvs()` 按中文表头关键词识别学生/课程要求/题目/测验/作答；
- `validate()` 阻断缺文件/缺字段/重复 event+revision；
- `import_rows()` 落库 students/classes/questions/exams/answers；
- 导入后生成 issues；
- 导入响应返回一次性学生初始账号（`student_credentials`）。

已修复：

- 去重改为 `(import_batch_id,event_id,revision)`，跨任务同事件不再互相吞掉；同批重复保留原因；
- 真实导入路径测试 `tests/test_import_scope.py`。

剩余限制：

- `teacher_questions`、`teacher_exams`、`teacher_classes/students` 仍不是 batch 级隔离；
- 题表同时保存真题与材料行，统计已分开，但物理表未拆。

### 1.3 数据预处理与问题

**部分完成**

- `_build_issues()` 生成缺失过程、缺失答案、分数越界、评分点总分不一致等问题；
- `GET /batches/{id}/preprocessing` 返回报告；
- `POST /preprocessing/confirm` 确认；
- `GET /batches/{id}/issues` 分页。

限制：

- 当前真实任务 547 条问题，9 条 pending；前端主要展示 pending；
- 部分 issue 的 `raw_value/expected_value` 是文本，前端未做完整处理；
- 缺失作答清单不能确定，报告里明确 `unknowns`。

### 1.4 批量判题

**部分完成，存在运行状态**

实现：

- 创建 job/items；
- BackgroundTasks 执行；
- 题目版本、原始作答、步骤、评分点、raw_response 落库；
- 暂停/继续/重试同 job_id；
- 失败项重试；
- 质量闸门 API 与前端提示。

限制/缺陷：

- v1.5.10 已强制 quality gate（409 + force 留痕）；
- 当前真实任务暂停：103 completed / 1 failed / 701 pending；没有全量完成；
- 教师判题使用服务器 `.env` 的 `DEEPSEEK_API_KEY`，不是每个教师 BYOK（未决）；
- `teacher_grade_attempts` 新代码已真实写入（mock 全链路测试验证），历史 108 条未回填；
- `teacher_question_versions` 54 个对应 54 道真题；11 条材料行不需要版本。

### 1.5 单题查看与重新判题

**已完成（基础）**

- `GET /answers/{id}`；
- `POST /answers/{id}/grade`；
- 题目优先读版本；
- 分数/步骤展示。

限制：

- 单题重新判题是否触达完整评分点确认流程，没有真实回归。

### 1.6 人工整题修正/确认

**部分完成**

- `manual()` 改分数和状态，写 `teacher_corrections`；
- `confirm()` 只确认整题，写 correction；
- `teacher_corrections` 当前 0 行，尚无真实历史。

### 1.7 逐评分点确认

**部分完成**

- 表 `teacher_scoring_point_confirmations`；
- `GET/POST /grades/{id}/scoring-points`；
- 前端单题详情逐评分点输入；
- 确认分参与学情分析；
- 总分不一致 → needs_review。

限制：

- 真实确认 0 条；
- 没有把确认历史与重新判题的版本 UI 完整关联；
- 仅确认评分点不能自动修正 `step_results_json` 原始 AI 分。

### 1.8 任务管理/删除

**部分完成**

- 任务列表、详情、删除；
- 删除清理 answers/job/items/grades/corrections/attempts/confirmations/analysis_runs/question_versions/scoring_points/links/import_files/batch；
- 不删 `teacher_questions` 全局表（可能仍被其他批次使用）。

限制：

- 没有真实“删除一个任务后另一任务继续分析”的生产回归，只有隔离测试。

## 2. 判题质量闸门

**部分完成**

API：

```text
GET /api/teacher/batches/{batch_id}/quality-gate
```

检查：

- preprocess confirmed；
- orphan student/question；
- invalid max_score；
- missing reference answer；
- missing scoring points；
- unmapped knowledge；
- duplicate event/revision（当前唯一约束下几乎不会出现）；
- missing process。

限制：

- 不是 `batch()` 的强制前置条件；
- 没有覆盖所有 issue 类型；
- 前端只显示摘要。

## 3. 评分点与知识点映射

**部分完成/存在设计债务**

实现：

- `scoring_points` 支持 batch/version；
- `scoring_point_knowledge_links` 多对多；
- `_knowledge_links(sp, batch_id)` 优先批次映射，回退全局；
- `learning_analysis` 按评分点计算证据。

现状：

- 180 个评分点：90 全局 legacy + 90 批次；
- 180 条 link，全部 `legacy`；
- 教师没有逐条确认；
- `mapping_audit` 报 `mapping_status=legacy indicator mappings are not teacher-confirmed`。

结论：

- 能生成可追溯的“候选映射”；
- 不能把当前映射当成人工确认。

## 4. 学情分析

### 4.1 学生个人学情

**已完成（基础）**

- `GET /api/teacher/analysis/me`；
- `StudentAnalytics` 显示已作答/应完成、评分点证据、可分析知识点、未映射证据、知识点掌握、可信区间、证据明细；
- 没有证据不显示为 0；
- 学生只能看自己的数据。

限制：

- “应完成”无完整清单时为未知；
- 证据只覆盖已判题/已映射部分。

### 4.2 班级学情

**已完成（基础）**

- `GET /api/teacher/analysis/classes`、`/classes/{id}`；
- 班级人数、有效估计、可分析知识点、整体掌握、薄弱学生、证据不足；
- 知识点分布、任务分析、趋势、高频错误、学生列表。

限制：

- 班级/学生是全局表，不是严格 batch 级；
- 没有真实任务多时间点完整趋势。

### 4.3 单题/评分点分析

**部分完成**

- `question_analysis_detail` 返回题目、总体、分数分布、评分点、错误类型、学生表现；
- 评分点区分 valid_events/valid_students/event_average/student_average；
- 人工修正总分有效，未同步评分点证据被标记；
- v1.5.10 修复学生等权平均变量遮蔽（Q011/Q014/Q015 回归 1.33/0.8/2.0）。

限制：

- 评分点确认只覆盖存在 step links 的评分点；
- 没有完整全量数据回归。

### 4.4 多班级对比

**已完成（基础）**

- `GET /analysis/compare?class_ids=...`；
- 去重、batch 归属校验、知识点并集、null、差异；
- 前端班级总览可勾选多个班级。

限制：

- 真实三班数据不在当前任务内，只有隔离测试。

### 4.5 趋势分析

**已实现（v1.5.10 重做可比性）**

- 班级趋势按 N01/N02 显示；
- 三态：`comparable / non_comparable / unverifiable`，附具体原因、规则签名、学生交集、版本差异；
- EWMA 与原始得分率同时返回；
- 单题纵向同样走 `question_signature`，不再永远返回 unverifiable。

限制：

- 真实 N01/N02 题集不同（27 题 vs 54 题），保持 `non_comparable`；仅回填版本字段不会让它可比。N01 为 `unverifiable`（缺版本元数据）。

### 4.6 单题纵向分析

**部分完成**

- `GET /classes/{class}/questions/{qid}/longitudinal`；
- 返回时间点、作答学生、有效判题、待复核、失败、平均分、得分率、满分/评分点一致性；
- Q011 真实返回 N01/N02 两个时间点，但状态 `unverifiable`；
- 前端显示时间点表格，没有伪造趋势线。

限制：

- 纵向可比性依赖版本元数据，目前没有；
- 没有正式任务多时间点回归测试。

## 5. 教材解析/课程生成/学习中心

### 5.1 教材解析

**已完成（基础，Node）**

- `learning_materials`；
- PDF/Word/PPT/Excel/图片等解析；
- `learning_source_blocks` 设计用于源块追溯；
- 课程生成失败/重试有 job/event。

限制：

- 当前 `learning_source_blocks=0`，现有课程没有完整源块级追溯；
- 解析质量字段存在，但不能等价于全部原文已核验。

### 5.2 课程/学案生成

**已完成（基础，Node）**

- `POST /api/learning/courses/joint`（教材+课标）；
- `POST /api/learning/courses`（单来源）；
- 课程/单元/学案/知识点/练习落库；
- 可重试、可删除、可共享；
- 当前 1 门课程 ready，54 个 lesson（38 ready/16 draft）。

限制：

- 知识点先修关系只在 `prerequisites_json`/教案展示，没有完整图谱消费；
- 课程质量/覆盖报告不是强校验；
- 生成依赖模型，真实成本需控制。

### 5.3 学生练习/进度

**部分完成**

- `learning_exercises`、`learning_exercise_attempts`、`learning_lesson_progress` 表与 API 存在；
- 当前 exercise attempts=0，说明真实学生练习尚未使用或未落库；
- 练习中心 UI 存在。

## 6. 学生/工作台 Agent

### 6.1 学情工作台对话

**部分完成（历史模块庞大）**

- `xueban-chat` 会话/消息/审批/Checkpoint；
- SSE 流式；
- 群聊/单聊；
- 运行日志/成果；
- 学生记忆/经验记忆。

限制：

- 与教师学情分析不是同一数据模型；
- 旧 company/order/task_execution 代码仍存在，文档/README 已声明商品订单下线；
- 真实模型调用依赖 BYOK/服务端 key，缺 key 时会报错。

### 6.2 知识依赖

**部分完成**

- `learning_knowledge_points.prerequisites_json` 有字段；
- 课程生成时把前置知识写入 lesson 文本；
- 没有独立知识依赖图谱页面或严格的先修校验。

## 6.5 v1.5.10 新增能力

| 能力 | 状态 | 说明 |
| --- | --- | --- |
| 评分点→知识点映射管理 | 已完成 | 多对多、权重归一化、教师确认、历史、审计接口；前端“评分点映射”面板 |
| 评分点→方法映射 | 已完成 | `learning_methods` + `scoring_point_method_links`，按方法单独估计 |
| 逐评分点确认历史 | 已完成 | 确认人/时间/前后值/版本写入 `teacher_scoring_point_confirmation_history` |
| 知识依赖图 | 已完成 | 候选边按主题顺序生成；确认边拒绝自环/重复/循环；直接/间接前置；学生前置薄弱；SVG 展示 |
| 错误类型辅助分类 | 已完成 | 仅用已保存判题步骤证据；10 类标签；教师可覆盖；批量接口 |
| 个性化练习推荐 | 已完成 | 知识点/方法/错误模式/前置依赖四类可解释推荐 |
| 学生账号发放 | 已实现（待真实发放） | 初始密码一次性返回、首登改密、教师重置、绑定、审计 |
| 多班对比 | 已完成 | 任意选择当前任务班级，含完成率、有效学生、覆盖、状态分布、错误类型 |
| 学生跨任务趋势 | 已完成 | `student_knowledge_trend` + EWMA；证据不足点不补 0 |
| 质量闸门强制/留痕 | 已完成 | 409 + force 理由 + `teacher_quality_gate_overrides` |

## 7. 明确未完成/不能宣称的能力

- 未对当前 806 条真实作答执行全量判题；
- 未真实跑过 701 条 pending 的暂停后继续；
- 未做并发 worker 压测；
- 未完成所有质量闸门异常矩阵；
- 未完成真实教师逐评分点确认；
- 未完成真实任务多时间点纵向趋势；
- 未完成班级/学生全局表的 batch 级隔离；
- 未回填真实趋势规则版本；
- 未验证旧 company/order 模块是否应删除或彻底隔离。
