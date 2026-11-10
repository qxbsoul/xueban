# 智慧学伴 Backend

Python FastAPI 业务后端（:8000）+ Node 22 网关（:8002）。网关处理真实模型会话与学伴编排，未匹配的 `/api/**` 反代到 Python。

## 启动与验证

```powershell
cd D:\capital lion\xueban-backend
.\.venv\Scripts\python.exe -m uvicorn xueban.server:app --host 127.0.0.1 --port 8000
```

```powershell
cd D:\capital lion\xueban-backend
.\.venv\Scripts\python.exe -m pytest -m "not requires_api_key" --tb=short

cd server
npm ci
npx tsc --noEmit
npx vitest run --maxWorkers=2
```

## 目录

- `xueban/` — Python 业务后端：账号与教研组、项目、审批、知识库、运行记录
- `server/` — Node 22 网关：真实模型会话、学伴编排、SSE
- `config/`、`templates/`、`examples/` — 声明式配置与示例，源码会读取
- `tests/` — pytest 契约测试

## 边界

- Node 路由放 `server/src/routes/`，挂载顺序在 `proxyFallback` 之前。
- Python 路由放 `xueban/api/`，由 `xueban/server.py` 组装。
- 运行状态（`.xueban/`、`server/.xueban/`）与凭据不入库、不提交。
- 商品与订单模块已下线，不要再引入店铺工具或订单端点。

## How to Plug Your Agent

自定义 Agent 的接入契约见 [`docs/AGENT_PLUGIN_CONTRACT.md`](docs/AGENT_PLUGIN_CONTRACT.md)：
包含 AgentExecutor 接口、任务／句柄／事件模型、CLI / MCP / ACP / HTTP 四种接入方式与 provider preset 字段说明。

Python 侧开发约定见 [`docs/PYTHON_BACKEND_DEVELOPMENT.md`](docs/PYTHON_BACKEND_DEVELOPMENT.md)。
