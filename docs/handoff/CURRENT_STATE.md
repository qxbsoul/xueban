# 当前状态（v1.5.13）

## 1. Git

```text
仓库：D:\capital lion\xueban
分支：main
v1.5.10 基线：be74df5
v1.5.11：1079e58
v1.5.12：d9dffe9
本次：v1.5.13（见 git log 最新提交）
工作区：包含 v1.5.12 代码、测试、脚本与 docs/handoff 更新
```

## 2. 版本

- 产品版本：`v1.5.13`。
- 学情算法：`sp-beta-v3`（方法机会分母独立；趋势版本严格比较）。
- 判题提示词：`teacher-grade-v3`；`grader_version=v1.5.11`。
- 历史 108 条判题 run 仍是旧版本（v2/v1.4.1），本次未重跑；旧 run 的 `mapping_version` 为 NULL。

## 3. 运行状态（交接时）

```text
真实后端（8000/8002）：未启动
前端 3007：开发会话可能仍在运行
验收使用隔离副本与临时数据库
```

启动：`npm run dev`。首次启动会自动执行 `schema_migrations` v1–v5；**正式库尚未执行**，等待授权。迁移失败时服务不会启动。

## 4. 真实数据库状态（v1.5.9 业务数据；v1.5.11 未改写业务数据）

### 4.1 主任务

```text
batch_305316cdd0
students=40, classes=2, requirements=9, questions=54 真题 + 11 材料
exams=2, answers=806, preprocess_status=confirmed
owner_id=NULL（历史共享任务；新导入任务才写 owner）
```

### 4.2 判题

```text
job_22f2c027b2：paused
items：103 completed / 1 failed / 701 pending
runs：104 graded / 2 needs_review / 2 failed（共 108）
DC01 已判，DC02 仍 0 条已判
```

### 4.3 数据问题

```text
547 条：accepted 538（missing_process）/ pending 9
```

### 4.4 账号（启动迁移后）

```text
xueban_users=88
  student=40（password）
  teacher=2（password）
  guest=46（auth_type=guest，v1.5.11 前 role=teacher）
schema_migrations v4 已完成角色收敛；副本验证 46 条审计记录
```

### 4.5 新增数据（真实库当前）

```text
teacher_enrollments：启动迁移 v5 回填 40 条（unverified）
teacher_method_opportunities：0
teacher_mapping_versions：0（映射版本从 0 开始）
teacher_version_compat：0
recommendation_feedback：0
error_label_gold：0
teacher_grade_runs.mapping_version：历史 0 条（列新增）
```

### 4.6 Node 学习中心

```text
活课程 1 门：26 单元 / 53 教案 / 0 知识点 / 0 练习
残留 guest 课程：2 单元 / 1 教案 / 2 知识点 / 4 练习
learning_generation_jobs=0、source_blocks=0、exercise_attempts=0
```

### 4.7 数据库体积

```text
xueban.sqlite ≈ 6.11MB
app.sqlite ≈ 7.18MB（v1.5.10 VACUUM 后）
备份：D:\capital lion\backup\v1.5.10
```

## 5. v1.5.13 已完成（摘要）

- 判题三层结构：source_step（原文+偏移）→ reasoning_step → operation_unit。
- 评分点闭合、证据文本、知识关联来源/确认状态、structure_json/request_context_json 持久化。
- 人工确认/修正语义：部分确认不整题通过，结构 review 阻止整题确认，改分记录 points_synced。
- job processed 计数同步；attempt 记录 grading_version 与 attempt_number。
- 只读全量准入预检：806 作答/105 已判/701 待判/227 直接/472 review/2 阻断；未调用模型。

## 5.1 v1.5.12 已完成（摘要）

- 迁移改为逐版本原子事务；失败记录 `schema_migration_failures` 并终止启动。
- 新增迁移状态接口与只读 CLI；教师业务接口在迁移未完成时 503。
- 正式库只读基线 + 副本迁移/失败注入/重试预演完成，正式库 SHA 未变。
- 权限补漏：图边、版本兼容、全局 legacy 评分点；93 个教师路由未登录全部 401。
- 方法机会映射变更同步；样本/映射/9 条 pending issue 清单已导出。

## 5.1 v1.5.11 已完成（摘要）

- 游客角色显式 `guest`，严格教师/学生白名单；历史 46 个游客账号安全收敛，不升级未知账号。
- 教师端任务/作答/判题/学生/分析资源归属校验；跨教师访问 404；游客/学生/未登录边界 HTTP 测试。
- 趋势版本严格三态：版本不同必须 non_comparable，显式兼容记录才可比；学生群体变化警告；前端只连接可比区间。
- 方法机会独立建模，覆盖率不再恒 100%；缺机会数据返回 null/暂不可计算。
- 任务内学生快照与身份状态，跨任务不自动合并；趋势提示未确认身份。
- 映射拒绝/版本/历史/影响面；判题与快照记录映射版本。
- 错误分类人工标注与指标；推荐反馈闭环；后台任务恢复与并发可靠性。

完整：`V1.5.13_CHANGES.md` / `V1.5.12_CHANGES.md`；验收：`V1.5.13_ACCEPTANCE.md`；准入：`V1.5.13_FULL_GRADING_ADMISSION.md`；迁移：`V1.5.12_MIGRATION_REPORT.md`。

## 6. 最优先待办（v1.5.13 之后）

1. **获得授权后执行正式库 v1–v5 迁移**；之后才能用新结构全量判题。
2. 配置 `DEEPSEEK_API_KEY`，按 `verify-artifacts/v1.5.13/real_grading_sample.csv` 做 10–20 条真实小批判题与人工评价。
3. 处理 `D16-N02-Q044-r1`、`D26-N02-Q034-r1` 两条 score_out_of_range；明确 472 条 review 复核策略。
4. 发放 40 名真实学生初始凭据。
5. 按 `mapping_review_checklist.csv` 逐条确认 90 条 legacy 映射，确认后重算学情。
6. 按 `pending_issues_review.csv` 处置 9 条 pending issue。
7. 决定 N02 趋势策略；产品决策：历史无主任务归属、未绑定学生池是否隔离。

## 7. 交接必须提醒

1. 正式库尚未迁移；不要在未授权时启动连接正式库的后端。
2. 游客没有教师权限；不要用 `role or 'teacher'` 兜底。
3. 新任务按 `owner_id` 隔离；历史 `owner_id=NULL` 是共享兼容，不是常态。
4. 同学生编号跨任务默认 `unverified`，不能宣称同一人。
5. 方法覆盖率缺失时必须返回 null，不能显示 100%。
6. 趋势版本不同即不可比；兼容关系必须有 `teacher_version_compat` 依据。
7. 判题/学情记录 `mapping_version`；历史记录为 NULL 属正常。
8. 错误分类指标以人工 `error_label_gold` 为真值，规则/模型不参与真值。
9. 本环境没有 pytest；Python 测试用 unittest。
10. 判题三层结构存于 structure_json/step_results_json；历史 108 条没有 structure，不能伪造回填。
11. 全量判题前先跑 `scripts/full_grading_preflight.py`；当前 2 条阻断、472 条 review；Key 已配置但未授权调用。
12. 真实模型判题仍未验收；不得声称已全量判题。
