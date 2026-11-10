/**
 * routes/artifacts.test.ts — Story 15.14 — standalone runner.
 *
 * Spins up a one-off express app with the artifacts router mounted, writes
 * fixture artifacts under a temp .xueban/projects/<id>/ tree, then exercises
 * the POST /api/artifacts/lint endpoint via http.request — no superagent dep.
 */

import express from 'express';
import http from 'http';
import fs from 'fs';
import path from 'path';
import os from 'os';
import artifactsRouter from './artifacts';

let failed = 0;
function assert(cond: unknown, msg: string): void {
  if (!cond) { console.error(`  FAIL: ${msg}`); failed += 1; }
  else { console.log(`  ok: ${msg}`); }
}

interface HttpResp {
  status: number;
  body: unknown;
}

function postJson(port: number, body: Record<string, unknown>): Promise<HttpResp> {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request({
      host: '127.0.0.1',
      port,
      method: 'POST',
      path: '/api/artifacts/lint',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(data) },
    }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf-8');
        let parsed: unknown = null;
        try { parsed = JSON.parse(text); } catch { parsed = text; }
        resolve({ status: res.statusCode ?? 0, body: parsed });
      });
    });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

function getReq(port: number, urlPath: string): Promise<HttpResp> {
  return new Promise((resolve, reject) => {
    const req = http.request({ host: '127.0.0.1', port, method: 'GET', path: urlPath }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf-8');
        let parsed: unknown = null;
        try { parsed = JSON.parse(text); } catch { parsed = text; }
        resolve({ status: res.statusCode ?? 0, body: parsed });
      });
    });
    req.on('error', reject);
    req.end();
  });
}

async function main() {
  // Use a temp cwd so we don't pollute the real .xueban/projects.
  const origCwd = process.cwd();
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'sflint-'));
  process.chdir(tmp);

  const sid = 'test-uuid-1234';
  const projectsDir = path.join(tmp, '.xueban', 'projects', sid);
  fs.mkdirSync(projectsDir, { recursive: true });
  fs.writeFileSync(
    path.join(projectsDir, 'sample.html'),
    `<!DOCTYPE html><html><body>
      <div id="x"><div id="x">dup</div><span>unclosed
    </body></html>`,
    'utf-8',
  );
  fs.writeFileSync(
    path.join(projectsDir, 'team.blueprint.yml'),
    `agents: []\nskills:\n  - id: a\n# no policy_matrix\n`,
    'utf-8',
  );

  // Mount and listen.
  const app = express();
  app.use(express.json());
  app.use('/api/artifacts', artifactsRouter);
  const server = app.listen(0);
  await new Promise<void>(r => server.on('listening', () => r()));
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;

  try {
    console.log('--- POST /api/artifacts/lint (HTML, valid) ---');
    {
      const r = await postJson(port, { session_id: sid, filename: 'sample.html' });
      assert(r.status === 200, `status=200 (got ${r.status})`);
      const body = r.body as { findings: { rule: string }[]; language: string; summary: { errors: number } };
      assert(body.language === 'html', `language=html`);
      assert(Array.isArray(body.findings) && body.findings.length >= 1, `>=1 finding`);
      assert(body.findings.some(f => f.rule === 'duplicate-id'), 'has duplicate-id');
      assert(body.summary.errors >= 1, 'errors >=1');
    }

    console.log('--- POST blueprint YAML (missing policy_matrix) ---');
    {
      const r = await postJson(port, { session_id: sid, filename: 'team.blueprint.yml' });
      assert(r.status === 200, `status=200`);
      const body = r.body as { findings: { rule: string }[] };
      assert(body.findings.some(f => f.rule === 'missing-required-field'), 'has missing-required-field');
    }

    console.log('--- POST 400 missing fields ---');
    {
      const r = await postJson(port, {});
      assert(r.status === 400, `status=400`);
      assert((r.body as { error?: { code?: string } }).error?.code === 'MISSING_FIELDS', 'code=MISSING_FIELDS');
    }

    console.log('--- POST 400 path traversal in session_id ---');
    {
      const r = await postJson(port, { session_id: '../etc', filename: 'sample.html' });
      assert(r.status === 400, `status=400`);
      assert((r.body as { error?: { code?: string } }).error?.code === 'INVALID_SESSION_ID', 'code=INVALID_SESSION_ID');
    }

    console.log('--- POST 400 path traversal in filename ---');
    {
      const r = await postJson(port, { session_id: sid, filename: '../passwd' });
      assert(r.status === 400, `status=400`);
      assert((r.body as { error?: { code?: string } }).error?.code === 'INVALID_FILENAME', 'code=INVALID_FILENAME');
    }

    console.log('--- POST 404 unknown filename ---');
    {
      const r = await postJson(port, { session_id: sid, filename: 'missing.html' });
      assert(r.status === 404, `status=404`);
      assert((r.body as { error?: { code?: string } }).error?.code === 'ARTIFACT_NOT_FOUND', 'code=ARTIFACT_NOT_FOUND');
    }

    console.log('--- GET /api/artifacts/lint → 405 ---');
    {
      const r = await getReq(port, '/api/artifacts/lint');
      assert(r.status === 405, `status=405 (got ${r.status})`);
    }
  } finally {
    server.close();
    process.chdir(origCwd);
  }

  console.log('---');
  if (failed > 0) {
    console.error(`${failed} assertion(s) failed`);
    process.exit(1);
  } else {
    console.log('All artifacts route assertions passed.');
  }
}

main().catch(err => { console.error('UNCAUGHT:', err); process.exit(1); });
