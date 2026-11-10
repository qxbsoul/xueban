# Xueban Backend Tests

`tests/` 包含 Python pytest 套件与少量 TypeScript 结构守卫。Node 的 Vitest 测试位于 `server/src/`，由 `server/` 自己管理运行器和依赖。

## Python

在仓库根目录使用项目虚拟环境：

```bash
.venv/Scripts/python -m pytest -m "not requires_api_key" --tb=short
# macOS / Linux 使用 .venv/bin/python
```

需要真实第三方 API key 的测试通过 marker 排除，不应在普通开发或 CI 中伪造通过结果。

## TypeScript wire / fixture guards

这些文件直接由 `tsx` 执行，不属于 pytest，也不进入 Vitest 汇总：

| 文件 | 守卫内容 |
|---|---|
| `wire-contract.test.ts` | 扫描 `server/src/` 的 `sf.*` 存储键读写，阻止 zombie write / phantom read |
| `cli-spawn-smoke.test.ts` | 对本机已安装 CLI 真执行版本探测；未安装则 skip |
| `sse-parser-fixture.test.ts` | 将真实 CLI 输出 fixture 分块送入 parser，验证事件流连通 |

从 `server/` 运行：

```bash
npm run test:wire
npm run test:cli
npm run test:fixtures
```

原前后端存储键 ALLOWLIST 已完整保存为 `tests/wire-contract-allowlist.original.txt`。旧前端删除后，`wire-contract.test.ts` 只扫描 Node 源码；合法例外必须保留具体原因，不能通过宽泛放行让检查变绿。

## Node

```bash
cd server
npx tsc --noEmit
npx vitest run --maxWorkers=2
```

旧 script-style Node 测试通过 `npm run test:scripts` 单独执行。其通过/失败数量与 Vitest 分开记录。
