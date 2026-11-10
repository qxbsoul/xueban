/**
 * acp.test.ts — Real ACP runner end-to-end (Story 15.23)
 *
 * Run from server/:  npx tsx src/skill-runners/acp.test.ts
 *
 * Spawns the JS mock ACP server fixture as a real subprocess, configures a
 * temporary `.xueban/acp-agents.json` registry pointing at it, and drives
 * the runner end-to-end:
 *
 *   1. happy path — initialize / session/new / session/prompt / 3 chunks / end
 *   2. unknown agent → ACP_UNREACHABLE
 *   3. abort mid-prompt → cancel + clean teardown (no zombies)
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { runAcpExecutor } from './acp';
import { __resetAcpDetectCacheForTest, detectAcpAgents } from '../../acp-detector';
import type { RunnerInput } from './types';

let pass = 0;
let fail = 0;
function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}`); if (detail !== undefined) console.log('        detail:', detail); }
}

const FIXTURE = path.resolve(__dirname, '__fixtures__', '__mock_acp_server.js');

function makeInput(signal?: AbortSignal): RunnerInput {
  const tmpCwd = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-acp-runner-'));
  return {
    system_prompt: 'You are a test assistant.',
    prompt: 'hello world',
    session_id: 'test-session-1',
    cwd: tmpCwd,
    signal,
  };
}

async function withRegistry<T>(agents: any[], fn: () => Promise<T>): Promise<T> {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-acp-cwd-'));
  fs.mkdirSync(path.join(dir, '.xueban'), { recursive: true });
  fs.writeFileSync(path.join(dir, '.xueban', 'acp-agents.json'), JSON.stringify({ agents }));
  const oldCwd = process.cwd();
  process.chdir(dir);
  try {
    __resetAcpDetectCacheForTest();
    await detectAcpAgents(true);
    return await fn();
  } finally {
    process.chdir(oldCwd);
  }
}

async function main() {
  console.log('\n── 1. Happy path: real subprocess ACP run ──');
  await withRegistry(
    [{ id: 'mock', type: 'acp', binary: process.execPath, args: [FIXTURE] }],
    async () => {
      const events: any[] = [];
      for await (const ev of runAcpExecutor(makeInput(), 'mock')) {
        events.push(ev);
      }
      const deltas = events.filter((e) => e.event === 'delta');
      const ends = events.filter((e) => e.event === 'end');
      const errors = events.filter((e) => e.event === 'error');
      check('happy: ≥3 delta events', deltas.length >= 3, deltas.length);
      check('happy: ≥1 end event', ends.length >= 1, ends.length);
      check('happy: 0 error events', errors.length === 0, errors);
      const text = deltas.map((e) => e.data.text).join('');
      check('happy: deltas joined contain chunk-1/2/3', /chunk-1.*chunk-2.*chunk-3/s.test(text), text);
      console.log(`        joined-text: ${JSON.stringify(text)}`);
    },
  );

  console.log('\n── 2. Unknown agent → ACP_UNREACHABLE ──');
  {
    const events: any[] = [];
    for await (const ev of runAcpExecutor(makeInput(), 'definitely-not-real-12345')) {
      events.push(ev);
    }
    const err = events.find((e) => e.event === 'error');
    check('unknown: emitted error event', !!err, events);
    check('unknown: code is ACP_UNREACHABLE', err?.data?.code === 'ACP_UNREACHABLE', err?.data);
    check('unknown: no anthropic fallback (only the error event)', events.length === 1, events.length);
  }

  console.log('\n── 3. Custom command form ──');
  {
    const events: any[] = [];
    const target = `custom?cmd=${encodeURIComponent(process.execPath)}&arg=${encodeURIComponent(FIXTURE)}`;
    for await (const ev of runAcpExecutor(makeInput(), target)) {
      events.push(ev);
    }
    const deltas = events.filter((e) => e.event === 'delta');
    check('custom: got deltas', deltas.length >= 3, deltas.length);
  }

  console.log('\n── 4. Abort mid-prompt ──');
  await withRegistry(
    [{ id: 'mock', type: 'acp', binary: process.execPath, args: [FIXTURE] }],
    async () => {
      const ac = new AbortController();
      const events: any[] = [];
      const gen = runAcpExecutor(makeInput(ac.signal), 'mock');
      // abort after first chunk arrives
      let aborted = false;
      const startTime = Date.now();
      for await (const ev of gen) {
        events.push(ev);
        if (!aborted && ev.event === 'delta') {
          aborted = true;
          ac.abort();
        }
        if (Date.now() - startTime > 8000) break;
      }
      check('abort: terminated within 8s', Date.now() - startTime < 8000);
      const ended = events.some((e) => e.event === 'end');
      check('abort: emitted end event', ended);
    },
  );

  console.log(`\nDone: ${pass} passed, ${fail} failed.`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((err) => { console.error(err); process.exit(1); });
