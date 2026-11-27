# 已知问题与风险

> v1.5.16：判题重试/幂等/租约、数据质量题目级异常、学情证据链、教师端导航已完成；**真实模型重判仍未执行**（环境无 Key），117 条契约违规样本待重判。

## v1.5.17 判题与学情更新（2026-11-27）

1. **93 条历史失败仍在**：28 SSL、27 响应中断、5 超时、3 断连需要真实网络重试；26 条分数空值可重判修复，但需授权批量调用模型；3 条满分不匹配、1 条契约需模型重出合法结果。
2. **历史 attempt 无堆栈**：旧版本只保存原始文本，`exception_class` 只能对新失败记录。
3. **语义分析按查看生成**：首次打开班级/学生会触发一次真实模型调用（约 20–30s）；无独立队列，进程重启会回收 running 任务并在下次查看时重建。
4. **知识依赖边为空**：真实库没有已确认前置关系，页面只按主题展示知识点，不伪造依赖。
5. **整题人工修正不产生评分点证据**：需逐评分点确认后才进入证据，页面显示证据不足是正确行为。
6. **26 条分数空值修复依赖模型返回完整步骤**：若步骤也缺失，仍会失败并保留原因。

## v1.5.16 学情分析专项（2026-11-27）

1. **逐生题单缺失**：CSV 没有「每个学生应做哪些题」，应作答按「参与节点 ∩ 班级题单」计算；`teacher_question_assignments.student_id` 已预留，需要导入端提供逐生题单才能精确到人。
2. **LLM 语义分析无人工评测**：真实调用可用、引用证据，但没有 gold 集，无法报告准确率；一次调用约 30s，按次计费。
3. **任务执行模型**：LLM 使用进程内线程，进程重启会留下 running 状态，需要手动「重新生成语义分析」；没有独立队列。
4. **确定性批次分析耗时**：约 8–9s（两次班级画像 + 题目统计 + 1.2MB 快照），进度只有 10%/100% 两档。
5. **部分作答数据**：当前 CSV 每生平均约 10 题，导致「未作答」占比高；这是数据事实，页面已标注口径，不应通过降低门槛让数字变好看。
6. **趋势**：真实数据只有同源两批，两个独立可比时间点尚未出现；可比性分支只有单测覆盖。

## v1.5.16 已修复/已实现

| 问题 | 状态 | 证据 |
| --- | --- | --- |
| 709 条「模型已判但证据为空」 | 已修复（重解析） | 709 → 583 graded / 9 review / 117 failed；`test_reparse_history` |
| 部分得分被判契约错误（0.5+0.5） | 已修复 | `_validate_point_link` 允许 ≤ 原始满分；实测 198 条不再报错 |
| 通信错误无重试（IncompleteRead/ReadTimeout/…） | 已修复 | 分类 + 指数退避 + 抖动；`test_grading_reliability` |
| 空值被当成 0 / `float(None)` | 已修复 | `_number` / `_number_or_none`；导入不再 500 |
| 任务卡 running（worker 死亡） | 已修复 | `recover_stale_jobs` + `reclaim_stale_items`；租约/心跳回收 |
| 同一作答重复保存 | 已修复 | 唯一索引 `(answer_id, grading_version)` + job 幂等 |
| 失败明细不可翻页/无中文说明 | 已修复 | `/grade/jobs/{id}/failures` 分页 + 尝试明细 |
| 题目级异常不影响作答状态 | 已修复 | `batch_data_quality` 展开题目级问题；`test_data_quality_model` |
| 「没有答案」三种状态混同 | 已修复 | `explicit_unsubmitted` / `empty_answer_unknown` / `missing_final_answer` |
| 未映射评分点稀释证据权重 | 已修复 | 分母只算有映射的评分点；`test_evidence_chain` |
| 不可定位步骤进入证据 | 已修复 | `source_step_id=None` 的步骤跳过 |
| 多任务画像取平均 | 已修复 | `student_profile_multi` 合并证据重估 |
| 教师端学情只有四个面板 | 已修复 | 二级导航 7 页 + 统一范围选择器 |
| AbortError 污染业务状态 | 已修复 | 每数据源独立控制器，AbortError 不计失败 |
| 班级页 >3s | 已缓解 | 3.4s → 0.7s（模型/机会/区间/判题映射缓存） |

## v1.5.16 未完成 / 残余风险

1. **真实模型重判未执行**：117 条契约违规（声明满分超原始、得分超上限、合计超总分）只标 failed，需要 Key + 额度重判。
2. **真实判题质量无评测**：`error_label_gold=0`，不能报告 precision/recall。
3. **错误分类无异步任务进度**：规则分类同步、幂等，但没有进度/重试面板。
4. **知识依赖图未预构建**：仍按需生成候选边，未在导入/解析阶段统一建图。
5. **教师端 UI 只统一了学情分析**：数据管理/判题中心仍是旧样式。
6. **`batch_305316cdd0` 与 `batch_a76e3dd0aa` 重复**：未删除、未合并，等产品决策。
7. **接口仍偏慢**：班级画像接口 1.2–2.6s（含快照写入与序列化），未做分页/增量。
8. **无并发/压测**：多 worker 只有 4 线程抢同一 item 的单元验证。
9. **`ensure_model` 缓存依赖签名**：若只改题目版本的 `scoring_points` 而不改数量/创建时间，评分点不会重建（正常导入不会出现）。
10. **164 个 pytest 模块仍无法运行**：环境未装 pytest；本次回归用的是 40 个 unittest 模块。

---

> 以下为 v1.5.15 及更早记录（部分结论已被 v1.5.16 取代，见上表）。

> v1.5.15：已定位“709 条判题证据为空”的根因（模型返回 reasoning_steps，验证器只读 step_results），并修复后续解析；历史记录未回填，P1/P2 多项未完成。

## v1.5.15 已修复

| 问题 | 状态 | 证据 |
| --- | --- | --- |
| reasoning_steps 被忽略 | 已修复 | `validate_llm_result` 别名；`test_step_structure` |
| 无过程题无 source | 已修复 | final_answer source；测试 |
| 证据链不可定位 | 审计工具 | `scripts/evidence_chain_audit.py` |

## v1.5.15 未完成

1. 历史 709 条 `needs_review` 未回填/重判，学情仍为空。
2. 数据质量主状态已实现；但 709 条历史证据仍未回填。
3. 教师端二级导航、学生画像、知识点矩阵、趋势、教学建议未实现。
4. AbortController、身份 UI 清理、错误分类进度/重试未完成。
5. 通信错误指数退避未实现。
6. 正式库未迁移，真实模型未调用。

---

> v1.5.14：知识点别名规范化、评分点统一校验复核、预检三态与日志清理已完成；正式库仍未迁移，真实模型判题仍未执行。

## v1.5.14 已修复/已实现

| 问题 | 状态 | 证据 |
| --- | --- | --- |
| `kp:I01` 等合法别名误报 unknown | 已修复 | canonical alias map；`test_scoring_knowledge` 17 项 |
| 合法别名无法匹配确认映射 | 已修复 | 别名规范化后命中 `link1` |
| 模型伪造确认/映射来源 | 已修复 | 强制 pending / mapping_claim_not_found |
| 评分点 max/超上限/不可解析绕过 | 已修复 | `_validate_point_link` 统一校验 |
| 双层评分点重复计分 | 已修复 | 操作层唯一来源 + 冲突 review |
| 预检跨学生误关联 | 已修复 | issue key 含 student_id |
| 误提交测试日志 | 已清理 | 删除 `_nodefinal.*` + `.gitignore` |

## v1.5.14 新增残余风险

1. 正式库 v1–v5 未迁移，46 个游客仍为 teacher。
2. 真实模型判题未执行：Key 已配置但未授权/未消耗额度。
3. 2 条 score_out_of_range 阻断仍未处理。
4. 472 条待复核数据策略未定。
5. 知识点关联仍在 grade JSON，未拆表。
6. 历史 108 条判题无 structure_json，旧 job processed 计数不一致。

---

> v1.5.13：判题三层结构、评分点闭合、知识来源标记、证据链与全量准入预检已实现；真实模型判题仍未执行。

## v1.5.13 已修复/已实现

| 问题 | 状态 | 证据 |
| --- | --- | --- |
| 换行直接当数学步骤 | 已修复 | source_step + reasoning_step + operation_unit；`test_step_structure` |
| 评分点重复/超分/未知评分点 | 已修复 | 评分点闭合校验；未知/超分拒绝 |
| 缺少证据文本/来源 | 已实现 | evidence_text、source_step_id、request_context |
| 知识关联无来源/确认状态 | 已实现 | knowledge_links source_type/confirmation_status；模型推断 pending |
| failed 与 needs_review 混淆 | 已修复 | failed review=0；状态区分 |
| job processed 口径 | 已修复 | sync_job_counts；详情返回 processed_consistent |
| 部分评分点确认误标整题 | 已修复 | fully_confirmed 判断 |
| 结构有问题仍可整题确认 | 已修复 | 409 STRUCTURE_REVIEW_REQUIRED |
| 人工改分不同步评分点 | 已记录 | manual_override.points_synced |

## v1.5.13 新增残余风险

1. **真实模型判题未执行**：Key 已配置但未授权，不能声称真实判题质量。
2. **真实数据三层结构未验证**：历史 108 条无 structure；701 待判尚未跑。
3. **知识点关联存于 grade JSON**：未拆独立表，查询和跨判题聚合需解析 JSON。
4. **2 条 score_out_of_range 阻断**：全量前必须处理。
5. **472 条待复核**：可判但不得直接计入有效学情。
6. **正式库未迁移**：v1.5.12 风险仍在。
7. **真实数据无单行多操作样本**：该场景仅隔离测试覆盖。

---

> 以下为 v1.5.12 及更早记录。


> v1.5.12：迁移改为逐版本原子事务，失败可查询并阻止启动；图边/版本兼容/全局 legacy 评分点权限已补；正式库未迁移，仍是最优先风险。

## v1.5.12 已修复

| 问题 | 状态 | 证据 |
| --- | --- | --- |
| v4 角色修改与审计非原子 | 已修复 | `_apply_migration` 单版本事务；失败注入回滚测试 |
| v5 回填与版本登记非原子 | 已修复 | 同事务；副本 40 条全 unverified |
| 迁移失败仍启动 | 已修复 | 启动钩子 raise；只读库启动实测 `Application startup failed` |
| 失败版本/原因不可查 | 已修复 | `schema_migration_failures` + `/migrations/status` + CLI |
| 迁移未完成仍可访问教师接口 | 已修复 | running/failed → 503 `MIGRATION_NOT_READY` |
| 图边/版本兼容跨教师操作 | 已修复 | batch 归属校验；权限测试 |
| 方法机会变更后残留 | 已修复 | 重算同步；`test_method_coverage` |

## v1.5.12 新增残余风险

1. **正式库尚未迁移**（最高）：46 个游客仍是 teacher，schema_migrations 表不存在；等待负责人确认。
2. **未绑定学生池跨教师可见**：`/student-accounts/unbound` 返回自注册学生邮箱，未按教师隔离。
3. **单版本原子而非整批原子**：v1–v3 成功后 v4 失败会保留 v1–v3；设计为可重试，不是整库回滚。
4. **历史判题/快照无 mapping_version**：影响面以 0 处理。
5. **真实判题/映射/标注/反馈未做**：见验收报告第 9 节。

---

> 以下为 v1.5.11 及更早记录。

## v1.5.11 已修复

| 问题 | 状态 | 证据 |
| --- | --- | --- |
| 游客账号被当作教师（46 个真实账号 role=teacher） | 已修复 | 游客创建 role=guest；迁移 v4；`test_permissions_http` |
| 跨教师任务/作答/判题/学生访问 | 已修复 | owner_id + 资源归属中间件；权限矩阵 6 类越权均 404/403 |
| 趋势“有版本号即可比” | 已修复 | 版本值必须一致或显式兼容；`test_trend_version_gate` |
| 方法覆盖率恒 100% | 已修复 | 独立 `teacher_method_opportunities`；`test_method_coverage` |
| 同编号跨任务被默认视为同一人 | 已缓解 | `teacher_enrollments` 默认 unverified，需教师确认 |
| 映射拒绝/版本/影响面缺失 | 已实现 | mapping_version、history、impact；`test_identity_mapping` |

## v1.5.11 新增残余风险

1. **历史无主任务**：v1.5.9 及以前 `owner_id=NULL` 的任务对所有教师可见（兼容策略），无法证明真实归属；需要产品决策指定 owner 后才可收紧。
2. **历史判题/快照无映射版本**：旧 run 的 `mapping_version` 为 NULL，影响面以版本 0 处理；不能追溯当时使用的映射。
3. **身份确认依赖人工**：默认 unverified；未确认前跨任务趋势只提示、不阻断；真实数据 40 条 enrollment 均未确认。
4. **方法机会依赖题目-方法映射**：真实任务尚未建立方法映射，方法机会为 0，覆盖率显示“暂不可计算”而非 100%。
5. **真实错误分类指标无 gold**：评价接口已就绪，但真实人工标注为 0，不能报告真实 precision/recall。
6. **真实判题未验收**：无 Key，未调用模型；样本与人工评价模板已导出到 `verify-artifacts/v1.5.11`。
7. **游客不能再进入教师端演示**：这是安全取舍；游客仍可使用工作台/学习中心演示空间。
8. **真实库尚未执行迁移**：v4/v5 在副本验证通过，正式库将在下次启动时迁移；迁移前 46 个游客仍是 teacher 角色。

---

> 以下为 v1.5.10 及更早记录。

> 状态分为：已确认 / 待确认 / 历史遗留 / 设计限制。
> 本文件只记录能由当前代码或数据库复核的问题，不记录道听途说。

## P0-1 跨批次导入会错误跳过相同 event_id + revision

**状态：已修复（v1.5.10）**

- `import_rows()` 去重条件已改为 `(import_batch_id,event_id,revision)`；
- 同批重复仍跳过并记录 `duplicate_answer_in_batch`；
- `tests/test_import_scope.py` 覆盖跨任务、重复导入、同批重复、文件内重复阻断。

**现象：**

导入第二份任务，如果新任务里有与第一任务相同的 `event_id` + `revision`，会被 `counts.skipped` 跳过，不会写入 `teacher_answers`。

**复现条件：**

1. 已有任务 A，含 `event_id=D01-N01-Q001-r1`、`revision=1`；
2. 上传任务 B，也含相同 `event_id`/`revision`（不同 batch、不同题目版本）；
3. `import_rows()` 的重复检查返回命中，B 的作答被跳过。

**代码位置：**

- `xueban-backend/xueban/api/xueban_teacher.py` `import_rows()`，约 340 行：

```python
if db.q1("SELECT id FROM teacher_answers WHERE event_id=? AND revision=?", (event, revision)):
    counts["skipped"] += 1
    continue
```

**根本原因：**

数据库唯一约束已迁移为 `UNIQUE(import_batch_id, event_id, revision)`，但导入去重 SQL 仍按全局 `event_id + revision` 查询。

**影响：**

- 跨批次导入丢作答；
- 后续判题/分析缺少该学生该题；
- 与“不同任务严格隔离”的目标直接冲突。

**当前处理：**

- 未修复；
- 新 Agent 修复时应改为 `WHERE import_batch_id=? AND event_id=? AND revision=?`。

## P0-2 全局表仍会被跨批次覆盖

**状态：已缓解（v1.5.10），物理隔离仍未做**

**涉及表：**

```text
teacher_questions
teacher_exams
teacher_classes
teacher_students
```

**现象：**

- `teacher_questions`：`INSERT OR REPLACE`，相同 `question_id` 的新批次会覆盖全局题面/满分/答案/评分点；
- `teacher_exams`：`INSERT OR REPLACE`，相同 N01/N02 会覆盖日期/题目清单；
- `teacher_classes/students`：按 code 全局复用，没有 batch_id。

**代码位置：**

- `import_rows()` 约 320 行（questions）、328 行（exams）、302/312 行（topics/students）；
- `teacher_question_versions` 虽然独立保存，但全局表仍可能被后续批次覆盖。

**影响：**

- 判题已优先版本；v1.5.10 起问题检测、质量闸门、成绩、学生详情、results 也全部按作答绑定的版本取题面/满分/评分点，并在响应标记 `question_source`；
- 但 `teacher_questions` 仍是全局“最新”表，不能作为跨批次权威；
- 班级/学生跨任务仍无法严格隔离；
- 趋势/节点配置仍会跨任务变化。

**当前处理：**

- 版本表已建立；
- 全局表未废弃；
- 未完成全面 batch 化。

## P0-3 质量闸门不是后端强制约束

**状态：已修复（v1.5.10）**

- `batch()` 在服务端调用 quality gate；blocking → 409 `QUALITY_GATE_BLOCKED`；
- `force=true` 必须附理由，写入 `teacher_quality_gate_overrides`；
- `tests/test_quality_gate_enforcement.py` 证明直接调 API 无法无痕绕过。

**现象：**

`quality_gate` API 能返回 blocking，但 `POST /grade/batch` 只检查：

```text
teacher_batches.preprocess_status == confirmed
```

没有调用 `quality_gate`。

**复现：**

直接用 API 对 `preprocess_status=confirmed` 但存在 grading 阻断的 batch 调 `POST /api/teacher/grade/batch`，仍可创建 job。

**代码位置：**

- `batch()`（`xueban_teacher.py` 约 716-745 行）；
- `quality_gate()`（约 1176 行）。

**影响：**

- 前端禁用按钮不是安全边界；
- 质量不合格数据仍可被判题；
- 会产生不可信的学情结果。

**当前处理：**

- 前端已接入质量闸门提示/禁用；
- 后端未强制；
- 未修复。

## P0-4 当前真实任务没有全量判完

**状态：已确认（运行状态）**

**当前任务：** `batch_305316cdd0`
**当前 job：** `job_22f2c027b2`，status=`paused`

```text
job items：103 completed / 1 failed / 701 pending
grade runs：104 graded / 2 needs_review / 2 failed
answers：102 graded / 3 needs_review / 701 pending
```

**影响：**

- 不能声称全量判题完成；
- 不能把 701 条 pending 当作 0 分；
- 班级/学生分析只覆盖已判部分。

**关键代码：**

- `run_job()` 在 `pause_requested` 时置 paused；
- 当前 `pause_requested=1`；
- `worker_id=worker-c027b2`；
- `heartbeat_at=2026-11-25T22:36:05`。

## P0-5 attempt 历史为空

**状态：已确认（历史数据）**

**现象：**

`teacher_grade_attempts` 当前 0 行，但已有 108 条 grade runs。

**原因：**

- 旧判题流程没有写 attempt；
- 新代码 `record_attempt()` 已补上成功/失败记录，但没有回填历史。

**影响：**

- 不能对历史 108 条判题做完整失败阶段审计；
- 只能从 grade.status/needs_review 推断。

**代码位置：**

- `record_attempt()`（`xueban_teacher.py` 约 611 行）；
- `grade_answer()` 成功后调用；
- 当前代码已写 batch/student/duration/retryable。

## P1-1 评分点映射全部是 legacy，未教师确认

**状态：已确认（设计债务）**

**数据：**

```text
scoring_points：180
  - 全局 legacy：90
  - 批次 scoped：90
scoring_point_knowledge_links：180
  - status 全部 legacy
  - source 全部 legacy_indicator
```

**影响：**

- 能生成候选知识点证据；
- 不能声称映射已被教师确认；
- 教师改动评分点后，旧映射不会自动重算。

**代码：**

- `learning_analysis.ensure_model()`；
- `_knowledge_links()`；
- `mapping_audit()` 明确返回 `mapping_status=legacy indicator mappings are not teacher-confirmed`。

## P1-2 真实趋势全部 unverifiable

**状态：已修正（v1.5.10）**

实际状态：

- N01：`unverifiable`（缺 grading_rule_version / scoring_rule_version / mapping_version）；
- N02：`non_comparable`（题集 27 vs 54 不同，回填版本也无法变可比）。

v1.5.10 已实现签名校验与具体原因；不要用“全部 unverifiable”概括真实数据。

**原因：**

`teacher_exams` 的：

```text
grading_rule_version = NULL
scoring_rule_version = NULL
mapping_version = NULL
```

`_class_dimensions()` 要求三个版本均非空才标 `comparable`。

**影响：**

- N01/N02 趋势显示 `unverifiable`；
- 不能据此声称“能力提升/下降”；
- 纵向分析同样 `unverifiable`。

**当前处理：**

- 状态已显式返回，不再伪称可比；
- 历史版本未回填。

## P1-3 `teacher_question_versions` 只有 54，`teacher_questions` 有 65

**状态：已重新解释（v1.5.10）**

65 行里 B01–B08 是教材、L01–L03 是学习卡材料，真题只有 54 道且全部有版本；差集不是“缺版本”，而是材料行。`summary`/`audit` 已分开输出 `questions=54` 与 `materials=11`。

**差集：**

```text
B01..B08, L01..L03
```

这些题目没有作答记录，`ensure_question_versions()` 只遍历“有作答的 question_id”，因此没有版本。

**影响：**

- 这些题若后续被作答，必须在导入时生成版本；
- 现有判题不会涉及它们，但题库/题目详情会出现“有题无版本”。

## P1-4 课程源块追溯为空

**状态：已确认（当前数据）**

- `learning_source_blocks = 0`；
- 当前课程 1 门、54 个 lesson、4 个 exercise；
- 原文在 `learning_materials.text_content`。

**影响：**

- 课程内容可学习；
- 但不能逐段追溯到原文 block；
- 课程质量/引用审计能力有限。

**代码：**

- `learning_source_blocks` 表和 `LearningSourceBlock` 类型存在；
- 当前真实课程没有落 block。

## P1-5 知识点先修关系没有形成可用图谱

**状态：部分实现**

**现状：**

- `learning_knowledge_points.prerequisites_json` 字段存在；
- 课程生成时在 lesson 文本中展示前置知识；
- 没有独立知识依赖 API、图谱 UI、先修校验。

**影响：**

- 不能把“知识依赖”宣传为完整功能；
- 只能作为课程生成/教案展示的辅助信息。

## P1-6 人工修正/确认没有真实历史

**状态：结构已完成，真实数据仍为 0（v1.5.10）**

```text
teacher_corrections=0
teacher_scoring_point_confirmations=0
```

v1.5.10 新增 `teacher_scoring_point_confirmation_history`（确认人/时间/前后值/版本），接口与 UI 已接入并有隔离数据库测试；真实任务尚未产生人工确认。

**影响：**

- 代码/API/UI 已实现；
- 但不能用真实数据证明教师操作闭环；
- 重新判题后的历史保留测试只在隔离数据库验证。

## P1-7 班级/学生全局表与 batch 隔离冲突

**状态：已确认（架构限制）**

**涉及：**

- `teacher_classes`、`teacher_students` 无 batch_id；
- `teacher_answers` 有 `import_batch_id`，但学生/班级实体跨任务共用；
- 同一 code 在不同任务若指向不同实体，会复用旧实体。

**影响：**

- 班级统计可能混入跨任务学生；
- 删除任务不会删除共享班级/学生；
- 学生账号/班级归属是全局的。

**未决：**

- 是否需要每个 batch 独立学生快照；
- 还是同 code 视为同一实体。

## P1-8 `teacher_courses`/`teacher_standards` 是遗留空表

**状态：已确认（历史遗留）**

- `teacher_courses=0`、`teacher_standards=0`；
- 当前课程在 Node `learning_courses`；
- 旧 README/旧代码若引用 `teacher_courses`，会与当前实现不一致。

## P1-9 `teacher_data_issues` 当前有 9 条待处理，538 条 accepted

**状态：已确认（当前数据）**

```text
accepted：538（missing_process）
pending_review：9
  - missing_final_answer：5
  - score_out_of_range：2
  - scoring_points_sum_mismatch：2
```

**影响：**

- 缺失过程被接受后，判题仍可继续，但步骤分析不足；
- 分数越界/评分点不一致仍待人工确认；
- 不能把这些异常当作正常数据。

## P2-1 `teacher_answers` 历史 ID 与新 ID 混合

**状态：设计限制**

- 旧 DAO 数据 ID：`D01-N01-Q002-r1`；
- 新导入 ID：`{batch_id}:{event}-r{revision}`；
- 当前 806 条全部仍是旧格式；
- 唯一约束已是 batch 级，但旧 ID 没有 batch 前缀。

**影响：**

- 代码不依赖 ID 前缀，暂不影响；
- 未来按 ID 正则/人工排查时要注意混合格式。

## P2-2 质量闸门不是全异常矩阵

**状态：部分**

已覆盖：

- preprocess；
- orphan student/question；
- invalid max；
- missing reference/scoring；
- unmapped knowledge；
- duplicate event/revision；
- missing process。

未覆盖/未逐项测试：

- 所有 issue_type 的组合；
- 模板/版本不一致；
- 评分点 ID 与题目版本不一致；
- 跨批次映射串用。

## P2-3 任务/worker 是进程内后台任务，不是可靠队列

**状态：设计限制**

- `run_job()` 用 BackgroundTasks；
- 单进程内并发靠 job 状态条件更新；
- 没有 Redis/独立 worker；
- 服务重启后暂停/运行状态需要人工恢复；
- 没有多 worker 压测。

**影响：**

- 不适合高并发全量批处理；
- 当前 701 pending 不应直接依赖单进程长跑。

## P2-4 README 与项目根路径有历史漂移

**状态：文档问题**

- README 的目录示例写 `D:\capital lion\`，实际 Git 仓库根是 `D:\capital lion\xueban`；
- README 的 Python 端口写 8000，启动器会按情况复用 8010/8011/8012；
- README 仍描述部分旧工作台能力，不能替代本目录审计文档。

**处理建议：**

- 以本目录文档为准；
- 修改 README 前先确认不影响部署脚本。

## v1.5.10 之后仍需关注

1. **班级/学生/考试节点未 batch 化**：需要产品决策，不能直接加列破坏账号绑定。
2. **DeepSeek Key 未配置**：真实模型判题无法验收；mock 全链路已通过，界面会显式提示。
3. **真实学生凭据未发放**：导入返回一次性初始密码，本次未向 40 名真实学生分发。
4. **旧 company/order/node 旧模块**：只做风险梳理，未删除。
5. **分析快照含证据原文**：已限制保留数量，若用于生产需脱敏策略。
6. **pytest 未安装**：Python 测试请用 `unittest`；全量 `discover` 会因 162 个旧测试 `import pytest` 失败，属环境缺失而非本次代码回归。
7. **真实数据没有重新判题**：v1.5.10 未消耗 token 重跑 806 条；历史 108 条 run 仍是旧版本（v2/v1.4.1）。
8. **前端离线字体**：无外网时 Google Fonts 请求失败，不影响功能。
