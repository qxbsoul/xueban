# AgentGraph 工作流自动生成规则配置

本目录包含 AgentGraph 工作流自动生成算法的规则配置文件。

## 目录结构

```
rules/
├── complexity-rules.yaml    # 复杂度规则
├── type-rules.yaml          # 类型规则
├── quality-rules.yaml       # 质量规则
├── parallel-rules.yaml      # 并行规则
└── README.md                # 本文件
```

## 规则文件说明

### complexity-rules.yaml（复杂度规则）

基于任务复杂度的工作流生成规则，包括：

- **极低复杂度** (`< 0.2`): 最简工作流，仅包含接收、理解、编码、报告
- **低复杂度** (`0.2 - 0.4`): 简单任务工作流
- **中等复杂度** (`0.4 - 0.6`): 包含规划阶段
- **高复杂度** (`0.6 - 0.8`): 包含设计和分解阶段
- **极高复杂度** (`>= 0.8`): 包含架构分析、原型设计等高级阶段
- **组件复杂度高**: 增加架构分析节点
- **协调复杂度高**: 增加集成测试和协调节点
- **动态复杂度高**: 增加状态管理和异步处理节点

### type-rules.yaml（类型规则）

基于任务类型的工作流生成规则，包括：

- **TDD 编码**: 测试驱动开发模式（测试 -> 编码 -> 验证 -> 循环）
- **标准编码**: 标准编码工作流
- **调试任务**: 诊断 -> 复现 -> 修复 -> 验证
- **测试任务**: 测试设计 -> 测试编写 -> 测试执行 -> 测试报告
- **文档生成**: 研究 -> 内容生成 -> 格式化
- **分析任务**: 深度分析 -> 报告生成
- **审核任务**: 审核 -> 建议生成
- **重构任务**: 代码分析 -> 重构设计 -> 重构执行 -> 回归测试
- **API 开发**: API 设计 -> 实现 -> 测试 -> 文档

### quality-rules.yaml（质量规则）

基于质量要求的工作流生成规则，包括：

- **关键质量** (`critical`): 严格审核 + 安全审计 + 性能测试
- **高质量** (`high`): 审核 + 代码质量检查
- **正常质量** (`normal`): 基础验证 + 语法检查
- **低质量** (`low`): 跳过测试和验证，用于快速原型
- **需要文档**: 添加文档生成节点
- **需要集成测试**: 添加集成测试节点
- **需要安全审计**: 添加安全审计节点
- **需要协商**: 添加协商节点
- **需要代码审核**: 添加审核节点
- **测试覆盖率**: 添加覆盖率检查节点

### parallel-rules.yaml（并行规则）

基于并行执行需求的工作流生成规则，包括：

- **中等规模并行** (2-3 子任务): 简单并行结构
- **大规模并行** (4-10 子任务): 分批执行
- **TDD 并行**: 测试和代码并行生成
- **文档并行**: 并行生成不同部分的文档
- **多平台构建**: 并行为不同平台构建
- **异步并行**: 异步任务执行模式
- **容错并行**: 部分任务失败时仍继续

## 规则格式

每条规则包含以下字段：

```yaml
- id: 规则唯一标识符
  name: 规则名称
  description: 规则描述
  priority: 规则优先级（数值越大越优先）
  category: 规则类别（complexity/type/quality/parallel）
  enabled: 是否启用
  tags: 规则标签列表

  # 触发条件
  condition:
    type: 条件类型（compare/range/in/and/or/not/custom）
    field: 要检查的特征字段
    operator: 操作符（eq/ne/gt/ge/lt/le/contains）
    value: 比较值

  # 执行动作
  action:
    type: 动作类型（add_node/add_nodes/remove_node/modify_node/custom）
    nodes: 节点列表（对于 add_nodes）
    fn: 自定义函数（对于 custom）
```

## 条件类型

### 比较条件

```yaml
condition:
  type: compare
  field: overall_complexity
  operator: gt  # eq/ne/gt/ge/lt/le/contains
  value: 0.7
```

### 范围条件

```yaml
condition:
  type: range
  field: overall_complexity
  min: 0.4
  max: 0.6
```

### 集合条件

```yaml
condition:
  type: in
  field: type
  value: [coding, analysis]
```

### 逻辑组合条件

```yaml
condition:
  type: and
  conditions:
    - type: compare
      field: type
      operator: eq
      value: coding
    - type: compare
      field: flags.needs_tdd
      operator: eq
      value: true
```

### 自定义条件

```yaml
condition:
  type: custom
  fn: |
    function(features) {
      return features.domain_features?.is_api_task === true;
    }
```

## 动作类型

### 添加节点

```yaml
action:
  type: add_nodes
  nodes:
    - id: my_node
      type: code
      position: { x: 250, y: 150 }
      config:
        language: typescript
```

### 自定义动作

```yaml
action:
  type: custom
  fn: |
    function(workflow, features) {
      // 在这里编写自定义逻辑
      const newNode = {
        id: 'custom_node',
        type: 'custom',
        position: { x: 250, y: 300 }
      };
      workflow.nodes.push(newNode);
    }
```

## 特征字段

可以在条件中使用的特征字段：

### 复杂度

- `overall_complexity`: 整体复杂度 (0-1)
- `complexity.component`: 组件复杂度 (0-1)
- `complexity.coordinative`: 协调复杂度 (0-1)
- `complexity.dynamic`: 动态复杂度 (0-1)

### 类型

- `type`: 任务类型 (coding/analysis/documentation/review/testing/debugging)

### 规模

- `scale.estimated_subtasks`: 估算子任务数
- `scale.estimated_duration`: 估算时长（分钟）
- `scale.estimated_tokens`: 估算 token 数
- `scale.estimated_files`: 估算文件数

### 特征标记

- `flags.needs_tdd`: 是否需要 TDD
- `flags.needs_review`: 是否需要审核
- `flags.needs_parallel`: 是否需要并行
- `flags.needs_negotiation`: 是否需要协商
- `flags.needs_design`: 是否需要设计
- `flags.needs_decompose`: 是否需要分解
- `flags.needs_security`: 是否需要安全审计
- `flags.needs_integration`: 是否需要集成
- `flags.needs_doc`: 是否需要文档
- `flags.needs_refactor`: 是否需要重构

### 质量要求

- `quality_requirement`: 质量要求 (low/normal/high/critical)

### 技术栈

- `tech_stack.languages`: 编程语言列表
- `tech_stack.frameworks`: 框架列表
- `tech_stack.libraries`: 库列表

### 域特定特征

- `domain_features.is_api_task`: 是否是 API 任务
- `domain_features.is_ui_task`: 是否是 UI 任务
- `domain_features.is_db_task`: 是否是数据库任务
- `domain_features.is_performance_analysis`: 是否是性能分析

## 规则优先级

规则按优先级从高到低执行。高优先级规则可以覆盖低优先级规则的结果。

推荐的优先级范围：

- 60-100: 关键规则（关键质量、安全）
- 50-59: 高优先级规则（特殊模式如 TDD）
- 40-49: 中优先级规则（类型特定规则）
- 30-39: 正常规则（正常质量要求）
- 20-29: 低优先级规则（复杂度基础规则）
- 10-19: 基础规则

## 冲突处理

当多条规则可能修改同一节点或边时，可以通过以下方式解决：

1. **优先级**: 高优先级规则优先执行
2. **条件精确度**: 更精确的条件优先
3. **自定义逻辑**: 在自定义动作中处理冲突

```yaml
# 配置规则引擎的冲突处理策略
rule_engine:
  conflict_resolution: highest_priority  # 或 first/last/merge
```

## 自定义规则

要添加自定义规则：

1. 在对应的规则文件中添加新规则
2. 确保规则 ID 唯一
3. 设置适当的优先级
4. 测试规则的行为

示例：

```yaml
- id: my_custom_rule
  name: 我的自定义规则
  description: 这是一个自定义规则
  priority: 45
  category: custom
  enabled: true
  tags: [custom]

  condition:
    type: compare
    field: scale.estimated_subtasks
    operator: gt
    value: 10

  action:
    type: custom
    fn: |
      function(workflow, features) {
        // 自定义逻辑
      }
```

## 调试规则

启用详细日志以调试规则行为：

```typescript
import { createTaskAnalyzer, createWorkflowGenerator } from '@agentgraph/generator';

const analyzer = createTaskAnalyzer({ verbose: true });
const generator = createWorkflowGenerator({ validate: true });
```

查看应用了哪些规则：

```typescript
const workflow = await generator.generate(features);
console.log('应用的规则:', workflow.metadata.applied_rules);
```

## 规则验证

规则文件会在加载时进行验证，确保：

- 规则 ID 唯一
- 条件和动作格式正确
- 引用的字段存在

如果发现无效规则，会抛出 `RuleParseError`。
