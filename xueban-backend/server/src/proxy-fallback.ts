/**
 * proxy-fallback.ts — Fallback Express middleware that forwards any
 * `/api/*` request not matched by Node's 12 routers to the Python FastAPI
 * backend (default http://localhost:8000).
 *
 * Design (matches OpenDesign single-daemon UX):
 *   Frontend (Vite 3007)
 *     │
 *     ▼
 *   Node API (8002)        ──── /api/run-sessions ─┐
 *     │                    ──── /api/runs         │  handled locally
 *     │                    ──── /api/agents       │  (12 routers)
 *     │                    ──── ...               │
 *     │                                            ┘
 *     │  unmatched /api/* ──► proxyFallback ──► Python (8000)
 *
 * Mount AFTER the 12 owned routers, BEFORE the 404 catch-all:
 *
 *   app.use('/api/run-sessions', runSessionsRouter);
 *   ... // 11 more
 *   app.use('/api', proxyFallback);
 *   app.use((_req, res) => res.status(404).json({ error: 'Not found' }));
 *
 * Behaviour:
 *   - target = process.env.PYTHON_BACKEND_URL ?? 'http://localhost:8000'
 *   - WebSocket-capable (ws: true) for future Python WS endpoints
 *   - SSE-friendly: clears upstream caching headers + no timeout
 *   - Python backend unreachable → 503 + structured JSON with hint,
 *     instead of http-proxy-middleware's default 502 socket-error page.
 */

import { createProxyMiddleware } from 'http-proxy-middleware';
import { PYTHON_BACKEND_URL } from './python-backend';

export const proxyFallback = createProxyMiddleware({
  target: PYTHON_BACKEND_URL,
  changeOrigin: true,
  ws: true,
  // SSE / long-poll friendly: no socket timeout
  proxyTimeout: 0,
  timeout: 0,
  logger: console,
  // Express strips the `/api` mount prefix before handing the request to this
  // middleware (so `req.url` becomes `/wallet/status`). Python's FastAPI routes
  // are defined under the full `/api/...` path, so we re-prepend `/api` here.
  pathRewrite: (path) => `/api${path}`,
  on: {
    proxyReq: (proxyReq, req) => {
      // express.json() consumes the body stream before we reach this middleware.
      // Re-inject the parsed body so Python receives it — including EMPTY objects:
      // an incoming `{}` still carries Content-Length: 2, and http-proxy would
      // otherwise wait forever for a body that was already consumed (observed:
      // POST /api/catalog/apps/{id}/fork with `{}` hung until client timeout).
      // Only re-inject for JSON requests — other content types (e.g. multipart)
      // are not parsed by express.json() and must stream through untouched.
      const body = (req as any).body;
      const hadIncomingBody =
        req.headers['content-length'] !== undefined ||
        req.headers['transfer-encoding'] !== undefined;
      const isJson = String(req.headers['content-type'] ?? '').includes(
        'application/json',
      );
      if (body && typeof body === 'object' && hadIncomingBody && isJson) {
        const raw = JSON.stringify(body);
        proxyReq.setHeader('Content-Type', 'application/json');
        proxyReq.setHeader('Content-Length', Buffer.byteLength(raw));
        proxyReq.write(raw);
        proxyReq.end();
      }
    },
    error: (err, _req, res) => {
      // Python backend down → respond 503 with structured JSON.
      // `res` may be a Node ServerResponse (http) OR an Express Response
      // depending on whether the failure is for HTTP or WS — we only
      // handle the HTTP case here.
      if (!res || !('writeHead' in res) || (res as any).headersSent) {
        return;
      }
      const body = JSON.stringify({
        error: {
          code: 'PYTHON_BACKEND_UNAVAILABLE',
          message: `Python backend ${PYTHON_BACKEND_URL} is not reachable: ${err.message}`,
          hint:
            'Start it via: docker-compose up xueban-api  OR  ' +
            'python -m uvicorn xueban.server:app --port 8000',
        },
      });
      (res as any).writeHead(503, {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body).toString(),
      });
      (res as any).end(body);
    },
    proxyRes: (proxyRes) => {
      // Don't let intermediaries buffer SSE
      const ct = proxyRes.headers['content-type'];
      if (typeof ct === 'string' && ct.includes('text/event-stream')) {
        proxyRes.headers['cache-control'] = 'no-cache';
        proxyRes.headers['x-accel-buffering'] = 'no';
      }
    },
  },
});

export const PROXY_TARGET_FOR_TEST = PYTHON_BACKEND_URL;
