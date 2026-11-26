# 交接文档索引（v1.5.14）

新窗口请按顺序阅读：

1. [START_HERE.md](START_HERE.md) — 启动摘要
2. [CURRENT_STATE.md](CURRENT_STATE.md) — Git、运行状态、真实数据、优先待办
3. [V1.5.14_ACCEPTANCE.md](V1.5.14_ACCEPTANCE.md) — 收尾审查、别名/评分点/预检复核与测试
4. [V1.5.13_CHANGES.md](V1.5.13_CHANGES.md) — 判题结构/评分闭合/证据链
5. [V1.5.13_ACCEPTANCE.md](V1.5.13_ACCEPTANCE.md) — v1.5.13 验收记录
6. [V1.5.13_FULL_GRADING_ADMISSION.md](V1.5.13_FULL_GRADING_ADMISSION.md) — 全量判题准入报告
7. [V1.5.12_MIGRATION_REPORT.md](V1.5.12_MIGRATION_REPORT.md) — 正式库迁移预演
8. [V1.5.12_CHANGES.md](V1.5.12_CHANGES.md) — 上一版迁移安全
9. [V1.5.11_CHANGES.md](V1.5.11_CHANGES.md) — 上一版逐项变更
10. [V1.5.11_ACCEPTANCE.md](V1.5.11_ACCEPTANCE.md) — 上一版验收
11. [V1.5.10_CHANGES.md](V1.5.10_CHANGES.md) — 上一版逐项状态
12. [KNOWN_ISSUES.md](KNOWN_ISSUES.md) — 已知问题、复现条件、影响
13. [DATABASE.md](DATABASE.md) — 表、字段、关系、隔离、迁移
14. [ARCHITECTURE.md](ARCHITECTURE.md) — 系统拓扑、调用链、异步任务
15. [FEATURE_STATUS.md](FEATURE_STATUS.md) — 已完成/部分/未完成/缺陷
16. [DECISIONS.md](DECISIONS.md) — 有效/废弃/未决设计决策
17. [LEGACY_RISK.md](LEGACY_RISK.md) — 旧表/旧模块风险评估（未删除）
18. [TEST_GUIDE.md](TEST_GUIDE.md) — 启动、隔离测试、业务与前端验证
19. [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) — 项目定位、技术栈、目录与启动

## 审计范围与边界

- 依据：当前 Git 工作区代码 + 当前 SQLite 数据库 + 已提交测试。
- v1.5.9 审计：未修改业务代码；v1.5.10 在此基础实施修复。
- 结论时间：v1.5.13；提交见 `git log -1`。

## 最重要的三条结论

1. 权限边界已收紧：游客不再拥有教师权限；新任务按教师归属隔离；历史无主任务仍是共享兼容项。
2. 学情口径已修正：趋势版本不同即不可比；方法覆盖率不再恒 100%；同编号跨任务默认身份未验证。
3. 真实模型判题、真实映射确认、真实错误标注与真实推荐反馈仍未完成；不能把隔离测试当成真实教学验收。
