# Xueban Agent Plugin Contract

本文档以后端源码为准，描述 AgentExecutor、任务/句柄/事件模型、四种接入方式与 CLI provider preset。权威实现位于 `xueban/runtime/contracts.py`、`executors.py`、`events.py` 和 `provider_presets.yaml`。

## 1. 概述

Xueban 用 `(kind, provider)` 二元组定位 AgentExecutor。`kind` 的可选值为 `api`、`cli`、`mcp`、`acp`，`provider` 是具体实现名称，例如 `claude`、`hermes` 或 `generic`。

执行流程为：构造 `AgentTask` → `dispatch()` 返回 `AgentHandle` → `stream_events()` 产生 `AgentEvent` → `capabilities()` 描述实现能力。ExecutorRegistry 负责注册与解析；未注册的 `(kind, provider)` 会抛出 `UnknownExecutorError`，不能静默换成其他 provider。

## 2. AgentExecutor ABC

### 2.1 接口

Pydantic 数据模型从 `xueban.runtime.contracts` 导入，抽象基类从 `xueban.runtime.executors` 导入：

```python
from typing import AsyncIterator

from xueban.runtime.contracts import (
    AgentCapabilities,
    AgentEvent,
    AgentHandle,
    AgentTask,
)
from xueban.runtime.executors import AgentExecutor


class MyExecutor(AgentExecutor):
    kind = "api"
    provider = "my-provider"

    async def dispatch(self, task: AgentTask) -> AgentHandle:
        ...

    def stream_events(self, handle: AgentHandle) -> AsyncIterator[AgentEvent]:
        ...

    def capabilities(self) -> AgentCapabilities:
        ...
```

`dispatch` 启动任务并返回句柄；`stream_events` 按运行进度异步产出事件；`capabilities` 必须反映真实行为，不得宣称未实现的流式、恢复或工具调用能力。

### 2.2 能力字段

`AgentCapabilities` 包含 `streaming`、`approval_required`、`session_resume`、`tool_calls`、`loop_capable` 五个布尔字段，默认均为 `False`。

## 3. 四种 Kind

| Kind | 用途 | 当前实现要点 |
|---|---|---|
| `api` | HTTP 推理或兼容 API 的 provider | 通过 API 配置调用模型服务，密钥按请求/运行配置提供 |
| `cli` | 本地命令行 Agent | 由 YAML preset 驱动，支持参数插值、stdin 和多种输出解析 |
| `mcp` | MCP 工具调用 | 单次工具调用；generic 与 hermes 默认注册，连接结束后不保留会话 |
| `acp` | ACP 会话 Agent | 通过 stdio JSON-RPC 握手并流式接收事件，支持审批与工具调用 |

ACP 当前在事件流结束时停止 transport，因此 `session_resume=False`；不要仅凭协议能力把该字段改为 `True`。

## 4. 三通道契约

### 4.1 AgentTask

核心字段：`task_id`、`run_id`、`node_id`、`agent_id`、`payload`、`metadata`。`payload` 是执行输入；CLI 原始提示可由 `prompt`、`step_input` 和 `context` 编译而成。

### 4.2 AgentHandle

核心字段：`handle_id`、`run_id`、`node_id`、`agent_id`、`status`、`metadata`。`status` 只允许 `pending`、`running`、`done`、`failed`、`degraded`。

实现相关的 stdout、stderr、returncode、parse_format 或传输会话信息放在 `metadata`，对外状态和错误仍需遵守统一模型。

### 4.3 AgentEvent

核心字段：`run_id`、`node_id`、`agent_id`、`type`、`payload`、`ts`。`ts` 是 UTC 时间戳，序列化后属于 wire format 的一部分。

事件中的密钥、token、密码等敏感字段在 SSE 输出前必须脱敏；不要把凭据写入 payload 后再依赖消费者清理。

## 5. YAML 样板

CLI preset 位于 `xueban/runtime/provider_presets.yaml`：

```yaml
my_cli:
  command: ["my-agent", "run"]
  args_template: ["--id", "{id}", "--input", "{stdin}"]
  stdin_format: "json"          # raw | json | none
  parse_format: "stdout-json"  # stdout-json | stdout-text | jsonl-tail | codex-jsonl
  workspace_template: null
  env: {}
```

字段语义：

- `command`：二进制与固定参数，列表形式，不拼接 shell 字符串。
- `args_template`：追加参数；支持 `{id}`、`{run_id}`、`{stdin}` 插值。
- `stdin_format`：`raw` 发送编译后的文本，`json` 发送完整任务 payload，`none` 不写 stdin 但仍允许 `{stdin}` 进入命令参数。
- `parse_format`：声明 stdout / JSONL 的实际解析方式。
- `workspace_template`：需要跟踪工作区 JSONL 时的目录模板，可为空。
- `env`：额外环境变量；不得提交真实密钥。

工作流 Agent 通过 `executor` 声明 `kind` 与 `provider`，必须能在注册表中找到对应实现。

## 6. AgentEvent 命名空间

### 6.1 核心事件

七个核心事件为：

- `agent.dispatched`
- `agent.thinking`
- `agent.tool_called`
- `agent.tool_result`
- `agent.completed`
- `agent.failed`
- `agent.rejected`

另外三个 canonical 事件为 `agent.output`、`agent.degraded`、`agent.approval_requested`。`AgentEventType.ALL` 是包含这十个字符串的 frozenset；代码常量与文档必须同步修改。

### 6.2 扩展事件

外部记忆桥还使用 `agent.memory_drink`、`agent.memory_pour`、`agent.memory_feedback`、`agent.memory_bridge_circuit_break`、`agent.memory_bridge_circuit_recover`。扩展事件不能改变核心事件的既有语义。

### 6.3 SSE Wire Format

`format_sse_event()` 输出 `id`、`event`、`data` 三行。`id` 是 run 内单调序号，`event` 是事件类型，`data` 是 JSON；AgentEvent 的 `run_id`、`node_id`、`agent_id`、`payload` 与 `ts` 都包含在 JSON 中。

RunEventBus 每个 run 保留最近 1000 个事件。客户端可用 `Last-Event-ID` 从指定序号之后继续订阅；代理必须关闭 SSE 缓冲。

## 7. 新 provider preset

1. 在 `provider_presets.yaml` 添加唯一 provider 名及完整字段。
2. 用真实 CLI 验证 `command`、`args_template`、`stdin_format` 与 `parse_format`，不要仅凭帮助文本猜测。
3. 启动时 ExecutorRegistry 自动加载 preset 并注册为 `(cli, provider)`；加载失败必须记录可见警告。
4. 若需直接实例化或覆盖默认注册，使用 `ExecutorRegistry.register_agent()`，再用 `resolve(kind, provider)` 验证。
5. 添加成功、非零退出、输出解析、二进制缺失和敏感信息脱敏测试。

同名覆盖会记录警告。不要把未知 provider 静默映射到默认 Agent。

## 8. 健康检查与降级

CLI 在 dispatch 前检查二进制是否在 PATH。缺失时返回 `status="degraded"` 的 AgentHandle，并产生 `agent.degraded` 事件，metadata 中包含原因与 provider；不得把它报告为成功完成。

非零进程退出对应 `failed` / `agent.failed`。MCP/ACP 的连接、握手、超时与协议错误应保留结构化错误语义。降级或 fallback 只能在明确配置和记录的链路中发生，不能吞掉真实错误。

## 9. 参考

权威源码与测试：

- `xueban/runtime/contracts.py` — AgentTask、AgentHandle、AgentEvent、AgentCapabilities、ProviderPreset。
- `xueban/runtime/executors.py` — AgentExecutor、CLI/ACP/MCP 实现与 ExecutorRegistry。
- `xueban/runtime/events.py` — AgentEventType、RunEventBus、SSE 格式化与脱敏。
- `xueban/runtime/provider_presets.yaml` — 内置 CLI presets。
- `tests/test_agent_executor_contract.py`、`tests/test_agent_event_namespace.py`、`tests/test_sse_endpoint.py`。

历史设计资料名称：`HERMES_INTEGRATION_SPIKE.md`、`HERMES_CLAW_SPIKE.md`、`SHADOWSOUL_RUNTIME_SPIKE.md`。这些历史资料未随纯后端仓库分发；若行为描述与当前源码冲突，以当前源码和测试为准。
