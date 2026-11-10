/**
 * tool-runner-approval.test.ts — 阶段 1 ToolRunner prompter 路径单测。
 *
 * Run with:  npx tsx src/lib/__tests__/tool-runner-approval.test.ts   (from server/)
 *
 * No framework — mirrors the surrounding lib/__tests__ pattern (single async
 * IIFE, inline check()).
 *
 * Coverage (ROADMAP 阶段 1 验收①②④):
 *   1. prompt + prompter approve → executor RUNS, permission.mode 'prompt'
 *   2. prompt + prompter deny   → executor NOT called, is_error tool_result,
 *      chunk-level permission_check frame carries decision:'deny'
 *   3. prompt WITHOUT prompter  → v1 soft-deny preserved (fail-closed default)
 *   4. whitelist visibility     → tool outside the registry never reaches a
 *      prompter/executor (acceptance ④: read-only agents cannot see write tools)
 *   5. replay integration       → registry frames wired into a real RunBus land
 *      in the run event log; a LATE-attached SSE view replays both
 *      permission_check frames (pending + decision) — acceptance ②
 */

import {
  ToolRunner,
  registerToolExecutor,
  _resetToolExecutorsForTests,
} from '../tool-runner';
import { PermissionPolicyV2 } from '../permission-policy-v2';
import { ToolRegistry, type ToolSpec } from '../tool-spec';
import {
  createToolApprovalRegistry,
  type PermissionCheckFrame,
} from '../tool-approval';
import { createRunBus, type RunEventRecord } from '../run-event-bus';
import { promptDefaultsForTools } from '../tools/builtin';

let pass = 0;
let fail = 0;

function check(label: string, cond: boolean, detail?: string): void {
  if (cond) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}${detail ? `\n        ${detail}` : ''}`);
  }
}

function spec(name: string): ToolSpec {
  return {
    name,
    description: `test tool ${name}`,
    input_schema: { type: 'object', properties: {} },
    source: 'base',
  };
}

function freshSignal(): AbortSignal {
  return new AbortController().signal;
}

(async () => {
  _resetToolExecutorsForTests();

  let executorCalls = 0;
  registerToolExecutor('write_file', {
    async execute() {
      executorCalls++;
      return { output: 'wrote it' };
    },
  });
  registerToolExecutor('read_file', {
    async execute() {
      executorCalls++;
      return { output: 'read it' };
    },
  });

  // ── 1. prompt + approve → executor runs ───────────────────────────────────
  {
    const registry = new ToolRegistry([spec('write_file')]);
    const policy = new PermissionPolicyV2({
      allowedTools: ['write_file'],
      defaults: { write_file: 'prompt' },
    });
    const runner = new ToolRunner(registry, policy, {
      prompter: async () => 'approve',
    });
    const before = executorCalls;
    const result = await runner.dispatch(
      { name: 'write_file', input: { path: 'a.md' } },
      freshSignal(),
    );
    check('1. approve → executor ran', executorCalls === before + 1);
    check('1. output passthrough', result.output === 'wrote it' && !result.isError);
    check(
      '1. permission records prompt+allow',
      result.permission.mode === 'prompt' && result.permission.allow === true,
    );
  }

  // ── 2. prompt + deny → fail-closed, chunk frame decision:'deny' ───────────
  {
    const registry = new ToolRegistry([spec('write_file')]);
    const policy = new PermissionPolicyV2({
      allowedTools: ['write_file'],
      defaults: { write_file: 'prompt' },
    });
    const runner = new ToolRunner(registry, policy, {
      prompter: async () => 'deny',
    });
    const before = executorCalls;
    const result = await runner.dispatch(
      { name: 'write_file', input: { path: 'a.md' } },
      freshSignal(),
    );
    check('2. deny → executor NOT called', executorCalls === before);
    check('2. is_error tool_result', result.isError === true);
    check(
      '2. chunk-level permission_check carries final deny',
      (result.sseEvents ?? []).some(
        (e) =>
          e.event === 'permission_check' &&
          (e.data as { decision?: string }).decision === 'deny',
      ),
      JSON.stringify(result.sseEvents),
    );
  }

  // ── 3. prompt without prompter → v1 soft-deny preserved ──────────────────
  {
    const registry = new ToolRegistry([spec('write_file')]);
    const policy = new PermissionPolicyV2({
      allowedTools: ['write_file'],
      defaults: { write_file: 'prompt' },
    });
    const runner = new ToolRunner(registry, policy); // no prompter
    const before = executorCalls;
    const result = await runner.dispatch(
      { name: 'write_file', input: {} },
      freshSignal(),
    );
    check('3. v1: executor NOT called', executorCalls === before);
    check(
      '3. v1: deny + permission_check SSE (legacy shape intact)',
      result.isError === true &&
        (result.sseEvents ?? []).some((e) => e.event === 'permission_check'),
    );
  }

  // ── 4. whitelist visibility (acceptance ④) ────────────────────────────────
  {
    // Read-only agent: registry/policy built from pickedTools=['read_file']
    // ONLY — exactly what workflow/executor.ts buildRuntimeStream does.
    const picked = ['read_file'];
    const registry = new ToolRegistry(picked.map(spec));
    const policy = new PermissionPolicyV2({
      allowedTools: picked,
      defaults: promptDefaultsForTools(picked),
    });
    let prompted = 0;
    const runner = new ToolRunner(registry, policy, {
      prompter: async () => {
        prompted++;
        return 'approve';
      },
    });
    const result = await runner.dispatch(
      { name: 'write_file', input: {} },
      freshSignal(),
    );
    check(
      '4. write tool invisible to read-only agent (unknown-tool, not prompt)',
      result.isError === true &&
        typeof result.output === 'object' &&
        String((result.output as { error?: string }).error).includes("unknown tool 'write_file'"),
      JSON.stringify(result.output),
    );
    check('4. prompter never consulted for unlisted tool', prompted === 0);
    const before = executorCalls;
    const okRead = await runner.dispatch({ name: 'read_file', input: {} }, freshSignal());
    check('4. listed read tool still runs', executorCalls === before + 1 && !okRead.isError);
    check(
      '4. promptDefaultsForTools lifts ONLY prompt (no new denials)',
      Object.keys(promptDefaultsForTools(['read_file', 'write_file', 'shell_exec'])).join() === 'write_file',
    );
  }

  // ── 5. replay integration: frames land in the run event log ───────────────
  {
    const bus = createRunBus(); // in-memory only; replay semantics identical
    const frames: PermissionCheckFrame[] = [];
    const reg = createToolApprovalRegistry({
      timeoutMs: 60_000,
      emit: (sid, event, frame) => {
        frames.push(frame);
        bus.emit(sid, event, frame);
      },
    });
    bus.claimStart('sess-replay');
    const p = reg.request(
      'sess-replay',
      { name: 'write_file', input: { path: 'b.md' } },
      'requires approval',
    );
    const requestId = frames[0]!.request_id;
    // Decision arrives later (as it would from POST /:id/permissions/:requestId).
    setTimeout(() => {
      reg.decide(requestId, 'approve', undefined, 'sess-replay');
    }, 10);
    const decision = await p;
    check('5. round trip resolved approve', decision === 'approve');
    bus.finish('sess-replay', 'succeeded');

    // Late-attached view (reconnect) replays EVERYTHING from cursor 0.
    const replayed: RunEventRecord[] = [];
    bus.attach('sess-replay', 0, {
      send: (rec) => replayed.push(rec),
      end: () => {},
    });
    const permEvents = replayed.filter((r) => r.event === 'permission_check');
    check(
      '5. both permission_check frames replayed (pending + decision)',
      permEvents.length === 2 &&
        (permEvents[0]!.data as PermissionCheckFrame).decision === 'pending' &&
        (permEvents[1]!.data as PermissionCheckFrame).decision === 'approve' &&
        permEvents[0]!.id < permEvents[1]!.id,
      JSON.stringify(permEvents.map((r) => (r.data as PermissionCheckFrame).decision)),
    );
  }

  _resetToolExecutorsForTests();
  console.log(`\ntool-runner-approval: ${pass} passed, ${fail} failed`);
  if (fail > 0) process.exit(1);
})().catch((err) => {
  console.error('tool-runner-approval test crashed:', err);
  process.exit(1);
});
