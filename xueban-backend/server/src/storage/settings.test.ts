/**
 * settings.test.ts — Story 15.17 standalone smoke test for storage/settings.ts.
 *
 * Run with:  npx tsx src/storage/settings.test.ts   (from server/)
 *
 * Mirrors the no-framework style of storage/runs.test.ts. Covers:
 *   - empty state when file missing
 *   - setSetting / getSetting roundtrip (string / number / boolean / object)
 *   - listSettings returns parsed map
 *   - deleteSetting idempotency
 *   - corrupt file falls back to {} (no throw)
 *   - validation: empty key, oversized key, oversized value
 *   - BYOK key rejection (`sf_anthropic_key`, prefix variants)
 *   - persistence: file content survives across module-level reads
 *
 * Plus an HTTP integration block that mounts the real router on an ephemeral
 * Express port and exercises the four endpoints (GET/GET-key/PUT/DELETE) plus
 * the KEY_FORBIDDEN 400 path. Modeled after routes/export.test.ts.
 */

import express from 'express';
import http from 'http';
import fs from 'fs';
import os from 'os';
import path from 'path';

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

// Each test scope gets its own tmp cwd so the storage file is isolated.
async function inIsolatedCwd(
  fn: (storage: typeof import('./settings')) => void | Promise<void>,
): Promise<void> {
  const originalCwd = process.cwd();
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-settings-test-'));
  process.chdir(tmpDir);
  try {
    const mod = await import('./settings');
    mod._resetForTests();
    await fn(mod);
    mod._resetForTests();
  } finally {
    process.chdir(originalCwd);
    try {
      fs.rmSync(tmpDir, { recursive: true, force: true });
    } catch {
      /* ignore */
    }
  }
}

interface FetchedJson {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: string;
}

function reqJson(
  port: number,
  method: string,
  urlPath: string,
  body?: unknown,
): Promise<FetchedJson> {
  return new Promise((resolve, reject) => {
    const payload =
      body === undefined ? undefined : Buffer.from(JSON.stringify(body), 'utf-8');
    const req = http.request(
      {
        host: '127.0.0.1',
        port,
        path: urlPath,
        method,
        headers: payload
          ? {
              'Content-Type': 'application/json',
              'Content-Length': String(payload.length),
            }
          : {},
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (c: Buffer) => chunks.push(c));
        res.on('end', () =>
          resolve({
            status: res.statusCode ?? 0,
            headers: res.headers,
            body: Buffer.concat(chunks).toString('utf-8'),
          }),
        );
      },
    );
    req.on('error', reject);
    if (payload) req.write(payload);
    req.end();
  });
}

async function main() {
  // ── Test 1: empty when file missing ────────────────────────────────────────
  console.log('\n[1] empty store');
  await inIsolatedCwd((s) => {
    check(
      'listSettings() returns {}',
      Object.keys(s.listSettings()).length === 0,
    );
    check('getSetting() returns undefined', s.getSetting('sf.maxTokens') === undefined);
  });

  // ── Test 2: roundtrip across multiple types ────────────────────────────────
  console.log('\n[2] CRUD roundtrip');
  await inIsolatedCwd((s) => {
    s.setSetting('sf.maxTokens', 4096);
    s.setSetting('sf.theme', 'dark');
    s.setSetting('sf.auto_critique', true);
    s.setSetting('sf.featureFlags', { ds: 'tailwind', exp: false });

    check('number roundtrip', s.getSetting('sf.maxTokens') === 4096);
    check('string roundtrip', s.getSetting('sf.theme') === 'dark');
    check('boolean roundtrip', s.getSetting('sf.auto_critique') === true);
    const obj = s.getSetting('sf.featureFlags') as { ds: string; exp: boolean };
    check(
      'object roundtrip',
      obj && obj.ds === 'tailwind' && obj.exp === false,
      obj,
    );

    const all = s.listSettings();
    check('listSettings size = 4', Object.keys(all).length === 4);
    check(
      'file persisted at .xueban/settings.json',
      fs.existsSync(path.join(process.cwd(), '.xueban', 'settings.json')),
    );
  });

  // ── Test 3: update overwrites previous value ───────────────────────────────
  console.log('\n[3] upsert overwrites');
  await inIsolatedCwd((s) => {
    s.setSetting('sf.maxTokens', 4096);
    s.setSetting('sf.maxTokens', 8192);
    check('updated value persisted', s.getSetting('sf.maxTokens') === 8192);
    check('only one entry', Object.keys(s.listSettings()).length === 1);
  });

  // ── Test 4: deleteSetting is idempotent ────────────────────────────────────
  console.log('\n[4] delete idempotent');
  await inIsolatedCwd((s) => {
    s.setSetting('sf.theme', 'dark');
    check('delete existing returns true', s.deleteSetting('sf.theme') === true);
    check('delete missing returns false', s.deleteSetting('sf.theme') === false);
    check('store empty after delete', Object.keys(s.listSettings()).length === 0);
  });

  // ── Test 5: corrupt file recovery ──────────────────────────────────────────
  console.log('\n[5] corrupt file recovery');
  await inIsolatedCwd((s) => {
    const dir = path.join(process.cwd(), '.xueban');
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(path.join(dir, 'settings.json'), '{ this is not json', 'utf-8');
    check(
      'listSettings returns {} on parse error',
      Object.keys(s.listSettings()).length === 0,
    );
    s.setSetting('sf.maxTokens', 2048);
    check('setSetting recovers', s.getSetting('sf.maxTokens') === 2048);
  });

  // ── Test 6: BYOK rejection ─────────────────────────────────────────────────
  console.log('\n[6] BYOK key rejection');
  await inIsolatedCwd((s) => {
    let threw = false;
    try {
      s.setSetting('sf_anthropic_key', 'sk-ant-leak');
    } catch (e) {
      threw = e instanceof Error && e.message === 'KEY_FORBIDDEN';
    }
    check('exact key rejected with KEY_FORBIDDEN', threw);

    threw = false;
    try {
      s.setSetting('sf_anthropic_key_secondary', 'sk-ant-leak');
    } catch (e) {
      threw = e instanceof Error && e.message === 'KEY_FORBIDDEN';
    }
    check('prefix variant rejected', threw);

    check(
      '_isForbiddenKeyForTests("sf.maxTokens") = false',
      !s._isForbiddenKeyForTests('sf.maxTokens'),
    );
  });

  // ── Test 7: validation — empty key / oversized key / oversized value ──────
  console.log('\n[7] validation');
  await inIsolatedCwd((s) => {
    let threw = false;
    try {
      s.setSetting('', 'x');
    } catch (e) {
      threw = e instanceof Error && e.message === 'INVALID_KEY';
    }
    check('empty key throws INVALID_KEY', threw);

    threw = false;
    try {
      s.setSetting('a'.repeat(129), 'x');
    } catch (e) {
      threw = e instanceof Error && e.message === 'INVALID_KEY';
    }
    check('>128-char key throws INVALID_KEY', threw);

    threw = false;
    try {
      // 65KB of payload is over the 64KB cap.
      s.setSetting('sf.large', 'x'.repeat(65 * 1024));
    } catch (e) {
      threw = e instanceof Error && e.message === 'VALUE_TOO_LARGE';
    }
    check('>64KB value throws VALUE_TOO_LARGE', threw);
  });

  // ── Test 8: HTTP route integration ─────────────────────────────────────────
  console.log('\n[8] route integration (PUT/GET/DELETE/KEY_FORBIDDEN)');
  await inIsolatedCwd(async () => {
    const settingsRouter = (await import('../routes/settings')).default;
    const app = express();
    app.use(express.json());
    app.use('/api/settings', settingsRouter);
    const server = await new Promise<http.Server>((resolve) => {
      const s = app.listen(0, () => resolve(s));
    });
    const addr = server.address();
    const port = typeof addr === 'object' && addr ? addr.port : 0;

    try {
      // Empty list (envelope shape: { settings: {} })
      const empty = await reqJson(port, 'GET', '/api/settings');
      check('GET /api/settings empty 200', empty.status === 200, empty.body);
      const emptyParsed = JSON.parse(empty.body) as { settings: Record<string, unknown> };
      check(
        'envelope shape {settings:{}}',
        emptyParsed.settings !== undefined && Object.keys(emptyParsed.settings).length === 0,
      );

      // PUT 200 + body echo
      const put = await reqJson(port, 'PUT', '/api/settings/sf.maxTokens', { value: 4096 });
      check('PUT 200', put.status === 200, put.body);
      const putParsed = JSON.parse(put.body);
      check(
        'PUT echoes key/value',
        putParsed.key === 'sf.maxTokens' && putParsed.value === 4096,
        putParsed,
      );

      // GET single key 200
      const got = await reqJson(port, 'GET', '/api/settings/sf.maxTokens');
      check('GET key 200', got.status === 200, got.body);
      const gotParsed = JSON.parse(got.body);
      check(
        'GET key value=4096',
        gotParsed.key === 'sf.maxTokens' && gotParsed.value === 4096,
      );

      // GET missing key 404
      const miss = await reqJson(port, 'GET', '/api/settings/sf.does-not-exist');
      check('GET missing 404', miss.status === 404, miss.body);
      const missParsed = JSON.parse(miss.body);
      check('NOT_FOUND error code', missParsed.error?.code === 'NOT_FOUND');

      // PUT missing body.value → 400 INVALID_BODY
      const badBody = await reqJson(port, 'PUT', '/api/settings/sf.foo', {});
      check('PUT empty body 400', badBody.status === 400, badBody.body);
      check(
        'INVALID_BODY error code',
        JSON.parse(badBody.body).error?.code === 'INVALID_BODY',
      );

      // PUT BYOK key → 400 KEY_FORBIDDEN
      const byok = await reqJson(port, 'PUT', '/api/settings/sf_anthropic_key', {
        value: 'sk-ant-leak',
      });
      check('PUT BYOK 400', byok.status === 400, byok.body);
      const byokParsed = JSON.parse(byok.body);
      check(
        'KEY_FORBIDDEN error code',
        byokParsed.error?.code === 'KEY_FORBIDDEN',
        byokParsed,
      );

      // DELETE 204
      const del = await reqJson(port, 'DELETE', '/api/settings/sf.maxTokens');
      check('DELETE existing 204', del.status === 204, del.body);

      // DELETE again 204 (idempotent)
      const del2 = await reqJson(port, 'DELETE', '/api/settings/sf.maxTokens');
      check('DELETE missing 204 (idempotent)', del2.status === 204);

      // GET after delete 404
      const gone = await reqJson(port, 'GET', '/api/settings/sf.maxTokens');
      check('GET after delete 404', gone.status === 404);

      // 15.9 endpoint still works (regression guard)
      const overrides = await reqJson(port, 'GET', '/api/settings/generation-overrides');
      check('GET /generation-overrides 200', overrides.status === 200, overrides.body);
      const ov = JSON.parse(overrides.body);
      check(
        '/generation-overrides has model_locked field',
        typeof ov.model_locked === 'boolean',
      );
    } finally {
      await new Promise<void>((resolve) => server.close(() => resolve()));
    }
  });

  // ── Summary ────────────────────────────────────────────────────────────────
  console.log('\n────────────────────────────────────────');
  console.log(`  ${passCount} passed,  ${failCount} failed`);
  console.log('────────────────────────────────────────\n');

  if (failCount > 0) process.exit(1);
}

main().catch((err) => {
  console.error('test runner crashed:', err);
  process.exit(1);
});
