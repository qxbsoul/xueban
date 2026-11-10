/**
 * cli.test.ts — Real spawn pipeline test (Story 15.19 v2)
 *
 * Run from server/:  npx tsx src/skill-runners/cli.test.ts
 *
 * "先别模拟了，能真实跑就行" — this test really spawns a child process and
 * exercises the entire spawn → stdin → stdout → exit → SIGTERM/SIGKILL chain.
 *
 * We use `node --version` (guaranteed to be present since we run under tsx).
 * For the abort path we use `node -e "setInterval(()=>{},1000)"` which sits
 * idle until killed.
 */

import { spawn } from 'node:child_process';
import { Readable } from 'node:stream';
import { dispatchSkillRunner } from './index';
import type { RunnerInput } from './types';
import { runCliSpawn } from './cli';
import { parsePlainLine } from '../../parsers/cli-streams/plain-line';
import type { SseEvent } from '../../parser';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';

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

const tmpCwd = fs.mkdtempSync(path.join(os.tmpdir(), 'sf-cli-test-'));
const noopArtifact = () => {};

async function main(): Promise<void> {
  // ─── 1. RAW SPAWN — verify primitives ──────────────────────────────────────
  console.log('\n── raw spawn primitives (`node --version`) ──');
  await new Promise<void>((resolve) => {
    const child = spawn(process.execPath, ['--version'], { shell: false, stdio: ['pipe', 'pipe', 'pipe'] });
    let out = '';
    child.stdout?.on('data', (b) => (out += b.toString()));
    child.on('close', (code) => {
      check('node --version exits 0', code === 0, code);
      check('node --version prints v<N>', /^v\d+/.test(out.trim()), out);
      resolve();
    });
    child.on('error', () => {
      check('node --version spawned without error', false);
      resolve();
    });
  });

  // ─── 2. STDIN PIPE — verify prompt-via-stdin works ─────────────────────────
  console.log('\n── stdin pipe roundtrip ──');
  await new Promise<void>((resolve) => {
    const child = spawn(process.execPath, ['-e', 'let d=""; process.stdin.on("data", c=>d+=c); process.stdin.on("end", ()=>{ process.stdout.write("ECHO:"+d); });'], {
      shell: false,
      stdio: ['pipe', 'pipe', 'pipe'],
    });
    let out = '';
    child.stdout?.on('data', (b) => (out += b.toString()));
    child.stdin?.end('hello-from-test\n');
    child.on('close', () => {
      check('stdin pipe delivers prompt', out.includes('ECHO:hello-from-test'), out);
      resolve();
    });
  });

  // ─── 3. PARSER through real stdout — feed a node script that emits sf tags ──
  console.log('\n── plain-line parser with real child stdout ──');
  const sfText = '<sf:classify output_type="answer" mode="single" confidence="0.9" complexity="1"/>OK<sf:complete redirect="/editor"/>';
  const child = spawn(
    'node',
    ['-e', `process.stdout.write(${JSON.stringify(sfText)});`],
    { shell: false, stdio: ['pipe', 'pipe', 'pipe'] },
  );
  if (!child.stdout) {
    check('child has stdout', false);
  } else {
    const events: SseEvent[] = [];
    for await (const evt of parsePlainLine(child.stdout, 'sess-real-1', noopArtifact)) {
      events.push(evt);
    }
    check('parser yields classify from real spawn', events.some((e) => e.event === 'classify'));
    check('parser yields complete from real spawn', events.some((e) => e.event === 'complete'));
  }

  // ─── 4. ABORT → SIGTERM → SIGKILL chain (with reduced grace for test speed) ──
  console.log('\n── abort signal → kill chain ──');
  await new Promise<void>((resolve) => {
    // Idle child: blocks forever
    const child = spawn(
      'node',
      ['-e', 'setInterval(()=>{},1000); process.stdout.write("ALIVE\\n");'],
      { shell: false, stdio: ['pipe', 'pipe', 'pipe'] },
    );

    let exited = false;
    let exitSignal: NodeJS.Signals | null = null;
    let exitCode: number | null = null;
    child.on('close', (code, sig) => {
      exited = true;
      exitCode = code;
      exitSignal = sig;
    });

    setTimeout(() => {
      child.kill('SIGTERM');
      // After 500ms (test-only grace), force kill if still alive
      setTimeout(() => {
        if (!exited) child.kill('SIGKILL');
      }, 500);
    }, 200);

    setTimeout(() => {
      check('idle child terminated by signal', exited, { exitCode, exitSignal });
      resolve();
    }, 1500);
  });

  // ─── 5. dispatchSkillRunner — anthropic-direct path with no key emits NO_API_KEY ──
  console.log('\n── dispatcher: anthropic-direct without key ──');
  const savedKey = process.env.ANTHROPIC_API_KEY;
  delete process.env.ANTHROPIC_API_KEY;
  {
    const input: RunnerInput = {
      system_prompt: 'sys',
      prompt: 'goal',
      session_id: 'sess-disp-1',
      cwd: tmpCwd,
    };
    const events: SseEvent[] = [];
    for await (const evt of dispatchSkillRunner('anthropic-direct', input)) {
      events.push(evt);
      if (events.length > 5) break;
    }
    check(
      'anthropic-direct without key yields NO_API_KEY error',
      events.some((e) => e.event === 'error' && (e.data as { code?: string }).code === 'NO_API_KEY'),
    );
  }
  if (savedKey !== undefined) process.env.ANTHROPIC_API_KEY = savedKey;

  // ─── 6. dispatchSkillRunner — cli:nonexistent yields CLI_NOT_REGISTERED ──
  console.log('\n── dispatcher: cli:nonexistent → CLI_NOT_REGISTERED ──');
  {
    const input: RunnerInput = {
      system_prompt: 'sys',
      prompt: 'goal',
      session_id: 'sess-disp-2',
      cwd: tmpCwd,
    };
    const events: SseEvent[] = [];
    for await (const evt of dispatchSkillRunner('cli:totally-not-a-real-cli', input)) {
      events.push(evt);
      if (events.length > 5) break;
    }
    check(
      'cli:totally-not-a-real-cli emits CLI_NOT_REGISTERED',
      events.some((e) => e.event === 'error' && (e.data as { code?: string }).code === 'CLI_NOT_REGISTERED'),
    );
  }

  // ─── 7. dispatchSkillRunner — unknown executor scheme ──
  console.log('\n── dispatcher: weird-scheme: → EXECUTOR_UNKNOWN ──');
  {
    const input: RunnerInput = {
      system_prompt: 'sys',
      prompt: 'goal',
      session_id: 'sess-disp-3',
      cwd: tmpCwd,
    };
    const events: SseEvent[] = [];
    for await (const evt of dispatchSkillRunner('weird-scheme:foo', input)) {
      events.push(evt);
      if (events.length > 5) break;
    }
    check(
      'unknown scheme emits EXECUTOR_UNKNOWN',
      events.some((e) => e.event === 'error' && (e.data as { code?: string }).code === 'EXECUTOR_UNKNOWN'),
    );
  }

  // ─── 8. dispatchSkillRunner — unknown acp target ──
  console.log('\n── dispatcher: acp:unknown → ACP_UNREACHABLE ──');
  {
    const input: RunnerInput = {
      system_prompt: 'sys',
      prompt: 'goal',
      session_id: 'sess-disp-4',
      cwd: tmpCwd,
    };
    const events: SseEvent[] = [];
    for await (const evt of dispatchSkillRunner('acp:definitely-not-real-12345', input)) {
      events.push(evt);
      if (events.length > 5) break;
    }
    check(
      'unknown acp target emits ACP_UNREACHABLE',
      events.some(
        (e) => e.event === 'error' && (e.data as { code?: string }).code === 'ACP_UNREACHABLE',
      ),
    );
  }

  // ─── 9. runCliSpawn unknown id → CLI_UNKNOWN ──
  console.log('\n── runCliSpawn unknown id ──');
  {
    const events: SseEvent[] = [];
    for await (const evt of runCliSpawn(
      { system_prompt: 's', prompt: 'p', session_id: 'sess-spawn-1', cwd: tmpCwd },
      'no-such-cli',
    )) {
      events.push(evt);
      if (events.length > 5) break;
    }
    check(
      'runCliSpawn yields CLI_UNKNOWN for unregistered id',
      events.some((e) => e.event === 'error' && (e.data as { code?: string }).code === 'CLI_UNKNOWN'),
    );
  }

  // Clean up tmp
  try {
    fs.rmSync(tmpCwd, { recursive: true, force: true });
  } catch {
    /* ignore */
  }

  console.log(`\n────────────────────────────────────────────`);
  console.log(`Total: ${passCount + failCount} | PASS: ${passCount} | FAIL: ${failCount}`);
  if (failCount > 0) process.exit(1);
}

// Suppress unused warning for Readable
void Readable;

main().catch((err) => {
  console.error('test crashed:', err);
  process.exit(1);
});
