import { selectRecipe } from '../select';

let pass = 0, fail = 0;
function check(label: string, cond: boolean) {
  if (cond) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}`); }
}

check('bug 原句 → single-agent', selectRecipe('帮我创建一个开发工程师agent')?.id === 'single-agent');
check('"单个 agent" → single-agent', selectRecipe('给我一个单个 agent')?.id === 'single-agent');
check('团队意图 → 非 single-agent（Phase A 无匹配返回 null）', selectRecipe('搭一个 3 人 review 团队') === null);
check('空 goal → null', selectRecipe('') === null);

console.log(`\n${pass} pass / ${fail} fail`);
if (fail > 0) process.exit(1);
