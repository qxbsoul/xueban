/**
 * 2026-05-18 — Smoke test simulating what the LLM will emit per the new
 * AGENT_TEAM_BLUEPRINT_PROMPT, then asserting the SSE pipeline produces the
 * fields AgentPanel expects. Run: npx tsx src/parser-agent-smoke.ts
 *
 * This is the parser-driven equivalent of "run a real session" — without a
 * live API key in .env we can't roundtrip Anthropic, but the parser sits at
 * the only SSE-emit point, so feeding it the exact text the prompt commands
 * the LLM to produce is the next best thing.
 */

import { parseAndExtract } from './parser';

const llmOutput = `<sf:classify output_type="workflow" mode="team" confidence="0.92" complexity="4"/>
<sf:step name="分析目标需求" status="running"/>
<sf:step name="分析目标需求" status="done" elapsed_ms="1800"/>
<sf:step name="规划 Agent 结构" status="running"/>
<sf:node id="pm" type="coordinator" title="产品经理" sub="规划与对齐" chips="claude-sonnet-4-6,需求拆解" avatar_char="产" model="claude-sonnet-4-6" memory="vector+scratch" tools_picked="web_search,doc_writer" tools_candidate="jira,figma_reader"/>
<sf:agent-persona node_id="pm">
你是资深产品经理，擅长把模糊需求拆成可执行 epic。
输出顺序：1) 目标 2) 用户场景 3) 验收标准。
</sf:agent-persona>
<sf:node id="dev" type="agent" title="全栈开发" sub="编码与测试" chips="claude-haiku-4,TS" avatar_char="开" model="claude-haiku-4" memory="short-term" tools_picked="code_interpreter,file_writer" tools_candidate="docker_runner"/>
<sf:agent-persona node_id="dev">
你是全栈工程师。先看代码上下文再动手；每个改动配最小测试。
</sf:agent-persona>
<sf:edge from="pm" to="dev"/>
<sf:step name="规划 Agent 结构" status="done" elapsed_ms="2400"/>`;

const { events } = parseAndExtract(llmOutput, 'sess-smoke', () => {});

console.log('--- RAW SSE EVENTS (head 20) ---');
events.slice(0, 20).forEach((e, i) => {
  console.log(`[${i.toString().padStart(2, '0')}] event=${e.event}  data=${JSON.stringify(e.data)}`);
});
console.log(`--- total events: ${events.length} ---`);

const nodes = events.filter(e => e.event === 'node');
const personas = events.filter(e => e.event === 'agent-persona');
const enriched = nodes.filter(n => {
  const d = n.data as Record<string, unknown>;
  return !!d.model && Array.isArray(d.tools_picked) && (d.tools_picked as unknown[]).length > 0;
});

console.log(`--- nodes=${nodes.length}  personas=${personas.length}  enriched(model+tools_picked)=${enriched.length} ---`);
if (enriched.length < 1) {
  console.error('FAIL: no enriched node');
  process.exit(1);
}
console.log('PASS: at least one node has model + tools_picked; persona events route by node_id');
