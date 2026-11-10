import { ALL_RECIPES } from './skills';
import type { AssemblyRecipe } from './skills/types';
import { classifyIntent, detectExplicitSingleAgent } from '../lib/intent-router';

/**
 * 确定性选 recipe。优先级：singleAgent > intent > keyword。都不中返回 null（走 fallback）。
 */
export function selectRecipe(goal: string): AssemblyRecipe | null {
  const g = (goal ?? '').trim();
  if (!g) return null;

  const single = detectExplicitSingleAgent(g).single;
  if (single) {
    const r = ALL_RECIPES.find(r => r.match.singleAgent);
    if (r) return r;
  }

  const intent = classifyIntent(g).outputType;
  for (const r of ALL_RECIPES) {
    if (r.match.singleAgent && !single) continue;        // singleAgent recipe 只在 single 时命中
    if (r.match.intents?.includes(intent)) return r;
    if (r.match.keywords?.some(k => g.toLowerCase().includes(k.toLowerCase()))) return r;
  }
  return null;
}
