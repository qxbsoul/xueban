# 当前状态（v1.5.11）

## 1. Git

```text
仓库：D:\capital lion\xueban
分支：main
v1.5.10 基线：be74df5
本次：v1.5.11（见 git log 最新提交）
工作区：包含 v1.5.11 代码、测试、脚本与 docs/handoff 更新
```

## 2. 版本

- 产品版本：`v1.5.11`。
- 学情算法：`sp-beta-v3`（方法机会分母独立；趋势版本严格比较）。
- 判题提示词：`teacher-grade-v3`；`grader_version=v1.5.11`。
- 历史 108 条判题 run 仍是旧版本（v2/v1.4.1），本次未重跑；旧 run 的 `mapping_version` 为 NULL。

## 3. 运行状态（交接时）

```text
真实后端（8000/8002）：未启动
前端 3007：开发会话可能仍在运行
验收使用隔离副本与临时数据库
```

启动：`npm run dev`。首次启动会自动执行 `schema_migrations` v4/v5。

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

## 5. v1.5.11 已完成（摘要）

- 游客角色显式 `guest`，严格教师/学生白名单；历史 46 个游客账号安全收敛，不升级未知账号。
- 教师端任务/作答/判题/学生/分析资源归属校验；跨教师访问 404；游客/学生/未登录边界 HTTP 测试。
- 趋势版本严格三态：版本不同必须 non_comparable，显式兼容记录才可比；学生群体变化警告；前端只连接可比区间。
- 方法机会独立建模，覆盖率不再恒 100%；缺机会数据返回 null/暂不可计算。
- 任务内学生快照与身份状态，跨任务不自动合并；趋势提示未确认身份。
- 映射拒绝/版本/历史/影响面；判题与快照记录映射版本。
- 错误分类人工标注与指标；推荐反馈闭环；后台任务恢复与并发可靠性。

完整：`V1.5.11_CHANGES.md`；验收：`V1.5.11_ACCEPTANCE.md`。

## 6. 最优先待办（v1.5.11 之后）

1. 配置 `DEEPSEEK_API_KEY`，按 `verify-artifacts/v1.5.11/real_grading_sample.csv` 做 10–20 条真实小批判题与人工评价。
2. 发放 40 名真实学生初始凭据。
3. 真实任务映射逐条确认（当前 0 confirmed / 90 legacy），确认后重算学情。
4. 处置 9 条 pending issue；决定 N02 趋势策略（题集不同，保持 non_comparable）。
5. 在真实库启动一次 v1.5.11，确认 v4/v5 迁移与 46 个游客账号收敛。
6. 产品决策：历史无主任务是否指定归属；班级/考试节点是否进一步 batch 化。

## 7. 交接必须提醒

1. 游客没有教师权限；不要用 `role or 'teacher'` 兜底。
2. 新任务按 `owner_id` 隔离；历史 `owner_id=NULL` 是共享兼容，不是常态。
3. 同学生编号跨任务默认 `unverified`，不能宣称同一人。
4. 方法覆盖率缺失时必须返回 null，不能显示 100%。
5. 趋势版本不同即不可比；兼容关系必须有 `teacher_version_compat` 依据。
6. 判题/学情记录 `mapping_version`；历史记录为 NULL 属正常。
7. 错误分类指标以人工 `error_label_gold` 为真值，规则/模型不参与真值。
8. 本环境没有 pytest；Python 测试用 unittest。
9. 真实模型判题仍未验收；不得声称已全量判题。
