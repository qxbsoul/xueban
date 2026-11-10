/**
 * read-skill-roster.test.ts — formatTeamRoster 单测。
 *
 * Run:
 *   cd server
 *   npx tsx src/lib/tools/builtin/__tests__/read-skill-roster.test.ts
 *
 * formatTeamRoster 把编译缓存的 teamConfig 转成紧凑 roster 文本(几 KB),供
 * read_skill 在 skill agents 落在非常规路径(BMAD: agent_files=0)时仍返回真实
 * roster。验:含全部 members、各自 persona、DAG edges;无 edges 时不崩。
 */

import { formatTeamRoster } from '../read-skill';

let pass = 0;
let fail = 0;
function assert(label: string, cond: boolean): void {
  if (cond) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.error(`  FAIL  ${label}`); }
}

const tc = {
  name: 'BMAD 全流程团队',
  members_ids: ['analyst', 'pm', 'architect', 'dev'],
  members_personas: {
    analyst: 'You are Mary, the Business Analyst.',
    pm: 'You are John, the Product Manager.',
    architect: 'You are Winston, the Architect.',
    dev: 'You are Amelia, the Developer.',
  },
  edges_v1: [
    { from: 'analyst', to: 'pm', kind: 'sequential' },
    { from: 'pm', to: 'architect', kind: 'sequential' },
    { from: 'architect', to: 'dev', kind: 'sequential' },
  ],
};

const out = formatTeamRoster(tc);

assert('含团队名', out.includes('BMAD 全流程团队'));
assert('含成员数 4', out.includes('4 members'));
assert('每个 member 都在', tc.members_ids.every((id) => out.includes(`agent: ${id}`)));
assert('每个 persona 都在', Object.values(tc.members_personas).every((p) => out.includes(p)));
assert('含 DAG edges 段', out.includes('edges (DAG)'));
assert('含一条边 analyst -> pm', out.includes('analyst -> pm [sequential]'));

// 无 edges / 无 name 不崩(兜底鲁棒性)
const bare = formatTeamRoster({ members_ids: ['solo'], members_personas: { solo: 'X' }, edges_v1: [] });
assert('无 edges 不崩、含 member', bare.includes('agent: solo') && !bare.includes('edges (DAG)'));
assert('无 name → (unnamed)', bare.includes('(unnamed)'));

console.log(`\n  ${pass} passed, ${fail} failed`);
// Standalone script: keep the shell exit code. Under vitest a direct
// process.exit() is an "unexpected exit" that reds out the whole file, so only
// throw on failure there (the runner then reports the real failed checks).
if (process.env.VITEST) {
  if (fail > 0) throw new Error(`${fail} checks failed`);
} else {
  process.exit(fail === 0 ? 0 : 1);
}
