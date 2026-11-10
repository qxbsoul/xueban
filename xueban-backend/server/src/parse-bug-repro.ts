import { parseAndExtract } from './parser';

const input = `<sf:step name="分析目标需求" status="running"/>
<sf:step name="分析目标需求" status="done" elapsed_ms="320"/>

<sf:step name="规划 Agent 结构" status="running"/>

<sf:node id="pm" type="coordinator" title="产品经理"
sub="需求规划与对齐"
       chips="claude-sonnet-4-6,需求拆解,优先级,验收标准"
       avatar_char="产" model="claude-sonnet-4-6"
memory="vector+scratch"
       tools_picked="web_search,doc_writer"
       tools_candidate="jira,figma_reader"/>
<sf:agent-persona node_id="pm">
你是资深产品经理（BMAD-PM），负责把模糊需求拆解为
可执行的 epic 与 user story。
</sf:agent-persona>

<sf:edge from="pm" to="arch"/>`;

const result = parseAndExtract(input, 'sid-test', () => {});
console.log('=== events emitted ===');
for (const e of result.events) {
  console.log(` ${e.event}: ${JSON.stringify(e.data).slice(0, 90)}`);
}
console.log('=== residual buffer (should be empty or whitespace only) ===');
console.log(JSON.stringify(result.buffer));
console.log('=== text events (these go to chatReply) ===');
for (const e of result.events.filter(e => e.event === 'text')) {
  console.log('TEXT:', JSON.stringify(e.data));
}
