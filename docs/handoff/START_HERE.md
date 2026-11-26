# 新 Agent 启动摘要（v1.5.11）

## 你是谁

接手 `D:\capital lion\xueban` 的“智慧学伴”。先读：

1. `CURRENT_STATE.md`（当前真相）
2. `V1.5.11_CHANGES.md`（本次改了什么、每项状态）
3. `V1.5.11_ACCEPTANCE.md`（验收结果与外部限制）
4. `DATABASE.md`、`ARCHITECTURE.md`、`KNOWN_ISSUES.md`
5. `FEATURE_STATUS.md`、`DECISIONS.md`、`TEST_GUIDE.md`

## 系统一句话

React/Vite:3007 → Node Express 网关:8002 → 未匹配 `/api/**` 回退 Python FastAPI（默认 8000，可 8010/8011/8012）。教师判题/学情在 Python；学习课程、聊天、学生记忆在 Node。

## 当前真实状态

```text
main，v1.5.11；工作区含本次全部修改
任务 batch_305316cdd0：40 学生 / 2 班 / 54 真题 + 11 材料 / 806 作答
job_22f2c027b2：paused
  items：103 completed / 1 failed / 701 pending
  grades：104 graded / 2 needs_review / 2 failed
问题 547：538 accepted（无过程）/ 9 pending
题目版本 54（覆盖全部真题）
评分点 180：90 全局 legacy + 90 批次；映射全部 legacy
人工修正 0、评分点确认 0、attempt 0（历史）
Node 活课程：26 单元 / 53 教案 / 0 知识点 / 0 练习；另有已删 guest 课程残留
数据库已备份并 VACUUM：app.sqlite 972.88MB → 7.18MB
```

## 核心原则

- 学案就是课程：权威是 Node `learning_courses`。
- 一份教师上传 = 一个 `teacher_batches` 任务。
- 暂停/继续/失败重试/重判不新建业务任务；job id 不变，单题用 `grading_version`。
- 任务内题目内容/满分/评分点一律读 `question_versions`；缺失才回退并标记 `global_fallback`。
- 未作答 ≠ 错误 ≠ 未评分 ≠ 缺失记录；没有证据不能显示 0 分。
- 评分点是学情证据；知识点与方法分开统计。
- 映射 legay 只是候选，教师确认才算 confirmed。
- 证据不足只能 `insufficient` / `unverifiable` / `non_comparable`，不能强行确定性诊断。
- 只有 `role == "teacher"` 才是教师；游客是 `guest`；同编号跨任务默认身份未验证。
- 趋势版本不同即不可比，除非有显式兼容依据。
- 方法覆盖率缺失时返回 null/暂不可计算，不能显示 100%。
- 质量闸门是服务端硬约束；绕过必须 force + 理由 + 留痕。

## 当前代码的硬事实

- `teacher_answers` 唯一约束是 `(batch_id,event_id,revision)`；806 条仍是旧 ID，但都绑定版本。
- 班级/学生/考试节点/全局题表没有 batch_id。
- 全部任务内查询已按题目版本；`teacher_questions` 只是兼容表。
- 趋势：N01 `unverifiable`，N02 `non_comparable`（题集不同，回填版本也不会变）。
- `manual_confirmed` 只代表整题确认；评分点确认在独立表 + 历史表。
- 无 `DEEPSEEK_API_KEY` 时判题 503，不写成绩；界面会提示未配置。
- Node 测试使用独立临时库；Python 测试用 unittest（本环境无 pytest）。

## 绝对不要做

1. 不要对 701 pending 直接全量调用模型；token 未确认。
2. 不要用 `processed` 判断判完；看 item status。
3. 不要把 `manual_confirmed` 当评分点已确认。
4. 不要把 `teacher_questions` 当跨批次权威。
5. 不要把 65 行题表当 65 道真题。
6. 不要用 N01/N02 当班级/任务组。
7. 没有版本元数据且规则签名不一致时不要声称趋势可比。
8. 不要在真实库上做删除/重判实验；用 `_demo` 副本。
9. 不要删除旧 company/order 表或 Node 旧模块（未决）。

## 第一小时

```powershell
cd D:\capital lion\xueban
git status --short
git log -1 --oneline
npm run dev
```

核心测试见 `TEST_GUIDE.md` 的 22 个 Python 模块；Node `npm test` 自动隔离。

## 下一步优先级

1. 启动 v1.5.11 让正式库执行 v4/v5 迁移，确认 46 个游客降级。
2. 配置判题 Key → 按 `verify-artifacts/v1.5.11` 样本做 10–20 条真实小批判题与人工评价。
3. 向真实学生发放初始凭据。
4. 逐条确认 90 个 legacy 映射并重算学情。
5. 处置 9 条 pending issue；产品决策历史无主任务归属。
