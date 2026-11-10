import type { AssemblyRecipe } from './types';
import { SINGLE_AGENT_RECIPE } from './single-agent';

/** Phase A 只有 single-agent；Phase B 追加 software-team / research-report。 */
export const ALL_RECIPES: AssemblyRecipe[] = [SINGLE_AGENT_RECIPE];
export type { AssemblyRecipe, RecipeRole, RecipeEdge } from './types';
