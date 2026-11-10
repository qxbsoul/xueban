/**
 * mcp.test.ts — Real MCP runner end-to-end (Story 15.23)
 *
 * Run from server/:  npx tsx src/skill-runners/mcp.test.ts
 *
 * Spawns the JS mock MCP server fixture as a real child process, configures
 * a tmp `.xueban/mcp.json`, and exercises:
 *   1. Happy path tools/list + tools/call → text delta
 *   2. Tool-not-found
 *   3. Tool errors out (JSON-RPC error)
 *   4. Server not in registry → MCP_SERVER_NOT_FOUND
 */

import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { runMcpExecutor } from './mcp';
import type { RunnerInput } from './types';

let pass = 0;
let fail = 0;
function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}`); if (detail !== undefined) console.log('        detail:', detail); }
}

const FIXTURE = path.resolve(__dirname, '__fixtures__', '__mock_mcp_server.js');

function makeInput(): RunnerInput {
  const tmpCwd = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-mcp-runner-'));
  return {
    system_prompt: 'SYS-PROMPT',
    prompt: 'GOAL-TEXT',
    session_id: 'mcp-session-1',
    cwd: tmpCwd,
  };
}

async function withRegistry<T>(fn: () => Promise<T>): Promise<T> {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-mcp-cwd-'));
  fs.mkdirSync(path.join(dir, '.xueban'), { recursive: true });
  fs.writeFileSync(
    path.join(dir, '.xueban', 'mcp.json'),
    JSON.stringify({
      servers: [
        { name: 'mock', command: process.execPath, args: [FIXTURE] },
      ],
    }),
  );
  const old = process.cwd();
  process.chdir(dir);
  try { return await fn(); } finally { process.chdir(old); }
}

async function main() {
  await withRegistry(async () => {
    console.log('\n── 1. Happy path: tools/list + tools/call → echo ──');
    {
      const events: any[] = [];
      for await (const ev of runMcpExecutor(makeInput(), 'mock/echo_goal')) events.push(ev);
      const deltas = events.filter((e) => e.event === 'delta');
      const ends = events.filter((e) => e.event === 'end');
      const errors = events.filter((e) => e.event === 'error');
      check('happy: ≥1 delta', deltas.length >= 1, deltas.length);
      check('happy: 1 end', ends.length === 1, ends.length);
      check('happy: 0 errors', errors.length === 0, errors);
      const text = deltas.map((e) => e.data.text).join('');
      check('happy: text contains goal', text.includes('GOAL-TEXT'), text);
      check('happy: text contains system', text.includes('SYS-PROMPT'), text);
      console.log(`        echoed-text: ${JSON.stringify(text)}`);
    }

    console.log('\n── 2. Tool not found ──');
    {
      const events: any[] = [];
      for await (const ev of runMcpExecutor(makeInput(), 'mock/no_such_tool')) events.push(ev);
      const err = events.find((e) => e.event === 'error');
      check('tool-not-found: emitted error', !!err, events);
      check('tool-not-found: code is MCP_TOOL_NOT_FOUND', err?.data?.code === 'MCP_TOOL_NOT_FOUND', err?.data);
    }

    console.log('\n── 3. Tool errors out (fail_tool) ──');
    {
      const events: any[] = [];
      for await (const ev of runMcpExecutor(makeInput(), 'mock/fail_tool')) events.push(ev);
      const err = events.find((e) => e.event === 'error');
      check('fail_tool: emitted error', !!err, events);
      check('fail_tool: error code present', typeof err?.data?.code === 'string', err?.data);
    }

    console.log('\n── 4. Invalid spec (no slash) ──');
    {
      const events: any[] = [];
      for await (const ev of runMcpExecutor(makeInput(), 'badspec')) events.push(ev);
      const err = events.find((e) => e.event === 'error');
      check('invalid-spec: code MCP_INVALID_SPEC', err?.data?.code === 'MCP_INVALID_SPEC', err?.data);
    }
  });

  // 5. Server not in registry — runs without a custom mcp.json (cwd has no
  // .xueban/mcp.json), and `definitely-not-real` is also not on PATH.
  {
    console.log('\n── 5. Server not in registry → MCP_SERVER_NOT_FOUND ──');
    const tmpCwd = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-mcp-empty-'));
    const old = process.cwd();
    process.chdir(tmpCwd);
    try {
      const events: any[] = [];
      for await (const ev of runMcpExecutor(makeInput(), 'definitely-not-real-zzz/x')) events.push(ev);
      const err = events.find((e) => e.event === 'error');
      check('not-found: emitted error', !!err, events);
      check('not-found: MCP_SERVER_NOT_FOUND or MCP_UNREACHABLE', /MCP_SERVER_NOT_FOUND|MCP_UNREACHABLE/.test(err?.data?.code ?? ''), err?.data);
    } finally {
      process.chdir(old);
    }
  }

  console.log(`\nDone: ${pass} passed, ${fail} failed.`);
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((err) => { console.error(err); process.exit(1); });
