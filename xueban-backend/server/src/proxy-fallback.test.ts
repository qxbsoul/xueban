/**
 * proxy-fallback.test.ts — Real-server integration test for the Python
 * fallback middleware.
 *
 * Run from server/:  npx tsx src/proxy-fallback.test.ts
 *
 * We spin up:
 *   - an ephemeral Express server that mimics the real index.ts mounting
 *     order (a fake `/api/run-sessions` "Node-owned" router, then the
 *     proxy fallback at `/api`, then a 404 catch-all)
 *   - an ephemeral plain-Node HTTP "mock Python backend" on another port
 *
 * Then we exercise:
 *   1. Node-owned route is NOT proxied (returns the local body)
 *   2. Unmatched /api/* IS proxied to mock Python (returns mock body)
 *   3. Mock Python down → response is 503 + structured JSON with
 *      PYTHON_BACKEND_UNAVAILABLE code and friendly hint (not the
 *      http-proxy-middleware default 502 page)
 *   4. SSE responses get cache-control: no-cache + x-accel-buffering: no
 *      injected by the proxyRes hook
 *   5. POST with EMPTY JSON object `{}` is proxied (regression: express.json()
 *      consumed the body, proxy used to hang waiting for Content-Length bytes)
 *   6. POST with non-empty JSON body is proxied with body intact
 *
 * Cardinality of passes is asserted at the bottom — process exits 1 on any
 * fail so this can be wired into a `npm test` script later.
 */

import express from 'express';
import http from 'node:http';
import type { AddressInfo } from 'node:net';

let passCount = 0;
let failCount = 0;

function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) {
    passCount++;
    console.log(`  PASS  ${label}`);
  } else {
    failCount++;
    console.log(`  FAIL  ${label}`);
    if (detail !== undefined) console.log('        detail:', detail);
  }
}

interface FetchResult {
  status: number;
  headers: Record<string, string | string[] | undefined>;
  body: string;
}

function rawRequest(
  port: number,
  path: string,
  method = 'GET',
  body?: string,
): Promise<FetchResult> {
  return new Promise((resolve, reject) => {
    const headers: Record<string, string> = {};
    if (body !== undefined) {
      headers['Content-Type'] = 'application/json';
      headers['Content-Length'] = String(Buffer.byteLength(body));
    }
    const req = http.request(
      { host: '127.0.0.1', port, path, method, headers },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c) => chunks.push(c));
        res.on('end', () => {
          resolve({
            status: res.statusCode ?? 0,
            headers: res.headers,
            body: Buffer.concat(chunks).toString('utf8'),
          });
        });
      },
    );
    req.on('error', reject);
    if (body !== undefined) req.write(body);
    req.end();
  });
}

function listenEphemeral(app: http.RequestListener): Promise<http.Server> {
  return new Promise((resolve, reject) => {
    const srv = http.createServer(app);
    srv.on('error', reject);
    srv.listen(0, '127.0.0.1', () => resolve(srv));
  });
}

async function main(): Promise<void> {
  // ── 1. Start mock Python backend first so we know its port ──────────────────
  const pythonMock = await listenEphemeral((req, res) => {
    if (req.url === '/api/sse-demo') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream' });
      res.write('data: hello\n\n');
      res.end();
      return;
    }
    // Read the full request body before responding — a proxy that fails to
    // forward body bytes will hang here (which is exactly what case 5 guards).
    const chunks: Buffer[] = [];
    req.on('data', (c) => chunks.push(c));
    req.on('end', () => {
      res.writeHead(200, { 'Content-Type': 'application/json' });
      res.end(
        JSON.stringify({
          source: 'mock-python',
          method: req.method,
          path: req.url,
          receivedBody: Buffer.concat(chunks).toString('utf8'),
        }),
      );
    });
  });
  const pythonPort = (pythonMock.address() as AddressInfo).port;
  console.log(`mock-python listening on :${pythonPort}`);

  // ── 2. Import proxyFallback with PYTHON_BACKEND_URL pointing at our mock ────
  // We must set the env BEFORE importing the module because it captures the
  // value at module-load time. Using dynamic import to ensure that order.
  process.env.PYTHON_BACKEND_URL = `http://127.0.0.1:${pythonPort}`;
  const { proxyFallback } = await import('./proxy-fallback');

  // ── 3. Build an Express app that mirrors index.ts mounting order ───────────
  const app = express();

  // index.ts mounts express.json() globally BEFORE all routers — replicate it,
  // because the body-consumption interplay with the proxy is what case 5/6 test.
  app.use(express.json({ limit: '1mb' }));

  // Fake "Node-owned" router (stands in for the real 12 routers)
  const nodeOwned = express.Router();
  nodeOwned.get('/', (_req, res) => {
    res.json({ source: 'node-owned', route: 'run-sessions' });
  });
  app.use('/api/run-sessions', nodeOwned);

  // Fallback to Python — must come AFTER node-owned, BEFORE 404
  app.use('/api', proxyFallback);

  // 404 catch-all
  app.use((_req, res) => res.status(404).json({ error: 'Not found' }));

  const proxySrv = await listenEphemeral(app);
  const proxyPort = (proxySrv.address() as AddressInfo).port;
  console.log(`proxy-host listening on :${proxyPort}`);

  // ── 4. Case 1: Node-owned route is NOT proxied ──────────────────────────────
  console.log('\n── case 1: Node-owned route is not proxied ──');
  {
    const r = await rawRequest(proxyPort, '/api/run-sessions');
    check('status 200', r.status === 200, r.status);
    let parsed: any = null;
    try {
      parsed = JSON.parse(r.body);
    } catch {}
    check(
      'body.source === "node-owned"',
      parsed && parsed.source === 'node-owned',
      r.body,
    );
  }

  // ── 5. Case 2: Unmatched /api/* IS proxied to mock Python ──────────────────
  console.log('\n── case 2: unmatched /api/* proxies to Python ──');
  {
    const r = await rawRequest(proxyPort, '/api/wallet/status');
    check('status 200', r.status === 200, r.status);
    let parsed: any = null;
    try {
      parsed = JSON.parse(r.body);
    } catch {}
    check(
      'body.source === "mock-python"',
      parsed && parsed.source === 'mock-python',
      r.body,
    );
    check(
      'path preserved (/api/wallet/status)',
      parsed && parsed.path === '/api/wallet/status',
      r.body,
    );
  }

  // ── 6. Case 4: SSE headers preserved + buffering hints injected ─────────────
  //  (done before case 3 so the mock is still alive)
  console.log('\n── case 4: SSE buffering hints injected ──');
  {
    const r = await rawRequest(proxyPort, '/api/sse-demo');
    check('status 200', r.status === 200, r.status);
    check(
      'content-type is text/event-stream',
      String(r.headers['content-type'] ?? '').includes('text/event-stream'),
      r.headers['content-type'],
    );
    check(
      'cache-control: no-cache',
      String(r.headers['cache-control'] ?? '').includes('no-cache'),
      r.headers['cache-control'],
    );
    check(
      'x-accel-buffering: no',
      r.headers['x-accel-buffering'] === 'no',
      r.headers['x-accel-buffering'],
    );
  }

  // ── 6b. Case 5/6: POST with JSON body proxied (empty + non-empty) ──────────
  console.log('\n── case 5: POST {} (empty JSON body) is proxied ──');
  {
    const r = await rawRequest(proxyPort, '/api/catalog/apps/x/fork', 'POST', '{}');
    check('status 200', r.status === 200, r.status);
    let parsed: any = null;
    try {
      parsed = JSON.parse(r.body);
    } catch {}
    check(
      'body.source === "mock-python"',
      parsed && parsed.source === 'mock-python',
      r.body,
    );
    check(
      'python received the {} body',
      parsed && parsed.receivedBody === '{}',
      parsed?.receivedBody,
    );
  }
  console.log('\n── case 6: POST non-empty JSON body is proxied intact ──');
  {
    const r = await rawRequest(
      proxyPort,
      '/api/groups/g1/messages',
      'POST',
      '{"content":"hi"}',
    );
    check('status 200', r.status === 200, r.status);
    let parsed: any = null;
    try {
      parsed = JSON.parse(r.body);
    } catch {}
    check(
      'python received the body verbatim',
      parsed && parsed.receivedBody === '{"content":"hi"}',
      parsed?.receivedBody,
    );
  }

  // ── 7. Case 3: Python down → 503 + structured JSON ──────────────────────────
  console.log('\n── case 3: Python down → 503 friendly error ──');
  await new Promise<void>((resolve) => pythonMock.close(() => resolve()));
  console.log('mock-python closed');
  {
    const r = await rawRequest(proxyPort, '/api/wallet/status');
    check('status 503', r.status === 503, r.status);
    check(
      'content-type is application/json',
      String(r.headers['content-type'] ?? '').includes('application/json'),
      r.headers['content-type'],
    );
    let parsed: any = null;
    try {
      parsed = JSON.parse(r.body);
    } catch {}
    check(
      'body.error.code === "PYTHON_BACKEND_UNAVAILABLE"',
      parsed && parsed.error && parsed.error.code === 'PYTHON_BACKEND_UNAVAILABLE',
      r.body,
    );
    check(
      'body.error.hint mentions docker-compose or uvicorn',
      parsed &&
        parsed.error &&
        typeof parsed.error.hint === 'string' &&
        (parsed.error.hint.includes('docker-compose') ||
          parsed.error.hint.includes('uvicorn')),
      parsed?.error?.hint,
    );
  }

  // ── 8. Cleanup ──────────────────────────────────────────────────────────────
  await new Promise<void>((resolve) => proxySrv.close(() => resolve()));

  // ── Summary ─────────────────────────────────────────────────────────────────
  console.log(`\n── result ──`);
  console.log(`  ${passCount} passed, ${failCount} failed`);
  if (failCount > 0) {
    process.exit(1);
  }
}

main().catch((err) => {
  console.error('test crashed:', err);
  process.exit(1);
});
