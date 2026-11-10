/**
 * parser-agent-extension.test.ts — 2026-05-18 agent-B extension
 *
 * Verifies that the new <sf:node> attributes (model / memory / tools_picked /
 * tools_candidate / persona) and the paired <sf:agent-persona> block parse
 * correctly. All new fields are OPTIONAL — legacy <sf:node> tags must still
 * parse without errors.
 *
 * Run with:  npx tsx src/parser-agent-extension.test.ts   (from server/)
 *
 * Same style as parser.test.ts (no test framework — plain checks + exit code).
 */

import { parseAndExtract, type SseEvent } from './parser';

let passCount = 0;
let failCount = 0;
const noopArtifact = (_f: string, _c: string, _t: string) => {};

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

function findAllEvents(events: SseEvent[], name: string): SseEvent[] {
  return events.filter(e => e.event === name);
}

// ─── Test 1: legacy <sf:node> (no new attrs) — backward compat ──────────────

(function testLegacyNode() {
  console.log('\n[1] legacy <sf:node> still parses (no new attrs)');
  const input = '<sf:node id="a1" type="agent" title="助手" sub="haiku" chips="plan,exec" avatar_char="助"/>';
  const { events } = parseAndExtract(input, 'sess-1', noopArtifact);
  const e = findEvent(events, 'node');
  const d = e?.data as Record<string, unknown> | undefined;
  check('node event emitted', !!e);
  check('node_id=a1', d?.node_id === 'a1');
  check('legacy chips preserved', Array.isArray(d?.chips) && (d?.chips as string[]).length === 2);
  check('model is undefined when absent', d?.model === undefined);
  check('memory is undefined when absent', d?.memory === undefined);
  check('tools_picked is undefined when absent', d?.tools_picked === undefined);
  check('tools_candidate is undefined when absent', d?.tools_candidate === undefined);
  check('persona is undefined when absent', d?.persona === undefined);
})();

// ─── Test 2: <sf:node> with all new attrs ───────────────────────────────────

(function testNodeWithExtensions() {
  console.log('\n[2] <sf:node> with model / memory / tools_picked / tools_candidate');
  const input =
    '<sf:node id="pm" type="coordinator" title="产品经理" sub="规划" chips="claude,plan" avatar_char="产" ' +
    'model="claude-sonnet-4-6" memory="vector+scratch" ' +
    'tools_picked="web_search,doc_writer" tools_candidate="jira,figma_reader"/>';
  const { events } = parseAndExtract(input, 'sess-2', noopArtifact);
  const e = findEvent(events, 'node');
  const d = e?.data as Record<string, unknown> | undefined;
  check('model=claude-sonnet-4-6', d?.model === 'claude-sonnet-4-6');
  check('memory=vector+scratch', d?.memory === 'vector+scratch');
  check('tools_picked is string[]', Array.isArray(d?.tools_picked));
  check('tools_picked length=2', (d?.tools_picked as string[])?.length === 2);
  check('tools_picked[0]=web_search', (d?.tools_picked as string[])?.[0] === 'web_search');
  check('tools_candidate length=2', (d?.tools_candidate as string[])?.length === 2);
  check('tools_candidate[1]=figma_reader', (d?.tools_candidate as string[])?.[1] === 'figma_reader');
})();

// ─── Test 3: short single-line persona attribute ────────────────────────────

(function testInlinePersona() {
  console.log('\n[3] inline persona="..." attribute');
  const input =
    '<sf:node id="qa" type="agent" title="测试" sub="qa" chips="qa" persona="你是 QA，输出复现步骤。"/>';
  const { events } = parseAndExtract(input, 'sess-3', noopArtifact);
  const d = findEvent(events, 'node')?.data as Record<string, unknown> | undefined;
  check('persona attr captured', d?.persona === '你是 QA，输出复现步骤。');
})();

// ─── Test 4: paired <sf:agent-persona> block (multi-line) ───────────────────

(function testPairedPersona() {
  console.log('\n[4] <sf:agent-persona node_id="..."> block');
  const input = `
<sf:node id="dev" type="agent" title="开发" sub="full-stack" chips="ts"/>
<sf:agent-persona node_id="dev">
你是全栈工程师。
先看代码上下文再动手。
每个改动配最小测试。
</sf:agent-persona>
`;
  const { events, buffer } = parseAndExtract(input, 'sess-4', noopArtifact);
  check('buffer drained of persona block', !buffer.includes('agent-persona'));
  const p = findEvent(events, 'agent-persona');
  const pd = p?.data as Record<string, unknown> | undefined;
  check('agent-persona event emitted', !!p);
  check('persona.node_id=dev', pd?.node_id === 'dev');
  const personaText = pd?.persona as string;
  check('persona body trimmed', personaText.startsWith('你是全栈工程师'));
  check('persona preserves line breaks', personaText.includes('\n'));
  check('persona contains all 3 lines',
    personaText.includes('全栈') && personaText.includes('上下文') && personaText.includes('测试'));
})();

// ─── Test 5: persona block does NOT bleed into unknown-tag stripper ─────────

(function testPersonaNotUnknownTag() {
  console.log('\n[5] <sf:agent-persona> handled before unknown-tag stripper');
  const input =
    '<sf:agent-persona node_id="x">prompt text</sf:agent-persona>';
  const { events } = parseAndExtract(input, 'sess-5', noopArtifact);
  check('exactly 1 agent-persona event', findAllEvents(events, 'agent-persona').length === 1);
  check('no unknown-tag event for agent-persona', findAllEvents(events, 'unknown-tag').length === 0);
})();

// ─── Test 6: empty tools_picked → undefined (not [""]) ──────────────────────

(function testEmptyToolsList() {
  console.log('\n[6] tools_picked="" / absent both yield undefined');
  // absent
  const r1 = parseAndExtract(
    '<sf:node id="a" type="agent" title="t" sub="s" chips="c"/>',
    'sess-6a',
    noopArtifact,
  );
  const d1 = findEvent(r1.events, 'node')?.data as Record<string, unknown>;
  check('absent → undefined', d1.tools_picked === undefined);

  // explicit empty string
  const r2 = parseAndExtract(
    '<sf:node id="a" type="agent" title="t" sub="s" chips="c" tools_picked=""/>',
    'sess-6b',
    noopArtifact,
  );
  const d2 = findEvent(r2.events, 'node')?.data as Record<string, unknown>;
  check('empty "" → undefined (no [""] artifact)', d2.tools_picked === undefined);
})();

// ─── Test 7: full E2E — node + persona + edge interleaved ───────────────────

(function testE2E() {
  console.log('\n[7] full E2E — interleaved node/persona/edge');
  const input = `
<sf:node id="pm" type="coordinator" title="PM" sub="plan" chips="claude" model="claude-sonnet-4-6" memory="vector+scratch" tools_picked="web_search"/>
<sf:agent-persona node_id="pm">
你是产品经理。
</sf:agent-persona>
<sf:node id="dev" type="agent" title="DEV" sub="code" chips="haiku" model="claude-haiku-4" memory="short-term" tools_picked="code_interpreter,file_writer"/>
<sf:agent-persona node_id="dev">
你是开发。
</sf:agent-persona>
<sf:edge from="pm" to="dev"/>
`;
  const { events } = parseAndExtract(input, 'sess-7', noopArtifact);
  const nodes = findAllEvents(events, 'node');
  const personas = findAllEvents(events, 'agent-persona');
  const edges = findAllEvents(events, 'edge');
  check('2 node events', nodes.length === 2);
  check('2 agent-persona events', personas.length === 2);
  check('1 edge event', edges.length === 1);

  // Cross-reference each persona to its node by id
  const pmPersona = personas.find(e => (e.data as Record<string, unknown>).node_id === 'pm');
  const devPersona = personas.find(e => (e.data as Record<string, unknown>).node_id === 'dev');
  check('pm persona found', !!pmPersona);
  check('dev persona found', !!devPersona);

  const pmNode = nodes.find(e => (e.data as Record<string, unknown>).node_id === 'pm')?.data as Record<string, unknown>;
  check('pm node carries model', pmNode?.model === 'claude-sonnet-4-6');
  check('pm node carries tools_picked', Array.isArray(pmNode?.tools_picked));
})();

// ─── Summary ────────────────────────────────────────────────────────────────

console.log('\n────────────────────────────────────────');
console.log(`  ${passCount} passed,  ${failCount} failed`);
console.log('────────────────────────────────────────\n');

if (failCount > 0) process.exit(1);
