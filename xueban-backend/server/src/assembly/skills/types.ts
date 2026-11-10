import type { OutputType } from '../../lib/intent-router';

/** Skill recipe = 确定性装配剧本（文章：Skill = 不现场发挥按剧本来）。与 Rule 分目录。 */
export interface AssemblyRecipe {
  id: string;
  description: string;
  match: {
    singleAgent?: boolean;       // detectExplicitSingleAgent 命中
    intents?: OutputType[];      // intent-router classifyIntent 的 outputType
    keywords?: string[];         // 兜底关键词（goal 含任一即命中）
  };
  roles: RecipeRole[];
  edges: RecipeEdge[];
  /**
   * Post-assembly UX hint — NOT a structural field. When true, the assembler
   * emits a lightweight `assembly-meta` SSE frame ({ ask_workspace: true }) so
   * the front-end can offer the user a "move this team to a new workspace"
   * affordance after the team auto-saves into the current workspace. Only
   * meaningful for single-agent recipes (multi-agent recipes leave it unset →
   * no prompt). Does not affect node/role/edge structure in any way.
   */
  askWorkspaceOnCreate?: boolean;
}
export interface RecipeRole {
  role_id: string;
  type: 'coordinator' | 'agent';
  title: string;
  hint?: string;
}
export interface RecipeEdge { from: string; to: string }
