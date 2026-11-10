/**
 * assembly-routing.test.ts — shouldOrchestrate 路由判定单测。
 *
 * Run:
 *   cd server
 *   npx tsx src/lib/__tests__/assembly-routing.test.ts
 *
 * 含 RED 对照:内联旧逻辑(assembler.ts line 690-691 改前),证明关键 case
 * 「@skill + 编译缓存」旧逻辑返回 false(= @BMAD 被编译缓存短路、5 步 yaml 没执行的根因),
 * 新逻辑返回 true(走 skill 工作流)。
 */

import { shouldOrchestrate, type OrchestrateInputs } from '../assembly-routing';

// 改前旧逻辑(对照,证明 RED):
//   const orchestrate = llmAvailable && (explicit_skill || !matchedRecipe) && !hasCompiledTeam && !hasLegacyTeam;
function oldShouldOrchestrate(i: OrchestrateInputs): boolean {
  return (
    i.llmAvailable && (i.explicitSkill || !i.matchedRecipe) && !i.hasCompiledTeam && !i.hasLegacyTeam
  );
}

let pass = 0;
let fail = 0;
function assert(label: string, cond: boolean): void {
  if (cond) {
    pass++;
    console.log(`  PASS  ${label}`);
  } else {
    fail++;
    console.error(`  FAIL  ${label}`);
  }
}

const base: OrchestrateInputs = {
  llmAvailable: true,
  explicitSkill: false,
  matchedRecipe: false,
  hasCompiledTeam: false,
  hasLegacyTeam: false,
};
const I = (o: Partial<OrchestrateInputs>): OrchestrateInputs => ({ ...base, ...o });

// ── GREEN:新逻辑期望 ─────────────────────────────────────────────
// 1) 核心修复:@skill + 有编译缓存 → 走 skill 工作流(不被短路)。这是 BMAD 场景。
assert('@skill + llm + compiledTeam → orchestrate(走 skill 工作流)',
  shouldOrchestrate(I({ explicitSkill: true, hasCompiledTeam: true })) === true);
// 2) @skill + 无 LLM → 不 orchestrate(走确定性离线兜底 emitCompiledTeamBlueprint)。
assert('@skill + !llm → 离线兜底(不 orchestrate)',
  shouldOrchestrate(I({ explicitSkill: true, llmAvailable: false, hasCompiledTeam: true })) === false);
// 3) @skill + llm + 无现成 team → orchestrate(read_skill 复现)。
assert('@skill + llm + 无 team → orchestrate',
  shouldOrchestrate(I({ explicitSkill: true })) === true);
// 4) 无 @skill + 编译 team → 直接执行编译 team(不 orchestrate)。
assert('无@skill + compiledTeam → 直接执行(不 orchestrate)',
  shouldOrchestrate(I({ hasCompiledTeam: true })) === false);
// 5) 无 @skill + legacy team → 直接执行(不 orchestrate)。
assert('无@skill + legacyTeam → 直接执行(不 orchestrate)',
  shouldOrchestrate(I({ hasLegacyTeam: true })) === false);
// 6) 无 @skill + 命中 recipe → 照 recipe(不 orchestrate)。
assert('无@skill + recipe → 照 recipe(不 orchestrate)',
  shouldOrchestrate(I({ matchedRecipe: true })) === false);
// 7) 无 @skill + 无 team 无 recipe → orchestrate(LLM 设计)。
assert('无@skill + 空 → orchestrate(设计)',
  shouldOrchestrate(I({})) === true);
// 8) 无 LLM + 无 @skill → 不 orchestrate。
assert('无@skill + !llm → 不 orchestrate',
  shouldOrchestrate(I({ llmAvailable: false })) === false);

// ── RED 对照:旧逻辑在 BMAD 场景给出错误结果(false),证明这是要修的根因 ──
const bmadCase = I({ explicitSkill: true, hasCompiledTeam: true });
assert('[RED 对照] 旧逻辑在 @skill+compiledTeam 返回 false(= BMAD 短路根因)',
  oldShouldOrchestrate(bmadCase) === false);
assert('[GREEN] 新逻辑修正同一 case 为 true',
  shouldOrchestrate(bmadCase) === true && shouldOrchestrate(bmadCase) !== oldShouldOrchestrate(bmadCase));

console.log(`\n  ${pass} passed, ${fail} failed`);
// Standalone script: keep the shell exit code. Under vitest a direct
// process.exit() is an "unexpected exit" that reds out the whole file, so only
// throw on failure there (the runner then reports the real failed checks).
if (process.env.VITEST) {
  if (fail > 0) throw new Error(`${fail} checks failed`);
} else {
  process.exit(fail === 0 ? 0 : 1);
}
