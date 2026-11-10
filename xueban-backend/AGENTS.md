# 智慧学伴 Backend — Agent Instructions

## Architecture

This is a backend-only repository with two mandatory services:

- `xueban/`: Python FastAPI business backend on port 8000.
- `server/`: Node 22 / TypeScript Express gateway on port 8002. It handles its own routes and reverse-proxies unmatched `/api/*` requests to Python.

The old React/Vite frontend and Tauri shell were deliberately removed. Do not recreate `src/`, `src-tauri/`, or root frontend tooling. The Python backend is not disposable legacy code.

Frontend integration is documented in `新前端对接契约.md`. New clients call the Node gateway only, preferably through a same-origin `/api` proxy.

## Setup and verification

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[server,dev]"  # Windows
# .venv/bin/python -m pip install -e ".[server,dev]"    # macOS/Linux

cd server && npm ci
npx tsc --noEmit
npx vitest run --maxWorkers=2
```

Run Python tests from repository root:

```bash
.venv/Scripts/python -m pytest -m "not requires_api_key" --tb=short
```

Start both backends with `node scripts/dev.mjs --no-open`. Use `--no-python` only for explicitly gateway-scoped debugging.

## Boundaries

- Node HTTP routes belong in `server/src/routes/`; complex logic belongs in `lib/`, `workflow/`, or `storage/`.
- Mount Node routes before `proxyFallback` in `server/src/index.ts`.
- Python domain routers belong in `xueban/api/` and are assembled by `xueban/server.py`.
- Preserve `config/`, `templates/`, `examples/runtime-contract/`, and source-tracked `.xueban` YAML data: backend code reads them.
- Runtime state, credentials, SQLite files, `.env`, and `server/.xueban/` must never be committed.
- Read nested `AGENTS.md` files before editing their directories.

## Contracts

- Update `backend-openapi.json` from the running Python service when its API changes.
- Node currently has no OpenAPI endpoint; `server/src/index.ts` is the route-mount authority.
- Run `npm run test:wire`, `npm run test:fixtures`, and `npm run test:cli` from `server/` after relevant contract changes.
- Section B.7 contains backend-verifiable integration semantics. Do not invent additional product behavior; new product-only rules require project-owner input.

## 0G direction cancelled

0G integration was removed in August 2026. Do not reintroduce ZeroG checkpoint stores, bridge packages, 0G writeback channels, or related upload endpoints. Historical mentions may remain only in changelog/history material.
