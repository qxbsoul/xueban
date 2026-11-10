// Repro the 2026-05-18 bug: LLM stream chunks split `<sf:…` across tokens,
// the old findPartialTagStart only matched complete `<sf:` prefixes, so
// `<sf` alone was emitted as raw text.

import { parseAndExtract } from './parser';

const fullTag = `<sf:step name="分析目标需求" status="running"/><sf:step name="分析目标需求" status="done" elapsed_ms="320"/><sf:node id="pm" type="coordinator" title="产品经理" sub="x" chips="claude-sonnet-4-6"/>`;

// Simulate per-token chunks like the real LLM streaming we saw in backend log.
const chunks: string[] = [];
for (const ch of fullTag) chunks.push(ch);  // worst case: 1 char per chunk

let buffer = '';
const allEvents: any[] = [];
const textEvents: any[] = [];
for (const chunk of chunks) {
  buffer += chunk;
  const r = parseAndExtract(buffer, 'sid', () => {});
  buffer = r.buffer;
  for (const e of r.events) {
    allEvents.push(e);
    if (e.event === 'text') textEvents.push(e);
  }
}
// flush
if (buffer.trim()) {
  const r = parseAndExtract(buffer, 'sid', () => {});
  for (const e of r.events) {
    allEvents.push(e);
    if (e.event === 'text') textEvents.push(e);
  }
}

console.log('total events:', allEvents.length);
const byType = allEvents.reduce<Record<string, number>>((acc, e) => {
  acc[e.event] = (acc[e.event] ?? 0) + 1;
  return acc;
}, {});
console.log('by type:', byType);
console.log('text events:', textEvents.length);
if (textEvents.length > 0) {
  console.log('FAIL: leaked text:');
  for (const e of textEvents) console.log('  >', JSON.stringify(e.data));
} else {
  console.log('PASS: no leaked text');
}
console.log('residual buffer:', JSON.stringify(buffer));
