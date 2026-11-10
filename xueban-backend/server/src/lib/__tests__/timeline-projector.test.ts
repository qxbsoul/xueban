/**
 * timeline-projector.test.ts — projector unit tests (no framework).
 *
 * Run with:
 *   cd server
 *   npx tsx src/lib/__tests__/timeline-projector.test.ts
 *
 * Mirrors the intent-router.test.ts / classify-error.test.ts no-framework
 * pattern (the server package doesn't ship vitest). Coverage:
 *
 *   1. user_turn opens a turn (turn_id changes, prior msg_foot finalized)
 *   2. classify → assistant_meta (first only), msg_foot created
 *   3. step_panel — append_step on running, update_step on done
 *   4. step_panel id is STABLE across multiple steps (same turn)
 *   5. step_panel id RESETS on new turn
 *   6. agent-substep — append_substep / update_substep ordering
 *   7. thinking — open, chunks → append_body patches, blueprint closes it
 *   8. diff_panel — message first then patches for each yaml-line
 *   9. msg_foot patches accumulate as steps complete
 *  10. End-to-end: synthesizeTeamRun-style sequence produces consistent IDs
 *  11. onComplete finalizes thinking + msg_foot
 */

import { createTimelineProjector } from '../timeline-projector';
import type { MessagePatch, TimelineMessage } from '../contracts';

let pass = 0;
let fail = 0;

function check(label: string, expected: unknown, actual: unknown) {
  const eq = JSON.stringify(expected) === JSON.stringify(actual);
  if (eq) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(
      `  FAIL  ${label}\n        expected=${JSON.stringify(expected)}\n        actual  =${JSON.stringify(actual)}`,
    );
  }
}

function assert(label: string, cond: boolean) {
  if (cond) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.log(`  FAIL  ${label}`);
  }
}

// ── Test 1: user_turn opens a turn ───────────────────────────────────────────
console.log('\n[1] user_turn opens turn');
{
  const p = createTimelineProjector();
  const r1 = p.onUserMessage('帮我搭一个 BMAD team');
  // user_turn is the first message; status_line slot bump is the second.
  const userTurns = r1.messages.filter((m) => m.kind === 'user_turn');
  check('emit 1 user_turn message', 1, userTurns.length);
  check('first kind=user_turn', 'user_turn', r1.messages[0].kind);
  const ut = userTurns[0] as Extract<TimelineMessage, { kind: 'user_turn' }>;
  check('user_turn text preserved', '帮我搭一个 BMAD team', ut.text);
  assert('turn_id present', typeof userTurns[0].turn_id === 'string' && userTurns[0].turn_id.length > 0);
}

// ── Test 2: classify → assistant_meta + msg_foot ─────────────────────────────
console.log('\n[2] classify → assistant_meta + msg_foot');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  const r = p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.98, complexity: 4, source: 'ts' });
  check('emits 2 messages (assistant_meta + msg_foot)', 2, r.messages.length);
  check('first is assistant_meta', 'assistant_meta', r.messages[0].kind);
  check('second is msg_foot', 'msg_foot', r.messages[1].kind);
  const meta = r.messages[0] as Extract<TimelineMessage, { kind: 'assistant_meta' }>;
  assert('summary contains confidence', meta.summary.includes('0.98'));

  // Second classify is ignored (front-end keeps legacy event)
  const r2 = p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, source: 'llm' });
  check('second classify emits 0 messages', 0, r2.messages.length);
  check('second classify emits 0 patches', 0, r2.patches.length);
}

// ── Test 3: step_panel append_step on running, update_step on done ──────────
console.log('\n[3] step_panel patch lifecycle');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, complexity: 3, source: 'ts' });
  const r1 = p.onAssembleStart(0, '分析目标需求');
  // Expect: step_panel message (first time) + append_step patch
  check('first assemble emits step_panel message', 'step_panel', r1.messages[0]?.kind);
  const panelId = r1.messages[0]!.id;
  const appendPatch = r1.patches.find((x) => x.op === 'append_step') as Extract<MessagePatch, { op: 'append_step' }>;
  assert('append_step patch present', !!appendPatch);
  check('append_step targets the panel', panelId, appendPatch.id);
  check('append_step name', '分析目标需求', appendPatch.step.name);
  check('append_step status running', 'running', appendPatch.step.status);

  const r2 = p.onAssembleDone(0, 200);
  const updatePatch = r2.patches.find((x) => x.op === 'update_step') as Extract<MessagePatch, { op: 'update_step' }>;
  assert('update_step patch present', !!updatePatch);
  check('update_step targets same panel id (stable!)', panelId, updatePatch.id);
  check('update_step status done', 'done', updatePatch.patch.status);
  check('update_step elapsed', 200, updatePatch.patch.elapsed_ms);
}

// ── Test 4: step_panel id stable across multiple steps ──────────────────────
console.log('\n[4] step_panel id stable across steps');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, complexity: 3, source: 'ts' });
  const r1 = p.onAssembleStart(0, 'step A');
  const panelId = r1.messages[0]!.id;
  p.onAssembleDone(0, 100);
  const r2 = p.onAssembleStart(1, 'step B');
  // Second start should NOT emit a new step_panel message. (Status_line
  // slot bumps are unrelated singletons — exclude from this assertion.)
  check(
    'second step start emits 0 new step_panel messages',
    0,
    r2.messages.filter((m) => m.kind === 'step_panel').length,
  );
  const append2 = r2.patches.find((x) => x.op === 'append_step') as Extract<MessagePatch, { op: 'append_step' }>;
  check('append_step still targets original panel id', panelId, append2.id);
}

// ── Test 5: step_panel id resets on new turn ────────────────────────────────
console.log('\n[5] step_panel resets on new turn');
{
  const p = createTimelineProjector();
  p.onUserMessage('first');
  p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, complexity: 3, source: 'ts' });
  const r1 = p.onAssembleStart(0, 'A');
  const panel1 = r1.messages[0]!.id;
  const turn1 = r1.messages[0]!.turn_id;
  p.onAssembleDone(0, 100);

  p.onUserMessage('second');
  const r2 = p.onAssembleStart(0, 'A');
  const panel2 = r2.messages[0]!.id;
  const turn2 = r2.messages[0]!.turn_id;
  assert('new turn has different panel id', panel1 !== panel2);
  assert('new turn has different turn_id', turn1 !== turn2);
}

// ── Test 6: agent-substep append/update ─────────────────────────────────────
console.log('\n[6] agent-substep lifecycle');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, complexity: 3, source: 'ts' });
  p.onAssembleStart(2, '配置 Agent 角色');
  const r1 = p.onAgentSubstepStart('agent-pm', 'identity');
  const appendSub = r1.patches.find((x) => x.op === 'append_substep') as Extract<MessagePatch, { op: 'append_substep' }>;
  assert('append_substep patch emitted', !!appendSub);
  check('substep status running', 'running', appendSub.sub.status);
  check('substep name composed', 'agent-pm · identity', appendSub.sub.name);

  const r2 = p.onAgentSubstepDone('agent-pm', 'identity', 80);
  const updateSub = r2.patches.find((x) => x.op === 'update_substep') as Extract<MessagePatch, { op: 'update_substep' }>;
  assert('update_substep patch emitted', !!updateSub);
  check('update_substep status done', 'done', updateSub.patch.status);
  check('update_substep elapsed', 80, updateSub.patch.elapsed_ms);

  // Duplicate start is idempotent (no double append)
  const r3 = p.onAgentSubstepStart('agent-pm', 'identity');
  check('duplicate substep start emits 0 patches', 0, r3.patches.length);
}

// ── Test 7: thinking lifecycle ──────────────────────────────────────────────
console.log('\n[7] thinking lifecycle');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  const r1 = p.onThinkingChunk('Hmm, ');
  // status_line bumps are independent singletons — filter them out when
  // counting thinking message lifecycle events.
  const r1Thinking = r1.messages.filter((m) => m.kind === 'thinking');
  check('thinking opens a message', 1, r1Thinking.length);
  check('thinking message kind', 'thinking', r1Thinking[0].kind);
  const tid = r1Thinking[0]!.id;
  const tMsg = r1Thinking[0] as Extract<TimelineMessage, { kind: 'thinking' }>;
  check('thinking status streaming', 'streaming', tMsg.status);
  const appendBody = r1.patches.find((x) => x.op === 'thinking_append_body') as Extract<MessagePatch, { op: 'thinking_append_body' }>;
  assert('append_body patch present', !!appendBody);
  check('chunk preserved', 'Hmm, ', appendBody.chunk);

  const r2 = p.onThinkingChunk('let me think');
  check(
    'second chunk: no new thinking message',
    0,
    r2.messages.filter((m) => m.kind === 'thinking').length,
  );
  const appendBody2 = r2.patches.find((x) => x.op === 'thinking_append_body') as Extract<MessagePatch, { op: 'thinking_append_body' }>;
  check('second append_body targets same id', tid, appendBody2.id);

  // Blueprint closes thinking
  const r3 = p.onBlueprint({ filename: 'team.yml', yaml: 'name: x' });
  const finalize = r3.patches.find((x) => x.op === 'thinking_finalize') as Extract<MessagePatch, { op: 'thinking_finalize' }>;
  assert('thinking finalized on blueprint', !!finalize);
  check('finalize targets the open thinking id', tid, finalize.id);
}

// ── Test 8: diff_panel + yaml-line ──────────────────────────────────────────
console.log('\n[8] diff_panel + yaml-line stream');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, complexity: 3, source: 'ts' });
  const rb = p.onBlueprint({ filename: 'bmad-team.yml', yaml: '' });
  const diffMsg = rb.messages.find((x) => x.kind === 'diff_panel') as Extract<TimelineMessage, { kind: 'diff_panel' }>;
  assert('diff_panel message emitted', !!diffMsg);
  check('diff_panel filename', 'bmad-team.yml', diffMsg.filename);
  check('diff_panel lines empty initially', 0, diffMsg.lines.length);
  const diffId = diffMsg.id;

  const r1 = p.onYamlLine('name: bmad-team');
  const appendLine = r1.patches.find((x) => x.op === 'diff_append_line') as Extract<MessagePatch, { op: 'diff_append_line' }>;
  assert('first yaml line emits diff_append_line', !!appendLine);
  check('line targets diff_panel id', diffId, appendLine.id);
  check('line mark', '+', appendLine.line.mark);
  check('line no=1', 1, appendLine.line.no);
  check('line code', 'name: bmad-team', appendLine.line.code);

  const r2 = p.onYamlLine('mode: serial');
  const appendLine2 = r2.patches[0] as Extract<MessagePatch, { op: 'diff_append_line' }>;
  check('second line no=2', 2, appendLine2.line.no);
}

// ── Test 9: msg_foot patches accumulate ─────────────────────────────────────
console.log('\n[9] msg_foot tools count accumulates');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  const rc = p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, complexity: 3, source: 'ts' });
  const footMsg = rc.messages.find((x) => x.kind === 'msg_foot') as Extract<TimelineMessage, { kind: 'msg_foot' }>;
  const footId = footMsg.id;

  const rb = p.onBlueprint({ filename: 'team.yml' });
  const footPatch = rb.patches.find((x) => x.op === 'msg_foot_update') as Extract<MessagePatch, { op: 'msg_foot_update' }>;
  assert('blueprint patches msg_foot', !!footPatch);
  check('footPatch targets msg_foot id', footId, footPatch.id);
  check('tools=1 after first blueprint', 1, footPatch.patch.tools);
}

// ── Test 10: end-to-end synthesizeTeamRun-style sequence ───────────────────
console.log('\n[10] end-to-end sequence (simulates synthesizeTeamRun)');
{
  const p = createTimelineProjector();
  const allMessages: TimelineMessage[] = [];
  const allPatches: MessagePatch[] = [];
  const drain = (r: { messages: TimelineMessage[]; patches: MessagePatch[] }) => {
    allMessages.push(...r.messages);
    allPatches.push(...r.patches);
  };

  drain(p.onUserMessage('搭一个 BMAD team'));
  drain(p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.98, complexity: 4, source: 'ts' }));
  drain(p.onAssembleStart(0, '分析目标需求'));
  drain(p.onThinkingChunk('命中 skill team — 跳过 LLM'));
  drain(p.onAssembleDone(0, 200));
  drain(p.onAssembleStart(1, '挑选 Team 蓝图'));
  drain(p.onAssembleDone(1, 200));
  drain(p.onAssembleStart(2, '配置 Agent 角色'));
  for (const node of ['pm', 'arch', 'dev', 'qa']) {
    for (const sub of ['identity', 'persona', 'model', 'tools', 'memory']) {
      drain(p.onAgentSubstepStart(node, sub));
      drain(p.onAgentSubstepDone(node, sub, 80));
    }
  }
  drain(p.onAssembleDone(2, 1600));
  drain(p.onAssembleStart(3, '设置工具集'));
  drain(p.onBlueprint({ filename: 'bmad-team.yml', yaml: 'name: t' }));
  drain(p.onYamlLine('name: bmad-team'));
  drain(p.onYamlLine('mode: serial'));
  drain(p.onAssembleDone(3, 300));
  drain(p.onAssembleStart(4, 'Policy 协作规则'));
  drain(p.onAssembleDone(4, 200));
  drain(p.onComplete());

  // Invariants:
  const stepPanels = allMessages.filter((m) => m.kind === 'step_panel');
  check('exactly 1 step_panel message in the turn', 1, stepPanels.length);
  const diffPanels = allMessages.filter((m) => m.kind === 'diff_panel');
  check('exactly 1 diff_panel message', 1, diffPanels.length);
  const userTurns = allMessages.filter((m) => m.kind === 'user_turn');
  check('1 user_turn', 1, userTurns.length);
  const footMessages = allMessages.filter((m) => m.kind === 'msg_foot');
  check('1 msg_foot', 1, footMessages.length);

  // All messages share the same turn_id
  const turnIds = new Set(allMessages.map((m) => m.turn_id));
  check('all messages share one turn_id', 1, turnIds.size);

  // Substep patches all target the single step_panel id
  const subPatches = allPatches.filter((x) => x.op === 'append_substep' || x.op === 'update_substep');
  const panelId = stepPanels[0]!.id;
  const allTargetPanel = subPatches.every((x) => x.id === panelId);
  assert('all substep patches target the step_panel id', allTargetPanel);
  check('exactly 4 agents × 5 substeps = 20 append_substep', 20, allPatches.filter((x) => x.op === 'append_substep').length);
  check('exactly 20 update_substep patches', 20, allPatches.filter((x) => x.op === 'update_substep').length);

  // msg_foot status should end as done
  const finalFootPatches = allPatches.filter((x) => x.op === 'msg_foot_update') as Array<Extract<MessagePatch, { op: 'msg_foot_update' }>>;
  const lastFoot = finalFootPatches[finalFootPatches.length - 1];
  check('last msg_foot status=done', 'done', lastFoot.patch.status);

  // id uniqueness
  const ids = allMessages.map((m) => m.id);
  check('all message ids unique', ids.length, new Set(ids).size);
}

// ── Test 11: onComplete finalizes thinking + msg_foot ───────────────────────
console.log('\n[11] onComplete cleanup');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, complexity: 3, source: 'ts' });
  p.onThinkingChunk('analyzing');
  const r = p.onComplete();
  const finalize = r.patches.find((x) => x.op === 'thinking_finalize') as Extract<MessagePatch, { op: 'thinking_finalize' }>;
  assert('thinking finalized on complete', !!finalize);
  const footUpd = r.patches.find((x) => x.op === 'msg_foot_update') as Extract<MessagePatch, { op: 'msg_foot_update' }>;
  assert('msg_foot updated to done', footUpd?.patch.status === 'done');
}

// ── Test 12: onText accumulates into single assistant_text ─────────────────
// 2026-05-24 P0-1 — multiple onText chunks must batch into one
// assistant_text message via text_append patches, not 200 separate rows.
console.log('\n[12] onText accumulates into single assistant_text');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  const r1 = p.onText('Hello ');
  const at = r1.messages.find((m) => m.kind === 'assistant_text') as Extract<TimelineMessage, { kind: 'assistant_text' }>;
  assert('first text emits assistant_text message', !!at);
  check('assistant_text body = first chunk', 'Hello ', at.body);
  const atId = at.id;

  const r2 = p.onText('world');
  check('second text emits 0 new assistant_text messages', 0, r2.messages.filter((m) => m.kind === 'assistant_text').length);
  const appendPatch = r2.patches.find((x) => x.op === 'text_append') as Extract<MessagePatch, { op: 'text_append' }>;
  assert('text_append patch emitted on second chunk', !!appendPatch);
  check('text_append targets the same assistant_text id', atId, appendPatch.id);
  check('text_append chunk', 'world', appendPatch.chunk);

  const r3 = p.onText('!');
  const appendPatch2 = r3.patches.find((x) => x.op === 'text_append') as Extract<MessagePatch, { op: 'text_append' }>;
  check('third chunk patches same id', atId, appendPatch2.id);
}

// ── Test 13: thinking-chunk closes assistant_text → next text opens new ────
console.log('\n[13] thinking-chunk interrupts text accumulation');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  const r1 = p.onText('first segment ');
  const at1 = r1.messages.find((m) => m.kind === 'assistant_text') as Extract<TimelineMessage, { kind: 'assistant_text' }>;
  const id1 = at1.id;

  // Thinking arrives mid-stream — closes text accumulation.
  p.onThinkingChunk('hmm, reconsidering');

  // Next text run should open a NEW assistant_text message.
  const r2 = p.onText('second segment');
  const at2 = r2.messages.find((m) => m.kind === 'assistant_text') as Extract<TimelineMessage, { kind: 'assistant_text' }>;
  assert('thinking-then-text opens new assistant_text', !!at2);
  assert('new assistant_text has different id', id1 !== at2.id);
  check('new assistant_text body is the new chunk', 'second segment', at2.body);
}

// ── Test 14: assemble step boundary closes assistant_text ──────────────────
console.log('\n[14] assemble step boundary closes text run');
{
  const p = createTimelineProjector();
  p.onUserMessage('q');
  p.onText('plain answer ');
  // Stepping into a new assemble step should reset accumulation.
  p.onAssembleStart(0, 'next step');
  const r = p.onText('more answer');
  const ats = r.messages.filter((m) => m.kind === 'assistant_text');
  check('text after assemble opens new assistant_text', 1, ats.length);
  const appended = r.patches.filter((x) => x.op === 'text_append');
  check('no text_append for the first chunk after boundary', 0, appended.length);
}

// ── Test 15: status_line emitted by onText/onComplete/onUserMessage ─────────
// 2026-05-24 P0-3 — status_line slot was never populated; now bumped on key
// events so the always-on bottom bar has a verb to render.
console.log('\n[15] status_line slot populated');
{
  const p = createTimelineProjector();
  const r1 = p.onUserMessage('hi');
  const sl1 = r1.messages.find((m) => m.kind === 'status_line') as Extract<TimelineMessage, { kind: 'status_line' }>;
  assert('user turn emits status_line', !!sl1);
  check('initial verb', 'Thinking', sl1?.verb);

  const r2 = p.onText('answering');
  const sl2 = r2.messages.find((m) => m.kind === 'status_line') as Extract<TimelineMessage, { kind: 'status_line' }>;
  assert('text emits status_line', !!sl2);
  check('text verb', 'Writing', sl2?.verb);
  assert('non-terminal status_line not flagged terminal', !sl2?.terminal);

  const r3 = p.onComplete();
  const sl3 = r3.messages.find((m) => m.kind === 'status_line') as Extract<TimelineMessage, { kind: 'status_line' }>;
  assert('complete emits status_line', !!sl3);
  check('complete verb', 'Done', sl3?.verb);
  check('complete tools_running=0', 0, sl3?.tools_running);
  // Regression: footer ticker must freeze on completion. The terminal flag is
  // what tells StatusLine.tsx to stop counting ("Done for 409s" bug).
  check('complete status_line terminal=true', true, sl3?.terminal);
}

// ── Test 16: P3 — deterministic ids across reconnect (same idSeed) ──────────
// The SSE /stream handler re-runs the whole pipeline through a FRESH projector
// on every reconnect. With a stable idSeed, the SAME emit order must reproduce
// the IDENTICAL id sequence so the front-end dedups by id (no duplicate
// user_turn). Different seeds must NOT collide.
console.log('\n[16] P3 deterministic ids across reconnect');
{
  const drive = (seed: string): string[] => {
    const p = createTimelineProjector({ idSeed: seed });
    const ids: string[] = [];
    for (const m of p.onUserMessage('帮我创建一个开发工程师agent').messages) ids.push(m.id);
    for (const m of p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, complexity: 3, source: 'ts' }).messages) ids.push(m.id);
    for (const m of p.onAssembleStart(0, '分析目标需求').messages) ids.push(m.id);
    return ids;
  };
  const run1 = drive('sess_ABC');
  const run2 = drive('sess_ABC'); // simulated reconnect: same session id
  const runX = drive('sess_XYZ'); // different session

  assert('reconnect reproduces ≥1 id', run1.length > 0);
  check('reconnect: identical id sequence (same seed)', JSON.stringify(run1), JSON.stringify(run2));
  assert('first user_turn id stable across reconnect', run1[0] === run2[0]);
  assert('different seed → different ids', run1[0] !== runX[0]);
  assert('ids carry the seed (traceable)', run1[0].includes('sess_ABC'));
}

// ── Test 17: P-raw — onRaw emits a standalone raw message, closes open text ──
console.log('\n[17] onRaw → raw message (closes open text, never merges)');
{
  const p = createTimelineProjector({ idSeed: 'sess-raw' });
  p.onUserMessage('q');
  p.onText('正常回答开头');
  const r = p.onRaw('event: assemble\ndata: {"x":1}', 'sse-frame-leak');
  const raw = r.messages.find((m) => m.kind === 'raw') as
    | Extract<TimelineMessage, { kind: 'raw' }>
    | undefined;
  assert('emits a raw message', !!raw);
  check('raw body preserved', 'event: assemble\ndata: {"x":1}', raw?.body);
  check('raw source preserved', 'sse-frame-leak', raw?.source);
  // onRaw calls closeOpenText, so the NEXT onText opens a FRESH assistant_text
  // message (different id) — proving raw didn't merge into the prior answer.
  const before = p._debug();
  const r2 = p.onText('后续新回答');
  const newText = r2.messages.find((m) => m.kind === 'assistant_text');
  assert('raw closed the open text → next onText opens a new message', !!newText);
  void before;
  // Empty/whitespace raw is a no-op.
  const r3 = p.onRaw('   ');
  check('blank raw emits nothing', 0, r3.messages.filter((m) => m.kind === 'raw').length);
}

// ── Test 18: tool chain — onToolUse → tool_call, onToolResult → tool_echo ────
console.log('\n[18] onToolUse/onToolResult → tool_call + tool_echo');
{
  const p = createTimelineProjector({ idSeed: 'sess-tool' });
  p.onUserMessage('q');
  p.onText('我来跑个命令');
  const r1 = p.onToolUse('Bash', { command: 'pytest' });
  const call = r1.messages.find((m) => m.kind === 'tool_call') as
    | Extract<TimelineMessage, { kind: 'tool_call' }>
    | undefined;
  assert('emits tool_call', !!call);
  check('tool_call name', 'Bash', call?.name);
  assert('args_summary carries input', (call?.args_summary ?? '').includes('pytest'));
  // gap #2 — tool_call 起始 status:'running'
  check('tool_call starts running', 'running', call?.status);
  const r2 = p.onToolResult('All tests passed');
  const echo = r2.messages.find((m) => m.kind === 'tool_echo') as
    | Extract<TimelineMessage, { kind: 'tool_echo' }>
    | undefined;
  assert('emits tool_echo', !!echo);
  check('tool_echo body', 'All tests passed', echo?.body);
  // gap #2 — onToolResult 发 tool_finalize patch（ok + duration + summary）
  const fin = r2.patches.find((x) => x.op === 'tool_finalize') as
    | Extract<MessagePatch, { op: 'tool_finalize' }>
    | undefined;
  assert('emits tool_finalize patch', !!fin);
  check('finalize targets the tool_call', call?.id, fin?.id);
  check('finalize status ok', 'ok', fin?.status);
  assert('finalize has duration_ms', typeof fin?.duration_ms === 'number');
  check('finalize result_summary (single short line)', 'All tests passed', fin?.result_summary);
  // gap #2 — 错误结果 → status:'error'
  {
    const pe = createTimelineProjector({ idSeed: 'sess-tool-err' });
    pe.onUserMessage('q');
    pe.onToolUse('read_file', { path: 'x' });
    const re = pe.onToolResult('Error: ENOENT no such file');
    const finE = re.patches.find((x) => x.op === 'tool_finalize') as
      | Extract<MessagePatch, { op: 'tool_finalize' }>
      | undefined;
    check('error result → status error', 'error', finE?.status);
  }
  // blank result still finalizes the open tool_call, no tool_echo message
  {
    const pb = createTimelineProjector({ idSeed: 'sess-tool-blank' });
    pb.onUserMessage('q');
    pb.onToolUse('Bash', {});
    const rb = pb.onToolResult('  ');
    check('blank result → no tool_echo msg', 0, rb.messages.length);
    assert('blank result still finalizes', rb.patches.some((x) => x.op === 'tool_finalize'));
  }
}

// ── Test 19: usage chain — onUsage accumulates tokens onto msg_foot ─────────
console.log('\n[19] onUsage → msg_foot tokens accumulate');
{
  const p = createTimelineProjector({ idSeed: 'sess-usage' });
  p.onUserMessage('q');
  p.onClassify({ output_type: 'answer', mode: 'single', confidence: 0.9, source: 'ts' }); // opens msg_foot
  const r1 = p.onUsage({ input_tokens: 100, output_tokens: 50 });
  const patch1 = r1.patches.find((x) => x.op === 'msg_foot_update') as
    | Extract<MessagePatch, { op: 'msg_foot_update' }>
    | undefined;
  check('first usage → 150 tokens', 150, patch1?.patch.tokens);
  const r2 = p.onUsage({ output_tokens: 25 });
  const patch2 = r2.patches.find((x) => x.op === 'msg_foot_update') as
    | Extract<MessagePatch, { op: 'msg_foot_update' }>
    | undefined;
  check('second usage accumulates → 175', 175, patch2?.patch.tokens);
  // total_tokens wins when present
  const r3 = p.onUsage({ total_tokens: 10, input_tokens: 999 });
  const patch3 = r3.patches.find((x) => x.op === 'msg_foot_update') as
    | Extract<MessagePatch, { op: 'msg_foot_update' }>
    | undefined;
  check('total_tokens preferred → 185', 185, patch3?.patch.tokens);
}

// ── Test 20: O3 — lastAssistantTextId back-trace anchor ─────────────────────
// The stream handler stamps this id onto the persisted conversation row so the
// front-end can map a timeline assistant_text message → its conversation row.
console.log('\n[20] O3 lastAssistantTextId');
{
  const p = createTimelineProjector({ idSeed: 'sess-o3' });
  // No turn / no text yet → null.
  assert('null before any assistant_text', p.lastAssistantTextId() === null);

  p.onUserMessage('q');
  assert('null after user turn, before text', p.lastAssistantTextId() === null);

  // First contiguous text run opens an assistant_text; getter reports its id.
  const r1 = p.onText('answer part 1 ');
  const at1 = r1.messages.find((m) => m.kind === 'assistant_text') as Extract<TimelineMessage, { kind: 'assistant_text' }>;
  check('getter == first assistant_text id', at1.id, p.lastAssistantTextId());

  // Continuation (append patch, no new message) keeps the same id.
  p.onText('part 2');
  check('getter stable across text_append', at1.id, p.lastAssistantTextId());

  // A non-text event closes the run; getter STILL remembers the last id
  // (unlike the internal openAssistantTextId which is nulled).
  p.onToolUse('Bash', { command: 'ls' });
  check('getter survives closeOpenText (tool boundary)', at1.id, p.lastAssistantTextId());

  // A second text run opens a NEW assistant_text → getter advances to it.
  const r2 = p.onText('final answer');
  const at2 = r2.messages.find((m) => m.kind === 'assistant_text') as Extract<TimelineMessage, { kind: 'assistant_text' }>;
  assert('second text run has a new id', at1.id !== at2.id);
  check('getter advances to latest assistant_text', at2.id, p.lastAssistantTextId());

  // onComplete must not lose the anchor (finally reads it after drain).
  p.onComplete();
  check('getter survives onComplete', at2.id, p.lastAssistantTextId());

  // New turn resets the anchor.
  p.onUserMessage('next q');
  assert('getter reset on new turn', p.lastAssistantTextId() === null);
}

// ── Test: complete-before-usage ordering (answer mode) ─────────────────────
console.log('\n[16] complete before usage → late msg_foot born done');
{
  const p = createTimelineProjector();
  p.onUserMessage('用一句话介绍 Xueban');
  // answer 模式的真实管线：classify 不进 projector（daemon 门控），
  // 无 assemble/step 事件，complete 先于 usage 到达
  p.onComplete();
  const ru = p.onUsage({ input_tokens: 642, output_tokens: 194 });
  const foot = ru.messages.find((m) => m.kind === 'msg_foot') as
    | Extract<TimelineMessage, { kind: 'msg_foot' }>
    | undefined;
  assert('usage after complete still creates msg_foot', !!foot);
  check('late-created foot is done, not running', 'done', foot!.status);
  const footPatch = ru.patches.find((x) => x.op === 'msg_foot_update') as
    | Extract<MessagePatch, { op: 'msg_foot_update' }>
    | undefined;
  check('tokens still accumulated on the done foot', 836, footPatch?.patch.tokens);

  // 新 turn 复位：下一个问题的 foot 应恢复 running 出生
  p.onUserMessage('next');
  const rc = p.onClassify({ output_type: 'workflow', mode: 'team', confidence: 0.9, complexity: 3, source: 'ts' });
  const foot2 = rc.messages.find((m) => m.kind === 'msg_foot') as Extract<TimelineMessage, { kind: 'msg_foot' }>;
  check('new turn foot born running again', 'running', foot2.status);
}

// ── Summary ─────────────────────────────────────────────────────────────────
console.log(`\n${pass} passed, ${fail} failed`);
if (fail > 0) process.exit(1);
