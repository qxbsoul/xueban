# Hackathon Demo — Hermes 作为 Team 成员

本目录包含 Xueban Hackathon 演示所需的所有脚本。

## 快速启动（推荐步骤）

```bash
# 终端 1 — 启动 Xueban
docker compose -f docker/docker-compose.yml up

# 终端 2 — 启动 Hermes ACP Adapter（mock 模式，无需安装 Hermes CLI）
python scripts/demo/hermes-adapter.py --mock

# 终端 3（可选）— 启动第二个 Agent 实例
python scripts/demo/hermes-adapter.py --mock \
  --agent-id hermes-v2-remote \
  --display-name "Hermes Agent（远程）"
```

## 一键预热 + 快进版演示

```bash
# 预热：clone 实验 repo，缓存依赖
bash scripts/demo/demo-warmup.sh

# 演示：跳过 clone/install，从已准备好的状态直接开始
bash scripts/demo/demo-run.sh
# 或跳过耗时步骤
bash scripts/demo/demo-run.sh --skip-clone --skip-install
```

Windows（PowerShell 环境无 WSL）：
```powershell
# 用 Python 内置启动
python scripts/demo/hermes-adapter.py --mock
```

## 验证 Hermes 已接入

```bash
# 查看 Agent 注册表
curl http://localhost:8000/api/agents/registry | python -m json.tool

# 查看演示 Team 成员
curl http://localhost:8000/api/teams/demo-team | python -m json.tool

# 测试 WebSocket 端点（需 wscat）
wscat -c ws://localhost:8765/acp
# 应收到 {"type":"capability_request","protocol":"acp-v1"}
```

## 选项说明

| 选项 | 默认值 | 说明 |
|---|---|---|
| `--url` | `ws://localhost:8765/acp` | ACP Server 地址 |
| `--api-key` | `sf_demo_key` | 认证密钥 |
| `--workspace` | `论文实验室` | 工作区 ID |
| `--agent-id` | `hermes-v2-local` | Agent 唯一 ID |
| `--display-name` | `Hermes Agent（代码理解者）` | 显示名称 |
| `--mock` | 自动检测 | 强制使用 mock 模式（推荐演示时使用） |
| `--max-retries` | `3` | 断线最大重连次数 |

## Mock 模式说明

当 Hermes CLI（`hermes` 命令）未安装时，adapter 自动切换到 **mock 模式**：
- 收到任务后，流式输出预设的代码分析日志（模拟 BERT repo 分析过程）
- 输出格式与真实 Hermes 完全一致：`Hermes > <line>`
- 无需网络，无需 GPU，演示时间固定约 3 秒

通过 `--mock` 参数可以强制启用 mock 模式。

## 演示预检清单

演示前请确认：

- [ ] `docker compose -f docker/docker-compose.yml up` 启动后，Node 健康检查 http://localhost:8002/health 与 Python 健康检查 http://localhost:8000/ 均可访问（旧前端已移除）
- [ ] ACP Server 在 ws://localhost:8765/acp 监听
- [ ] hermes-adapter.py 启动后 Agent 列表出现 `Hermes Agent（代码理解者）` 状态 🟢 online
- [ ] Team「论文复现团队」已在 Team Builder 中创建，包含 3 个成员
- [ ] （可选）实验 repo 已 clone 到本地（参见 demo-warmup.sh）

## 架构说明

```
Xueban ACP Server (ws://localhost:8765/acp)
    │
    │  ACP WebSocket (auth → handshake → tasks)
    ▼
hermes-adapter.py
    │
    │  subprocess (hermes run "...") 或 mock 生成器
    ▼
Hermes CLI（或 mock 流式输出）
```

Adapter 实现了完整的 ACP 协议客户端：
1. **Auth**：发送 api_key + workspace_id
2. **Capability Handshake**：响应 capability_request，声明 3 个工具能力
3. **Session Loop**：接收 task 消息 → 执行 → 流式回传 task_stream → task_complete
4. **Heartbeat**：每 30 秒响应一次，汇报活跃任务数
5. **断线重连**：指数退避，最多 3 次
