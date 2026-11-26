# 核心设计决策

> 本文区分“当前有效设计”“已废弃设计”“尚未决定”。判断依据是当前代码，不是旧聊天或旧 README。

> v1.5.10 更新：无 Key 时不再有 rule-assisted 降级；趋势可比性改为规则签名校验；新增学生账号、覆盖率分层、映射确认、依赖图、错误分类、推荐等已确认设计。详见 `V1.5.10_CHANGES.md`。

## A. 当前有效设计

### A1. 学案就是课程，不另建内置课程体系

**决策：** 学生课程/学案使用 Node 学习中心的一套模型：

```text
learning_courses
  → learning_course_units
  → learning_lessons
  → learning_exercises
  → learning_knowledge_points
  → learning_materials / learning_source_blocks
```

**证据：**

- `server/src/routes/learning.ts` 的课程/教材/练习接口；
- `LearningCenterView.tsx` 的“我的课程/教材管理/练习中心/我的学情”；
- `TeacherApp.tsx` 的 TextbookCenter 调用 `/api/learning/courses`。

**不采用：**

- 不再以 Python `teacher_courses` / `teacher_standards` 作为当前课程权威。
- `teacher_courses` 当前 0 行，是遗留兼容表。

### A2. 一份教师上传数据 = 一个任务（batch）

**决策：** 一次多文件上传生成一个 `teacher_batches.id`；所有作答通过 `import_batch_id` 归属该任务。

**证据：**

- `import_rows()` 生成 `batch_id`；
- `teacher_answers.import_batch_id`；
- 批量判题按 `batch_id` 选答案；
- 学情 API 参数 `batch_id`。

**注意：**

- 班级/学生/题目全局表尚未 batch 化，见 `KNOWN_ISSUES.md`。

### A3. 暂停、继续、失败重试、重新判题不新建业务任务

**决策：**

- `teacher_batches` 是业务任务；
- `teacher_grade_jobs` 是判题执行；
- 暂停 → `pause_requested` / `paused`；
- 继续 → 同一 job_id 重新 queued；
- 失败重试 → 同一 job_id 重置 failed items；
- 单题重新判题 → 同一 answer_id 增加 `grading_version`。

**证据：**

- `pause_job()` / `resume_job()` / `retry_job()`；
- `run_job()` 领取 pending item；
- `insert_grade()` 生成 `grading_version`。

### A4. 判题必须保留原始作答、步骤、评分点、模型依据、人工修正

**决策：**

- 原始作答：`teacher_answers.original_answer`、`solution_process`、`raw_json`；
- 判题原始模型输出：`teacher_grade_runs.raw_response`；
- 步骤/评分点/错误类型：`step_results_json`；
- 人工整题修正：`teacher_corrections`；
- 逐评分点确认：`teacher_scoring_point_confirmations`；
- attempt：`teacher_grade_attempts`。

**现状：**

- 结构已存在；
- `teacher_corrections` 当前 0 行；
- `teacher_scoring_point_confirmations` 当前 0 行；
- `teacher_grade_attempts` 当前 0 行（历史任务没有记录）。

### A5. 未作答、错误作答、缺失记录、未评分必须区分

**决策：**

- 原始答案空：`missing_final_answer` issue；
- 解题过程空：`missing_process` issue；
- 未判题：`grading_status='pending'`；
- 判题失败：`grading_status='failed'` / grade.status='failed'；
- 需复核：`needs_review`；
- 无模型 Key：`503 GRADER_NOT_CONFIGURED`，**不写入成绩、不生成证据**；v1.5.10 已删除不可达的 rule-assisted 分支。

**实际：**

- 当前 806 条作答中 701 pending、102 graded、3 needs_review；
- 538 条 missing_process 被 accepted，不是 0 分；
- 5 条 missing_final_answer 仍 pending_review。

### A6. 评分点是学情分析的基础证据单元

**决策：** 掌握估计不从“题目对错”直接算，而从评分点证据：

```text
teacher_grade_runs.step_results_json.scoring_point_links
  → scoring_points
  → scoring_point_knowledge_links
  → knowledge point evidence
  → Beta(2,2)
```

**证据：**

- `learning_analysis._evidence()`；
- `_knowledge_links()`；
- `student_profile()`；
- 学生端“评分点证据/未映射证据”。

### A7. 评分点与知识点支持多对多

**决策：**

- 一个评分点可链接多个知识点；
- 一个知识点可由多个评分点提供证据；
- 每题每知识点使用 `1 / 可用评分点数 × link.weight` 归一；
- 同一题同一知识点总权重有上限。

**现状：**

- `scoring_point_knowledge_links` 当前 180 条，全部 legacy；
- 没有教师逐条确认，不能宣称映射已被人工确认。

### A8. 学情结果必须可追溯

**决策：** 学生/班级分析返回 evidence 明细，前端可展开到具体：

```text
answer_id
question_id
scoring_point_id
earned_score/full_score
error_type
judgment_reason
```

**实现：**

- `student_profile()` 返回 evidence；
- `StudentAnalytics.tsx` 可展开证据；
- 评分点表显示映射/异常/缺结果。

### A9. 数据不足不强行给确定性诊断

**决策：**

- 没有评分点证据 → 不显示为 0 分；
- 掌握估计使用 Beta 后验与可信区间；
- 证据权重/覆盖率不足 → `insufficient`；
- 趋势缺版本元数据 → `unverifiable`；
- 正确率/得分率在 0 有效判题时返回 null。

**实现：**

- `_status()`；
- `Beta(2,2)` 精确区间；
- `question_longitudinal()` 的三种状态。

### A10. 教师/学生角色分流

**决策：**

- `xueban_users.role` 决定前端壳与 API 权限；
- teacher → TeacherApp；
- student → 学生工作台/学习中心；
- 学生只能看自己的 `analysis/me`；
- 教师可看班级/学生/任务。

### A11. 学生账号必须可发放、首登改密、可重置（v1.5.10 新增）

- 导入学生时创建账号并生成一次性初始密码，库中只存 PBKDF2 哈希；
- `must_change_password=1`，首次登录后必须改密；
- 教师可重置密码（产生一次性新密码 + 审计），可把自注册学生账号绑定到花名册；
- 不允许明文存密码、日志输出密码。

### A12. 掌握度分层与覆盖口径（v1.5.10 新增）

- 覆盖分母 = 该知识点在任务中的可作答题目数，不是全卷题目数；
- 证据门槛 = 有效权重、覆盖率、题目数三者同时满足；
- 状态：证据不足 / 掌握不足 / 初步掌握 / 基本掌握 / 熟练掌握；
- 证据质量区分 AI 与教师确认，单题权重集中度单独暴露；
- 门槛可通过环境变量配置，算法版本 `sp-beta-v2`。

### A13. 映射必须可确认、可追溯（v1.5.10 新增）

- legacy 映射只是候选，不自动视为教师确认；
- 教师确认写入 `status=confirmed` + `confirmed_by/at/version`；
- 每次修改写 `scoring_point_link_history`（前后值、操作人、时间）；
- 权重按评分点归一化到 1；
- 知识点与方法分开建模、分开统计。

## B. 已废弃/不应继续采用的设计

### B1. 独立“内置课程体系”

**废弃。**

- 不再以 Python `teacher_courses` 作为课程主表；
- 当前课程主表是 Node `learning_courses`。
- 看到旧文档/旧代码里“内置课程”描述时，不要据此新建第二套课程。

### B2. 把 N01/N02 当作班级任务组或“题组”

**废弃/错误理解。**

- N01/N02 是测验节点 `exam_node_id`；
- 不是班级任务组；
- 不是 N01/N02 两个小组；
- 班级是 DC01/DC02；
- 任务分析单位是 `question_id`，不是 N 节点。

### B3. 用全局 `teacher_questions` 作为跨批次判题权威

**已废弃（但代码仍有兼容写入）。**

- 导入仍 `INSERT OR REPLACE INTO teacher_questions`；
- 新代码判题/分析优先 `teacher_question_versions`；
- 不要把全局题面当作多批次安全的权威来源。

### B4. `manual_confirmed` = 逐评分点已确认

**废弃。**

- `manual_confirmed` 只是整题确认；
- 逐评分点确认在 `teacher_scoring_point_confirmations`；
- 不能因为整题 confirmed 就把旧 AI 评分点当作人工确认。

### B5. 商品/订单/公司经营作为当前学情产品主线

**已下线/历史遗留。**

- README 声明商品/订单模块下线；
- 主库仍有 `products/orders/companies/...` 旧表；
- 不要把这些表当当前业务能力。

### B6. 用 `teacher_courses`/`teacher_standards` 作为当前学案来源

**废弃。**

- 当前教师 TextbookCenter 调 `/api/learning/courses`；
- Python `teacher_courses` 为空；
- `teacher_standards` 也是兼容表。

## C. 尚未决定/需要产品确认

### C1. 班级/学生是否必须 batch 级隔离

当前：

- `teacher_classes/students` 没有 batch_id；
- 同一 code 跨任务复用。

未决：

- 是“同校同 code 视为同一实体”，还是“每任务独立快照”？
- 若跨任务同 code 但不同年级/班级，当前会复用错误实体。

### C2. 趋势版本元数据如何回填

当前：

- `teacher_exams.grading_rule_version/scoring_rule_version/mapping_version` 全 NULL；
- 所以真实趋势全部 `unverifiable`。

未决：

- 历史任务补什么版本？
- 版本号由谁签发（评分规则/映射/算法）？
- 旧数据是否允许标记“不可比”永久保留？

### C3. 是否把教师判题改为 BYOK

当前：

- 教师批量判题用服务器 `.env` 的 `DEEPSEEK_API_KEY`；
- 学生对话/课程生成用前端 BYOK。

未决：

- 教师判题是否也按教师账号计费/限额？
- 服务器 key 与 BYOK 的优先级？

### C4. 是否保留旧 company/order/task_execution 框架

当前：

- 代码和表仍在；
- 产品主线已转向学情/学习。

未决：

- 删除、隔离还是保留作为 Agent 运行底座？

### C5. 知识依赖是否需要成为独立图谱

当前：

- `learning_knowledge_points.prerequisites_json` 有字段；
- 教案里展示“前置知识”；
- 没有独立依赖图/先修校验 API。

未决：

- 是否做知识点 DAG、掌握传播、先修阻断？

### C6. 评分点映射的最终权威

当前：

- 90 条全局 legacy + 90 条批次 scoped；
- 全部 `legacy_indicator`，无教师确认；
- `mapping_audit` 明确标记 legacy。

未决：

- 教师逐条确认？
- 导入时带人工映射？
- 允许全局模板 + 批次覆盖的优先级？

### C7. 质量闸门是否强制阻断 API

**已决（v1.5.10）：** 后端强制。

- 阻断时 `POST /grade/batch` 返回 409 `QUALITY_GATE_BLOCKED`；
- 教师确需继续必须 `force=true` + 理由，写 `teacher_quality_gate_overrides`；
- 前端禁用只是提示，不是安全边界。

### C8. 班级/学生/考试节点是否 batch 级隔离（仍未决）

v1.5.10 未改全局表结构。需要产品确认后再迁移，避免破坏跨任务学生复用与账号绑定。

## D. 当前实现的明确约束（给新 Agent）

1. 任何跨批次结论必须先看 `import_batch_id` / `question_version_id`；不要只看 `question_id`。
2. 任何学情结论必须能回到 `answer_id + scoring_point_id`。
3. 不要把 `manual_confirmed` 当成评分点确认。
4. 不要把 `comparable` 当成默认；先看版本元数据。
5. 不要把 `teacher_questions` 当批次权威；优先 `teacher_question_versions`。
6. 不要把 `learning_courses` 和 Python `teacher_courses` 混用。
7. 不要在前端禁用作为安全边界；后端 API 才是边界。

## v1.5.11 新增决策

- **A14 角色严格白名单**：只有 `role == "teacher"` 才是教师；缺失/未知角色一律不是教师，也不默认学生。游客显式 `guest`。
- **A15 教师资源归属**：新任务写 `owner_id`；任务/作答/判题/学生/分析按归属校验；历史 `owner_id NULL` 作为共享兼容，不允许新任务无主。
- **A16 趋势版本规则**：版本值不同必须 non_comparable；只有 `teacher_version_compat` 中登记的显式兼容关系才允许 comparable；缺少版本元数据为 unverifiable。
- **A17 方法机会独立**：覆盖率分母来自题目-方法机会表，不能从学生已用方法反推；无机会数据返回 null，不显示 100%。
- **A18 身份不确定**：同学生编号跨任务默认 `unverified`，教师显式确认同一人/不同人并填依据；未确认不做合并结论。
- **A19 映射版本**：确认/拒绝生成新映射版本；判题和学情快照记录版本；历史版本与旧结果保留，变化后通过 impact 识别。
- **A20 错误分类真值**：人工标注单独存 `error_label_gold`，规则/模型标签不参与真值计算；没有 gold 不报告指标。
- **A21 游客演示边界**：游客不能进入教师端真实数据；演示保留在工作台/学习中心，不为了安全直接关闭演示。


## v1.5.12 新增决策

- **A22 单版本原子迁移**：每个 `schema_migrations` 版本在独立 `BEGIN IMMEDIATE` 事务中完成数据变更与版本登记；失败只回滚该版本。
- **A23 迁移失败不启动**：启动钩子遇到迁移异常必须终止进程；不允许在部分迁移的库上继续提供教师业务。
- **A24 失败可观测**：失败版本/原因写入 `schema_migration_failures`，并可通过公开只读状态接口与 CLI 查询；错误必须脱敏。
- **A25 未完成迁移的接口门槛**：`running/failed` 状态教师业务接口返回 503，不依赖前端。
- **A26 版本兼容按任务**：兼容记录必须绑定 `batch_id` 并校验归属，不允许全局静默影响其他教师。
- **A27 正式库迁移需授权**：只做只读预检与副本预演；正式执行需明确确认，不自动启动连接正式库的服务。
