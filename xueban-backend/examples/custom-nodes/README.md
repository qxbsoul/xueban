# AgentGraph 自定义节点示例

本目录包含 AgentGraph 的自定义节点示例，展示了如何创建和使用自定义节点。

## 节点列表

### 1. API 调用节点 (api-call)

执行 REST API 调用，支持多种 HTTP 方法和认证方式。

**功能特性：**
- 支持 GET、POST、PUT、PATCH、DELETE 等 HTTP 方法
- Bearer Token、Basic Auth、API Key 认证
- 可配置的重试逻辑和超时设置
- 自动 JSON 响应解析

**文件位置：** `E:\VScode\AgentGraph\examples\custom-nodes\api-call\`

**使用示例：**
```yaml
nodes:
  - id: api_call
    type: api-call
    config:
      url: https://api.example.com/users
      method: GET
      headers:
        Authorization: Bearer your-token
```

**详细文档：** [api-call/README.md](./api-call/README.md)

---

### 2. 数据转换节点 (data-transform)

在 JSON、XML 和 CSV 格式之间转换数据，支持字段映射。

**功能特性：**
- JSON、XML、CSV 格式互转
- 自动格式检测
- 字段重命名和转换
- 数据验证支持

**文件位置：** `E:\VScode\AgentGraph\examples\custom-nodes\data-transform\`

**使用示例：**
```yaml
nodes:
  - id: transform
    type: data-transform
    config:
      input_format: json
      output_format: csv
    inputs:
      input_data:
        - { name: "Alice", age: 30 }
```

**详细文档：** [data-transform/README.md](./data-transform/README.md)

---

### 3. 条件过滤节点 (filter)

基于条件过滤数据，支持复杂表达式。

**功能特性：**
- 14 种比较操作符
- 自定义 JavaScript 表达式
- 多条件 AND/OR 逻辑
- 结果排序和限制

**文件位置：** `E:\VScode\AgentGraph\examples\custom-nodes\filter\`

**使用示例：**
```yaml
nodes:
  - id: filter_active
    type: filter
    config:
      mode: array
      condition:
        field: status
        operator: eq
        value: active
    inputs:
      data:
        - { name: "Alice", status: "active" }
        - { name: "Bob", status: "inactive" }
```

**详细文档：** [filter/README.md](./filter/README.md)

---

### 4. 定时触发节点 (schedule)

基于 Cron 表达式和时间间隔触发工作流。

**功能特性：**
- Cron 表达式支持
- 时间间隔触发
- 工作日/月度计划
- 时区支持和时间窗口

**文件位置：** `E:\VScode\AgentGraph\examples\custom-nodes\schedule\`

**使用示例：**
```yaml
nodes:
  - id: daily_task
    type: schedule
    config:
      mode: daily
      time: "09:00"
      timezone: "America/New_York"
```

**详细文档：** [schedule/README.md](./schedule/README.md)

---

### 5. 邮件通知节点 (email)

通过 SMTP 发送邮件通知。

**功能特性：**
- SMTP 认证支持
- HTML/纯文本邮件
- 模板引擎（Handlebars、Mustache）
- 附件支持

**文件位置：** `E:\VScode\AgentGraph\examples\custom-nodes\email\`

**使用示例：**
```yaml
nodes:
  - id: send_email
    type: email
    config:
      smtp:
        host: smtp.example.com
        port: 587
        auth:
          user: user@example.com
          pass: "${SMTP_PASSWORD}"
      from:
        name: "AgentGraph"
        address: noreply@example.com
    inputs:
      to: ["user@example.com"]
      subject: "Hello"
      body: "Email content"
```

**详细文档：** [email/README.md](./email/README.md)

---

## 如何使用自定义节点

### 方法 1：注册到节点注册表

```typescript
import { NodeRegistry } from 'agentgraph';
import ApiCallExecutor from './api-call/executor';

NodeRegistry.register({
  node: ApiCallExecutor.nodeDefinition,
  executor: ApiCallExecutor
});
```

### 方法 2：通过配置文件

在 `agentgraph.config.ts` 中配置：

```typescript
export default {
  customNodes: [
    './examples/custom-nodes/api-call',
    './examples/custom-nodes/data-transform',
    './examples/custom-nodes/filter',
    './examples/custom-nodes/schedule',
    './examples/custom-nodes/email'
  ]
};
```

### 方法 3：使用 CLI

```bash
# 注册节点
agentgraph node register ./examples/custom-nodes/api-call

# 验证节点
agentgraph node validate api-call

# 测试节点
agentgraph node test api-call --input '{"data": "test"}'
```

---

## 创建自定义节点

每个自定义节点应包含以下文件：

```
my-node/
├── node.yaml      # 节点定义（必需）
├── executor.ts    # 执行器实现（必需）
├── README.md      # 使用文档（推荐）
└── test.ts        # 测试用例（推荐）
```

### 节点定义 (node.yaml)

```yaml
id: "my-node"
type: "custom"
category: "execution"

name:
  en: "My Node"
  zh: "我的节点"

description:
  en: "Node description"
  zh: "节点描述"

icon: "🔧"

inputs:
  - name: "input_data"
    type: "any"
    required: true

outputs:
  - name: "output_data"
    type: "any"

configSchema:
  type: object
  properties:
    param1:
      type: string
```

### 执行器实现 (executor.ts)

```typescript
import { BaseNodeExecutor, NodeContext, NodeResult } from 'agentgraph';

export default class MyNodeExecutor extends BaseNodeExecutor {
  async execute(context: NodeContext): Promise<NodeResult> {
    try {
      const { input_data } = context.inputs;
      const { param1 } = context.config;

      // 处理逻辑
      const result = this.processData(input_data, param1);

      return this.success({
        output_data: result
      });
    } catch (error) {
      return this.failure(error);
    }
  }

  private processData(data: any, param: string): any {
    // 自定义处理逻辑
    return data;
  }
}

export const nodeDefinition = {
  id: 'my-node',
  executor: MyNodeExecutor
};
```

---

## 运行测试

每个节点都包含完整的测试套件：

```bash
# 运行所有节点测试
npm test

# 运行特定节点测试
npm test api-call
npm test data-transform
npm test filter
npm test schedule
npm test email
```

---

## 依赖项

安装所有示例节点的依赖：

```bash
# API 调用节点
npm install axios@^1.6.0

# 数据转换节点
npm install xml2js@^0.6.0 csv-parser@^3.0.0 csv-writer@^1.6.0

# 条件过滤节点
npm install lodash@^4.0.0 jsonpath-plus@^7.0.0

# 定时触发节点
npm install cron-parser@^4.0.0 date-fns@^3.0.0 date-fns-tz@^2.0.0

# 邮件通知节点
npm install nodemailer@^6.9.0 handlebars@^4.7.0 mustache@^4.2.0
```

---

## 贡献指南

欢迎贡献更多自定义节点示例！

1. 遵循现有的文件结构
2. 包含完整的文档（中英文）
3. 编写测试用例
4. 更新本 README.md

---

## 许可证

这些自定义节点示例遵循 AgentGraph 项目的许可证。
