// Verify <sf:thinking> survives per-character chunked streaming.
// Same harness as parse-streaming-bug.ts but with thinking blocks mixed in.

import { parseAndExtract } from './parser';

const fullStream = `<sf:step name="分析目标需求" status="running"/>
<sf:thinking step="分析目标需求">
用户要求 BMAD 4 角，所以应该是 team 模式串行管道。
PM 当 coordinator 是 BMAD 范式。
</sf:thinking>
<sf:step name="分析目标需求" status="done" elapsed_ms="320"/>
<sf:step name="规划 Agent 结构" status="running"/>
<sf:thinking>
4 个角色按 BMAD 阶段分别配 sonnet/sonnet/sonnet/sonnet。
工具集差异化：PM 需 doc_writer，dev 需 code_interpreter。
</sf:thinking>
<sf:node id="pm" type="coordinator" title="产品经理" sub="规划" chips="claude-sonnet-4-6"/>`;

const chunks: string[] = [];
for (const ch of fullStream) chunks.push(ch);

let buffer = '';
const events: any[] = [];
for (const chunk of chunks) {
  buffer += chunk;
  const r = parseAndExtract(buffer, 'sid', () => {});
  buffer = r.buffer;
  for (const e of r.events) events.push(e);
}
if (buffer.trim()) {
  const r = parseAndExtract(buffer, 'sid', () => {});
  for (const e of r.events) events.push(e);
}

const byType: Record<string, number> = {};
for (const e of events) byType[e.event] = (byType[e.event] ?? 0) + 1;
console.log('events by type:', byType);
console.log('thinking-chunk events:');
for (const e of events.filter(e => e.event === 'thinking-chunk')) {
  console.log('  step=', JSON.stringify(e.data.step), 'text len=', e.data.text.length, 'preview=', JSON.stringify(e.data.text.slice(0, 30)));
}
const textLeaked = events.filter(e => e.event === 'text' && e.data.text.trim().length > 0);
console.log('non-whitespace text events:', textLeaked.length);
if (textLeaked.length > 0) {
  console.log('LEAKS:');
  for (const e of textLeaked) console.log('  ', JSON.stringify(e.data));
}
console.log('residual buffer:', JSON.stringify(buffer));
