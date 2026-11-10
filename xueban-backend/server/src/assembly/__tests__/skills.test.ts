import { ALL_RECIPES } from '../skills';
import { SINGLE_AGENT_RECIPE } from '../skills/single-agent';

let pass = 0, fail = 0;
function check(label: string, cond: boolean) {
  if (cond) { pass++; console.log(`  PASS  ${label}`); }
  else { fail++; console.log(`  FAIL  ${label}`); }
}

check('single-agent recipe 在注册表里', ALL_RECIPES.some(r => r.id === 'single-agent'));
check('single-agent 只有 1 个角色', SINGLE_AGENT_RECIPE.roles.length === 1);
check('single-agent 角色是 coordinator', SINGLE_AGENT_RECIPE.roles[0].type === 'coordinator');
check('single-agent 无边', SINGLE_AGENT_RECIPE.edges.length === 0);
check('single-agent match.singleAgent=true', SINGLE_AGENT_RECIPE.match.singleAgent === true);
check('所有 recipe id 唯一', new Set(ALL_RECIPES.map(r => r.id)).size === ALL_RECIPES.length);

console.log(`\n${pass} pass / ${fail} fail`);
if (fail > 0) process.exit(1);
