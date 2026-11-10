/**
 * tool-approval.test.ts — 阶段 1 审批注册表单测。
 *
 * Run with:  npx tsx src/lib/__tests__/tool-approval.test.ts   (from server/)
 *
 * No framework — mirrors the surrounding lib/__tests__ pattern. Whole file
 * runs inside one async IIFE (CommonJS tsconfig, no top-level await).
 *
 * Coverage:
 *   1. request() emits the pending frame immediately (decision:'pending')
 *   2. decide('approve') settles → 'approve', decision frame decided_by:'user'
 *   3. decide('deny', reason) settles → 'deny', reason carried on the frame
 *   4. timeout settles fail-closed → 'deny', decided_by:'timeout'
 *   5. pre-aborted signal settles → 'deny', decided_by:'abort'
 *   6. double decide → second returns false; unknown id → false
 *   7. session mismatch (expectedSession) → false, request stays pending
 *   8. listPending filters by session; settled requests drop out
 */

import {
  createToolApprovalRegistry,
  type PermissionCheckFrame,
} from '../tool-approval';

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

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// 注册表的超时/中止定时器全部 unref（生产纪律：审批等待不得阻塞进程退出，
// 与 run-event-bus 的 cleanup 定时器同款）。副作用：单测的 await 期间事件
// 循环里没有其它 ref，进程会在超时触发前退出——用 keepalive 撑住整个文件。
const keepalive = setInterval(() => {}, 1_000_000);

(async () => {
  // ── 1. pending frame emitted immediately ──────────────────────────────────
  {
    const frames: Array<{ sid: string; frame: PermissionCheckFrame }> = [];
    const reg = createToolApprovalRegistry({
      timeoutMs: 60_000,
      emit: (sid, _ev, frame) => frames.push({ sid, frame }),
    });
    const p = reg.request('s1', { name: 'write_file', input: { path: 'a.md' } }, 'requires approval');
    await sleep(5); // let the pending frame emit synchronously
    check(
      '1. pending frame emitted before decision',
      frames.length === 1 &&
        frames[0]!.sid === 's1' &&
        frames[0]!.frame.decision === 'pending' &&
        frames[0]!.frame.tool_name === 'write_file' &&
        frames[0]!.frame.mode === 'prompt' &&
        typeof frames[0]!.frame.request_id === 'string',
      JSON.stringify(frames),
    );
    check(
      '1. listPending shows the parked request',
      reg.listPending('s1').length === 1 && reg.listPending('s2').length === 0,
    );
    check('1. decide approve settles', reg.decide(frames[0]!.frame.request_id, 'approve', undefined, 's1'));
    check('1. resolved approve', (await p) === 'approve');
    check(
      '1. decision frame: approve / user',
      frames.length === 2 &&
        frames[1]!.frame.decision === 'approve' &&
        frames[1]!.frame.decided_by === 'user' &&
        frames[1]!.frame.request_id === frames[0]!.frame.request_id,
    );
    check('1. settled request drops from listPending', reg.listPending('s1').length === 0);
  }

  // ── 2. deny with reason ───────────────────────────────────────────────────
  {
    const frames: PermissionCheckFrame[] = [];
    const reg = createToolApprovalRegistry({
      timeoutMs: 60_000,
      emit: (_sid, _ev, frame) => frames.push(frame),
    });
    const p = reg.request('s1', { name: 'edit_file' }, 'requires approval');
    const id = frames[0]!.request_id;
    reg.decide(id, 'deny', 'not today', 's1');
    check('2. resolved deny', (await p) === 'deny');
    check(
      '2. decision frame carries user reason',
      frames[1]!.decision === 'deny' &&
        frames[1]!.decision_reason === 'not today',
    );
  }

  // ── 3. timeout → fail-closed deny ─────────────────────────────────────────
  {
    const frames: PermissionCheckFrame[] = [];
    const reg = createToolApprovalRegistry({
      timeoutMs: 30,
      emit: (_sid, _ev, frame) => frames.push(frame),
    });
    const t0 = Date.now();
    const decision = await reg.request('s1', { name: 'shell_exec' }, 'requires approval');
    check('3. timeout settles deny', decision === 'deny');
    check(
      '3. decision frame: timeout',
      frames[1]!.decision === 'deny' && frames[1]!.decided_by === 'timeout',
    );
    check('3. settled fast (timer fired, not 120s default)', Date.now() - t0 < 5_000);
  }

  // ── 4. pre-aborted signal ─────────────────────────────────────────────────
  {
    const frames: PermissionCheckFrame[] = [];
    const reg = createToolApprovalRegistry({
      timeoutMs: 60_000,
      emit: (_sid, _ev, frame) => frames.push(frame),
    });
    const ac = new AbortController();
    ac.abort();
    const decision = await reg.request('s1', { name: 'write_file' }, 'requires approval', ac.signal);
    check('4. pre-aborted signal settles deny', decision === 'deny');
    check(
      '4. decision frame: abort',
      frames[1]!.decision === 'deny' && frames[1]!.decided_by === 'abort',
    );
  }

  // ── 5. abort mid-wait ─────────────────────────────────────────────────────
  {
    const reg = createToolApprovalRegistry({ timeoutMs: 60_000 });
    const ac = new AbortController();
    const p = reg.request('s1', { name: 'write_file' }, 'requires approval', ac.signal);
    setTimeout(() => ac.abort(), 10);
    check('5. mid-wait abort settles deny', (await p) === 'deny');
  }

  // ── 6. double decide / unknown id ─────────────────────────────────────────
  {
    const reg = createToolApprovalRegistry({ timeoutMs: 60_000 });
    const p = reg.request('s1', { name: 'write_file' }, 'requires approval');
    const id = reg.listPending('s1')[0]!.request_id;
    check('6. first decide ok', reg.decide(id, 'approve'));
    check('6. second decide rejected', reg.decide(id, 'deny') === false);
    check('6. unknown id rejected', reg.decide('nope', 'approve') === false);
    check('6. original promise unaffected (approve)', (await p) === 'approve');
  }

  // ── 7. session mismatch ───────────────────────────────────────────────────
  {
    const reg = createToolApprovalRegistry({ timeoutMs: 60_000 });
    const p = reg.request('s1', { name: 'write_file' }, 'requires approval');
    const id = reg.listPending('s1')[0]!.request_id;
    check('7. cross-session decide rejected', reg.decide(id, 'approve', undefined, 's-other') === false);
    check('7. request still pending after mismatch', reg.listPending('s1').length === 1);
    reg.decide(id, 'deny', undefined, 's1');
    check('7. correct session still settles', (await p) === 'deny');
  }

  // ── 8. emitter wiring via setEmitter (routes pattern) ─────────────────────
  {
    const reg = createToolApprovalRegistry({ timeoutMs: 60_000 });
    let wired = 0;
    reg.setEmitter(() => {
      wired++;
    });
    const p = reg.request('s1', { name: 'write_file' }, 'requires approval');
    const id = reg.listPending('s1')[0]!.request_id;
    check('8. setEmitter applies to pending frame', wired === 1);
    reg.decide(id, 'approve');
    await p;
    check('8. setEmitter applies to decision frame', wired === 2);
  }

  console.log(`\ntool-approval: ${pass} passed, ${fail} failed`);
  clearInterval(keepalive);
  if (fail > 0) process.exit(1);
})().catch((err) => {
  console.error('tool-approval test crashed:', err);
  clearInterval(keepalive);
  process.exit(1);
});
