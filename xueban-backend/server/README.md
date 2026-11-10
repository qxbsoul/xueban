# Xueban Node Gateway

Self-contained Node.js / TypeScript backend for Xueban. It serves the frontend-facing API on port 8002 and proxies unmatched `/api/*` requests to the Python FastAPI backend on port 8000.

The complete product requires both processes; see the repository root README for Python setup and the unified launcher.

## Requirements

- Node.js 22
- npm
- Python backend at `PYTHON_BACKEND_URL` (defaults to `http://127.0.0.1:8000`) for proxied business routes

## Install

```bash
cd server
npm ci
```

PDF export uses `@sparticuz/chromium` and `puppeteer-core`, which make the first dependency download comparatively large. If Chromium cannot launch, PDF export returns `503 PDF_ENGINE_UNAVAILABLE`; Markdown and ZIP export are independent.

## Run

```bash
npm run dev       # tsx watch with auto-reload
npm start         # one-shot tsx process
npx tsc --noEmit  # typecheck
```

From the repository root, `node scripts/dev.mjs --no-open` starts both this gateway and Python.

## Tests

Vitest and all test-only dependencies are declared locally in this package:

```bash
npx vitest run --maxWorkers=2
npm test -- --maxWorkers=2
```

Cross-backend wire and fixture guards:

```bash
npm run test:wire
npm run test:fixtures
npm run test:cli
```

Older self-contained script-style tests remain available separately:

```bash
npm run test:scripts
```

Their results must not be conflated with the Vitest summary.

## Integration

- Frontend clients use `/api/**` on port 8002 only.
- Node-specific routes are mounted in `src/index.ts` before `proxyFallback`.
- `GET /health` is the gateway health check.
- SSE routes require proxy buffering to be disabled.
- The gateway does not currently expose OpenAPI; `src/index.ts` is the route-mount authority.

## Selected endpoints

- `GET /api/skills` — list skills loaded from `.xueban/skills/`.
- `POST /api/runs` — start a RunSession.
- `GET /api/runs/:id/stream` — SSE stream.
- `GET /api/export/:session_id/zip` — export a ZIP project.
- `GET /api/export/:session_id/pdf?filename=*.html&page=A4|Letter&viewport=WxH` — render an HTML artifact to PDF.
- `GET /api/export/:session_id/markdown?filename=*.html` — convert HTML to Markdown.

See `AGENTS.md` and nested directory instructions before editing.
