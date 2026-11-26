# 当前状态（v1.5.10）

## 1. Git

```text
仓库：D:\capital lion\xueban
分支：main
基线：85000f5 (v1.5.9)
本次：v1.5.10（见 git log 最新提交）
工作区：包含 v1.5.10 代码、测试与 docs/handoff 更新
```

## 2. 版本

- 产品版本：`v1.5.10`（提交信息）。
- Python 包元数据：0.3.0；Node server / web package：1.0.0。
- 学情算法：`sp-beta-v2`（v1.5.9 历史记录为 `sp-beta-v1`）。
- 判题提示词：`teacher-grade-v3`；`grader_version=v1.5.10`。
- 注意：`teacher_grade_runs` 里已有的 108 条历史 run 仍是旧版本（v2 / v1.4.1），v1.5.10 未重跑真实模型。

## 3. 运行状态（交接时）

```text
真实后端（8000/8002）：未启动
前端 3007：由验收/开发会话启动（Vite）
验收副本：Python 8010 + Node 8002 + 前端 3007（数据来自 _demo 副本）
```

启动：

```powershell
npm run dev
```

## 4. 真实数据库状态（v1.5.9 数据，v1.5.10 未改业务数据）

### 4.1 主任务

```text
batch_305316cdd0
students=40, classes=2, requirements=9, questions=65 行（54 真题 + 11 材料）
exams=2, answers=806, preprocess_status=confirmed
```

### 4.2 判题

```text
job_22f2c027b2：paused，pause_requested=1
items：103 completed / 1 failed / 701 pending
runs：104 graded / 2 needs_review / 2 failed（共 108）
answers：102 graded / 3 needs_review / 701 pending
全部已判作答都在 DC01；DC02 的 374 条仍 pending
```

### 4.3 数据问题

```text
547 条：accepted 538（missing_process）
pending 9：missing_final_answer 5 / score_out_of_range 2 / scoring_points_sum_mismatch 2
```

### 4.4 题目/评分点

```text
teacher_question_versions=54（54 道真题全部覆盖）
scoring_points=180（90 全局 legacy + 90 批次）
scoring_point_knowledge_links=180（全部 legacy，未教师确认）
v1.5.10 新增：确认历史/方法/依赖图/错误标签等新表当前真实数据为 0
```

### 4.5 人工修正/确认/attempt

```text
teacher_corrections=0
teacher_scoring_point_confirmations=0
teacher_scoring_point_confirmation_history=0
teacher_grade_attempts=0（历史未回填；新 job 会写入）
```

### 4.6 Node 学习中心

```text
活课程 1 门（Markdown 导入）
活课程：26 单元 / 53 教案（38 ready / 15 draft）/ 0 知识点 / 0 练习
残留（已删 guest 课程）：2 单元 / 1 教案 / 2 知识点 / 4 练习
learning_generation_jobs=0、source_blocks=0、exercise_attempts=0
learning_textbook_shares=40
```

### 4.7 账号

```text
xueban_users=88（40 学生 + 48 教师，其中 46 个 guest）
xueban_sessions=155（大部分已过期）
v1.5.10 新增 must_change_password 与账号审计；真实 40 名学生尚未发放初始密码
```

### 4.8 数据库体积

```text
v1.5.10 已备份并 VACUUM：
  xueban.sqlite 6.43MB → 6.11MB
  app.sqlite 972.88MB → 7.18MB
备份：D:\capital lion\backup\v1.5.10（含 sha256 记录）
```

## 5. v1.5.10 已完成（摘要）

- D1 导入去重、D3 题目版本权威、D4 删除清理、D5 学生等权、D6 覆盖口径、D17 revisions；
- D2 闸门强制、D8 无 Key 行为与 mock 全链路；
- D7 学生账号、D9 趋势可比性/EWMA、D10 映射审计、D11/D12/D13/D15/D16/D19；
- 新增映射管理、方法维度、依赖图、错误分类、推荐、多班对比、证据下钻；
- 前端教师端/学生端关键页面已真实启动验收并截图。

完整逐项状态：`V1.5.10_CHANGES.md`；验收报告：`V1.5.10_ACCEPTANCE.md`。

## 6. 最优先待办（v1.5.10 之后）

1. **配置判题 Key**，在副本上做真实小批判题并验证 attempt/暂停续跑，再考虑 701 pending。
2. **发放学生初始凭据**，让学生端真实可用。
3. **产品决策：班级/学生/考试节点是否需要 batch 隔离**。
4. 处置 9 条 pending issue；决定趋势版本策略（N02 题集不同，保持 non_comparable）。
5. 评分点映射逐条确认（当前 180 legacy），并在确认后重新生成学情快照。
6. 处理学习中心残留课程数据（需产品确认是否删除）。

## 7. 交接时必须提醒新 Agent 的坑

1. `processed` 现在是 completed+failed，不再等于 item 总数。
2. `manual_confirmed` 不代表评分点确认；逐点确认看 `teacher_scoring_point_confirmations`。
3. `teacher_questions` 是全局兼容表，任务内一律读 `question_versions`，缺失才回退并标记 `global_fallback`。
4. 班级/学生表没有 batch_id。
5. `teacher_courses` 是空遗留表，不是当前课程。
6. N01 是 unverifiable，N02 是 non_comparable，不是“全部 unverifiable”。
7. 65 行题表 = 54 真题 + 11 材料。
8. Node 测试现在隔离运行；若改动 `storage/sqlite.ts`/vitest 配置，先验证真实库行数不变。
9. 本环境没有 pytest；Python 测试用 unittest。
10. 真实模型判题尚未验收，不能声称“全量判题完成”。
