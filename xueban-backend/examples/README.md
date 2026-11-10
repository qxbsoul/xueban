# Examples

当前 `examples/` 目录中同时存在两类内容：

- `runtime-contract/`
  - Phase 1 官方主线示例
  - 与 `docs/CORE_CHARTER.md`、`docs/RUNTIME_CONTRACT_SPEC.md`、`docs/WORKFLOW_SCHEMA.md` 对齐
  - 可直接被 `agentgraph validate` / `agentgraph run` 消费
- 其他历史示例
  - 主要保留为 legacy / 研究 / 旧接口参考
  - 当前不视为 Phase 1 canonical workflow schema 的权威来源
- `backend-poc/`
  - 学伴原型后端（Python/FastAPI，可独立 `run.py` 启动）
  - `xueban/services/xueban_*.py` 即由此移植而来（移植差异见各文件头注释）
- `ui-kits/`
  - 设计稿 HTML（含 `_kit.css`），界面规格的来源参考

## Phase 1 官方示例

- [official-examples.yaml](runtime-contract/official-examples.yaml)
- [docs-gap-review.yaml](runtime-contract/docs-gap-review.yaml)
- [parallel-synthesis.yaml](runtime-contract/parallel-synthesis.yaml)
- [research-review-loop.yaml](runtime-contract/research-review-loop.yaml)
- [simple-assistant.yaml](runtime-contract/simple-assistant.yaml)
- [code-review-phase1.yaml](runtime-contract/code-review-phase1.yaml)
- [research-report-phase1.yaml](runtime-contract/research-report-phase1.yaml)
- [content-creation-phase1.yaml](runtime-contract/content-creation-phase1.yaml)
- [data-processing-phase1.yaml](runtime-contract/data-processing-phase1.yaml)
- [github-monitoring-phase1.yaml](runtime-contract/github-monitoring-phase1.yaml)

`official-examples.yaml` 是当前官方样例注册表；测试矩阵和后续自动化应优先围绕这份清单，而不是手工维护示例列表。

## 快速运行

### 校验 workflow

```bash
agentgraph validate -w examples/runtime-contract/docs-gap-review.yaml
```

### 执行 workflow

```bash
agentgraph run -w examples/runtime-contract/docs-gap-review.yaml -i "{\"goal\":\"Analyze docs gaps\"}"
```

```bash
agentgraph run -w examples/runtime-contract/research-review-loop.yaml -i "{\"goal\":\"Produce a research review\"}"
```

## Canonical 结构

官方示例统一采用以下结构：

```yaml
workflow_id: "workflow-id"
version: "0.1"
name: "Workflow Name"
entrypoint: "start-node"
nodes:
  - id: "start-node"
    kind: "agent"
    type: "planning.analyze"
    config:
      role: "planner"
      prompt: "Analyze the task."
edges:
  - from: "start-node"
    to: "END"
    type: "final"
defaults: {}
metadata: {}
```

详细约束见：

- [Workflow Schema](../docs/WORKFLOW_SCHEMA.md)
- [Runtime Contract Spec](../docs/RUNTIME_CONTRACT_SPEC.md)

## 当前已支持

- 单节点和多节点串行执行
- 条件边
- 基础 parallel fan-out + barrier join
- final edge / `END`
- step / artifact / checkpoint 结构化输出
- CLI / HTTP API 共用同一 contract

## 当前未支持

- 真正循环恢复
- streaming
- workflow registry 引用

## Legacy 说明

以下目录中的内容暂保留，但不作为 Phase 1 主线 contract 的权威来源：

- `multi-agent/`
- `complex-workflows/`
- `integrations/`
- `templates/`
- `custom-nodes/`

如果后续继续使用这些内容，需要先迁移到 `docs/WORKFLOW_SCHEMA.md` 对应结构，再作为官方主线示例。
