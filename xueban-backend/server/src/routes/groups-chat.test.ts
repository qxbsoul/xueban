/**
 * groups-chat.test.ts — integration test for the Node chat gateway.
 *
 * Run from server/:  npx tsx src/routes/groups-chat.test.ts
 *
 * Standalone tsx script (matches proxy-fallback.test.ts style; server/ has no
 * vitest runner). We verify the parts that don't need a real LLM/CLI:
 *
 *   1. user message → gateway persists it to (mock) Python WITH the
 *      X-SF-No-Dispatch header set (so Python's own bridge stays quiet)
 *   2. gateway returns 201 immediately (does not block on reply generation)
 *   3. non-user (agent/system) message → persisted WITHOUT the suppress header
 *      and triggers NO fan-out
 *
 * Reply fan-out (resolveCallable → CLI/API turn()) is verified in the browser
 * (CLI/API both need real backends); mocking the ESM dispatcher in a plain tsx
 * script isn't worth the harness. The gateway's persistence + header contract
 * is what this guards.
 *
 * PYTHON_BACKEND_URL is pointed at an ephemeral mock Python via env BEFORE the
 * router module (which reads it transitively) is imported.
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

interface Captured {
  path: string;
  noDispatch: boolean;
  body: Record<string, unknown>;
}

async function main() {
  const captured: Captured[] = [];

  // ── mock Python backend ────────────────────────────────────────────────
  const mockPy = http.createServer((req, res) => {
    let raw = '';
    req.on('data', (c) => (raw += c));
    req.on('end', () => {
      const body = raw ? JSON.parse(raw) : {};
      if (req.method === 'POST' && /\/api\/groups\/.+\/messages$/.test(req.url ?? '')) {
        captured.push({
          path: req.url ?? '',
          noDispatch: req.headers['x-sf-no-dispatch'] === '1',
          body,
        });
        res.writeHead(201, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ ...body, message_id: 'm-' + captured.length }));
        return;
      }
      if (req.method === 'GET' && /\/api\/groups\/[^/]+$/.test(req.url ?? '')) {
        // group record — NO agents so fan-out short-circuits (writes a system
        // notice via POST, which we also capture but don't assert on).
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ data: { group_id: 'g1', agent_ids: [], messages: [] } }));
        return;
      }
      res.writeHead(404);
      res.end('{}');
    });
  });
  await new Promise<void>((r) => mockPy.listen(0, '127.0.0.1', r));
  const pyPort = (mockPy.address() as AddressInfo).port;
  process.env.PYTHON_BACKEND_URL = `http://127.0.0.1:${pyPort}`;

  // Import the router AFTER env is set (python-backend.ts reads it at module load).
  const { groupsChatRouter } = await import('./groups-chat');

  const app = express();
  app.use(express.json());
  app.use('/api/groups', groupsChatRouter);
  const nodeSrv = http.createServer(app);
  await new Promise<void>((r) => nodeSrv.listen(0, '127.0.0.1', r));
  const nodePort = (nodeSrv.address() as AddressInfo).port;
  const base = `http://127.0.0.1:${nodePort}`;

  // ── 1+2. user message: persisted w/ suppress header, 201 returned ───────
  const r1 = await fetch(`${base}/api/groups/g1/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: 'hi', sender_kind: 'user', executor: 'byok:zhipu' }),
  });
  check('user POST returns 201', r1.status === 201, r1.status);
  // give the fire-and-forget fanout a tick to hit the mock (it'll write a
  // "no agents" system notice since group has no agent_ids)
  await new Promise((r) => setTimeout(r, 150));
  const userWrite = captured.find((c) => c.body.sender_kind === 'user');
  check('user message persisted to Python', !!userWrite, captured);
  check('user write carries X-SF-No-Dispatch', !!userWrite?.noDispatch, userWrite);

  // ── 3. agent message: persisted WITHOUT suppress header, no fanout ──────
  captured.length = 0;
  const r2 = await fetch(`${base}/api/groups/g1/messages`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ content: 'reply', sender_kind: 'agent', sender_name: 'Bot' }),
  });
  check('agent POST returns 201', r2.status === 201, r2.status);
  await new Promise((r) => setTimeout(r, 100));
  const agentWrite = captured.find((c) => c.body.sender_kind === 'agent');
  check('agent message persisted', !!agentWrite, captured);
  check('agent write has NO suppress header', agentWrite?.noDispatch === false, agentWrite);
  // agent send must NOT trigger a GET group (no fanout) → only the 1 POST captured
  check('agent send triggers no fanout', captured.length === 1, captured);

  nodeSrv.close();
  mockPy.close();

  console.log(`\n${passCount} passed, ${failCount} failed`);
  process.exit(failCount === 0 ? 0 : 1);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
