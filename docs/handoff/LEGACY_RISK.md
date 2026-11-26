# 历史遗留依赖风险评估（D20）

> 结论：v1.5.10 **只做风险梳理，不删除任何旧表/旧模块**。删除需要单独产品决策与数据迁移方案。

## 1. Python 主库旧表

| 表 | 当前用途 | 风险 | 建议 |
| --- | --- | --- | --- |
| `companies/departments/employees/employee_config_versions` | 旧“公司/教研组”工作台 | 账号/权限/种子数据仍被 `xueban_seed`、`ensure_company` 使用 | 保留；先理清工作台是否下线 |
| `projects/products/orders/order_events/ledger/approvals/task_executions` | 旧商品/订单/审批/任务 | 新代码基本不读；表结构占用空间、容易被误当业务能力 | 保留但标注 legacy；如清理需先确认无脚本/报表依赖 |
| `xueban_projects` | 旧项目层 | 与 Node 项目概念冲突 | 保留；文档明确不是当前课程/学情 |
| `teacher_courses/teacher_standards` | Python 兼容表 | 0 行；旧 README 可能误导 | 保留；文档已声明不是当前课程权威 |
| `teacher_topics/teacher_indicators/teacher_questions/teacher_exams` | 全局知识主题/题表 | 无 batch_id，跨任务覆盖 | 保留；任务内查询已改为版本优先 |

## 2. Node 旧模块/依赖

| 模块 | 风险 | 建议 |
| --- | --- | --- |
| `server/src/routes/groups-chat.ts`、`groups-node.ts` | 旧群聊/公司协作；与教师学情无关 | 保留；不要与新 `/api/groups` 语义混用 |
| `company_billing`、`wallet` 路由/表 | 旧计费；与学情无关 | 保留；如产品确认下线再单独迁移 |
| `ethers`、0G 相关依赖 | 0G 方向已取消，依赖仍可能被扫描/审计质疑 | 先确认无引用再移除；不在本次范围 |
| `agents/teams/skills` YAML 加载器 | 旧 Agent 框架；新功能仍复用运行底座 | 保留；改动需回归整个工作台 |

## 3. 未删除理由

1. 旧表/模块可能仍被历史数据、脚本、演示或 Agent 运行底座引用；
2. 删除不可逆，且与本次“数据正确性修复”无直接关系；
3. 赛题交付需要稳定系统，而不是代码洁癖。

## 4. 后续动作（需产品确认）

- 明确“学情产品主线”是否彻底放弃公司/订单/群聊功能；
- 若放弃：先冻结写入 → 导出归档 → 删除路由挂载 → 再评估表删除；
- 任何删除都必须有备份（参考 `D:\capital lion\backup\v1.5.10`）与回滚脚本。
