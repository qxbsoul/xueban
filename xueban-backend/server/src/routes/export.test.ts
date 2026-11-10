/**
 * export.test.ts — Story 15.6 standalone smoke test for /api/export/:id/zip.
 *
 * Run with:  npx tsx src/routes/export.test.ts   (from server/)
 *
 * No external test framework — matches parser.test.ts style. Spins up an
 * Express app on an ephemeral port, exercises the export router against a
 * temp `.xueban/projects/<id>/` fixture, and asserts on response
 * status / headers / body bytes.
 */

import express from 'express';
import http from 'http';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { randomUUID } from 'crypto';
import exportRouter, { UUID_V4_RE } from './export';

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

interface FetchedResponse {
  status: number;
  headers: http.IncomingHttpHeaders;
  body: Buffer;
}

function getRaw(port: number, urlPath: string): Promise<FetchedResponse> {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port, path: urlPath }, (res) => {
      const chunks: Buffer[] = [];
      res.on('data', (c: Buffer) => chunks.push(c));
      res.on('end', () =>
        resolve({
          status: res.statusCode ?? 0,
          headers: res.headers,
          body: Buffer.concat(chunks),
        }),
      );
    });
    req.on('error', reject);
  });
}

async function main() {
  // Use a private temp cwd so we don't collide with a developer's real
  // .xueban/projects directory.
  const cwdBackup = process.cwd();
  const tmpRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-export-test-'));
  process.chdir(tmpRoot);

  // Fixture: a real session directory with two artifacts.
  const realId = randomUUID();
  const sessionDir = path.join(tmpRoot, '.xueban', 'projects', realId);
  fs.mkdirSync(sessionDir, { recursive: true });
  fs.writeFileSync(path.join(sessionDir, 'prototype.html'), '<!doctype html><h1>hi</h1>');
  fs.writeFileSync(path.join(sessionDir, 'team_blueprint.yml'), 'name: t\n');

  // Mount the router on an ephemeral port.
  const app = express();
  app.use('/api/export', exportRouter);
  const server = await new Promise<http.Server>((resolve) => {
    const s = app.listen(0, () => resolve(s));
  });
  const addr = server.address();
  const port = typeof addr === 'object' && addr ? addr.port : 0;

  try {
    // --- UUID regex unit checks ---------------------------------------------
    check('regex accepts a randomUUID()', UUID_V4_RE.test(randomUUID()));
    check('regex rejects empty', !UUID_V4_RE.test(''));
    check(
      'regex rejects path traversal segment',
      !UUID_V4_RE.test('../etc/passwd'),
    );
    check('regex rejects too-short hex', !UUID_V4_RE.test('abc'));
    check(
      'regex rejects v3 UUID (third group not 4xxx)',
      !UUID_V4_RE.test('11111111-1111-3111-8111-111111111111'),
    );
    check(
      'regex rejects bad variant (third group not 4xxx)',
      !UUID_V4_RE.test('11111111-1111-1111-1111-111111111111'),
    );

    // --- 400 invalid id ------------------------------------------------------
    {
      const r = await getRaw(port, '/api/export/not-a-uuid/zip');
      check('400 on invalid session_id', r.status === 400, r.status);
    }

    // --- 400 on traversal attempt -------------------------------------------
    {
      // Express will collapse `..`, but route must still reject the segment.
      const r = await getRaw(
        port,
        `/api/export/${encodeURIComponent('../../../etc')}/zip`,
      );
      check(
        '400 on traversal-shaped session_id',
        r.status === 400,
        r.status,
      );
    }

    // --- 404 unknown session -------------------------------------------------
    {
      const fake = randomUUID();
      const r = await getRaw(port, `/api/export/${fake}/zip`);
      check('404 on unknown valid uuid', r.status === 404, r.status);
    }

    // --- 200 happy path ------------------------------------------------------
    {
      const r = await getRaw(port, `/api/export/${realId}/zip`);
      check('200 on real session zip', r.status === 200, r.status);
      check(
        'content-type is application/zip',
        (r.headers['content-type'] ?? '').toString().startsWith('application/zip'),
        r.headers['content-type'],
      );
      const cd = (r.headers['content-disposition'] ?? '').toString();
      check(
        'content-disposition has session prefix',
        cd.includes(`session-${realId.slice(0, 8)}.zip`),
        cd,
      );
      // PK\x03\x04 — local file header magic for zip.
      check(
        'body starts with PK zip magic',
        r.body.length > 4 &&
          r.body[0] === 0x50 &&
          r.body[1] === 0x4b &&
          r.body[2] === 0x03 &&
          r.body[3] === 0x04,
        { len: r.body.length, head: r.body.subarray(0, 4) },
      );
      check('body is non-trivial size', r.body.length > 100, r.body.length);
    }
  } finally {
    server.close();
    process.chdir(cwdBackup);
    try {
      fs.rmSync(tmpRoot, { recursive: true, force: true });
    } catch {
      /* noop */
    }
  }

  console.log(`\n${passCount} passed, ${failCount} failed`);
  if (failCount > 0) process.exit(1);
}

main().catch((e) => {
  console.error(e);
  process.exit(1);
});
