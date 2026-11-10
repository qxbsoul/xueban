/**
 * assembly-routing.ts — 组装路由判定(从 assembler.ts 抽出的纯函数,可单测)。
 *
 * `shouldOrchestrate` 决定一次组装请求走「LLM 驱动的 skill 工作流」(orchestrate=true,
 * read_skill 复现 / LLM 设计)还是「确定性直出」(orchestrate=false:编译 team 执行 /
 * recipe / 无 LLM 离线兜底 emitCompiledTeamBlueprint)。
 *
 * 设计依据:docs/design/assembly-skill-optimize-plan-2026-06-02.md §3.0
 *   @skill = 复现意图 → **优先走 skill 工作流**,不被编译缓存短路(旧逻辑的 bug:
 *   hasCompiledTeam 把 @BMAD 短路到 emitCompiledTeamBlueprint,5 步 yaml 一行没执行)。
 *   编译缓存降级为「无 LLM 离线兜底」与「read_skill 第 4 级回退源」。
 */

export interface OrchestrateInputs {
  /** 有可用 LLM 通路(有 key / executor 能调模型)。无 LLM → 必走确定性兜底。 */
  llmAvailable: boolean;
  /** 用户显式 @skill:<id>(复现意图)。 */
  explicitSkill: boolean;
  /** goal 命中确定性 recipe(仅设计模式有意义)。 */
  matchedRecipe: boolean;
  /** 该 skill 有编译缓存的 team(compiled.mode==='team')。 */
  hasCompiledTeam: boolean;
  /** 该 skill 自带 legacy 静态 TeamDef(skill.team)。 */
  hasLegacyTeam: boolean;
}

/**
 * 是否走 LLM 驱动的统一组装 skill 工作流。
 *
 * 规则:
 *  1. 无 LLM → false(走确定性离线兜底,@skill 用 emitCompiledTeamBlueprint)。
 *  2. @skill(复现)→ **true**,优先走 skill 工作流(read_skill),不被编译缓存短路。
 *  3. 无 @skill(设计)→ 有现成 team(编译/legacy)或命中 recipe 就 false(直接执行/照 recipe);
 *     否则 true(LLM 设计团队)。
 */
export function shouldOrchestrate(i: OrchestrateInputs): boolean {
  if (!i.llmAvailable) return false;
  if (i.explicitSkill) return true;
  return !i.matchedRecipe && !i.hasCompiledTeam && !i.hasLegacyTeam;
}
