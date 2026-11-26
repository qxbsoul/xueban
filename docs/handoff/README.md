# 交接文档索引（v1.5.10）

新窗口请按顺序阅读：

1. [START_HERE.md](START_HERE.md) — 启动摘要
2. [CURRENT_STATE.md](CURRENT_STATE.md) — Git、运行状态、真实数据、优先待办
3. [V1.5.10_CHANGES.md](V1.5.10_CHANGES.md) — 本次逐项变更与 D1–D20 状态
4. [V1.5.10_ACCEPTANCE.md](V1.5.10_ACCEPTANCE.md) — 验收报告、测试数字、外部限制
5. [KNOWN_ISSUES.md](KNOWN_ISSUES.md) — 已知问题、复现条件、影响
6. [DATABASE.md](DATABASE.md) — 表、字段、关系、隔离、迁移
7. [ARCHITECTURE.md](ARCHITECTURE.md) — 系统拓扑、调用链、异步任务
8. [FEATURE_STATUS.md](FEATURE_STATUS.md) — 已完成/部分/未完成/缺陷
9. [DECISIONS.md](DECISIONS.md) — 有效/废弃/未决设计决策
10. [LEGACY_RISK.md](LEGACY_RISK.md) — 旧表/旧模块风险评估（未删除）
11. [TEST_GUIDE.md](TEST_GUIDE.md) — 启动、隔离测试、业务与前端验证
12. [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) — 项目定位、技术栈、目录与启动

## 审计范围与边界

- 依据：当前 Git 工作区代码 + 当前 SQLite 数据库 + 已提交测试。
- v1.5.9 审计：未修改业务代码；v1.5.10 在此基础实施修复。
- 结论时间：v1.5.10；提交见 `git log -1`。

## 最重要的三条结论

1. 教师端真实任务仍只判了 103/805，701 条 pending；不能把未判题当 0 分，也没有完成“全量判题验收”。
2. v1.5.10 已修复导入去重、题目版本污染、删除残留、学生等权平均、revisions 串任务、闸门绕过、无 Key 假降级；并有回归测试。
3. 评分点映射 180 条仍是 legacy（未教师确认）；N01 趋势 `unverifiable`、N02 `non_comparable`（题集不同），不能宣称“映射已确认/趋势已可比”。
