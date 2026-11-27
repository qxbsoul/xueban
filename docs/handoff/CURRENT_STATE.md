# 当前状态（v1.5.16）

## 1. Git

```text
仓库：D:\capital lion\xueban
分支：main
v1.5.15 基线：36d5a87
本次：基于该基线的 P0/P1 修复（未提交，见 git status）
```

## 2. 版本

- 产品版本：`v1.5.16`（验收见 `V1.5.16_ACCEPTANCE.md`）。
- 学情算法：`sp-beta-v3`（`learning_analysis.ALGORITHM_VERSION`）。
- 判题提示词：`teacher-grade-v4`；`grader_version=v1.5.13`（代码常量，未随版本号改名）。
- 历史 108 条判题 run 仍是旧版本（v2/v1.4.1），未重跑。

## 3. 运行状态

```text
正式库：已迁移（schema_migrations v1–v8，2026-11-26T21:28:10）
判题模型 Key：当前 .env 未配置 → 新建判题任务会 503，不会伪造结果
开发栈：npm run dev（前端 3007 / 网关 8002 / Python 8010）
```

## 4. 真实数据库状态（v1.5.16，两批数据）

```text
批次 1：batch_305316cdd0（owner NULL，旧导入）806 作答
        状态：graded 102 / needs_review 3 / pending 701（未做重解析；无可用 raw_response）
        job：job_22f2c027b2 paused
批次 2：batch_a76e3dd0aa（owner teacher_62e4c8ee3b，2026-11-26 21:30 导入）806 作答
        状态：graded 586 / needs_review 9 / failed 211
        job：job_23a2d13b29 partially_failed
teacher_grade_runs：1623（709 条为 raw_response 重解析的新版本，旧版本全部保留）
teacher_grade_attempts：1515（712 成功 / 84 model_response / 127 structure_validation / 592 reparse）
teacher_data_issues：1094（每批 547）
teacher_enrollments：80（每批 40，全部 unverified）
scoring_points：270（90 全局 + 90×2 批次）
scoring_point_knowledge_links：270（全部 legacy，0 confirmed）
teacher_question_versions：119（旧批 54，新批 65）
```

数据质量主状态（`reconciled=true`）：

```text
batch_a76e3dd0aa：有效 775 / 异常 26 / 需人工核实 5（806，reconciled）；待处理问题 9；旧 valid/abnormal 口径已废弃
batch_305316cdd0：valid  97 / abnormal  27 / pending_review 682 / excluded 0（806）
```

## 5. 学情证据链（v1.5.16 已打通）

```text
原始作答 806 → 最新事件 799 → 有判题 799 → 可用于学情 583
→ 有步骤 583 → 评分点链接 811 → 有映射 805 → 证据行 784
有证据学生 40 人；DC01 有效 20/21，DC02 有效 19/19
```

诊断入口：`GET /api/teacher/analysis/evidence-trace?batch_id=...`

## 6. v1.5.16 做了什么（摘要）

- 判题：通信错误分类 + 指数退避重试 + 每次尝试落库；同 job 幂等；租约/心跳回收；失败明细分页与中文错误。
- 解析：允许部分得分（0.5+0.5 → 1 分评分点），禁止超分；空值/类型错误可定位；软告警不再整题复核。
- 历史：709 条 raw_response 重解析生成新版本（583 成功 / 9 复核 / 117 契约违规失败），旧版本保留。
- 数据质量：题目级异常展开到该题全部作答；三种「没有答案」分离；20 类检查补齐；总数对账。
- 学情：未映射评分点不再稀释权重；不可定位步骤不进证据；多任务画像重估不平均；知识点矩阵/错误/趋势/建议接口。
- 教师端：学情分析二级导航 + 统一数据范围选择器 + 单题详情 + 逐评分点确认 + 证据链追溯；删除身份确认 UI。
- 教师端 UI：`teacher.css` 按学生端 token 重写（页头/卡片/表格/按钮/表单/上传/空态/滚动条）；任务列表与汇总的「完成/复核」改为作答级计数（原按判题记录数，718 实为 9）。
- 性能：班级画像 3.4s → 0.7s（ensure_model/机会/区间/判题映射缓存）。

## 6.1 v1.5.16 学情分析专项（本轮）

- 题目表现：应作答/实际/有效/待复核/失败/未判/未作答，按「学生 × 题目」去重；真实数据 2133/755/559/115/81/0/1378，`partition_ok`、`expected_ok` 均为 true。
- 自动分析：判题完成自动生成确定性统计（批次 + 每班），LLM 语义分析单独任务；`teacher_analysis_tasks` 持久化 pending/running/completed/partial/failed，`data_version` 变化使旧任务 stale 并重建。
- 班级分布：每知识点堆叠条形图 + 状态学生名单 + 知识点详情；状态人数之和等于全班人数（`status_total_check`）。
- 班级比较：双条形、pp 差值、按差值排序、优势/共同薄弱/样本不足提示。
- 学生画像：全局数据范围（单次/全部/多次），跨任务只合并通过可比性校验的任务，返回 `excluded_batches`。
- 数据正确性：同题多评分点先按题聚合；修订取当前版本；低置信度错误标签不入确定性统计；错误频次分单次/重复/高频。
- 迁移：v6 新增 `teacher_question_assignments`（真实库回填 288 条）与 `teacher_analysis_tasks`；备份 `D:\capital lion\backup\pre-v6-20261127`。
- 验收：Python 225 tests OK / Node 1132 passed / tsc 0 / build 0 / 浏览器 0 错误；详见 `V1.5.16_ANALYTICS_UPGRADE.md`。

## 6.2 v1.5.17 判题可靠性与学情自动更新（本轮）

- 判题失败：18 类分类；历史 94 条失败全部可读化（SSL 28 / 响应中断 27 / 分数空值 26 / 超时 5 / 断连 3 / 满分不匹配 3 / 契约 1）。
- 根因修复：模型漏顶层 score → 步骤分修复 + 结构重试；声明满分高于原始且给分未超 → 修回原始满分并复核；SQLite `temp_store=MEMORY` 修复分析接口 500。
- 重试：只重试失败项、跳过人工确认、成功后同步 job item（失败数 94→93）、历史 attempt 保留、单题重判幂等。
- 学情更新：`teacher_analysis_events` + 对象级 LLM 任务（学生/班级）；`evidence_version` 去重；旧任务 `superseded` 保留；失败不覆盖旧结果；进程重启回收 running。
- 前端：删除分析状态条/刷新/重新生成；班级卡片选择；学生分组列表→详情（单一选择器）；七个二级页面统一布局。
- 迁移：v7（任务对象列 + 事件表）；备份 `D:\capital lion\backup\pre-v7-20261127`。
- 验收：Python 242 tests OK / Node 1132 / tsc 0 / build 0；场景 A–G 真实数据通过，详见 `V1.5.17_ACCEPTANCE.md`。

## 6.3 v1.5.19 判题任务生命周期（本轮）

- 状态机：preprocessing / queued / running / pausing / paused / retrying / completed / failed；旧 partially_failed 归一为 completed。
- 进度：后端 `job_counts` 单一来源，success/review/failed 互斥，completed=三者之和；`job()` 返回整体进度与重判进度，`counts_consistent`。
- 暂停/继续：服务端状态控制，幂等；queued 可直接 paused，running 走 pausing；继续只接受 paused，重复继续复用。
- Worker：queued/retrying 无心跳时自动补 worker；锁冲突延迟重试；failed grade 的 item 归一为 failed；结束 paused 回归 completed。
- 重判：`retry_total/success/failed/started_at` + item `retry_round`，不新建任务、不改 total、不改已成功记录、跳过人工确认。
- 预处理：上传后自动预处理（同一任务生命周期），状态 mutually exclusive（valid/anomaly/pending_confirmation），总数严格对账；过程要求分级（answer_only/requires_process/process_optional/unknown）。
- 质量闸门：删除阻断与 force 留痕，只保留报告信息。
- 历史结果修复：仅保留后端“重解析”（不调用模型、不覆盖旧记录），教师端入口已移除。
- 迁移：v8；备份 `D:\capital lion\backup\pre-v8-20261127`。
- 验收：Python 247 tests OK / 前端 build 0 / 真实 3 条任务完成“开始→暂停→刷新→继续→完成”，806 条任务计数 658/12/136 一致。

## 6.4 v1.5.20 统计口径与学情持久化（本轮）

- 预处理：卡片只保留原始/有效/异常/需人工核实；异常作答记录可全量下钻（学生/班级/题目/版本/事件+全部问题）；异常记录数与问题记录数分单位展示；一条作答多问题只算一条异常记录。
- 需人工核实：与判题待复核、尚未判题分开；未知题型、空答案、待复核问题才进入；只需答案缺过程不算异常；映射缺失标记为映射问题。
- 班级口径：只有逐生题单/明确班级题单才计算应作答；真实 N01/N02 是题目池，DC01 应作答=无法确定、题目池参考 1107、实际 399、成功330/复核61/失败8/未判0、确认未提交3、无法确定范围54题；不再用 1107-399 推算 708 未交。
- 学生证据：后端分页 20/50/100 + 题目/评分点/错误类型/关键词筛选；删除前端 slice(0,50)。
- 错误分类：按评分点输出，满分=正确，未知=未识别错误；统一六类；低置信度不伪装。
- 分析持久化：`get_deterministic` 读 `teacher_analysis_tasks.result_json`，同版本直接复用；算法版本升到 sp-beta-v4；映射修改只失效使用该评分点的学生/班级；失败重判成功后增量刷新。
- 学案课程：两个独立入口，教材+教案走 `/courses/joint`，Markdown 选择后自动导入；修复 FileList 被清空导致文件列表为空。
- 判题：增加评分点满分表与一次针对性纠错；136 失败分组 0 自动修复 / 135 需重判 / 1 需人工。
- 验收：Python 255 tests OK / tsc 0 / build 0 / 浏览器 0 error 0 溢出；构造 60 条证据验证 >50 分页（真实最多 35 条）。

## 7. 最优先待办

1. 配置 `DEEPSEEK_API_KEY`，对 117 条契约违规样本做小批重判，验证修复后的真实质量。
2. 决定 `batch_305316cdd0`（重复导入）去留：保留为历史基线或删除。
3. 逐条确认 270 条 legacy 映射中的当前批次 90 条，确认后重算学情。
4. 生成 `error_label_gold`，才能报告真实分类指标。
5. 决定知识依赖图是否在导入/解析阶段预构建。

## 8. 交接必须提醒

1. 正式库**已经迁移**，不要再按「未迁移」处理；但生产变更前仍应先备份。
2. 判题模型未配置时创建判题任务返回 503，不写成绩。
3. 历史 709 条已重解析，但**没有重新调用模型**；117 条失败需要真实重判才能变有效。
4. `needs_review` 仍不参与学情证据；9 条复核是「证据无法定位/评分点不闭合」，不要批量改成 graded。
5. 软告警（操作分不一致、未处置评分点）只是告警，不阻塞学情；硬错误仍会 failed。
6. 学生「全部任务」掌握度来自合并证据重估，不是各任务平均。
7. 趋势仍遵循三态：N01 `unverifiable`、N02 `non_comparable`。
8. 本环境没有 pytest；Python 测试用 unittest。
9. 沙箱下 `tempfile.mkdtemp` 目录不可写，跑测试需要可写的 TEMP 或等价垫片。
