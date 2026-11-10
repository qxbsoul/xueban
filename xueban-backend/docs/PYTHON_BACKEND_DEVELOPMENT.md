# Xueban Python 后端开发规范

本文档用于指导在剥离后的后端仓库中开发 Python 新功能、修改现有接口及同步契约。环境安装和启动见 [README.md](../README.md)，协作者通用规则见 [CONTRIBUTING.md](../CONTRIBUTING.md)，前端协议见 [新前端对接契约.md](../新前端对接契约.md)。

## 1. 先理解双后端边界

Xueban 当前由两个必须协同工作的后端进程组成：

| 服务 | 源码 | 默认端口 | 职责 |
|---|---|---:|---|
| Node 网关 | `server/` | 8002 | 前端统一入口、RunSession/SSE、项目、设置、技能、CLI/ACP 和反向代理 |
| Python FastAPI | `xueban/` | 8000 | 工作流编排、Agent/Team、策略、记忆、计划任务及其他领域能力 |

请求链路如下：

```text
前端 → Node :8002 的 /api/** → Node 未匹配的 /api/* → Python :8000
```

必须遵守以下边界：

1. 前端只访问 Node 网关，不直接连接 Python 的 8000 端口。
2. Python 新增的对外路由必须以 `/api` 开头，否则不会命中 Node 的 Python `proxyFallback`。
3. 纯 Python `/api/**` 路由不需要在 Node 中重复实现或重复挂载。
4. 新增路由前检查 `server/src/index.ts`，避免与 Node 已处理的同路径路由冲突；Node 匹配到的路由不会再回退到 Python。
5. 旧 `/runs`、`/workflow` 等非 `/api` 路由属于存量契约，不得在没有兼容别名和迁移计划的情况下直接改路径。
6. 修改局部代码前，阅读根目录及目标目录中的 `AGENTS.md`，并优先沿用相邻模块的既有模式。

## 2. 代码应该放在哪里

### 2.1 Python 包内部

| 目录或文件 | 用途 | 开发约定 |
|---|---|---|
| `xueban/server.py` | FastAPI 应用、服务装配、中间件、异常处理和路由注册 | 只做组合与必要兼容，不再把复杂新业务堆进此文件 |
| `xueban/api/` | 按领域拆分的 HTTP 路由、请求校验和响应转换 | 新对外接口优先放在这里，使用 `APIRouter` |
| `xueban/services/` | 可复用领域服务、持久化编排和外部服务适配 | HTTP 层不直接承载复杂业务流程 |
| `xueban/core/` | 工作流图、节点等核心抽象 | 不依赖 FastAPI 或 HTTP 请求对象 |
| `xueban/assembly/` | 工作流编译、激活和装配 | 图结构到可运行对象的逻辑放这里 |
| `xueban/contracts/` | 契约模型及契约校验 | 跨模块共享的模型放这里，路由私有模型可留在 API 模块 |
| `xueban/runtime/` | 执行器、事件、检查点、循环和运行时状态 | 运行时规则不要散落到路由函数中 |
| `xueban/memory/` | 记忆存储与检索 | 新存储后端保持接口一致并补隔离测试 |
| `xueban/llm/` | LLM Provider、调用和协议适配 | 不在业务路由中直接拼接 Provider 细节 |
| `xueban/planner/` | 规划与任务分解 | 与 HTTP 层解耦 |
| `xueban/protocol/` | 跨进程或跨组件协议 | 修改时检查生产者和消费者两侧 |
| `xueban/mcp_server.py`、`mcp_servers/` | MCP 服务及工具集 | 依赖放入对应 optional extra |
| `xueban/cli.py` | 命令行入口 | CLI 与 API 复用服务层，不复制业务实现 |

通用原则：**路由负责 HTTP，服务负责业务，runtime/core 负责执行语义，contracts 负责共享约束。** 不要从底层模块反向导入 `xueban.server`，避免循环依赖。

### 2.2 仓库级目录

| 目录 | 用途 |
|---|---|
| `server/` | 独立 Node/TypeScript 网关及其测试、依赖和 Dockerfile |
| `tests/` | Python 测试以及跨后端 wire、SSE、CLI 契约守卫 |
| `scripts/` | 双后端启动器及保留的运行时、演示工具 |
| `config/` | 后端运行时读取的配置数据，不是可随意删除的示例 |
| `templates/` | 工作流模板和 Agent Pack，可能被运行时读取 |
| `examples/runtime-contract/` | 可执行的运行时契约示例 |
| `examples/backend-poc/` | 独立参考实现，不在主服务启动链路中 |
| `.xueban/` | 跟踪声明式 skills、`*.agent.yaml`、`*.team.yaml`；运行状态被忽略 |
| `docker/` | Python 镜像和双服务 Compose 编排 |
| `docs/` | 可执行契约及本开发规范 |
| `validation/` | 有意保留的验证档案，不是产品源码 |
| `.github/workflows/` | Python、Node、Docker 和密钥扫描 CI |

`.venv/`、`.pytest_cache/`、`xueban.egg-info/`、`_data/`、`xueban-runtime/`、`node_modules/`、`server/node_modules/` 和 `server/.xueban/` 都是本地依赖、缓存或运行状态，不应提交。不要把这些目录当成新功能的源码位置。

## 3. 新增 Python 功能的标准流程

### 第一步：先确认归属和现有能力

1. 明确功能是 HTTP 新接口、已有接口扩展，还是 runtime/core 内部能力。
2. 搜索 `xueban/api/`、`services/` 和 `tests/`，优先扩展现有领域模块，避免创建同义模块。
3. 检查 Node 网关是否已有相同路径或相同职责。
4. 小步修改，不在功能提交中顺便大规模移动、改名或格式化无关文件。

### 第二步：先定义输入、输出和错误契约

在写业务实现前明确：

- 方法、完整路径、路径参数和查询参数。
- 请求字段是否必填、长度和取值范围、默认值。
- 成功响应字段、状态码和分页元数据。
- 404、409、422、503 等可预期失败及稳定错误码。
- 是否影响 SSE、存储格式、前端生成类型或既有客户端。

路由私有模型可以定义在对应 API 模块；多个领域共享的模型放入 `contracts/`，不要为了复用而制造不合理的跨域依赖。

### 第三步：实现领域逻辑

1. 复杂逻辑放入 `services/`、`runtime/` 或对应核心模块。
2. 业务函数尽量接收明确参数并返回明确模型，不依赖 FastAPI 的 `Request`。
3. 外部 HTTP、数据库和文件系统访问应可替换、可测试。
4. `async def` 路由中不要直接执行长时间阻塞的文件、CPU 或同步网络操作；使用真正的异步实现，或将阻塞边界清晰隔离。
5. 新增依赖必须同时更新 `pyproject.toml` 的 core 或合适 extra，并同步重新生成 `uv.lock`；不能只在本机 `pip install`。

### 第四步：新增 APIRouter

新领域参考以下骨架。这里只演示 HTTP 结构，`demo` 不代表应创建的正式业务模块：

```python
# xueban/api/<domain>.py
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

router = APIRouter(prefix="/api/<domain>", tags=["<domain>"])


class CreateItemRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ItemRecord(BaseModel):
    id: str
    name: str


class ItemEnvelope(BaseModel):
    data: ItemRecord
    meta: dict[str, Any] = Field(default_factory=dict)


@router.post(
    "/items",
    response_model=ItemEnvelope,
    status_code=201,
    operation_id="create_domain_item",
)
async def create_item(body: CreateItemRequest) -> ItemEnvelope:
    # 正式实现调用服务层；不要在路由里复制持久化和业务流程。
    record = ItemRecord(id="example-only", name=body.name)
    return ItemEnvelope(data=record)
```

要求：

- 使用明确的 Pydantic 请求模型和 `response_model`。
- 新接口显式设置稳定、全局唯一的 `operation_id`，不要依赖函数名隐式生成后再随意改名。
- 路径静态段优先于 `/{id}` 等动态段；注册顺序也要检查，避免被 catch-all 吞掉。
- 路由只做参数转换、调用服务和响应封装，不在模块 import 时启动任务或连接外部服务。

### 第五步：在应用中注册

在 `xueban/server.py` 导入并注册：

```python
from xueban.api import <domain> as _domain_api

app.include_router(_domain_api.router)
```

注册不是机械追加到文件末尾。必须检查同前缀路由顺序，例如更具体的 registry 路由必须先于 agents 的 `/{agent_id}` 动态路由。注册后同时验证 Python 直连和经 Node :8002 的实际 HTTP 路径。

### 第六步：先写契约测试，再补边界测试

至少覆盖：

- 一个成功路径，断言实际状态码和完整关键响应形状。
- 输入校验失败，包含必填、边界值或非法枚举。
- 资源不存在、状态冲突和外部依赖不可用等适用分支。
- 路由顺序，确保新路径没有被动态路由截获。
- 文件或数据库功能使用隔离的临时状态，不读写开发者真实数据。
- 若前端经网关访问，增加或更新跨后端契约测试。

不要只测服务函数而完全跳过 HTTP 层，也不要通过删除断言、放宽异常匹配或跳过新测试来让套件变绿。

### 第七步：更新 OpenAPI、对接文档和变更记录

路由、字段、状态码或错误语义变化后：

1. 从真实运行的 Python 服务更新 `backend-openapi.json`。
2. 更新 `新前端对接契约.md` 中受影响的说明；B.7 只记录能由路由、测试或 OpenAPI 验证的语义。
3. 更新相关 README、示例和 `CHANGELOG.md`。
4. 破坏性变更必须同时给出迁移说明和兼容策略。

## 4. HTTP 接口规范

### 4.1 路径、方法和状态码

- 新对外路径统一使用 `/api/<domain>`，资源名使用复数或沿用同域现有命名，不在同一领域混用风格。
- 查询参数用 Pydantic/FastAPI 类型和 `Query` 约束限定范围，列表接口要有明确且稳定的排序。
- 创建成功通常用 201；删除成功且无响应体用 204；普通查询和更新用 200。
- 400 用于格式错误的标识或请求；401 用于认证/密钥问题；403 用于权限或策略拒绝。
- 404 用于资源不存在；409 用于当前状态不允许操作；422 用于语义或模型校验失败。
- 429 用于限流；503 用于必要依赖不可用；500 只用于未预期服务端错误。
- 不把所有失败都转换成 200，也不把可预期的客户端错误直接抛成 500。

### 4.2 成功响应

新领域默认可采用：

```json
{
  "data": {},
  "meta": {}
}
```

列表的 `meta` 可携带 `count`、`total` 等真实存在的元数据，不填写无法保证的字段。已有接口存在多种响应形状，修改时以同域兼容为先，不能为了“统一”而顺手破坏旧客户端。

### 4.3 错误响应

目标逻辑错误对象为：

```json
{
  "error": {
    "code": "STABLE_ERROR_CODE",
    "message": "Safe, actionable message",
    "trace_id": "optional-trace-id"
  }
}
```

注意：使用 `HTTPException(detail={"error": ...})` 时，FastAPI 默认实际 HTTP 响应外层还包含 `detail`。必须根据 [新前端对接契约.md](../新前端对接契约.md) B.5 和现有客户端确定最终形状，并通过真实响应测试验证，不能只检查 Python 异常对象。存量接口形状不一致时，不做未经验证的全局改写。

错误码使用稳定的大写下划线字符串；用户提示可以改进，错误码不能在无迁移计划时随意改名。500 响应不得泄露异常堆栈、密钥、绝对路径或 SQL，详细异常写日志，并尽量返回可关联日志的 `trace_id`。

## 5. 修改现有接口：先判断兼容性

| 变更 | 通常判断 | 要求 |
|---|---|---|
| 新增可选请求字段，有安全默认值 | 可能兼容 | 验证旧请求结果不变 |
| 新增响应字段 | 可能兼容 | 确认严格客户端和 `response_model` 行为 |
| 改字段名、删除字段、改变字段类型 | 破坏性 | 保留旧字段/旧版本或制定迁移方案 |
| 把可选字段改为必填 | 破坏性 | 不直接发布为兼容更新 |
| 改路径、方法、成功状态码 | 破坏性 | 保留旧路由别名并补契约测试 |
| 改错误码或错误信封 | 可能破坏性 | 检查前端分支及 wire 测试 |
| 改 SSE 帧名、事件顺序或终止语义 | 破坏性 | 同时检查生产者、消费者和重连测试 |
| 改存储键或持久化结构 | 可能破坏性 | 提供读取兼容或数据迁移，不只改新写入 |

修改流程：

1. 先为现有行为补一个回归测试，明确什么不能变。
2. 实现最小改动，保留旧调用方式；必要时新增 `/v2` 或新字段，而非原地替换。
3. 同时测试旧请求和新请求，不只测试新前端需要的路径。
4. 更新 OpenAPI 和对接契约，并在提交说明中标注破坏性变化及迁移步骤。
5. 如果改变 `response_model`，检查它是否会过滤或转换旧字段，避免“函数返回了字段但 HTTP 响应没有”。

## 6. 文件、状态和安全规范

1. 声明式源数据与运行状态分开：`.xueban/skills/`、`.xueban/agents/*.agent.yaml` 和 `.xueban/teams/*.team.yaml` 可跟踪；SQLite、sessions、projects、settings 和凭证不得提交。
2. 文件读写显式使用 UTF-8；时间统一使用带时区的 UTC，并在序列化时保持明确格式。
3. 用户提供的 ID、文件名和相对路径必须校验。路径拼接后使用 `resolve()`，并确认结果仍位于允许根目录内，防止路径穿越。
4. 重要 JSON 写入采用同目录临时文件、flush/fsync 和 `os.replace()` 原子替换；失败时清理临时文件。
5. 测试使用 `tmp_path`、`monkeypatch` 或临时数据库隔离模块级路径和环境变量，禁止写入开发者真实 `.xueban/`。
6. 不在日志、异常响应或测试中输出真实 API key、token、Cookie 和完整敏感配置。
7. 新增环境变量时更新 `.env.example`，只放安全占位值，并说明默认值及必填条件。
8. 密钥按现有 BYOK 约定传递，不另建明文持久化机制。

## 7. 测试与质量检查

以下命令默认在仓库根目录执行；macOS/Linux 将 `.venv/Scripts/python` 换成 `.venv/bin/python`。

### 7.1 定向测试

```bash
.venv/Scripts/python -m pytest tests/test_<domain>_api.py -q
.venv/Scripts/python -m pytest tests/test_openapi_contract.py -q
```

测试文件优先放在与被测模块相邻的现有分组中；没有对应分组时使用 `tests/test_<domain>_api.py`。服务逻辑和 HTTP 契约分别测试，但一次接口改动至少要有 HTTP 层覆盖。

### 7.2 格式和静态检查

只对本次修改的文件运行，避免制造大量无关 diff：

```bash
.venv/Scripts/python -m black xueban/api/<domain>.py tests/test_<domain>_api.py
.venv/Scripts/python -m ruff check xueban/api/<domain>.py tests/test_<domain>_api.py
```

项目行宽为 100。当前 Ruff 基线主要检查 F 类规则，不代表可以忽略类型、资源释放和并发问题。

### 7.3 Python 全量回归

```bash
.venv/Scripts/python -m pytest -m "not requires_api_key" --tb=short
```

真实 Provider 密钥测试必须标记为 `requires_api_key`，不得成为默认本地或 CI 测试的隐式依赖。

### 7.4 涉及网关或跨端契约时

```bash
cd server
npx tsc --noEmit
npx vitest run --maxWorkers=2
npm run test:scripts
npm run test:wire
npm run test:fixtures
npm run test:cli
```

Vitest 与旧 script-style 测试是不同套件，结果分别记录。纯 Python 内部修改可以不跑全部 Node 套件，但只要路径、响应、SSE、存储键或网关行为变化，就必须检查跨端影响。

## 8. OpenAPI 快照更新规范

`backend-openapi.json` 必须由应用真实生成，不能手工编造路径或字段。

1. 在一个终端启动 Python：

   ```bash
   .venv/Scripts/python -m uvicorn xueban.server:app --host 127.0.0.1 --port 8000
   ```

2. 在另一个终端从运行中的服务保存 schema：

   ```bash
   .venv/Scripts/python -c "import json, urllib.request; from pathlib import Path; schema=json.load(urllib.request.urlopen('http://127.0.0.1:8000/openapi.json')); Path('backend-openapi.json').write_text(json.dumps(schema, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')"
   ```

3. 验证唯一 Operation ID 和契约：

   ```bash
   .venv/Scripts/python -m pytest tests/test_openapi_contract.py -q
   ```

4. 检查 diff 只包含预期路径、模型和描述变化，再同步前端类型与对接文档。不要提交来自其他项目、旧进程或错误端口的 schema。
5. 验证结束后正常停止临时启动的服务，不遗留占用 8000/8002 的进程。

## 9. 提交前检查清单

### 代码与契约

- [ ] 新对外 Python 路由带 `/api` 前缀，且经 Node :8002 能访问。
- [ ] 路由注册顺序正确，没有被同前缀动态路由吞掉。
- [ ] 请求、响应、错误和状态码明确，`operation_id` 稳定且唯一。
- [ ] 复杂业务不在 `server.py` 或路由函数中膨胀。
- [ ] 旧请求、旧响应和旧存储读取保持兼容，或已有明确迁移方案。
- [ ] OpenAPI 快照、前端契约、示例和 CHANGELOG 已按需同步。

### 测试与安全

- [ ] 定向测试通过，相关全量回归通过。
- [ ] 涉及跨端时已运行对应 Node、wire、fixture 或 CLI 测试。
- [ ] 测试没有读写真实运行数据，没有依赖本机隐藏状态。
- [ ] 新依赖已写入 `pyproject.toml` 并更新锁文件。
- [ ] 没有提交密钥、`.env`、SQLite、会话、缓存、虚拟环境或临时日志。
- [ ] `git diff --check` 通过，最终 diff 只包含本任务相关修改。

提交信息使用 Conventional Commits，例如：

```text
feat(api): add domain item creation
fix(runtime): preserve checkpoint compatibility
docs: update frontend integration contract
```

文档变更不必伪装成已运行全量测试；提交说明和交付记录只写实际执行并通过的检查。

## 10. 推荐的最小提交单元

一个完整的 Python 接口功能通常包含：

```text
xueban/api/<domain>.py              HTTP 路由与路由私有模型
xueban/services/<domain>_service.py 可选：领域逻辑与持久化
tests/test_<domain>_api.py              HTTP 契约及边界测试
tests/test_<domain>_service.py          可选：领域逻辑单元测试
backend-openapi.json                    真实应用生成的 schema
新前端对接契约.md                       受影响时更新
CHANGELOG.md                            用户可见变化时更新
```

不是所有功能都需要同样数量的文件；判断标准是职责清晰、契约完整、测试可复现，而不是为了套结构创建空层。
