/**
 * parser.test.ts — standalone smoke test for parseAndExtract (Story 15.2)
 *
 * Run with:  npx tsx src/parser.test.ts   (from server/)
 *
 * No external test framework — vitest/jest are not yet installed in the server
 * package. Each `check` prints PASS or FAIL and increments counters; the
 * process exits non-zero if any check fails.
 */

import { parseAndExtract, type SseEvent } from './parser';

let passCount = 0;
let failCount = 0;
const captured: Array<{ filename: string; content: string; type: string }> = [];

const noopArtifact = (filename: string, content: string, type: string) => {
  captured.push({ filename, content, type });
};

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

function findEvent(events: SseEvent[], name: string): SseEvent | undefined {
  return events.find(e => e.event === name);
}

function countEvent(events: SseEvent[], name: string): number {
  return events.filter(e => e.event === name).length;
}

// ─── Test 1: classify tag ────────────────────────────────────────────────────

(function testClassify() {
  console.log('\n[1] sf:classify');
  const input = '<sf:classify output_type="report" mode="single" confidence="0.92" complexity="3"/>';
  const { buffer, events } = parseAndExtract(input, 'sess-1', noopArtifact);
  check('buffer drained', buffer.trim() === '', { buffer });
  check('exactly 1 classify event', countEvent(events, 'classify') === 1);
  const e = findEvent(events, 'classify');
  const d = e?.data as Record<string, unknown> | undefined;
  check('output_type=report', d?.output_type === 'report');
  check('mode=single', d?.mode === 'single');
  check('confidence parsed as number', d?.confidence === 0.92, { val: d?.confidence });
  check('complexity parsed as int', d?.complexity === 3);
})();

// ─── Test 2: sf:step status running/done ─────────────────────────────────────

(function testStep() {
  console.log('\n[2] sf:step status');
  const input = `
    <sf:step name="分析目标需求" status="running"/>
    <sf:step name="分析目标需求" status="done" elapsed_ms="1800"/>
  `;
  const { events } = parseAndExtract(input, 'sess-2', noopArtifact);
  check('2 assemble events', countEvent(events, 'assemble') === 2);
  const running = events.find(e => e.event === 'assemble' && (e.data as Record<string, unknown>).status === 'running');
  const done = events.find(e => e.event === 'assemble' && (e.data as Record<string, unknown>).status === 'done');
  check('running event has step name', (running?.data as Record<string, unknown>)?.step === '分析目标需求');
  check('done event has elapsed_ms=1800', (done?.data as Record<string, unknown>)?.elapsed_ms === 1800);
})();

// ─── Test 3: sf:node attrs ───────────────────────────────────────────────────

(function testNode() {
  console.log('\n[3] sf:node attributes');
  const input =
    '<sf:node id="coord-1" type="coordinator" title="项目协调者" sub="claude-sonnet-4-6" chips="orchestrate,plan,review" avatar_char="协"/>';
  const { events } = parseAndExtract(input, 'sess-3', noopArtifact);
  const e = findEvent(events, 'node');
  const d = e?.data as Record<string, unknown> | undefined;
  check('node_id=coord-1', d?.node_id === 'coord-1');
  check('type=coordinator', d?.type === 'coordinator');
  check('title=项目协调者', d?.title === '项目协调者');
  check('sub=claude-sonnet-4-6', d?.sub === 'claude-sonnet-4-6');
  check('chips array of 3', Array.isArray(d?.chips) && (d?.chips as unknown[]).length === 3, { chips: d?.chips });
  check('chips[0]=orchestrate', (d?.chips as string[])?.[0] === 'orchestrate');
  check('avatar_char=协', d?.avatar_char === '协');
  check('default status=building', d?.status === 'building');
})();

// ─── Test 4: sf:edge ─────────────────────────────────────────────────────────

(function testEdge() {
  console.log('\n[4] sf:edge');
  const input = '<sf:edge from="coord-1" to="agent-1"/>';
  const { events } = parseAndExtract(input, 'sess-4', noopArtifact);
  const e = findEvent(events, 'edge');
  const d = e?.data as Record<string, unknown> | undefined;
  check('from=coord-1', d?.from === 'coord-1');
  check('to=agent-1', d?.to === 'agent-1');
  check('status=active', d?.status === 'active');
})();

// ─── Test 5: artifact yaml — close + write file + yaml-line × N ─────────────

(function testArtifactYaml() {
  console.log('\n[5] <artifact type="yaml"> close → blueprint + yaml-line + callback');
  captured.length = 0;
  const yaml = `name: demo\nversion: "1.0"\nagents:\n  - id: coord-1\n    type: coordinator`;
  const input = `<artifact type="yaml" filename="team_blueprint.yml">\n${yaml}\n</artifact>`;
  const { events } = parseAndExtract(input, 'sess-5', noopArtifact);
  check('artifact callback fired exactly once', captured.length === 1);
  check('callback filename=team_blueprint.yml', captured[0]?.filename === 'team_blueprint.yml');
  check('callback type=yaml', captured[0]?.type === 'yaml');
  check('callback content trimmed', captured[0]?.content === yaml);

  const bp = findEvent(events, 'blueprint');
  const bd = bp?.data as Record<string, unknown> | undefined;
  check('blueprint event exists', !!bp);
  check('blueprint.yaml = trimmed content', bd?.yaml === yaml);
  check('blueprint.filename', bd?.filename === 'team_blueprint.yml');
  check('blueprint.artifact_type=yaml', bd?.artifact_type === 'yaml');
  check('blueprint.artifact_url path', bd?.artifact_url === '/projects/sess-5/team_blueprint.yml');

  const expectedLines = yaml.split('\n').length; // 5
  check(`yaml-line events count = ${expectedLines}`, countEvent(events, 'yaml-line') === expectedLines, { got: countEvent(events, 'yaml-line') });
  const firstLine = events.find(e => e.event === 'yaml-line');
  check('yaml-line[0].line=name: demo', (firstLine?.data as Record<string, unknown>)?.line === 'name: demo');
  check('yaml-line.total_lines = expected', (firstLine?.data as Record<string, unknown>)?.total_lines === expectedLines);
})();

// ─── Test 6: artifact html — write + blueprint, no yaml-line ─────────────────

(function testArtifactHtml() {
  console.log('\n[6] <artifact type="html"> close → blueprint only (no yaml-line)');
  captured.length = 0;
  const html = '<!DOCTYPE html>\n<html><body>hi</body></html>';
  const input = `<artifact type="html" filename="prototype.html">${html}</artifact>`;
  const { events } = parseAndExtract(input, 'sess-6', noopArtifact);
  check('callback fired', captured.length === 1);
  check('html written', captured[0]?.type === 'html');
  check('blueprint event exists', countEvent(events, 'blueprint') === 1);
  check('no yaml-line events', countEvent(events, 'yaml-line') === 0);
  const bp = findEvent(events, 'blueprint');
  const bd = bp?.data as Record<string, unknown> | undefined;
  check('artifact_url for html', bd?.artifact_url === '/projects/sess-6/prototype.html');
})();

// ─── Test 7: incomplete artifact stays in buffer ─────────────────────────────

(function testIncompleteArtifact() {
  console.log('\n[7] open <artifact> without close → stays in buffer');
  captured.length = 0;
  const input = '<artifact type="yaml" filename="x.yml">\nname: open';
  const { buffer, events } = parseAndExtract(input, 'sess-7', noopArtifact);
  check('callback NOT fired (still open)', captured.length === 0);
  check('no blueprint event', countEvent(events, 'blueprint') === 0);
  check('buffer still contains artifact opener', buffer.includes('<artifact'));
})();

// ─── Test 8: complete tag ────────────────────────────────────────────────────

(function testComplete() {
  console.log('\n[8] sf:complete');
  const input = '<sf:complete redirect="/editor"/>';
  const { events } = parseAndExtract(input, 'sess-8', noopArtifact);
  const c = findEvent(events, 'complete');
  const d = c?.data as Record<string, unknown> | undefined;
  check('complete event fired', !!c);
  check('redirect=/editor', d?.redirect === '/editor');
  check('session_id propagated', d?.session_id === 'sess-8');
  check('run_id derived from session', d?.run_id === 'run-sess-8');
})();

// ─── Test 9: complete with no attrs ──────────────────────────────────────────

(function testCompleteNoAttrs() {
  console.log('\n[9] sf:complete with no attrs → default redirect');
  const input = '<sf:complete/>';
  const { events } = parseAndExtract(input, 'abc12345-9876-5432-1098-543210987654', noopArtifact);
  const c = findEvent(events, 'complete');
  const d = c?.data as Record<string, unknown> | undefined;
  check('default redirect path', typeof d?.redirect === 'string' && (d?.redirect as string).includes('/editor?session='));
})();

// ─── Test 10: full E2E sample (a realistic streamed buffer) ─────────────────

(function testFullSample() {
  console.log('\n[10] full E2E sample');
  captured.length = 0;
  const sample = `
<sf:classify output_type="workflow" mode="team" confidence="0.9" complexity="3"/>
<sf:step name="分析目标需求" status="running"/>
<sf:step name="分析目标需求" status="done" elapsed_ms="1800"/>
<sf:step name="规划 Agent 角色结构" status="running"/>
<sf:node id="coord-1" type="coordinator" title="协调者" sub="sonnet" chips="plan" avatar_char="协"/>
<sf:node id="agent-1" type="agent" title="执行专家" sub="haiku" chips="exec" avatar_char="执"/>
<sf:edge from="coord-1" to="agent-1"/>
<sf:step name="规划 Agent 角色结构" status="done" elapsed_ms="2400"/>
<artifact type="yaml" filename="team_blueprint.yml">
name: demo
version: "1.0"
</artifact>
<sf:complete redirect="/editor"/>
  `;
  const { events } = parseAndExtract(sample, 'sess-10', noopArtifact);
  check('classify=1', countEvent(events, 'classify') === 1);
  check('assemble=4', countEvent(events, 'assemble') === 4);
  check('node=2', countEvent(events, 'node') === 2);
  check('edge=1', countEvent(events, 'edge') === 1);
  check('blueprint=1', countEvent(events, 'blueprint') === 1);
  check('yaml-line>=2', countEvent(events, 'yaml-line') >= 2);
  check('complete=1', countEvent(events, 'complete') === 1);
  check('artifact written', captured.length === 1 && captured[0].filename === 'team_blueprint.yml');
})();

// ─── Test 11: Phase 2 A4 — node_id on chunk events when supplied ────────────
//
// Topological parallel DAG emits multiple agents' chunks concurrently. The
// front-end needs `node_id` on every chunk to route to the right AgentDetail
// panel. parseAndExtract() now accepts an optional `currentNodeId` 4th arg
// which it stamps onto text / assemble / thinking-chunk / question-form
// events. agent-substep already carries the LLM-emitted node_id but falls
// back to currentNodeId when the tag attribute is empty.

(function testNodeIdPropagation() {
  console.log('\n[11] Phase 2 A4 — currentNodeId propagated to chunk events');
  const input = `
<sf:step name="规划" status="running" output_kind="none"/>
<sf:thinking>思考中</sf:thinking>
<sf:question-form id="q1" title="确认范围">{"questions":[]}</sf:question-form>
plain natural-language reply
<sf:agent-substep substep="persona" status="running"/>
`;
  const { events } = parseAndExtract(input, 'sess-11', noopArtifact, 'agent-reader');

  const txt = findEvent(events, 'text');
  check('text event carries node_id=agent-reader',
    (txt?.data as Record<string, unknown>)?.node_id === 'agent-reader',
    { data: txt?.data });

  const asm = findEvent(events, 'assemble');
  check('assemble event carries node_id=agent-reader',
    (asm?.data as Record<string, unknown>)?.node_id === 'agent-reader',
    { data: asm?.data });

  const think = findEvent(events, 'thinking-chunk');
  check('thinking-chunk carries node_id=agent-reader',
    (think?.data as Record<string, unknown>)?.node_id === 'agent-reader',
    { data: think?.data });

  const q = findEvent(events, 'question-form');
  check('question-form carries node_id=agent-reader',
    (q?.data as Record<string, unknown>)?.node_id === 'agent-reader',
    { data: q?.data });

  const sub = findEvent(events, 'agent-substep');
  check('agent-substep (no tag attr) falls back to currentNodeId',
    (sub?.data as Record<string, unknown>)?.node_id === 'agent-reader',
    { data: sub?.data });
})();

// ─── Test 12: undefined currentNodeId → no node_id key (backward compat) ────

(function testNodeIdBackwardCompat() {
  console.log('\n[12] Phase 2 A4 — undefined nodeId → events omit node_id (legacy)');
  const input = `
<sf:step name="规划" status="running" output_kind="none"/>
<sf:thinking>思考中</sf:thinking>
plain reply
`;
  const { events } = parseAndExtract(input, 'sess-12', noopArtifact);

  const txt = findEvent(events, 'text');
  const txtData = txt?.data as Record<string, unknown>;
  check('text event has no node_id key when caller omits',
    !('node_id' in (txtData ?? {})),
    { data: txtData });

  const asm = findEvent(events, 'assemble');
  const asmData = asm?.data as Record<string, unknown>;
  check('assemble has no node_id key when caller omits',
    !('node_id' in (asmData ?? {})),
    { data: asmData });

  const think = findEvent(events, 'thinking-chunk');
  const thinkData = think?.data as Record<string, unknown>;
  check('thinking-chunk has no node_id key when caller omits',
    !('node_id' in (thinkData ?? {})),
    { data: thinkData });
})();

// ─── Test 13: agent-substep tag attribute wins over currentNodeId ───────────
//
// When the LLM explicitly emits <sf:agent-substep node_id="X"/> the tag's
// attribute is authoritative — orchestrator's currentNodeId must NOT
// override it. This matters when an orchestrator multiplexes multiple
// agents and the LLM voluntarily tags chunks with the precise node.

(function testAgentSubstepTagWins() {
  console.log('\n[13] agent-substep tag attribute wins over currentNodeId');
  const input = '<sf:agent-substep node_id="agent-reviewer" substep="model" status="done"/>';
  const { events } = parseAndExtract(input, 'sess-13', noopArtifact, 'agent-router');
  const sub = findEvent(events, 'agent-substep');
  check('tag attr node_id=agent-reviewer (not agent-router)',
    (sub?.data as Record<string, unknown>)?.node_id === 'agent-reviewer',
    { data: sub?.data });
})();

// ─── Test 14: separate parse calls with different node ids don't bleed ──────

(function testNoBleedAcrossCalls() {
  console.log('\n[14] separate parse calls with different nodeIds — no state bleed');
  // Two distinct parser invocations representing two parallel-DAG nodes
  // emitting chunks concurrently. Each call carries its own currentNodeId;
  // the chunks must be tagged accordingly. The parser is per-session-stateful
  // but the node_id MUST NOT persist across calls.
  const r1 = parseAndExtract('alpha reply', 'sess-14', noopArtifact, 'agent-A');
  const r2 = parseAndExtract('beta reply', 'sess-14', noopArtifact, 'agent-B');
  const r3 = parseAndExtract('gamma reply', 'sess-14', noopArtifact); // no nodeId

  const t1 = findEvent(r1.events, 'text');
  const t2 = findEvent(r2.events, 'text');
  const t3 = findEvent(r3.events, 'text');
  check('call1 text → node_id=agent-A',
    (t1?.data as Record<string, unknown>)?.node_id === 'agent-A');
  check('call2 text → node_id=agent-B',
    (t2?.data as Record<string, unknown>)?.node_id === 'agent-B');
  check('call3 text (no nodeId arg) → no node_id key',
    !('node_id' in ((t3?.data as Record<string, unknown>) ?? {})),
    { data: t3?.data });
})();

// ─── Test 15: P1 — <tool_use> XML is NO LONGER extracted (flows as text) ────
// Tools are now structured TurnChunks (ConversationRuntime / ApiClientCallable
// → pipeChunksToSse), never re-parsed out of text. So a literal <tool_use>
// wrapper in the text stream passes through as a plain `text` event.

(function testToolUseNotExtracted() {
  console.log('\n[15] P1 <tool_use> wrapper flows as text (no tool-use event)');
  const input =
    '<tool_use name="Bash" id="toolu_01ABC">\n{"command":"ls","description":"list"}\n</tool_use>';
  const { buffer, events } = parseAndExtract(input, 'sess-15', noopArtifact);
  check('buffer drained', buffer.trim() === '', { buffer });
  check('NO tool-use event extracted from text', countEvent(events, 'tool-use') === 0);
  const t = findEvent(events, 'text');
  check(
    'wrapper passes through as text',
    typeof (t?.data as Record<string, unknown>)?.text === 'string' &&
      ((t?.data as Record<string, unknown>)?.text as string).includes('<tool_use name="Bash"'),
    { t },
  );
})();

// ─── Test 16: P1 — <tool_result> XML is NO LONGER extracted (flows as text) ─

(function testToolResultNotExtracted() {
  console.log('\n[16] P1 <tool_result> flows as text (no tool-result event)');
  const input =
    '<tool_result for="toolu_01ABC">file1.txt\nfile2.txt</tool_result>';
  const { buffer, events } = parseAndExtract(input, 'sess-16', noopArtifact);
  check('buffer drained', buffer.trim() === '', { buffer });
  check('NO tool-result event extracted', countEvent(events, 'tool-result') === 0);
  const t = findEvent(events, 'text');
  check(
    'result passes through as text',
    typeof (t?.data as Record<string, unknown>)?.text === 'string' &&
      ((t?.data as Record<string, unknown>)?.text as string).includes('<tool_result for='),
    { t },
  );
})();

// ─── Test 17: P1 — <function_calls> block is NO LONGER extracted ────────────

(function testFunctionCallsNotExtracted() {
  console.log('\n[17] P1 <function_calls> flows as text (no tool-use event)');
  const input =
    '<function_calls>\n<invoke name="Read"><parameter name="path">/tmp/x</parameter></invoke>\n</function_calls>';
  const { buffer, events } = parseAndExtract(input, 'sess-17', noopArtifact);
  check('buffer drained', buffer.trim() === '', { buffer });
  check('NO tool-use event extracted', countEvent(events, 'tool-use') === 0);
  const t = findEvent(events, 'text');
  check(
    'block passes through as text',
    typeof (t?.data as Record<string, unknown>)?.text === 'string' &&
      ((t?.data as Record<string, unknown>)?.text as string).includes('<function_calls>'),
    { t },
  );
})();

// ─── Test 18: P1 — a <tool_use prefix is no longer specially held back ──────
// findPartialTagStart dropped the tool prefixes, so a mid-stream <tool_use
// fragment is NOT buffered waiting for a close — it just flows as text. The
// only tags still held back are <sf:* / <artifact.

(function testToolUseNotHeldBack() {
  console.log('\n[18] P1 <tool_use fragment is not held back (flows as text)');
  const input1 = 'some text before <tool_use name="Bash"';
  const { buffer: b1, events: e1 } = parseAndExtract(
    input1,
    'sess-18',
    noopArtifact,
  );
  check('no tool-use event', countEvent(e1, 'tool-use') === 0);
  // The whole fragment flows as text; nothing is held in the buffer for tools.
  check('buffer does NOT hold the <tool_use fragment', !b1.includes('<tool_use'), { b1 });
  const t1 = findEvent(e1, 'text');
  check(
    'fragment emitted as text including the <tool_use literal',
    typeof (t1?.data as Record<string, unknown>)?.text === 'string' &&
      ((t1?.data as Record<string, unknown>)?.text as string).includes('<tool_use name="Bash"'),
    { e1 },
  );
})();

// ─── Test 19: P4 — leaked SSE wire frames stripped, not rendered as text ────

(function testSseFrameLeakStripped() {
  console.log('\n[19] P4 leaked SSE frames (event:/data:) stripped from text');
  const input =
    'event: assemble\ndata: {"step":"分析目标需求","status":"running"}\n\n' +
    'event: assemble\ndata: {"step":"分析目标需求","status":"done"}\n\n';
  const { buffer, events } = parseAndExtract(input, 'sess-19', noopArtifact);
  const textEvents = events.filter((e) => e.event === 'text');
  const leaked = textEvents.some((e) =>
    String((e.data as Record<string, unknown>)?.text ?? '').includes('event: assemble'),
  );
  check('no SSE frame leaks into text', !leaked, { textEvents });
  check('buffer holds no residual frame', !buffer.includes('event: assemble'), { buffer });
  // T3: leaked frames are now surfaced as a `raw` event (not silently dropped).
  const rawEv = findEvent(events, 'raw');
  check('emits a raw event', !!rawEv, { events });
  check(
    'raw event source = sse-frame-leak',
    (rawEv?.data as Record<string, unknown>)?.source === 'sse-frame-leak',
    { rawEv },
  );
  check(
    'raw body carries the frame text',
    String((rawEv?.data as Record<string, unknown>)?.text ?? '').includes('event: assemble'),
    { rawEv },
  );
})();

// ─── Test 20: P4 — normal prose mentioning "event:" is NOT stripped ─────────

(function testProseWithEventWordPreserved() {
  console.log('\n[20] P4 normal prose with "event:" preserved (tight match)');
  // No `data: {` follow-on → must NOT be treated as an SSE frame.
  const input = 'The click event: when the user taps the button, we log it.';
  const { events } = parseAndExtract(input, 'sess-20', noopArtifact);
  const txt = String(
    (findEvent(events, 'text')?.data as Record<string, unknown>)?.text ?? '',
  );
  check('prose preserved verbatim', txt === input, { txt });
})();

// ─── Summary ─────────────────────────────────────────────────────────────────

console.log('\n────────────────────────────────────────');
console.log(`  ${passCount} passed,  ${failCount} failed`);
console.log('────────────────────────────────────────\n');

if (failCount > 0) process.exit(1);
