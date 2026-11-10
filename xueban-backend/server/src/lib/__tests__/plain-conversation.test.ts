/**
 * plain-conversation.test.ts — 普通对话直通路判定回归测试（investigate 根因修复，2026-06-23）。
 *
 * Run with:  npx tsx src/lib/__tests__/plain-conversation.test.ts   (from server/)
 *
 * 守住的 bug：个人态发 "hi" 仍被套「团队组装指挥官」人格。修复让 mode='single' 且无组装
 * 意图时走 shouldUsePlainConversation()===true → assembler 跳过组装、用普通对话 prompt。
 * 这里只测纯判定函数（LLM/transport 不可单测，靠 tsc + 浏览器亲验覆盖端到端）。
 */

import { shouldUsePlainConversation } from '../intent-router';

let pass = 0;
let fail = 0;
function check(label: string, cond: boolean, detail?: unknown) {
  if (cond) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}`); if (detail !== undefined) console.log('        detail:', detail); }
}

// ── 走普通对话（true）──────────────────────────────────────────────────────────
check('hi（寒暄）→ 普通对话', shouldUsePlainConversation({ mode: 'single', goal: 'hi' }) === true);
check('你好 → 普通对话', shouldUsePlainConversation({ mode: 'single', goal: '你好' }) === true);
check('单步问答 → 普通对话', shouldUsePlainConversation({ mode: 'single', goal: '什么是闭包？' }) === true);
check('写代码 → 普通对话', shouldUsePlainConversation({ mode: 'single', goal: '帮我写个贪吃蛇' }) === true);

// ── 不走普通对话（false）──────────────────────────────────────────────────────
check('mode=team → 否（团队态不受影响）', shouldUsePlainConversation({ mode: 'team', goal: 'hi' }) === false);
check('mode 缺省（团队态 Auto）→ 否', shouldUsePlainConversation({ mode: undefined, goal: 'hi' }) === false);
check('显式 @skill → 否（组装意图）', shouldUsePlainConversation({ mode: 'single', explicit_skill: true, goal: '复现这个' }) === false);
check('显式建 1 个 agent → 否（仍组装、锁 1 节点）', shouldUsePlainConversation({ mode: 'single', goal: '帮我创建一个开发工程师agent' }) === false);
check('含团队词「组个团队」→ 否', shouldUsePlainConversation({ mode: 'single', goal: '帮我组个全栈开发团队' }) === false);
check('含团队词「小队」→ 否', shouldUsePlainConversation({ mode: 'single', goal: '搭个调研小队' }) === false);
check('英文 a team of → 否', shouldUsePlainConversation({ mode: 'single', goal: 'build a team of researchers' }) === false);

console.log('\n────────────────────────────────────────');
console.log(`  ${pass} passed,  ${fail} failed`);
console.log('────────────────────────────────────────\n');
if (fail > 0) process.exit(1);
