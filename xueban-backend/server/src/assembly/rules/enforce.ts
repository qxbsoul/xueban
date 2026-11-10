import type { AssemblyRule, RosterNode } from './types';
import { detectExplicitSingleAgent } from '../../lib/intent-router';
import type { AssemblyRecipe } from '../skills/types';

/**
 * 从 goal + recipe 合成本次装配的 Rule 列表。
 * - goal 明确"一个 agent" → roster_max:1（复用 server intent-router 的确定性检测）
 * - recipe 含 coordinator 角色 → require_coordinator
 */
export function deriveRules(goal: string, recipe: AssemblyRecipe | null): AssemblyRule[] {
  const rules: AssemblyRule[] = [];
  const single = detectExplicitSingleAgent(goal);
  if (single.single) {
    rules.push({ kind: 'roster_max', max: 1, reason: `用户明确要单个 agent（"${single.matched}"）` });
  }
  if (recipe?.roles.some(r => r.type === 'coordinator')) {
    rules.push({ kind: 'require_coordinator', reason: `recipe ${recipe.id} 要求 coordinator` });
  }
  return rules;
}

/**
 * 确定性应用规则。roster_max 截断（保留前 max）；require_coordinator 缺则记 violation；
 * role_whitelist 剔除越界角色。返回保留 / 丢弃 / 违规说明。
 */
export function enforceRules(
  roster: RosterNode[],
  rules: AssemblyRule[],
): { kept: RosterNode[]; dropped: RosterNode[]; violations: string[] } {
  let kept = [...roster];
  let dropped: RosterNode[] = [];
  const violations: string[] = [];

  for (const rule of rules) {
    if (rule.kind === 'roster_max') {
      if (kept.length > rule.max) {
        dropped = dropped.concat(kept.slice(rule.max));
        kept = kept.slice(0, rule.max);
      }
    } else if (rule.kind === 'role_whitelist') {
      const allowed = new Set(rule.allowed);
      const removed = kept.filter(n => !allowed.has(n.role_id));
      if (removed.length > 0) {
        dropped = dropped.concat(removed);
        kept = kept.filter(n => allowed.has(n.role_id));
      }
    } else if (rule.kind === 'require_coordinator') {
      if (!kept.some(n => n.type === 'coordinator')) {
        violations.push(rule.reason);
      }
    }
  }
  return { kept, dropped, violations };
}
