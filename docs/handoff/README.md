# 交接文档索引（v1.5.11）

新窗口请按顺序阅读：

1. [START_HERE.md](START_HERE.md) — 启动摘要
2. [CURRENT_STATE.md](CURRENT_STATE.md) — Git、运行状态、真实数据、优先待办
3. [V1.5.11_CHANGES.md](V1.5.11_CHANGES.md) — 本次逐项变更
4. [V1.5.11_ACCEPTANCE.md](V1.5.11_ACCEPTANCE.md) — 验收报告、测试数字、外部限制
5. [V1.5.10_CHANGES.md](V1.5.10_CHANGES.md) — 上一版逐项状态
6. [KNOWN_ISSUES.md](KNOWN_ISSUES.md) — 已知问题、复现条件、影响
7. [DATABASE.md](DATABASE.md) — 表、字段、关系、隔离、迁移
8. [ARCHITECTURE.md](ARCHITECTURE.md) — 系统拓扑、调用链、异步任务
9. [FEATURE_STATUS.md](FEATURE_STATUS.md) — 已完成/部分/未完成/缺陷
10. [DECISIONS.md](DECISIONS.md) — 有效/废弃/未决设计决策
11. [LEGACY_RISK.md](LEGACY_RISK.md) — 旧表/旧模块风险评估（未删除）
12. [TEST_GUIDE.md](TEST_GUIDE.md) — 启动、隔离测试、业务与前端验证
13. [PROJECT_OVERVIEW.md](PROJECT_OVERVIEW.md) — 项目定位、技术栈、目录与启动

## 审计范围与边界

- 依据：当前 Git 工作区代码 + 当前 SQLite 数据库 + 已提交测试。
- v1.5.9 审计：未修改业务代码；v1.5.10 在此基础实施修复。
- 结论时间：v1.5.10；提交见 `git log -1`。

## 最重要的三条结论

1. 权限边界已收紧：游客不再拥有教师权限；新任务按教师归属隔离；历史无主任务仍是共享兼容项。
2. 学情口径已修正：趋势版本不同即不可比；方法覆盖率不再恒 100%；同编号跨任务默认身份未验证。
3. 真实模型判题、真实映射确认、真实错误标注与真实推荐反馈仍未完成；不能把隔离测试当成真实教学验收。
