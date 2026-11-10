import { deriveRules, enforceRules } from '../rules/enforce';
import type { RosterNode } from '../rules/types';

let pass = 0, fail = 0;
function check(label: string, cond: boolean) {
  if (cond) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}`); }
}

const roster = (n: number): RosterNode[] =>
  Array.from({ length: n }, (_, i) => ({
    role_id: i === 0 ? 'coordinator' : `agent${i}`,
    type: i === 0 ? 'coordinator' : 'agent',
    title: i === 0 ? '协调' : `角色${i}`,
  }));

{
  const rules = deriveRules('帮我创建一个开发工程师agent', null);
  check('deriveRules 单 agent → roster_max=1',
    rules.some(r => r.kind === 'roster_max' && r.max === 1));
}
{
  const rules = deriveRules('帮我搭一个 review 团队，要 3 个 agent', null);
  check('deriveRules 团队意图 → 无 roster_max',
    !rules.some(r => r.kind === 'roster_max'));
}
{
  const { kept, dropped } = enforceRules(roster(4), [{ kind: 'roster_max', max: 1, reason: 'x' }]);
  check('roster_max=1 → 保留 1', kept.length === 1);
  check('roster_max=1 → 丢 3', dropped.length === 3);
}
{
  const { kept, violations } = enforceRules(roster(3), []);
  check('无规则 → 全保留', kept.length === 3);
  check('无规则 → 无 violation', violations.length === 0);
}
{
  const noCoord: RosterNode[] = [{ role_id: 'a', type: 'agent', title: 'A' }];
  const { violations } = enforceRules(noCoord, [{ kind: 'require_coordinator', reason: 'x' }]);
  check('require_coordinator 缺 → violation', violations.length === 1);
}
{
  const r: RosterNode[] = [
    { role_id: 'coordinator', type: 'coordinator', title: 'C' },
    { role_id: 'hacker', type: 'agent', title: 'H' },
  ];
  const { kept } = enforceRules(r, [{ kind: 'role_whitelist', allowed: ['coordinator'], reason: 'x' }]);
  check('role_whitelist → 剔除越界角色', kept.length === 1 && kept[0].role_id === 'coordinator');
}

console.log(`\n${pass} pass / ${fail} fail`);
if (fail > 0) process.exit(1);
