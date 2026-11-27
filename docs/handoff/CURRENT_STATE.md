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
正式库：已迁移（schema_migrations v1–v6，2026-11-26T21:28:10）
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
batch_a76e3dd0aa：valid 584 / abnormal 213 / pending_review 9 / excluded 0（806）
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
